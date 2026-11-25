# app/services/prediction/calendar_service.py
import datetime
import re
import time
import logging
from typing import Optional, Dict, Any

from app.services.scrapers.scraper import fetch_fis_calendar
from app.services.scrapers.event_scraper import fetch_event_details
from app.services.hill_service import normalize_place_name

logger = logging.getLogger(__name__)

# Pamięć podręczna RAM na wynik najbliższych zawodów (TTL: 15 minut)
_NEXT_COMPETITION_CACHE: Dict[str, Any] = {}
_CACHE_TIMESTAMP: float = 0.0
_CACHE_TTL_SECONDS: float = 900.0  # 15 minut


def parse_date_range_end(date_str: str, default_year: int) -> datetime.date:
    """
    Parsuje napis daty FIS np. '27-29 Nov 2026', '20-22 Nov' lub '31 Dec-01 Jan 2027'.
    Zwraca końcową datę weekendu jako datetime.date.
    """
    months = {
        'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
        'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
    }
    date_str_clean = date_str.strip().upper()

    # Format: "27-29 NOV 2026"
    m1 = re.search(r'\d{1,2}\s*-\s*(\d{1,2})\s+([A-Z]{3})\s+(\d{4})', date_str_clean)
    if m1:
        day = int(m1.group(1))
        mon = months.get(m1.group(2), 11)
        yr = int(m1.group(3))
        return datetime.date(yr, mon, day)

    # Format: "20-22 NOV"
    m2 = re.search(r'\d{1,2}\s*-\s*(\d{1,2})\s+([A-Z]{3})', date_str_clean)
    if m2:
        day = int(m2.group(1))
        mon = months.get(m2.group(2), 11)
        return datetime.date(default_year, mon, day)

    # Format: "28 NOV 2026"
    m3 = re.search(r'(\d{1,2})\s+([A-Z]{3})\s+(\d{4})', date_str_clean)
    if m3:
        day = int(m3.group(1))
        mon = months.get(m3.group(2), 11)
        yr = int(m3.group(3))
        return datetime.date(yr, mon, day)

    return datetime.date(default_year, 12, 31)


async def get_next_upcoming_competition(
    db,
    target_category: str = "WC",
    gender: str = "M",
    force_refresh: bool = False
) -> Dict[str, Any]:
    """
    Identyfikuje najbliższe nadchodzące zawody w kalendarzu FIS (np. Ruka, Wisła, Titisee).
    1. Sprawdza czy w bazie sessions istnieją zaplanowane sesje na przyszłość.
    2. Jeśli nie, pobiera oficjalny kalendarz FIS z aktualnego sezonu (eventselection='actual').
    3. Pobiera szczegóły najbliższego weekendu i dopasowuje skocznię z bazy hills.
    Wynik jest buforowany w pamięci RAM przez 15 minut.
    """
    global _NEXT_COMPETITION_CACHE, _CACHE_TIMESTAMP

    cache_key = f"{target_category}_{gender}"
    now_ts = time.time()

    if not force_refresh and (now_ts - _CACHE_TIMESTAMP < _CACHE_TTL_SECONDS) and cache_key in _NEXT_COMPETITION_CACHE:
        return _NEXT_COMPETITION_CACHE[cache_key]

    today = datetime.date.today()
    today_str = today.isoformat()

    # --- KROK 1: Sprawdzenie sesji w bazie MongoDB ---
    try:
        future_session = await db["sessions"].find_one({
            "date": {"$gte": today_str},
            "session_type": {"$in": ["COMPETITION", "QUA"]}
        }, sort=[("date", 1), ("time", 1)])

        if future_session:
            place_name = normalize_place_name(future_session.get("event_place"), future_session.get("event_country"))
            hill_size = future_session.get("hillSize") or 130
            hill_type = future_session.get("hillType") or "LARGE"
            hill = await db["hills"].find_one({
                "place": {"$regex": f"^{re.escape(place_name)}$", "$options": "i"},
                "size": hill_size
            })

            res = {
                "found": True,
                "source": "database",
                "place": place_name,
                "country": future_session.get("event_country") or "",
                "country_code": future_session.get("event_country") or "",
                "hill_id": hill["_id"] if hill else f"{place_name.upper()}_HS{int(hill_size)}",
                "hill_size": int(hill_size),
                "hill_type": hill_type,
                "date": future_session.get("date"),
                "time": future_session.get("time") or "16:00",
                "competition_type": future_session.get("category_code") or target_category,
                "event_name": f"{place_name} ({future_session.get('event_country') or ''})",
                "session_title": future_session.get("title") or "Konkurs Indywidualny",
                "qualification": None
            }
            _NEXT_COMPETITION_CACHE[cache_key] = res
            _CACHE_TIMESTAMP = now_ts
            return res
    except Exception as e:
        logger.warning(f"Błąd podczas szukania przyszłych sesji w bazie: {e}")

    # --- KROK 2: Sprawdzenie kalendarza FIS ---
    year = today.year
    season = str(year + 1 if today.month >= 10 else year)

    try:
        import asyncio
        cal = await asyncio.to_thread(
            fetch_fis_calendar,
            seasoncode=season,
            categorycode=target_category,
            gendercode=gender,
            seasonmonth="",
            eventselection="actual"
        )

        upcoming_event = None
        for ev in cal:
            d_str = ev.get("date", "")
            end_date = parse_date_range_end(d_str, year)
            if end_date >= today:
                upcoming_event = ev
                break

        if not upcoming_event:
            # Fallback: zwróć pierwszy element jeśli wszystkie wydają się przeszłe
            upcoming_event = cal[0] if cal else None

        if not upcoming_event:
            return {"found": False, "message": "Brak nadchodzących zawodów w kalendarzu FIS"}

        event_id = upcoming_event.get("id")
        races = await asyncio.to_thread(
            fetch_event_details,
            sectorcode="JP",
            eventid=event_id,
            seasoncode=season,
            categorycode=target_category,
            filter_gender=gender
        )

        comp_race = None
        qua_race = None
        for r in races:
            if r.get("session_type") == "COMPETITION" and not comp_race:
                comp_race = r
            elif r.get("session_type") == "QUA" and not qua_race:
                qua_race = r

        target_race = comp_race or qua_race or (races[0] if races else {})
        place_name = normalize_place_name(upcoming_event.get("place"), upcoming_event.get("country"))
        country_code = upcoming_event.get("country") or ""

        # Dopasowanie skoczni z bazy
        hill = await db["hills"].find_one({
            "place": {"$regex": f"^{re.escape(place_name)}$", "$options": "i"},
            "is_current": True
        })
        if not hill:
            hill = await db["hills"].find_one({
                "place": {"$regex": f"^{re.escape(place_name)}$", "$options": "i"}
            })

        default_size = 142 if "ruka" in place_name.lower() else (hill.get("size", 130) if hill else 130)
        default_type = hill.get("type", "LARGE") if hill else "LARGE"

        res = {
            "found": True,
            "source": "calendar",
            "place": place_name,
            "country": country_code,
            "country_code": country_code,
            "hill_id": hill["_id"] if hill else f"{place_name.upper()}_HS{default_size}",
            "hill_size": hill.get("size", default_size) if hill else default_size,
            "hill_type": default_type,
            "date": target_race.get("date") or today_str,
            "time": target_race.get("time") or "16:00",
            "competition_type": target_category,
            "event_id": event_id,
            "event_name": f"{place_name} ({country_code})",
            "session_title": target_race.get("title") or "Konkurs Indywidualny",
            "qualification": {
                "date": qua_race.get("date") if qua_race else None,
                "time": qua_race.get("time") if qua_race else None,
                "title": qua_race.get("title") if qua_race else None
            } if qua_race else None
        }

        _NEXT_COMPETITION_CACHE[cache_key] = res
        _CACHE_TIMESTAMP = now_ts
        return res

    except Exception as e:
        logger.error(f"Błąd podczas pobierania najbliższych zawodów z kalendarza FIS: {e}")
        return {
            "found": False,
            "error": str(e)
        }
