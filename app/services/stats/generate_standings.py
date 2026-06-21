# app/services/stats/generate_standings.py
from collections import defaultdict
from app.services.dictionaries.dictionary_service import DictionaryService


async def generate_standings_from_db(db, season: str, category: str, dictionaries: DictionaryService):
    """
    Agreguje punkty wszystkich skoczków z podanego sezonu i kategorii,
    pobierając dane bezpośrednio z kolekcji 'sessions' w MongoDB.

    Działa dokładnie tak samo jak skrypt generate_stats.py:
    - Zlicza starty każdemu zawodnikowi z listy (niezależnie od punktów).
    - Sumuje punkty za konkursy.
    - Sortuje tabelę malejąco według punktów.
    """

    # 1. BUDUJEMY PRECYZYJNE ZAPYTANIE (Tylko oficjalne konkursy merytoryczne)
    query = {
        "season": season,
        "category_code": category,
        "session_type": {"$regex": "COMPETITION$", "$options": "i"}
    }

    cursor = db["sessions"].find(query)
    sessions = await cursor.to_list(length=None)

    if not sessions:
        return {
            "season": season,
            "category": category,
            "total_events": 0,
            "standings": []
        }

    # ZABEZPIECZENIE: Zbiór do eliminacji duplikatów sesji z bazy danych
    seen_race_ids = set()

    # Mapa: fis_code -> dane zawodnika
    standings = defaultdict(lambda: {
        "fis_code": None,
        "name": "",
        "country": "",
        "starts": 0,
        "total_points": 0,
        "flag": ""
    })

    # 2. Przetwarzanie sesji
    for session in sessions:
        race_id = session.get("race_id")
        if race_id in seen_race_ids:
            continue
        if race_id:
            seen_race_ids.add(race_id)

        # Używamy ścieżki z pliku JSON po spłaszczeniu w admin_controller
        results_data = session.get("results_data", {})
        athletes = results_data.get("results", [])

        for athlete in athletes:
            # KLUCZOWY FILTR: Bierzemy tylko zawodników, którzy zostali sklasyfikowani
            status = athlete.get("status", "")
            if status != "Classified":
                continue

            fis_code = athlete.get("fis_code")
            if not fis_code:
                continue

            # Konwersja fis_code na int (zgodnie z intencją w skrypcie DB)
            try:
                fis_code_key = int(fis_code)
            except (ValueError, TypeError):
                fis_code_key = fis_code

            # Aktualizacja podstawowych danych zawodnika
            standings[fis_code_key]["fis_code"] = fis_code_key
            standings[fis_code_key]["name"] = athlete.get("name")
            standings[fis_code_key]["country"] = athlete.get("country")

            # Zgodnie z generate_stats.py: każdemu obecnemu zawodnikowi dodajemy start
            standings[fis_code_key]["starts"] += 1

            # W generate_stats.py pole nazywa się 'world_cup_points',
            # w db używasz 'competition_points' (pobieramy je bezpiecznie)
            points = athlete.get("competition_points") or athlete.get("world_cup_points", 0)
            standings[fis_code_key]["total_points"] += points

            # Pobranie flagi ze wstrzykniętego serwisu słowników
            standings[fis_code_key]["flag"] = dictionaries.get_flag_url(athlete.get("country"))

    # 4. Sortowanie klasyfikacji według punktów (malejąco)
    sorted_standings = sorted(
        standings.values(),
        key=lambda x: x["total_points"],
        reverse=True
    )

    # 5. Dodanie rankingu
    for rank, athlete_data in enumerate(sorted_standings, start=1):
        athlete_data["rank"] = rank

    return {
        "season": season,
        "category": category,
        "total_events": len(seen_race_ids),
        "standings": sorted_standings
    }