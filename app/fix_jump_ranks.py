import pymongo

# Ustaw parametry połączenia ze swoją bazą danych MongoDB
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "FIS_DATA_DB"  # <--- Zmień na swoją nazwę bazy, jeśli jest inna

client = pymongo.MongoClient(MONGO_URI)
db = client[DB_NAME]
sessions_col = db["sessions"]


def fix_jump_ranks():
    print("Rozpoczynam weryfikację i naprawę pola rank w seriach skoków...")

    # Szukamy tylko dokumentów, które w ogóle posiadają jakieś wyniki
    cursor = sessions_col.find({"results_data.results": {"$exists": True, "$not": {"$size": 0}}})

    updated_count = 0
    skipped_count = 0

    for session in cursor:
        results = session.get("results_data", {}).get("results", [])
        if not results:
            continue

        # KROK 1: Sprawdzenie, czy sesja wymaga naprawy
        needs_update = False
        for athlete in results:
            jumps = athlete.get("jumps", [])
            # Sprawdzamy czy pierwszy dostępny skok ma brakujące pole rank lub ustawione na None
            if jumps and jumps[0].get("rank") is None:
                # Omijamy puste skoki (np. jeśli jump nie ma distance/points)
                if jumps[0].get("points") is not None:
                    needs_update = True
                    break

        if not needs_update:
            skipped_count += 1
            continue

        # KROK 2: Grupowanie skoków według rundy (round)
        # Struktura: { runda: [(index_zawodnika, index_skoku, punkty)] }
        rounds_data = {}

        for a_idx, athlete in enumerate(results):
            jumps = athlete.get("jumps", [])

            for j_idx, jump in enumerate(jumps):
                pts = jump.get("points")
                rnd = jump.get("round")

                # Przetwarzamy tylko te skoki, w których przyznano notę punktową
                # (Zabezpiecza to przed przypisaniem miejsc zawodnikom DNS/DSQ/NPS)
                if rnd is not None and pts is not None:
                    try:
                        pts_val = float(pts)
                        if rnd not in rounds_data:
                            rounds_data[rnd] = []
                        rounds_data[rnd].append((a_idx, j_idx, pts_val))
                    except (ValueError, TypeError):
                        pass  # Ignorujemy nieprawidłowe wartości punktowe

        # KROK 3: Sortowanie i przypisywanie miejsc (rank)
        for rnd, jumps_in_round in rounds_data.items():
            # Sortujemy wyniki malejąco po notach punktowych
            sorted_jumps = sorted(jumps_in_round, key=lambda x: x[2], reverse=True)

            current_rank = 1
            previous_points = None

            for i, (a_idx, j_idx, pts) in enumerate(sorted_jumps):
                # Logika remisów (tzw. zasada ex aequo)
                if previous_points is None:
                    current_rank = 1
                elif pts < previous_points:
                    # 'i' to indeks iteracji (0, 1, 2...).
                    # Jeśli jest to trzeci zawodnik (i=2) i ma mniej punktów, zajmuje 3. miejsce (i+1).
                    current_rank = i + 1

                # Aktualizacja konkretnego skoku w liście wyników
                results[a_idx]["jumps"][j_idx]["rank"] = current_rank
                previous_points = pts

        # KROK 4: Aktualizacja dokumentu w bazie danych
        sessions_col.update_one(
            {"_id": session["_id"]},
            {"$set": {"results_data.results": results}}
        )
        updated_count += 1

    print("=========================================")
    print(f"Operacja zakończona!")
    print(f"Zaktualizowano sesji: {updated_count}")
    print(f"Pominięto (już miały rank): {skipped_count}")
    print("=========================================")


if __name__ == "__main__":
    fix_jump_ranks()