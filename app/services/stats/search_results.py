# app/services/stats/search_results.py
import re
from collections import defaultdict


async def search_athlete_results(db, names=None, place=None, hill_type=None, date_from=None, date_to=None,
                                 category=None, rank=None, limit=None, session_type=None):
    """
    Przeszukuje kolekcję 'sessions' w bazie MongoDB według podanych kryteriów
    i zwraca wyniki jako listę obiektów gotową dla nowej wersji frontendu.
    """

    # --- 1. BUDOWANIE ZAPYTANIA DO MONGODB (FILTROWANIE NA POZIOMIE BAZY) ---
    query = {}

    if place:
        # Szukamy po części nazwy miejsca (niezależnie od wielkości liter)
        query["event_place"] = {"$regex": place, "$options": "i"}

    if category:
        query["category_code"] = category

    if hill_type:
        query["hillType"] = hill_type

    if session_type:
        query["session_type"] = {"$regex": session_type, "$options": "i"}

    # Filtrowanie po dacie (MongoDB świetnie radzi sobie z formatem YYYY-MM-DD)
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    # Wstępne filtrowanie po nazwiskach już na poziomie bazy danych (optymalizacja)
    if names:
        # Tworzymy regex typu: "KOUDELKA Roman|STOCH Kamil"
        regex_names = "|".join([re.escape(n) for n in names])
        query["results_data.results.name"] = {"$regex": regex_names, "$options": "i"}

    if rank:
        query["results_data.results.rank"] = rank

    # --- 2. POBRANIE DANYCH Z BAZY ---
    # Pobieramy pasujące sesje. Jeśli nie podano filtrów, może być tego dużo, ale MongoDB to ogarnie
    cursor = db["sessions"].find(query)
    sessions = await cursor.to_list(length=None)

    if not sessions:
        return []

    # --- 3. PRZETWARZANIE DANYCH (GRUPOWANIE W PYTHONIE) ---
    grouped_results = defaultdict(list)

    for session in sessions:
        event_place = session.get("event_place", "")
        comp_date_str = session.get("date")

        results_data = session.get("results_data") or {}
        athletes = results_data.get("results", [])

        for athlete in athletes:
            ath_name = athlete.get("name", "")

            # Podwójna weryfikacja nazwiska w Pythonie (częściowe dopasowania)
            if names:
                match = any(req_name.lower() in ath_name.lower() for req_name in names)
                if not match:
                    continue

            if rank and athlete.get("rank") != rank:
                continue

            jumps = athlete.get("jumps", [])

            # ZBUDOWANIE REKORDU JSON DLA KAŻDEGO SKOKU
            for jump in jumps:
                record = {
                    "session_id": session.get("_id"),  # Zwróci np. "7555_3185"
                    "codex": session.get("codex"),
                    "race_id": session.get("raceId"),  # DODANE dla pełnej jasności
                    "date": comp_date_str or "9999-99-99",
                    "time": session.get("time") or "99:99",
                    "place": event_place,
                    "hill_size": session.get("hillSize"),
                    "hill_type": session.get("hillType"),
                    "category": session.get("category_code"),
                    "session_type": session.get("session_type"),
                    "rank": athlete.get("rank"),
                    "round": jump.get("round"),
                    "distance": jump.get("distance"),
                    "points": jump.get("points"),
                    "jump_rank": jump.get("rank"),
                    "competition_points": athlete.get("competition_points", 0),
                }
                grouped_results[ath_name].append(record)

    # --- 4. SORTOWANIE I LIMITOWANIE ---
        # --- 4. SORTOWANIE I LIMITOWANIE ---
        for ath_name in grouped_results:
            # Sortowanie chronologiczne wyników dla każdego zawodnika
            # Dodajemy 'round' do klucza sortowania. Używamy 'or 0', aby uniknąć błędu
            # w przypadku braku numeru rundy (None).
            grouped_results[ath_name].sort(
                key=lambda x: (x["date"], x["time"], x.get("round") or 0),
                reverse=True
            )

        # Zastosowanie limitu
        if limit is not None:
            grouped_results[ath_name] = grouped_results[ath_name][:limit]

    # --- 5. KONWERSJA NA DOCELOWY FORMAT ---
    final_results = []
    for ath_name, jumps in grouped_results.items():
        final_results.append({
            "name": ath_name,
            "jumps": jumps
        })

    return final_results