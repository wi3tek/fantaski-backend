from fastapi import APIRouter, Depends, HTTPException, Request
from starlette import status
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import re

from app.repository.athlete_repository import AthleteRepository, get_athlete_repository
from app.services.dictionaries.dictionary_service import DictionaryService, get_dictionary_service

router = APIRouter(
    prefix="/athletes",
    tags=["Athletes"],
)


# --- FUNKCJA POMOCNICZA ---
def subtract_years(dt: datetime, years: int) -> datetime:
    """Odejmuje lata od daty (zabezpieczenie dla 29 lutego)."""
    try:
        return dt.replace(year=dt.year - years)
    except ValueError:
        return dt.replace(year=dt.year - years, day=28)


# --- MODELE ---
class AthleteSearchFilter(BaseModel):
    search_name: Optional[str] = None
    countries: Optional[List[str]] = []
    status: Optional[str] = None
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    sort_by: Optional[str] = "full_name"  # Domyślne pole sortowania
    sort_order: int = 1  # 1 = rosnąco (A-Z), -1 = malejąco (Z-A)
    limit: int = 20
    skip: int = 0


# --- NOWE ENDPOINTY (PAGINACJA I SŁOWNIKI) ---
@router.post("/list")
async def get_athletes_list(
        request: Request,
        filters: AthleteSearchFilter,
        dictionaries: DictionaryService = Depends(get_dictionary_service)
):
    """Pobiera przefiltrowaną, posortowaną i paginowaną listę skoczków bezpośrednio z MongoDB."""
    db = request.app.database
    query = {}

    if filters.search_name:
        query["full_name"] = {"$regex": re.escape(filters.search_name), "$options": "i"}

    if filters.countries:
        query["country_code"] = {"$in": filters.countries}

    if filters.status:
        if filters.status == "ACTIVE":
            query["status"] = "Active"
        elif filters.status == "INACTIVE":
            query["status"] = {"$ne": "Active"}

    if filters.age_min is not None or filters.age_max is not None:
        now = datetime.utcnow()
        birthdate_query = {}

        if filters.age_min is not None:
            max_date = subtract_years(now, filters.age_min)
            birthdate_query["$lte"] = max_date

        if filters.age_max is not None:
            min_date = subtract_years(now, filters.age_max + 1)
            birthdate_query["$gt"] = min_date

        if birthdate_query:
            query["birthdate"] = birthdate_query

    # Szukamy, sortujemy (zależnie od tego, co kliknie użytkownik) i paginujemy
    sort_field = filters.sort_by if filters.sort_by else "full_name"
    sort_dir = filters.sort_order if filters.sort_order in [1, -1] else 1

    cursor = db["athletes"].find(query).sort(sort_field, sort_dir).skip(filters.skip).limit(filters.limit)
    athletes = await cursor.to_list(length=filters.limit)

    for athlete in athletes:
        if "_id" in athlete:
            athlete["_id"] = str(athlete["_id"])
        athlete["flagUrl"] = dictionaries.get_flag_url(athlete.get("country_code"))

    return athletes


@router.get("/countries")
async def get_countries(request: Request):
    """Zwraca listę krajów ze słownika załadowanego w main.py."""
    countries_dict = request.app.state.dicts.get("countries", {})
    result = [{"code": k, "name": v.get("pl", k)} for k, v in countries_dict.items()]
    return sorted(result, key=lambda x: x["name"])


# --- STARE ENDPOINTY (Zachowane dla kompatybilności repozytorium) ---
@router.get("/find")
async def find_athlete(
        fis_code: int,
        repo: AthleteRepository = Depends(get_athlete_repository),
        dictionaries: DictionaryService = Depends(get_dictionary_service),
):
    athlete = await repo.find_by_fis_code(fis_code)

    if not athlete:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Nie znaleziono zawodnika o fis_code: {fis_code}"
        )

    athlete["flag"] = dictionaries.get_flag_url(athlete.get("country_code"))
    return athlete


@router.get("/all")
async def get_all_athletes(
        limit: int = 1000,
        repo: AthleteRepository = Depends(get_athlete_repository),
        dictionaries: DictionaryService = Depends(get_dictionary_service),
):
    athletes = await repo.find_all(limit=limit)  # type: ignore
    for athlete in athletes:
        athlete["flagUrl"] = dictionaries.get_flag_url(athlete.get("country_code"))
    return athletes


@router.get("/names")
async def get_names(repo: AthleteRepository = Depends(get_athlete_repository)):
    return await repo.get_all_names()