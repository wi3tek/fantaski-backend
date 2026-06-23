# app/controller/stats_controller.py
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.auth import verify_api_key
from app.infrastructure.limiter import limiter
from app.model.SearchRequest import SearchRequest
from app.model.StandingsRequest import StandingsRequest
from app.services.stats.search_results import search_athlete_results
from app.services.stats.generate_standings import generate_standings_from_db
from services.dictionaries.dictionary_service import DictionaryService, get_dictionary_service

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
@limiter.limit("10/minute")
async def search(request: Request, search_data: SearchRequest):
    db = request.app.database
    return await search_athlete_results(db, **search_data.model_dump())


@router.post("/standings")
@limiter.limit("1000/minute")
async def get_standings(request: Request, request_data: StandingsRequest,
                        dictionaries: DictionaryService = Depends(get_dictionary_service)):
    db = request.app.database
    return await generate_standings_from_db(db, request_data.season, request_data.category, dictionaries)


# --- NOWE ENDPOINTY BAZY SESJI ---
@router.post("/sessions")
@limiter.limit("30/minute")
async def get_sessions_list(request: Request, filters: SessionSearchFilter):
    """Pobiera przefiltrowaną listę sesji z MongoDB."""
    db = request.app.database
    query = {}

    if filters.date_from or filters.date_to:
        query["date"] = {}
        if filters.date_from: query["date"]["$gte"] = filters.date_from
        if filters.date_to: query["date"]["$lte"] = filters.date_to

    if filters.session_type:
        query["session_type"] = {"$regex": filters.session_type, "$options": "i"}

    if filters.category:
        query["category_code"] = filters.category

    if filters.season:
        query["season"] = filters.season

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
    dicts = request.app.state.dicts

    # Z całego słownika hills wyciągamy tylko unikalne nazwy miejscowości
    hills_list = dicts.get("hills", [])
    places = sorted(list(set([h["name"] for h in hills_list if h.get("name")])))

    return {
        "places": places,
        "hill_types": dicts.get("hill_type", {}),
        "categories": dicts.get("categories", {}), # DODANO SŁOWNIK KATEGORII
        "sessions": dicts.get("sessions", {})      # DODANO SŁOWNIK SESJI
    }


@router.get("/athletes/with-prices")
@limiter.limit("60/minute")
async def get_athletes_with_prices(request: Request):
    """Pobiera listę skoczków z określoną ceną do porównywarki ESM."""
    db = request.app.database

    # Filtrujemy tylko aktywnych zawodników, którzy mają ustaloną cenę > 0
    cursor = db["athletes"].find(
        {"price": {"$exists": True, "$gt": 0}, "isDisabled": False},
        {"full_name": 1, "fis_code": 1, "price": 1, "_id": 0}
    ).sort("price", -1)  # -1 oznacza sortowanie malejące (najdrożsi na górze)

    athletes = await cursor.to_list(length=1000)

    # Przemapowujemy klucz na 'name', aby Front-End łatwiej to skonsumował
    return [
        {
            "name": a.get("full_name"),
            "fis_code": a.get("fis_code"),
            "price": a.get("price")
        } for a in athletes
    ]