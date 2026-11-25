# app/controller/hills_controller.py
# Kontroler CRUD dla bazy skoczni narciarskich z obsługą 3-poziomowej hierarchii i wersjonowania

from typing import Optional
import re
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, Field

from app.infrastructure.limiter import limiter
from app.auth import require_role
from app.services.auth_service import Role
from app.infrastructure.cache import get_cached_value, set_cached_value, clear_cache
from app.services.hill_service import (
    get_hills_hierarchy,
    set_current_version,
    infer_hill_type,
)

router = APIRouter(
    prefix="/hills",
    tags=["Hills"],
)


# --- MODELE PYDANTIC ---

class HillUpdate(BaseModel):
    """Model aktualizacji wersji skoczni — wszystkie pola opcjonalne."""
    place: Optional[str] = None
    country_code: Optional[str] = None
    size: Optional[int] = None
    unit: Optional[str] = None
    type: Optional[str] = None
    k_point: Optional[float] = None
    year_built: Optional[int] = None
    record: Optional[float] = None
    record_holder: Optional[str] = None
    meter_value: Optional[float] = None
    is_current: Optional[bool] = None


class HillCreate(BaseModel):
    """Model dodawania nowej skoczni / nowej wersji skoczni."""
    place: str = Field(..., min_length=2, description="Nazwa miejscowości")
    country_code: str = Field(..., min_length=2, max_length=3, description="Kod kraju (np. POL)")
    size: int = Field(..., gt=0, description="Rozmiar skoczni (HS/K)")
    unit: str = Field(default="HS", description="Jednostka rozmiaru (HS lub K)")
    type: Optional[str] = Field(default=None, description="Rodzaj skoczni (FLYING, LARGE, NORMAL, SMALL)")
    k_point: Optional[float] = None
    year_built: Optional[int] = None
    record: Optional[float] = None
    record_holder: Optional[str] = None
    meter_value: Optional[float] = None
    is_current: bool = Field(default=True, description="Czy ta wersja ma być oznaczona jako aktualna")


# --- ENDPOINTY ---

@router.get("/hierarchy")
@limiter.limit("120/minute")
async def get_hierarchy(request: Request, query: Optional[str] = None):
    """
    Zwraca 3-poziomową hierarchię skoczni:
    Miejscowość -> Rodzaj skoczni -> Wersje skoczni (z flagą is_current).
    """
    cache_key = f"hills_hierarchy_{query.strip().lower() if query else 'all'}"
    cached = get_cached_value("long", cache_key)
    if cached is not None:
        return cached

    db = request.app.database
    hierarchy = await get_hills_hierarchy(db, query=query)
    set_cached_value("long", cache_key, hierarchy)
    return hierarchy


@router.get("/list")
@limiter.limit("120/minute")
async def get_all_hills(request: Request):
    """Pobiera płaską listę wszystkich wersji skoczni z bazy danych."""
    cached = get_cached_value("long", "hills_list")
    if cached is not None:
        return cached

    db = request.app.database
    hills_col = db["hills"]

    cursor = hills_col.find({}).sort([("place", 1), ("type", 1), ("size", -1)])
    hills = await cursor.to_list(length=5000)

    for hill in hills:
        hill["hill_id"] = str(hill.pop("_id"))
        if "is_current" not in hill:
            hill["is_current"] = True

    set_cached_value("long", "hills_list", hills)
    return hills


@router.get("/{hill_id}")
@limiter.limit("60/minute")
async def get_hill(request: Request, hill_id: str):
    """Pobiera szczegóły jednej wersji skoczni na podstawie hill_id."""
    db = request.app.database
    hill = await db["hills"].find_one({"_id": hill_id})

    if not hill:
        raise HTTPException(status_code=404, detail="Nie znaleziono skoczni")

    hill["hill_id"] = str(hill.pop("_id"))
    if "is_current" not in hill:
        hill["is_current"] = True
    return hill


@router.post("/version")
@limiter.limit("60/minute")
async def create_hill_version(
    request: Request,
    payload: HillCreate,
    admin: dict = Depends(require_role(Role.ADMIN))
):
    """Tworzy nową wersję skoczni w kompleksie (wymaga roli ADMIN)."""
    db = request.app.database
    hills_col = db["hills"]

    place_clean = payload.place.strip()
    country_clean = payload.country_code.strip().upper()
    unit_clean = payload.unit.strip().upper()
    size_clean = int(payload.size)
    type_clean = (payload.type or infer_hill_type(size_clean)).strip().upper()

    hill_id = f"{place_clean.upper()}_{unit_clean}{size_clean}"

    doc = {
        "place": place_clean,
        "country_code": country_clean,
        "size": size_clean,
        "unit": unit_clean,
        "type": type_clean,
        "is_current": payload.is_current,
        "total_sessions": 0,
    }
    if payload.k_point is not None: doc["k_point"] = float(payload.k_point)
    if payload.year_built is not None: doc["year_built"] = int(payload.year_built)
    if payload.record is not None: doc["record"] = float(payload.record)
    if payload.record_holder: doc["record_holder"] = payload.record_holder.strip()
    if payload.meter_value is not None: doc["meter_value"] = float(payload.meter_value)

    await hills_col.update_one({"_id": hill_id}, {"$set": doc}, upsert=True)

    if payload.is_current:
        # Odznaczamy inne wersje w tym samym kompleksie i typie
        await hills_col.update_many(
            {
                "place": {"$regex": f"^{re.escape(place_clean)}$", "$options": "i"},
                "type": type_clean,
                "_id": {"$ne": hill_id}
            },
            {"$set": {"is_current": False}}
        )

    clear_cache("long")
    return {"message": f"Zapisano wersję skoczni: {hill_id}", "hill_id": hill_id}


@router.post("/{hill_id}/set-current")
@limiter.limit("60/minute")
async def set_hill_as_current(
    request: Request,
    hill_id: str,
    admin: dict = Depends(require_role(Role.ADMIN))
):
    """Oznacza wskazaną wersję jako aktualną (current) dla danego obiektu (wymaga roli ADMIN)."""
    db = request.app.database
    success = await set_current_version(db, hill_id)
    if not success:
        raise HTTPException(status_code=404, detail="Nie znaleziono skoczni o podanym ID")

    return {"message": f"Wersja {hill_id} została pomyślnie oznaczona jako aktualna."}


@router.put("/{hill_id}")
@limiter.limit("60/minute")
async def update_hill(
    request: Request,
    hill_id: str,
    payload: HillUpdate,
    admin: dict = Depends(require_role(Role.ADMIN))
):
    """Aktualizuje wersję skoczni (wymaga roli ADMIN)."""
    db = request.app.database
    hills_col = db["hills"]

    existing = await hills_col.find_one({"_id": hill_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Nie znaleziono skoczni")

    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="Brak danych do aktualizacji")

    result = await hills_col.update_one(
        {"_id": hill_id},
        {"$set": update_data}
    )

    # Jeśli ustawiono is_current = True, wyłączamy inne wersje w kompleksie
    if update_data.get("is_current") is True:
        target_place = update_data.get("place") or existing.get("place")
        target_type = update_data.get("type") or existing.get("type")
        if target_place and target_type:
            await hills_col.update_many(
                {
                    "place": {"$regex": f"^{re.escape(target_place.strip())}$", "$options": "i"},
                    "type": target_type,
                    "_id": {"$ne": hill_id}
                },
                {"$set": {"is_current": False}}
            )

    clear_cache("long")
    return {"message": f"Zaktualizowano skocznię: {hill_id}", "modified": result.modified_count}


@router.delete("/{hill_id}")
@limiter.limit("60/minute")
async def delete_hill(
    request: Request,
    hill_id: str,
    admin: dict = Depends(require_role(Role.ADMIN))
):
    """Usuwa wersję skoczni z bazy danych (wymaga roli ADMIN)."""
    db = request.app.database
    hills_col = db["hills"]

    hill_to_delete = await hills_col.find_one({"_id": hill_id})
    if not hill_to_delete:
        raise HTTPException(status_code=404, detail="Nie znaleziono skoczni do usunięcia")

    was_current = hill_to_delete.get("is_current", False)
    place = hill_to_delete.get("place")
    htype = hill_to_delete.get("type")

    result = await hills_col.delete_one({"_id": hill_id})

    # Jeśli usunięto wersję oznaczoną jako current, awansujemy najnowszą pozostałą wersję
    if was_current and place and htype:
        remaining = await hills_col.find({
            "place": {"$regex": f"^{re.escape(place.strip())}$", "$options": "i"},
            "type": htype
        }).sort([("last_session_date", -1), ("size", -1)]).to_list(1)

        if remaining:
            await hills_col.update_one(
                {"_id": remaining[0]["_id"]},
                {"$set": {"is_current": True}}
            )

    clear_cache("long")
    return {"message": f"Usunięto wersję skoczni: {hill_id}"}
