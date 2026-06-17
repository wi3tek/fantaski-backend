import os
import glob
import json
from collections import defaultdict


def generate_standings_for_file(json_file):
    """
    Przetwarza pojedynczy plik z wynikami sezonu i zwraca strukturę
    klasyfikacji generalnej gotową do zapisu jako JSON.
    """
    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError:
        print(f"[BŁĄD] Nie można odczytać pliku: {json_file}")
        return None

    # Struktura dla klasyfikacji generalnej
    # Kluczem jest fis_code zawodnika
    standings = defaultdict(lambda: {
        "name": "",
        "country": "",
        "wc_starts": 0,
        "total_wc_points": 0
    })

    total_wc_events = 0

    # Przechodzimy przez wszystkie eventy
    for event in data:
        for comp in event.get("competitions", []):
            # Liczymy starty tylko dla kategorii WC (Puchar Świata)
            if comp.get("category") == "WC":
                total_wc_events += 1

                # Pobieramy wyniki z konkursu
                results_data = comp.get("results_data", {})
                results = results_data.get("results", [])

                for athlete in results:
                    fis_code = athlete.get("fis_code")
                    if not fis_code:
                        continue

                    # Aktualizacja danych zawodnika
                    standings[fis_code]["name"] = athlete.get("name")
                    standings[fis_code]["country"] = athlete.get("country")
                    standings[fis_code]["wc_starts"] += 1
                    standings[fis_code]["total_wc_points"] += athlete.get("world_cup_points", 0)

    # Sortowanie klasyfikacji według punktów (malejąco)
    sorted_standings = sorted(
        standings.values(),
        key=lambda x: x["total_wc_points"],
        reverse=True
    )

    # Budujemy słownik wynikowy (JSON)
    file_basename = os.path.basename(json_file)
    result_json = {
        "source_file": file_basename,
        "total_wc_events": total_wc_events,
        "standings": sorted_standings
    }

    return result_json


def process_all_results(input_dir="results", output_dir="standings"):
    """
    Analizuje wszystkie pliki JSON w podanym katalogu wejściowym,
    generuje statystyki i zapisuje je do katalogu wyjściowego.
    """
    # 1. Upewniamy się, że folder wyjściowy istnieje
    os.makedirs(output_dir, exist_ok=True)

    # 2. Szukamy wszystkich plików w katalogu wejściowym
    search_pattern = os.path.join(input_dir, "*.json")
    json_files = glob.glob(search_pattern)

    if not json_files:
        print(f"Brak plików w katalogu '{input_dir}'. Upewnij się, że ścieżka jest poprawna.")
        return

    print(f"Znaleziono {len(json_files)} plików w katalogu '{input_dir}'. Rozpoczynam przetwarzanie...\n")

    for json_file in json_files:
        print(f"--- Analiza pliku: {os.path.basename(json_file)} ---")

        # Generowanie klasyfikacji
        file_stats = generate_standings_for_file(json_file)

        if not file_stats:
            continue

        # Zapis do pliku JSON
        out_filename = f"standings_{file_stats['source_file']}"
        out_path = os.path.join(output_dir, out_filename)

        with open(out_path, 'w', encoding='utf-8') as out_f:
            json.dump(file_stats, out_f, indent=4, ensure_ascii=False)

        # Wyświetlanie sformatowanego JSON-a w konsoli
        print(json.dumps(file_stats, indent=4, ensure_ascii=False))
        print(f"[SUKCES] Zapisano klasyfikację do: {out_path}\n")


if __name__ == "__main__":
    # Uruchomienie głównej funkcji skanującej
    process_all_results(input_dir="../../scripts/results", output_dir="../../scripts/standings")