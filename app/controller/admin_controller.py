from fastapi import APIRouter, Request, Depends
from pymongo import UpdateOne

from app.infrastructure.limiter import limiter
from app.services.scrapers.fis_athletes_scraper import fetch_all_athletes_by_chunks
from app.auth import verify_api_key

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(verify_api_key)] # Wymaga autoryzacji dla WSZYSTKICH endpointów w tym pliku
)


@router.post("/refresh-athletes")
@limiter.limit("1/minute")
async def refresh_athletes(request: Request):
    athletes = fetch_all_athletes_by_chunks(limit=1000)

    # 2. Dobieramy się do kolekcji w MongoDB (stworzy się automatycznie, jeśli nie istnieje)
    collection = request.app.database["athletes"]

    operations = []

    for athlete in athletes:

        fis_code = athlete.get("fis_code")

        if not fis_code:
            continue

        operations.append(
            UpdateOne(
                {"fis_code": fis_code},  # Warunek: szukaj skoczka o takim ID
                {"$set": athlete},  # Jeśli znajdziesz/nie znajdziesz: ustaw mu te dane
                upsert=True  # Jeśli nie istnieje: stwórz go (Insert)
            )
        )

    if operations:
        # 4. Wykonujemy masowy zapis asynchronicznie za pomocą 'await'
        result = await collection.bulk_write(operations)

        return {
            "message": "Odświeżanie skoczków zakończone sukcesem",
            "inserted_count": result.upserted_count,
            "modified_count": result.modified_count
        }

    return {"message": "Brak danych do zapisania", "inserted_count": 0, "modified_count": 0}
