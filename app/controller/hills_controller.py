# app/controller/hills_controller.py
# Kontroler CRUD dla bazy skoczni narciarskich

from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from app.infrastructure.limiter import limiter

router = APIRouter(
    prefix="/hills",
    tags=["Hills"],
)


# --- MODEL PYDANTIC ---

class HillUpdate(BaseModel):
    """Model aktualizacji skoczni — wszystkie pola opcjonalne."""
    place: Optional[str] = None
    country_code: Optional[str] = None
    size: Optional[int] = None
    unit: Optional[str] = None
    type: Optional[str] = None
    k_point: Optional[float] = None
    year_built: Optional[int] = None
    record: Optional[float] = None
    record_holder: Optional[str] = None


# --- ENDPOINTY ---

@router.get("/list")
@limiter.limit("60/minute")
async def get_all_hills(request: Request):
    """Pobiera listę wszystkich skoczni z bazy danych."""
    db = request.app.database
    hills_col = db["hills"]

    cursor = hills_col.find({})
    hills = await cursor.to_list(length=5000)

    # Konwersja _id na hill_id w odpowiedzi
    for hill in hills:
        hill["hill_id"] = str(hill.pop("_id"))

    return hills


@router.get("/{hill_id}")
@limiter.limit("60/minute")
async def get_hill(request: Request, hill_id: str):
    """Pobiera szczegóły jednej skoczni na podstawie hill_id."""
    db = request.app.database
    hill = await db["hills"].find_one({"_id": hill_id})

    if not hill:
        raise HTTPException(status_code=404, detail="Nie znaleziono skoczni")

    hill["hill_id"] = str(hill.pop("_id"))
    return hill


@router.put("/{hill_id}")
@limiter.limit("60/minute")
async def update_hill(request: Request, hill_id: str, payload: HillUpdate):
    """Aktualizuje (lub tworzy) skocznię — tryb upsert."""
    db = request.app.database

    # Budujemy dokument tylko z niepustych pól
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}

    if not update_data:
        raise HTTPException(status_code=400, detail="Brak danych do aktualizacji")

    result = await db["hills"].update_one(
        {"_id": hill_id},
        {"$set": update_data},
        upsert=True,
    )

    if result.upserted_id:
        return {"message": f"Utworzono nową skocznię: {hill_id}"}

    return {"message": f"Zaktualizowano skocznię: {hill_id}", "modified": result.modified_count}


@router.delete("/{hill_id}")
@limiter.limit("60/minute")
async def delete_hill(request: Request, hill_id: str):
    """Usuwa skocznię z bazy danych."""
    db = request.app.database
    result = await db["hills"].delete_one({"_id": hill_id})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Nie znaleziono skoczni do usunięcia")

    return {"message": f"Usunięto skocznię: {hill_id}"}
