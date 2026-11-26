# app/controller/stats_controller.py
import asyncio
import datetime
import re
from fastapi import APIRouter, Depends, Request, HTTPException, Query, status
from pydantic import BaseModel, Field
from typing import Optional

from app.auth import require_role, get_current_user
from app.services.auth_service import Role, is_pro_role
from app.infrastructure.limiter import limiter
from app.model.SearchRequest import SearchRequest
from app.model.StandingsRequest import StandingsRequest
from app.services.stats.search_results import search_athlete_results
from app.services.stats.generate_standings import generate_standings_from_db
from app.services.dictionaries.dictionary_service import DictionaryService, get_dictionary_service
from app.infrastructure.cache import get_cached_value, set_cached_value
from app.services.scrapers.scraper import fetch_fis_calendar
from app.services.prediction.calendar_service import parse_date_range_start, parse_date_range_end
from app.services.draft_service import TCS_VENUES, RAW_AIR_VENUES, PLANICA_7_VENUES, _tournament_default_bounds

router = APIRouter(
    prefix="/stats",
    tags=["Stats"],
)


# --- MODELE ---
class SessionSearchFilter(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    session_type: Optional[str] = None
    category: Optional[str] = None
    season: Optional[str] = None
    limit: int = Field(default=20, ge=1, le=100)
    skip: int = Field(default=0, ge=0)


# --- ENDPOINTY STATYSTYK ZAWODNIKÓW ---
@router.post("/search")
@limiter.limit("60/minute")
async def search(
    request: Request,
    search_data: SearchRequest,
    user: dict = Depends(get_current_user)
):
    """Zaawansowane statystyki i porównywarka. Darmowe konto pozwala na porównanie max 2 skoczków i 10 ostatnich prób."""
    if not is_pro_role(user.get("role")):
        names_list = search_data.names or []
        if len(names_list) > 2:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Darmowe konto pozwala na jednoczesne porównanie maksymalnie 2 zawodników. Przejdź na pakiet PRO, aby analizować nielimitowaną liczbę skoczków."
            )
        if not search_data.limit or search_data.limit > 10:
            search_data.limit = 10
    db = request.app.database
    return await search_athlete_results(db, **search_data.model_dump())


@router.post("/standings")
@limiter.limit("120/minute")
async def get_standings(
    request: Request,
    request_data: StandingsRequest,
    dictionaries: DictionaryService = Depends(get_dictionary_service),
):
    """Klasyfikacja generalna i punktacja pucharowa (dostępna publicznie, cache 5 min)."""
    cache_key = f"standings_{request_data.season}_{request_data.category}"
    cached = get_cached_value("medium", cache_key)
    if cached is not None:
        return cached

    db = request.app.database
    result = await generate_standings_from_db(db, request_data.season, request_data.category, dictionaries)
    set_cached_value("medium", cache_key, result)
    return result


# --- NOWE ENDPOINTY BAZY SESJI ---
@router.post("/sessions")
@limiter.limit("120/minute")
async def get_sessions_list(request: Request, filters: SessionSearchFilter):
    """Pobiera przefiltrowaną listę sesji z MongoDB."""
    db = request.app.database
    query = {}

    if filters.date_from or filters.date_to:
        query["date"] = {}
        if filters.date_from and filters.date_from.strip(): query["date"]["$gte"] = filters.date_from.strip()
        if filters.date_to and filters.date_to.strip(): query["date"]["$lte"] = filters.date_to.strip()
        if not query["date"]:
            del query["date"]

    if filters.session_type and filters.session_type.strip():
        query["session_type"] = {"$regex": re.escape(filters.session_type.strip()), "$options": "i"}

    if filters.category and filters.category.strip():
        query["category_code"] = filters.category.strip()

    if filters.season and filters.season.strip():
        query["season"] = filters.season.strip()

    # Pobieramy podstawowe dane, pomijając ciężki obiekt wyników dla optymalizacji listy
    projection = {"results_data": 0}

    # Sortujemy malejąco po dacie i czasie
    cursor = db["sessions"].find(query, projection).sort([("date", -1), ("time", -1)]).skip(filters.skip).limit(
        filters.limit)
    sessions = await cursor.to_list(length=filters.limit)

    # Rzutowanie ObjectId na string, na wypadek gdyby trafiło się systemowe ID
    for session in sessions:
        if "_id" in session:
            session["_id"] = str(session["_id"])

    return sessions


@router.get("/sessions/{session_id}")
@limiter.limit("60/minute")
async def get_session_details(request: Request, session_id: str):
    """Pobiera pełne szczegóły pojedynczej sesji (wraz ze skokami zawodników, cache 5 min)."""
    cache_key = f"session_detail_{session_id}"
    cached = get_cached_value("medium", cache_key)
    if cached is not None:
        return cached

    db = request.app.database
    session = await db["sessions"].find_one({"_id": session_id})

    if not session:
        raise HTTPException(status_code=404, detail="Nie znaleziono sesji")

    session["_id"] = str(session["_id"])
    set_cached_value("medium", cache_key, session)
    return session


# Dodaj na górze pliku, jeśli brakuje
# z fastapi import Request

@router.get("/form-data")
@limiter.limit("60/minute")
async def get_stats_form_data(request: Request):
    """Zwraca słowniki (w tym dynamicznie wygenerowane skocznie, kategorie i sesje) dla wyszukiwarki statystyk."""
    cached = get_cached_value("medium", "stats_form_data")
    if cached is not None:
        return cached

    dicts = request.app.state.dicts

    # Z całego słownika hills wyciągamy tylko unikalne nazwy miejscowości
    hills_list = dicts.get("hills", [])
    places = sorted(list(set([h["name"] for h in hills_list if h.get("name")])))

    result = {
        "places": places,
        "hill_types": dicts.get("hill_type", {}),
        "categories": dicts.get("categories", {}),
        "sessions": dicts.get("sessions", {})
    }
    set_cached_value("medium", "stats_form_data", result)
    return result


@router.get("/athletes/with-prices")
@limiter.limit("60/minute")
async def get_athletes_with_prices(
    request: Request,
    user: dict = Depends(require_role(Role.USER_PRO, Role.ADMIN))
):
    """Pobiera listę skoczków z określoną ceną do porównywarki i narzędzi Fantasy (wymaga FantaSKI PRO, cache 1 min)."""
    cached = get_cached_value("short", "athletes_with_prices")
    if cached is not None:
        return cached

    db = request.app.database

    # Filtrujemy tylko aktywnych zawodników, którzy mają ustaloną cenę > 0
    cursor = db["athletes"].find(
        {"price": {"$exists": True, "$gt": 0}, "isDisabled": False},
        {"full_name": 1, "fis_code": 1, "price": 1, "_id": 0}
    ).sort("price", -1)

    athletes = await cursor.to_list(length=1000)

    result = [
        {
            "name": a.get("full_name"),
            "fis_code": a.get("fis_code"),
            "price": a.get("price")
        } for a in athletes
    ]
    set_cached_value("short", "athletes_with_prices", result)
    return result


@router.get("/calendar")
@limiter.limit("30/minute")
async def get_fis_season_calendar(
    request: Request,
    season: str = Query(default="2027"),
    category: str = Query(default="WC"),
):
    """
    Pobiera oficjalny kalendarz FIS na wskazany sezon i kategorię (np. Puchar Świata 2026/27),
    oznacza turnieje (TCS / Raw Air / Planica 7) oraz sprawdza, które przystanki mają już sesje w bazie.
    """
    clean_season = season.strip()
    clean_cat = category.strip().upper()
    cache_key = f"fis_calendar_v3_{clean_season}_{clean_cat}"
    cached = get_cached_value("medium", cache_key)
    if cached is not None:
        return cached

    try:
        year_int = int(clean_season)
    except ValueError:
        year_int = datetime.date.today().year

    raw_events = await asyncio.to_thread(
        fetch_fis_calendar,
        seasoncode=clean_season,
        categorycode=clean_cat,
        gendercode="M",
        seasonmonth="",
        eventselection="actual",
    )

    db = request.app.database
    try:
        season_vals = [clean_season, int(clean_season)]
    except ValueError:
        season_vals = [clean_season]

    existing_sessions = await db["sessions"].find(
        {"season": {"$in": season_vals}, "category_code": clean_cat},
        {
            "_id": 1,
            "title": 1,
            "event_place": 1,
            "date": 1,
            "time": 1,
            "session_type": 1,
            "hillSize": 1,
            "hillType": 1,
            "is_cancelled": 1,
            "results_data.is_cancelled": 1,
        },
    ).to_list(length=1000)

    sessions_by_place: dict = {}
    for s in existing_sessions:
        p_norm = str(s.get("event_place") or "").strip().lower()
        if p_norm:
            sess_cancelled = bool(
                s.get("is_cancelled") is True
                or (s.get("results_data") or {}).get("is_cancelled") is True
            )
            sessions_by_place.setdefault(p_norm, []).append({
                "id": str(s["_id"]),
                "title": s.get("title") or "",
                "date": str(s.get("date") or "")[:10],
                "time": s.get("time") or "",
                "session_type": s.get("session_type") or "",
                "hillSize": s.get("hillSize"),
                "hillType": s.get("hillType") or "",
                "is_cancelled": sess_cancelled,
            })

    today = datetime.date.today()
    tcs_from, tcs_to = _tournament_default_bounds(clean_season, "TCS")
    raw_from, raw_to = _tournament_default_bounds(clean_season, "RAW_AIR")
    pla_from, pla_to = _tournament_default_bounds(clean_season, "PLANICA_7")

    events_out = []
    for ev in raw_events or []:
        place = str(ev.get("place") or "").strip()
        place_low = place.lower()
        country = str(ev.get("country") or "").strip().upper()
        date_label = str(ev.get("date") or "").strip()

        # Pomijamy zbiorczy nagłówek typu "FIS Four Hills Tournament" — zostawiamy 4 indywidualne skocznie turnieju
        if "four hills tournament" in place_low or country == "FIS":
            continue

        lbl_up = date_label.upper()
        def_yr = year_int - 1 if any(m in lbl_up for m in ("OCT", "NOV", "DEC")) else year_int
        start_dt = parse_date_range_start(date_label, def_yr)
        end_dt = parse_date_range_end(date_label, def_yr)
        if end_dt < start_dt:
            end_dt = start_dt
        end_iso = end_dt.isoformat()

        tournament_tag = None
        if clean_cat == "WC":
            if place_low in TCS_VENUES and tcs_from <= end_iso <= tcs_to:
                tournament_tag = "TCS"
            elif place_low in RAW_AIR_VENUES and raw_from <= end_iso <= raw_to:
                tournament_tag = "RAW_AIR"
            elif place_low in PLANICA_7_VENUES and pla_from <= end_iso <= pla_to:
                tournament_tag = "PLANICA_7"

        matched_sessions = sessions_by_place.get(place_low, [])
        window_start = start_dt - datetime.timedelta(days=2)
        window_end = end_dt + datetime.timedelta(days=2)
        near_sessions = []
        for ms in matched_sessions:
            d_str = ms.get("date")
            if not d_str:
                continue
            try:
                s_dt = datetime.date.fromisoformat(d_str)
            except ValueError:
                continue
            if window_start <= s_dt <= window_end:
                near_sessions.append(ms)

        near_sessions.sort(key=lambda x: (x.get("date") or "", x.get("time") or "", x.get("session_type") or ""))

        active_sessions = [ms for ms in near_sessions if not ms.get("is_cancelled")]
        has_comp_in_db = any(ms.get("session_type") == "COMPETITION" for ms in active_sessions)
        all_db_cancelled = len(near_sessions) > 0 and len(active_sessions) == 0
        fis_cancelled = bool(ev.get("is_cancelled")) and not bool(ev.get("has_results_on_fis"))

        if has_comp_in_db:
            status_str = "COMPLETED"
        elif len(active_sessions) > 0:
            status_str = "IN_PROGRESS"
        elif fis_cancelled or all_db_cancelled:
            status_str = "CANCELLED"
        elif end_dt < today:
            status_str = "AWAITING_RESULTS"
        else:
            status_str = "UPCOMING"

        events_out.append({
            "event_id": ev.get("id"),
            "date_label": date_label,
            "end_date": end_iso,
            "place": place,
            "country": ev.get("country") or "",
            "category_code": clean_cat,
            "season": clean_season,
            "url": ev.get("url") or "",
            "tournament_tag": tournament_tag,
            "status": status_str,
            "sessions_count": len(near_sessions),
            "sessions": near_sessions,
        })

    result = {
        "season": clean_season,
        "category": clean_cat,
        "events": events_out,
    }
    set_cached_value("medium", cache_key, result)
    return result
