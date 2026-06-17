from fastapi import APIRouter, Depends, Request
from app.auth import verify_api_key
from app.infrastructure.limiter import limiter
from app.model.SearchRequest import SearchRequest
from app.services.stats.search_results import search_athlete_results

router = APIRouter(
    prefix="/stats",
    tags=["Stats"],
)

@router.post("/search")
@limiter.limit("10/minute")
def search(request: Request, search_data: SearchRequest):
    """
        Pamiętaj: obiekt 'request' musi tu być, bo SlowAPI go wymaga.
        Wewnątrz funkcji możesz też dzięki temu dobrać się do bazy MongoDB:
        db = request.app.database
        """
    # Zmieniłem nazwę z 'request' na 'search_data', żeby nie gryzła się z FastAPI Request
    return search_athlete_results(**search_data.model_dump())
