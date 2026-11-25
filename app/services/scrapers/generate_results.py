import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Callable, Dict, Any

import os

# Importujemy nasze moduły z poprzednich kroków
from app.services.scrapers.scraper import fetch_fis_calendar
from app.services.scrapers.event_scraper import fetch_event_details
from app.services.scrapers.results_scraper_ind import fetch_race_results as fetch_ind_results
from app.services.scrapers.results_scraper_team import fetch_race_results as fetch_team_results
from app.services.scrapers.http_client import clear_last_http_error, get_last_http_error

# Zabezpieczenie przed limitami Nginx na serwerach FIS (1-2 zapytania/sekundę)
# Zmniejszenie puli wątków do 2 zapobiega burstom, a globalny rate limiter synchronizuje zapytania
MAX_EVENT_WORKERS = 2
MAX_RACE_WORKERS = 2


def process_single_race(race, stop_check: Optional[Callable[[], bool]] = None):
    """Funkcja pobierająca wyniki dla pojedynczego wyścigu (uruchamiana w osobnym wątku)."""
    if stop_check and stop_check():
        race["results_data"] = None
        race["is_cancelled"] = False
        race["_scrape_error"] = "STOPPED"
        return race

    race_url = race.get("raceUrl")
    race_title = race.get("title", "Nieznana sesja")

    print(f"    [Wątek Wyników] -> Rozpoczęto pobieranie: {race_title}")
    clear_last_http_error()

    if race_url:
        if stop_check and stop_check():
            race["results_data"] = None
            race["is_cancelled"] = False
            race["_scrape_error"] = "STOPPED"
            return race

        if "Qualification" in race_title or "Training" in race_title or "Trial" in race_title:
            session_type = "Qualification"
        else:
            session_type = "Competition"

        # Dynamiczny dobór scrapera i naliczania punktów na bazie tytułu serii
        if "Super Team" in race_title:
            race_results = fetch_team_results(race_url, session_type=session_type, team_type="SUPER_TEAM")
        elif "Team" in race_title:
            race_results = fetch_team_results(race_url, session_type=session_type, team_type="TEAM")
        else:
            race_results = fetch_ind_results(race_url, session_type=session_type)

        if race_results:
            # PRZEPISANIE METADANYCH SKOCZNI DO GŁÓWNEGO DOKUMENTU SESJI
            race["hillSize"] = race_results.pop("hill_size", None)
            race["hill_size_unit"] = race_results.pop("hill_size_unit", None)
            race["hillType"] = race_results.pop("hill_type", None)
            has_athletes = bool(race_results.get("results") and len(race_results.get("results")) > 0)
            race["is_cancelled"] = bool(race_results.get("is_cancelled", False) and not has_athletes)
            race["_scrape_error"] = None
        else:
            race["is_cancelled"] = False
            race["_scrape_error"] = get_last_http_error() or "FETCH_FAILED"

        race["results_data"] = race_results
    else:
        race["results_data"] = None
        race["is_cancelled"] = False
        race["_scrape_error"] = "NO_URL"

    print(f"    [Wątek Wyników] <- Zakończono: {race_title}")
    return race


# Procesujemy pojedynczy event z kalendarza.
def process_single_event(
    event,
    seasoncode,
    gendercode,
    mode,
    categorycode,
    existing_complete_ids=None,
    existing_complete_event_ids=None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None
):
    event_id = str(event.get("id") or "")
    place = event.get("place", "Nieznane")

    if stop_check and stop_check():
        event["sessions"] = []
        event["_skipped_stopped"] = True
        return event

    # Szybkie pominięcie całego eventu bez zapytania HTTP o event-details, jeśli wszystkie jego sesje są już kompletne w bazie
    if existing_complete_event_ids and event_id in existing_complete_event_ids:
        print(f"[Scraper] -> Pomijam cały event (kompletny w bazie, 0 zapytań HTTP): {place} (ID: {event_id})")
        event["sessions"] = []
        event["_skipped_event_complete"] = True
        if progress_callback:
            progress_callback({"type": "event_skipped", "place": place, "event_id": event_id})
        return event

    print(f"\n[Wątek Eventu] Rozpoczynam event: {place} - ID: {event_id}")
    if progress_callback:
        progress_callback({"type": "event_started", "place": place, "event_id": event_id})

    clear_last_http_error()
    # 1. Pobieramy listę wyścigów dla tego eventu z uwzględnieniem trybu (IND/TEAM/ALL)
    event_details = fetch_event_details(
        sectorcode="JP",
        eventid=event_id,
        seasoncode=seasoncode,
        filter_gender=gendercode,
        categorycode=categorycode,
        mode=mode
    )
    if not event_details:
        err = get_last_http_error()
        if err:
            event["_event_fetch_error"] = err

    event["sessions"] = []

    # 2. Uruchamiamy drugą pulę wątków dla konkretnych wyścigów w ramach tego eventu
    with ThreadPoolExecutor(max_workers=MAX_RACE_WORKERS) as executor:
        future_to_race = {}
        for race in event_details:
            if stop_check and stop_check():
                break

            race["event_id"] = event_id
            r_id = race.get("raceId")
            codex = race.get("codex")
            race_composite_id = f"{r_id}_{codex}"

            if existing_complete_ids and r_id and codex and race_composite_id in existing_complete_ids:
                print(f"    [Scraper] -> Pomijam już kompletną sesję w bazie: {race.get('title')} ({race_composite_id})")
                race["_skipped_already_complete"] = True
                event["sessions"].append(race)
                if progress_callback:
                    progress_callback({"type": "race_skipped", "place": place, "title": race.get("title")})
            else:
                future_to_race[executor.submit(process_single_race, race, stop_check)] = race

        # as_completed zwraca wyniki w takiej kolejności, w jakiej wątki kończą pracę
        for future in as_completed(future_to_race):
            if stop_check and stop_check():
                for f in future_to_race:
                    f.cancel()
                break
            try:
                completed_race = future.result()
                event["sessions"].append(completed_race)
                if progress_callback:
                    if completed_race.get("_scrape_error") == "HTTP_429":
                        progress_callback({"type": "race_429", "place": place, "title": completed_race.get("title")})
                    elif completed_race.get("_scrape_error"):
                        progress_callback({"type": "race_failed", "place": place, "title": completed_race.get("title")})
                    else:
                        progress_callback({"type": "race_fetched", "place": place, "title": completed_race.get("title")})
            except Exception as exc:
                print(f"Błąd podczas przetwarzania wyścigu: {exc}")

    print(f"[Wątek Eventu] Zakończono event: {place}")
    if progress_callback:
        progress_callback({"type": "event_done", "place": place, "event_id": event_id})
    return event


def run_scraper_pipeline(
    seasoncode="2026",
    categorycode="WC",
    gendercode="M",
    limit_events=None,
    mode="IND",
    existing_complete_ids=None,
    existing_complete_event_ids=None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None
):
    print(f"\n[KROK 1] Pobieranie kalendarza (Sezon: {seasoncode}, Płeć: {gendercode})...")
    calendar = fetch_fis_calendar(seasoncode=seasoncode, categorycode=categorycode, gendercode=gendercode)

    if not calendar:
        print("Nie znaleziono zawodów w kalendarzu. Przerywam.")
        return []

    events_to_process = calendar[:limit_events]  # jeśli limit_events = None to analizujemy wszystkie eventy z kalendarza
    all_season_data = []

    if progress_callback:
        progress_callback({
            "type": "calendar_loaded",
            "season": seasoncode,
            "category": categorycode,
            "total_events": len(events_to_process)
        })

    # KROK 2 & 3: Zrównoleglenie pobierania eventów i wyścigów
    print(f"\n[KROK 2] Uruchamianie puli wątków dla {len(events_to_process)} eventów...")

    with ThreadPoolExecutor(max_workers=MAX_EVENT_WORKERS) as executor:
        futures = [
            executor.submit(
                process_single_event,
                event,
                seasoncode,
                gendercode,
                mode,
                categorycode,
                existing_complete_ids,
                existing_complete_event_ids,
                progress_callback,
                stop_check
            )
            for event in events_to_process
        ]

        # Zbieramy wyniki w miarę ich kończenia
        for future in as_completed(futures):
            if stop_check and stop_check():
                print("[Scraper] Zażądano zatrzymania — anuluję oczekujące zadania w puli...")
                for f in futures:
                    f.cancel()
                break
            try:
                processed_event = future.result()
                all_season_data.append(processed_event)
            except Exception as exc:
                print(f"Błąd podczas przetwarzania eventu: {exc}")

    return all_season_data

    #
    # PONIŻEJ Usunięcie funkcjonalności zapisu danych do pliku/
    #

    # # KROK 4: Zapis do pliku
    # print("\n=== ZAKOŃCZONO SCRAPOWANIE ===")
    #
    # # 1. Upewniamy się, że folder "results" istnieje
    # os.makedirs("./../results", exist_ok=True)
    #
    # # 2. Generujemy nazwę pliku i tworzymy pełną ścieżkę
    # filename = f"wyniki_sezonu_{seasoncode}_{mode}_{gendercode}_{categorycode}.json"
    # output_filepath = os.path.join("./../results", filename)
    #
    # # 3. Zapisujemy plik we wskazanym miejscu
    # with open(output_filepath, "w", encoding="utf-8") as file:
    #     json.dump(all_season_data, file, indent=4, ensure_ascii=False)
    #
    # print(f"Dane zostały prawidłowo połączone i zapisane w pliku: {output_filepath}")

#
# if __name__ == "__main__":
#     # 1. Definiujemy listy parametrów
#     # Używamy list comprehension, aby szybko wygenerować lata jako stringi (od 2015 do 2026)
#     seasons = [str(year) for year in range(2015, 2027)]
#     categories = ["WC", "OWG", "SFWC", "WJC" ,"GP" , "FESA", "FC" ,"COC", "WSC"]
#
#     total_combinations = len(seasons) * len(categories)
#     print(f"Rozpoczynam masowe scrapowanie. Do przetworzenia: {total_combinations} kombinacji.\n")
#
#     # 2. Mieszamy je za pomocą zagnieżdżonych pętli
#     for season in seasons:
#         for category in categories:
#             print(f"=====================================================")
#             print(f" SCRAPOWANIE: Sezon {season} | Kategoria {category} ")
#             print(f"=====================================================")
#
#             try:
#                 run_scraper_pipeline(
#                     seasoncode=season,
#                     categorycode=category,
#                     gendercode="M",
#                     limit_events=None,  # <--- UWAGA: Usuń to lub zmień na None, gdy chcesz pobrać pełny sezon!
#                     mode="IND"
#                 )
#             except Exception as e:
#                 # Zabezpieczenie: Jeśli scraper z jakiegoś powodu się wywali,
#                 # pętla przejdzie do kolejnej kombinacji zamiast zatrzymać cały skrypt.
#                 print(f"[BŁĄD KRYTYCZNY] Pominęto kombinację {season} {category} z powodu błędu: {e}")
#
#     print("\n--- ZAKOŃCZONO CAŁY PROCES SCRAPOWANIA ---")