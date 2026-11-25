# app/services/athlete_service.py
"""
Serwis analityczny dla modułu skoczków (Athletes):
- Zaawansowane metryki kariery (starty, zwycięstwa, podia, top 10, top 30)
- Profil formy na typach skoczni (FLYING / Mamuty, LARGE / Duże, NORMAL / Normalne)
- Historia ostatnich występów i średnie odległości
"""

from typing import Optional, Dict, Any
from app.infrastructure.cache import get_cached_value, set_cached_value


async def get_athlete_full_metrics(db, fis_code: int, dictionaries=None) -> Optional[Dict[str, Any]]:
    """
    Pobiera pełne statystyki zawodnika, w tym formę na typach skoczni i ostatnie starty.
    Obsługuje dopasowanie zarówno typu int jak i str dla maksymalnej odporności danych.
    """
    fc_int = int(fis_code)
    fc_str = str(fis_code)

    cache_key = f"athlete_metrics_{fc_int}"
    cached = get_cached_value("medium", cache_key)
    if cached is not None:
        return cached

    athlete = await db["athletes"].find_one({"fis_code": fc_int})
    if not athlete:
        # Fallback na string
        athlete = await db["athletes"].find_one({"fis_code": fc_str})
        if not athlete:
            return None

    name = athlete.get("full_name")
    country_code = athlete.get("country_code")

    pipeline = [
        {
            "$match": {
                "$or": [
                    {"results_data.results.fis_code": fc_str},
                    {"results_data.results.fis_code": fc_int},
                    {"results_data.results.name": name}
                ]
            }
        },
        {"$sort": {"date": -1, "time": -1}},
        {
            "$project": {
                "date": 1,
                "event_place": 1,
                "hillSize": 1,
                "hillType": 1,
                "session_type": 1,
                "category_code": 1,
                "race_category": 1,
                "title": 1,
                "results_data.results": {
                    "$filter": {
                        "input": "$results_data.results",
                        "as": "r",
                        "cond": {
                            "$or": [
                                {"$eq": ["$$r.fis_code", fc_str]},
                                {"$eq": ["$$r.fis_code", fc_int]},
                                {"$eq": ["$$r.name", name]}
                            ]
                        }
                    }
                }
            }
        }
    ]

    sessions = await db["sessions"].aggregate(pipeline).to_list(None)

    total_competitions = 0
    wins = 0
    podiums = 0
    top10 = 0
    top30 = 0

    hill_stats = {
        "FLYING": {"label": "Skocznia mamucia", "starts": 0, "best_rank": None, "distances": [], "points": []},
        "LARGE": {"label": "Duża skocznia", "starts": 0, "best_rank": None, "distances": [], "points": []},
        "NORMAL": {"label": "Normalna skocznia", "starts": 0, "best_rank": None, "distances": [], "points": []},
    }

    recent_performances = []

    for s in sessions:
        results = s.get("results_data", {}).get("results", [])
        if not results:
            continue
        r = results[0]
        rank = r.get("rank")
        stype = (s.get("session_type") or "").upper().strip()
        htype = (s.get("hillType") or "LARGE").upper().strip()
        if htype not in hill_stats:
            htype = "LARGE"

        is_comp = stype in ["COMPETITION", "COMP"] or "competition" in (s.get("title") or "").lower()
        if is_comp and rank:
            total_competitions += 1
            if rank == 1:
                wins += 1
            if rank <= 3:
                podiums += 1
            if rank <= 10:
                top10 += 1
            if rank <= 30:
                top30 += 1

            h = hill_stats[htype]
            h["starts"] += 1
            if h["best_rank"] is None or rank < h["best_rank"]:
                h["best_rank"] = rank
            if r.get("total_points") is not None:
                h["points"].append(float(r["total_points"]))

            for j in r.get("jumps", []):
                if j.get("distance"):
                    h["distances"].append(float(j["distance"]))

            if len(recent_performances) < 6:
                recent_performances.append({
                    "date": s.get("date"),
                    "place": s.get("event_place"),
                    "hill_size": s.get("hillSize"),
                    "hill_type": htype,
                    "rank": rank,
                    "total_points": r.get("total_points"),
                    "jumps": r.get("jumps", [])
                })

    for ht, data in hill_stats.items():
        data["avg_distance"] = round(sum(data["distances"]) / len(data["distances"]), 1) if data["distances"] else None
        data["avg_points"] = round(sum(data["points"]) / len(data["points"]), 1) if data["points"] else None
        data.pop("distances", None)
        data.pop("points", None)

    flag_url = None
    if dictionaries and country_code:
        flag_url = dictionaries.get_flag_url(country_code)

    result = {
        "athlete": {
            "fis_code": fc_int,
            "name": name,
            "country": country_code,
            "flagUrl": flag_url,
            "status": athlete.get("status"),
            "birthdate": athlete.get("birthdate"),
            "birth_year": athlete.get("birth_year"),
            "price": athlete.get("price"),
            "club": athlete.get("club")
        },
        "career": {
            "total_competitions": total_competitions,
            "wins": wins,
            "podiums": podiums,
            "top10": top10,
            "top30": top30,
        },
        "hill_type_breakdown": hill_stats,
        "recent_performances": recent_performances
    }

    set_cached_value("medium", cache_key, result)
    return result
