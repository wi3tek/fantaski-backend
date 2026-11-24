# app/controller/stats_controller.py
import re
from fastapi import APIRouter, Depends, Request, HTTPException, status
from pydantic import BaseModel
from typing import Optional

from app.auth import require_role, get_current_user
from app.services.auth_service import Role
from app.infrastructure.limiter import limiter
from app.model.SearchRequest import SearchRequest
from app.model.StandingsRequest import StandingsRequest
from app.services.stats.search_results import search_athlete_results
from app.services.stats.generate_standings import generate_standings_from_db
from app.services.dictionaries.dictionary_service import DictionaryService, get_dictionary_service
from app.infrastructure.cache import get_cached_value, set_cached_value

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
    limit: int = 20
    skip: int = 0


# --- ENDPOINTY STATYSTYK ZAWODNIKÓW ---
@router.post("/search")
@limiter.limit("60/minute")
async def search(
    request: Request,
    search_data: SearchRequest,
    user: dict = Depends(require_role(Role.USER_LITE, Role.USER_PRO, Role.ADMIN))
):
    """Zaawansowane statystyki i porównywarka. Konto LITE pozwala na porównanie maksymalnie 2 zawodników."""
    user_role = user.get("role", Role.USER)
    if user_role == Role.USER_LITE:
        names_list = search_data.names or []
        if len(names_list) > 2:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Konto FantaSKI LITE pozwala na jednoczesne porównanie maksymalnie 2 zawodników. Przejdź na pakiet PRO, aby analizować nielimitowaną liczbę skoczków."
            )
    db = request.app.database
    return await search_athlete_results(db, **search_data.model_dump())


@router.post("/standings")
@limiter.limit("1000/minute")
async def get_standings(
    request: Request,
    request_data: StandingsRequest,
    dictionaries: DictionaryService = Depends(get_dictionary_service),
    user: dict = Depends(get_current_user)
):
    """Klasyfikacja generalna i punktacja pucharowa (dostępna dla każdego zalogowanego użytkownika, cache 5 min)."""
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
    """Pobiera pełne szczegóły pojedynczej sesji (wraz ze skokami zawodników)."""
    db = request.app.database
    session = await db["sessions"].find_one({"_id": session_id})

    if not session:
        raise HTTPException(status_code=404, detail="Nie znaleziono sesji")

    session["_id"] = str(session["_id"])
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