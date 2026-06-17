from fastapi import APIRouter, Request, Depends, HTTPException
from pymongo import UpdateOne
from starlette import status

from app.infrastructure.limiter import limiter
from app.services.scrapers.fis_athletes_scraper import fetch_all_athletes_by_chunks
from app.repository.athlete_repository import AthleteRepository, get_athlete_repository
from app.services.dictionaries.dictionary_service import DictionaryService, get_dictionary_service

router = APIRouter(
    prefix="/athletes",
    tags=["Athletes"],
)


@router.get("/find")
async def find_athlete(
        fis_code: int,
        repo: AthleteRepository = Depends(get_athlete_repository)  # <-- Wstrzykiwanie zależności!
):
    # 2. Dobieramy się do kolekcji w MongoDB (stworzy się automatycznie, jeśli nie istnieje)
    athlete = await  repo.find_by_fis_code(fis_code)

    if not athlete:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Nie znaleziono zawodnika o fis_code: {fis_code}"
        )

    return athlete


@router.get("/all")
async def get_all_athletes(
        limit: int = 1000,  # Pozwalamy użytkownikowi kontrolować, ile wyników chce pobrać (domyślnie 1000)
        repo: AthleteRepository = Depends(get_athlete_repository),
        dictionaries: DictionaryService = Depends(get_dictionary_service),

):
    athletes = await repo.find_all(limit=limit) # type: ignore

    for athlete in athletes:
        athlete["flagUrl"] = dictionaries.get_flag_url(athlete["country_code"])

    # Nie musimy zwracać 404, jeśli baza jest pusta - po prostu zwracamy pustą listę []
    return athletes


@router.get("/names")
async def get_names(repo: AthleteRepository = Depends(get_athlete_repository)):
    return await repo.get_all_names()
