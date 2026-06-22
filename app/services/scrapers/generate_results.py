import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import os

# Importujemy nasze moduły z poprzednich kroków
from services.scrapers.scraper import fetch_fis_calendar
from services.scrapers.event_scraper import fetch_event_details
from services.scrapers.results_scraper_ind import fetch_race_results as fetch_ind_results
from services.scrapers.results_scraper_team import fetch_race_results as fetch_team_results

# UWAGA: Ustawienie zbyt dużej liczby wątków (np. 20) szybko skończy się banem IP od FIS.
# 3-5 workerów to bezpieczny kompromis między szybkością a udawaniem "człowieka".
MAX_EVENT_WORKERS = 3
MAX_RACE_WORKERS = 3


def process_single_race(race):
    """Funkcja pobierająca wyniki dla pojedynczego wyścigu (uruchamiana w osobnym wątku)"""
    race_url = race.get("raceUrl")
    race_title = race.get("title", "Nieznana sesja")

    print(f"    [Wątek Wyników] -> Rozpoczęto pobieranie: {race_title}")

    if race_url:
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

        race["results_data"] = race_results
    else:
        race["results_data"] = None

    print(f"    [Wątek Wyników] <- Zakończono: {race_title}")
    return race

# Procesujemy pojedynczy event z kalendarza.
def process_single_event(event, seasoncode, gendercode, mode, categorycode):
    event_id = event.get("id")
    place = event.get("place", "Nieznane")

    print(f"\n[Wątek Eventu] Rozpoczynam event: {place} - ID: {event_id}")

    # 1. Pobieramy listę wyścigów dla tego eventu z uwzględnieniem trybu (IND/TEAM/ALL)
    event_details = fetch_event_details(
        sectorcode="JP",
        eventid=event_id,
        seasoncode=seasoncode,
        filter_gender=gendercode,
        categorycode=categorycode,
        mode=mode
    )

    event["sessions"] = []

    # 2. Uruchamiamy drugą pulę wątków dla konkretnych wyścigów w ramach tego eventu
    with ThreadPoolExecutor(max_workers=MAX_RACE_WORKERS) as executor:
        # Zlecamy zadania do puli
        future_to_race = {executor.submit(process_single_race, race): race for race in event_details}

        # as_completed zwraca wyniki w takiej kolejności, w jakiej wątki kończą pracę
        for future in as_completed(future_to_race):
            try:
                completed_race = future.result()
                event["sessions"].append(completed_race)
            except Exception as exc:
                print(f"Błąd podczas przetwarzania wyścigu: {exc}")

    print(f"[Wątek Eventu] Zakończono event: {place}")
    return event


def run_scraper_pipeline(seasoncode="2026", categorycode="WC", gendercode="M", limit_events=None, mode="IND"):
    print(f"\n[KROK 1] Pobieranie kalendarza (Sezon: {seasoncode}, Płeć: {gendercode})...")
    # Pobieramy kalendarz FIS dla danego sezonu, kategorii zawodów orac płci.
    # Jest to lista wydarzeń, do których skrypt się pokolei dostaje analizując ich htmle.
    calendar = fetch_fis_calendar(seasoncode=seasoncode, categorycode=categorycode, gendercode=gendercode)

    if not calendar:
        print("Nie znaleziono zawodów w kalendarzu. Przerywam.")
        return

    events_to_process = calendar[:limit_events] # jeśli limit_events = None to będziemy analizować wszystkie eventy z kalendarza
    all_season_data = []

    # KROK 2 & 3: Zrównoleglenie pobierania eventów i wyścigów
    print(f"\n[KROK 2] Uruchamianie puli wątków dla {len(events_to_process)} eventów...")

    with ThreadPoolExecutor(max_workers=MAX_EVENT_WORKERS) as executor:
        # Zlecamy wszystkie eventy do przetworzenia przekazując wybrany tryb (mode)
        futures = [executor.submit(process_single_event, event, seasoncode, gendercode, mode, categorycode) for event in events_to_process]

        # Zbieramy wyniki w miarę ich kończenia
        for future in as_completed(futures):
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