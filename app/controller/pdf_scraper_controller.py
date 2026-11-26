import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request

from google import genai

from app import config
from app.auth import require_role
from app.infrastructure.limiter import limiter
from app.model.pdf_models import ScraperRequest
from app.services.auth_service import Role

# Opcjonalnie: importujemy nasz model danych z serwisu, aby użyć go w response_model
from app.services.ai_pdf_service import przetworz_wyniki_fis

router = APIRouter(
    prefix="/pdf",
    tags=["PdfScraper"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


def _get_genai_client():
    if not config.GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="Brak skonfigurowanego klucza GEMINI_API_KEY.")
    return genai.Client(api_key=config.GEMINI_API_KEY)


@router.post("/scrape-ski-jumping")
@limiter.limit("10/minute")
async def scrape_results(request: Request, payload: ScraperRequest):
    pdf_url = str(payload.pdf_link)
    if not pdf_url.startswith("https://www.fis-ski.com/") and not pdf_url.startswith("https://medias") and not pdf_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Dozwolone są wyłącznie bezpieczne adresy HTTPS.")

    zawody = await asyncio.to_thread(przetworz_wyniki_fis, pdf_url)

    if not zawody:
        raise HTTPException(
            status_code=500,
            detail="Nie udało się przetworzyć pliku PDF lub API zwróciło błąd."
        )

    return zawody


def list_ai_models():
    client = _get_genai_client()
    return [model.name for model in client.models.list()]


@router.get("/ai-models")
@limiter.limit("20/minute")
async def available_models(request: Request):
    return await asyncio.to_thread(list_ai_models)
