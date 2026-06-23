# app/services/stats/search_results.py
import re
from collections import defaultdict


async def search_athlete_results(db, names=None, place=None, hill_type=None, date_from=None, date_to=None,
                                 category=None, limit=None, session_type=None):
    """
    Przeszukuje kolekcję 'sessions' w bazie MongoDB według podanych kryteriów.
    Zoptymalizowane: Ogranicza wyniki do dokładnie N ostatnich MOŻLIWYCH SKOKÓW (Serii/Rund).
    """

    # --- 1. BUDOWANIE ZAPYTANIA $match (FILTROWANIE W BAZIE) ---
    match_stage = {}

    if place:
        match_stage["event_place"] = {"$regex": place, "$options": "i"}

    if category:
        if isinstance(category, list) and len(category) > 0:
            match_stage["category_code"] = {"$in": category}
        else:
            match_stage["category_code"] = category

    if hill_type:
        match_stage["hillType"] = hill_type

    if session_type:
        match_stage["session_type"] = {"$regex": session_type, "$options": "i"}

    if date_from or date_to:
        match_stage["date"] = {}
        if date_from:
            match_stage["date"]["$gte"] = date_from
        if date_to:
            match_stage["date"]["$lte"] = date_to

    if names:
        match_stage["results_data.results"] = {"$elemMatch": {"name": {"$in": names}}}

    pipeline = [{"$match": match_stage}]

    # --- 2. SORTOWANIE SESJI ---
    pipeline.append({"$sort": {"date": -1, "time": -1}})

    # Pobieramy N sesji - jedna sesja to zwykle 1-3 skoków, więc
    # pobranie 'limit' sesji gwarantuje, że będziemy mieli co przycinać.
    if limit:
        pipeline.append({"$limit": limit})

    # --- 3. OPTYMALIZACJA PAYLOADU ($project + $filter) ---
    if names:
        pipeline.append({
            "$project": {
                "codex": 1,
                "raceId": 1,
                "date": 1,
                "time": 1,
                "event_place": 1,
                "hillSize": 1,
                "hillType": 1,
                "category_code": 1,
                "session_type": 1,
                "results_data.results": {
                    "$filter": {
                        "input": "$results_data.results",
                        "as": "athlete",
                        "cond": {"$in": ["$$athlete.name", names]}
                    }
                }
            }
        })
    else:
        pipeline.append({
            "$project": {
                "codex": 1, "raceId": 1, "date": 1, "time": 1, "event_place": 1,
                "hillSize": 1, "hillType": 1, "category_code": 1, "session_type": 1,
                "results_data.results": 1
            }
        })

    # --- 4. POBRANIE DANYCH Z BAZY ---
    cursor = db["sessions"].aggregate(pipeline)
    sessions = await cursor.to_list(length=None)

    if not sessions:
        return []

    # --- 5. IDENTYFIKACJA OSTATNICH 'N' MOŻLIWYCH SKOKÓW (RUND/SERII) ---
    all_possible_rounds = set()
    raw_jumps = defaultdict(list)

    for session in sessions:
        codex = session.get("codex")
        comp_date_str = session.get("date") or "9999-99-99"
        comp_time_str = session.get("time") or "99:99"
        event_place = session.get("event_place", "")

        results_data = session.get("results_data") or {}
        athletes = results_data.get("results", [])

        for athlete in athletes:
            ath_name = athlete.get("name", "")
            jumps = athlete.get("jumps", [])

            for jump in jumps:
                round_num = jump.get("round")

                # Unikalny identyfikator konkretnej serii skoków (np. 2 Seria w Zakopanem)
                round_identifier = (comp_date_str, comp_time_str, codex, round_num)
                all_possible_rounds.add(round_identifier)

                record = {
                    "session_id": str(session.get("_id")),
                    "codex": codex,
                    "race_id": session.get("raceId"),
                    "date": comp_date_str,
                    "time": comp_time_str,
                    "place": event_place,
                    "hill_size": session.get("hillSize"),
                    "hill_type": session.get("hillType"),
                    "category": session.get("category_code"),
                    "session_type": session.get("session_type"),
                    "rank": athlete.get("rank"),
                    "round": round_num,
                    "distance": jump.get("distance"),
                    "points": jump.get("points"),
                    "jump_rank": jump.get("rank"),
                    "competition_points": athlete.get("competition_points", 0),
                }
                raw_jumps[ath_name].append((round_identifier, record))

    # Sortujemy WSZYSTKIE dostępne rundy chronologicznie (od najnowszych)
    sorted_rounds = sorted(list(all_possible_rounds), key=lambda x: (x[0], x[1], x[3] or 0), reverse=True)

    # Obcinamy do żądanego limitu skoków
    if limit:
        allowed_rounds = set(sorted_rounds[:limit])
    else:
        allowed_rounds = set(sorted_rounds)

    # --- 6. BUDOWANIE FINALNEJ TABELI TYLKO DLA DOZWOLONYCH SKOKÓW ---
    grouped_results = defaultdict(list)

    for ath_name, jumps_with_id in raw_jumps.items():
        for round_id, record in jumps_with_id:
            # Dodajemy skok zawodnika TYLKO jeśli mieści się w puli najnowszych rund!
            if round_id in allowed_rounds:
                grouped_results[ath_name].append(record)

        # Sortowanie wewnątrz samego zawodnika
        grouped_results[ath_name].sort(
            key=lambda x: (x["date"], x["time"], x.get("round") or 0),
            reverse=True
        )

    final_results = [{"name": ath_name, "jumps": jumps} for ath_name, jumps in grouped_results.items()]

    if names:
        names_dict = {name: i for i, name in enumerate(names)}
        final_results.sort(key=lambda x: names_dict.get(x["name"], 999))

    return final_results