import os
import glob
import json
from datetime import datetime
from collections import defaultdict


def search_athlete_results(names=None, place=None, hill_type=None, date_from=None, date_to=None, category=None,
                           rank=None, limit=None, session_type=None):
    """
    Przeszukuje wszystkie pliki JSON w katalogu 'results/' według podanych kryteriów
    i zwraca wyniki jako listę obiektów gotową dla nowej wersji frontendu.
    """
    directory_path = "app/scripts/results"

    search_pattern = os.path.join(directory_path, "*.json")
    json_files = glob.glob(search_pattern)

    if not json_files:
        # Zwracamy pustą listę, jeśli nie ma plików
        return []

    dt_from = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else None
    dt_to = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else None

    grouped_results = defaultdict(list)

    for json_file in json_files:
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError:
            continue

        for event in data:
            event_place = event.get("place", "")
            if place and place.lower() not in event_place.lower():
                continue

            if category and event.get("categorycode") != category:
                continue

            for session in event.get("sessions", []):
                if hill_type and session.get("hillType") != hill_type:
                    continue

                if session_type:
                    if session_type.upper() not in session.get("session_type", "").upper():
                        continue

                comp_date_str = session.get("date")
                if comp_date_str:
                    try:
                        comp_date = datetime.strptime(comp_date_str, "%Y-%m-%d").date()
                        if dt_from and comp_date < dt_from:
                            continue
                        if dt_to and comp_date > dt_to:
                            continue
                    except ValueError:
                        pass
                elif dt_from or dt_to:
                    continue

                results_data = session.get("results_data") or {}
                athletes = results_data.get("results", [])

                for athlete in athletes:
                    ath_name = athlete.get("name", "")

                    # --- ZMIANA: Obsługa listy imion zamiast pojedynczego stringa ---
                    if names:
                        # Sprawdzamy czy nazwisko z pliku pasuje do któregokolwiek z wyszukiwanych
                        match = any(req_name.lower() in ath_name.lower() for req_name in names)
                        if not match:
                            continue

                    if rank and athlete.get("rank") != rank:
                        continue

                    file_basename = os.path.basename(json_file)
                    jumps = athlete.get("jumps", [])

                    # --- ZBUDOWANIE REKORDU JSON ---
                    for jump in jumps:
                        record = {
                            "codex": session.get("codex"),
                            "date": comp_date_str or "9999-99-99",
                            "time": session.get("time") or "99:99",
                            "place": event_place,
                            "hill_size": session.get("hillSize"),
                            "hill_type": session.get("hillType"),
                            "category": event.get("categorycode"),
                            "session_type": session.get("session_type"),
                            "rank": athlete.get("rank"),
                            "round": jump.get("round"),
                            "distance": jump.get("distance"),
                            "points": jump.get("points"),
                            "jump_rank": jump.get("rank"),
                            "competition_points": athlete.get("competition_points", 0),
                        }
                        grouped_results[ath_name].append(record)

    # Sortowanie chronologiczne wyników dla każdego zawodnika
    for ath_name in grouped_results:
        grouped_results[ath_name].sort(
            key=lambda x: (x["date"], x["time"]),
            reverse=True
        )

        # zastosowanie limitu
        if limit is not None:
            grouped_results[ath_name] = grouped_results[ath_name][:limit]

    # --- ZMIANA: Konwersja słownika na docelową listę obiektów ---
    final_results = []
    for ath_name, jumps in grouped_results.items():
        final_results.append({
            "name": ath_name,
            "jumps": jumps
        })

    return final_results


# --- PRZYKŁADOWE UŻYCIE (Dostosowane do nowej logiki) ---
if __name__ == "__main__":

    # Wykonujemy zapytanie do naszej lokalnej bazy plików (zauważ użycie listy w 'names')
    wyniki = search_athlete_results(
        names=["KOUDELKA Roman", "STOCH Kamil"],
        place="Lillehammer",
        hill_type=None,
        date_from="2023-01-01",
        date_to=None,
        category=None,
        rank=None,
        limit=None
    )

    if not wyniki:
        final_json = json.dumps({"message": "Brak wynikow", "data": []}, indent=4)
        print(final_json)
    else:
        print(json.dumps(wyniki, indent=4, ensure_ascii=False))

        nazwa_pliku_wyjsciowego = "znalezione_wyniki.json"
        with open(nazwa_pliku_wyjsciowego, "w", encoding="utf-8") as outfile:
            json.dump(wyniki, outfile, indent=4, ensure_ascii=False)

        print(f"\n[INFO] Wyniki zostały pomyślnie wygenerowane i zapisane do pliku: {nazwa_pliku_wyjsciowego}")