import os
import glob
import json
from collections import defaultdict


def find_pathologies():
    """
    Skanuje pliki JSON w katalogu 'results/' w poszukiwaniu patologii:
    1. Zawodnik sklasyfikowany ma ujemne punkty.
    2. Zawodnik sklasyfikowany nie ma zapisanych żadnych skoków (pusta lista jumps).
    """
    directory_path = "../results"
    search_pattern = os.path.join(directory_path, "*.json")
    json_files = glob.glob(search_pattern)

    # Używamy słownika do grupowania problemów po URL-u zawodów
    pathologies = defaultdict(list)

    # Statusy, dla których brak skoków lub dziwne punkty są naturalne (odrzucamy je)
    excluded_statuses = ["DSQ", "DNS", "NPS"]

    for json_file in json_files:
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError:
            continue

        for event in data:
            for comp in event.get("sessions", []):
                race_url = comp.get("raceUrl")
                if not race_url:
                    continue

                results_data = comp.get("results_data") or {}
                athletes = results_data.get("results", [])

                for athlete in athletes:
                    status = athlete.get("status", "")

                    # Ignorujemy zawodników, którzy nie wystartowali lub zostali zdyskwalifikowani
                    if status in excluded_statuses:
                        continue

                    total_points = athlete.get("total_points")
                    jumps = athlete.get("jumps", [])

                    is_pathology = False
                    reasons = []

                    # Warunek 1: Ujemne punkty (i upewnienie się, że nie jest to None)
                    if total_points is not None and isinstance(total_points, (int, float)) and total_points < 0:
                        is_pathology = True
                        reasons.append("Ujemne punkty (prawdopodobnie strata do lidera)")

                    # Warunek 2: Brak skoków w liście
                    if not jumps:  # len(jumps) == 0
                        is_pathology = True
                        reasons.append("Brak skoków na liście")

                    # Jeśli znaleziono problem, dodajemy zawodnika do listy dla danego URL-a
                    if is_pathology:
                        pathologies[race_url].append({
                            "name": athlete.get("name"),
                            "rank": athlete.get("rank"),
                            "status": status,
                            "total_points": total_points,
                            "jumps": jumps,  # Pokaże [] jeśli lista jest pusta
                            "reasons": reasons,
                            "file_source": os.path.basename(json_file),
                            "category": comp.get("category"),
                            "date": comp.get("date"),
                        })

    # Formatowanie wyników do przyjaznej listy JSON
    formatted_output = []
    for url, problematic_athletes in pathologies.items():



        formatted_output.append({
            "raceUrl": url,
            "affected_athletes_count": len(problematic_athletes),
            "category": problematic_athletes[0]["category"],
            "date": problematic_athletes[0]["date"],
            # "athletes": problematic_athletes
        })

    return [resultItem for resultItem in formatted_output if resultItem["category"] == "WC"]


if __name__ == "__main__":
    print("Rozpoczynam skanowanie w poszukiwaniu patologii...")

    wyniki = find_pathologies()
    output_file = "report.json"

    if wyniki:
        # Zapis do pliku
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(wyniki, f, indent=4, ensure_ascii=False)

        print(f"\n[UWAGA] Znaleziono {len(wyniki)} zawodów, w których wystąpiły błędy scrapowania.")
        print(f"Pełny raport został zapisany do pliku: {output_file}")
    else:
        print(
            "\nNie znaleziono żadnych patologii! Wszystkie sklasyfikowane rekordy mają dodatnie punkty i zliczone skoki.")