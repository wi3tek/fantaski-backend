from fastapi import APIRouter, HTTPException
import uuid

from google import genai

import config
from app.model.pdf_models import ScraperRequest

# Opcjonalnie: importujemy nasz model danych z serwisu, aby użyć go w response_model
from app.services.ai_pdf_service import przetworz_wyniki_fis

router = APIRouter(
    prefix="/pdf",
    tags=["PdfScraper"],
)


@router.post("/scrape-ski-jumping")
async def scrape_results(request: ScraperRequest):
    # Usunęliśmy BackgroundTasks. Teraz Endpoint czeka na przetworzenie pliku.
    print(f"Otrzymano żądanie scrape'owania linku: {request.pdf_link}")

    # Wywołujemy naszą zoptymalizowaną funkcję synchronicznie
    zawody = przetworz_wyniki_fis(str(request.pdf_link))

    # Zabezpieczenie: jeśli coś poszło nie tak (zwrócono None)
    if not zawody:
        raise HTTPException(
            status_code=500,
            detail="Nie udało się przetworzyć pliku PDF lub API zwróciło błąd."
        )

    # Zwracamy bezpośrednio obiekt Pydantic. FastAPI automatycznie
    # zamieni go na poprawny, zagnieżdżony JSON!
    return zawody


client = genai.Client(api_key=config.GEMINI_API_KEY)


def list_ai_models():
    models_list = []
    for model in client.models.list():
        models_list.append(model.name)
    return models_list



@router.get("/ai-models")
async def available_models():
    return list_ai_models()
