# app/controller/admin_controller.py
import json
import hashlib
from typing import List

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from pymongo import UpdateOne
from datetime import datetime, date

from app.infrastructure.limiter import limiter
# from app.auth import verify_api_key # Odkomentuj na produkcji

from app.services.scrapers.generate_results import run_scraper_pipeline
from app.services.scrapers.fis_athletes_scraper import fetch_all_athletes_by_chunks

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    # dependencies=[Depends(verify_api_key)]
)


# --- MODELE PYDANTIC ---

class RefreshRequest(BaseModel):
    seasons: List[str]
    categories: List[str]
    mode: str = "IND"


# --- FUNKCJE POMOCNICZE ---

def json_serial(obj):
    """Zabezpieczenie parsowania dat dla json.dumps"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def generate_full_checksum(data: dict) -> str:
    """Tworzy deterministyczny hash ignorując klucze systemowe."""
    clean_data = {k: v for k, v in data.items() if
                  k not in ["_id", "checksum", "original_checksum", "last_checked", "review_reason"]}
    json_string = json.dumps(clean_data, sort_keys=True, ensure_ascii=False, default=json_serial)
    return hashlib.md5(json_string.encode('utf-8')).hexdigest()


# --- ENDPOINTY ---

@router.get("/config")
async def get_admin_config(request: Request):
    return {"categories": request.app.state.dicts.get("categories", {})}


@router.post("/refresh-athletes")
@limiter.limit("100/minute")
async def refresh_athletes(request: Request):
    db = request.app.database
    athletes_col = db["athletes"]
    review_col = db["athletes_review"]

    all_scraped_athletes_dict = {}

    for year in range(1950, 2021):
        scraped_chunk = fetch_all_athletes_by_chunks(str(year), limit=1000)

        if not scraped_chunk:
            continue

        for athlete in scraped_chunk:
            fis_code_raw = athlete.get("fis_code")
            if not fis_code_raw:
                continue

            fis_code = int(fis_code_raw)
            athlete["fis_code"] = fis_code

            current_status = athlete.get("status")
            athlete["status"] = "Active" if current_status == "Active" else "Inactive"

            all_scraped_athletes_dict[fis_code] = athlete

    if not all_scraped_athletes_dict:
        raise HTTPException(
            status_code=500,
            detail="Scraper FIS zwrócił 0 wyników dla wszystkich lat. Przerywam operację, by nie usunąć zawodników."
        )

    current_athletes_cursor = athletes_col.find({})
    current_athletes = {}
    async for a in current_athletes_cursor:
        if "fis_code" in a:
            current_athletes[int(a["fis_code"])] = a

    seen_fis_codes = set()
    stats = {"new_to_review": 0, "modified_to_review": 0, "deleted_to_review": 0, "unchanged": 0}

    for fis_code, athlete in all_scraped_athletes_dict.items():
        seen_fis_codes.add(fis_code)

        current_hash = generate_full_checksum(athlete)
        athlete["checksum"] = current_hash
        athlete["last_checked"] = datetime.utcnow()

        existing = current_athletes.get(fis_code)

        if not existing:
            athlete["review_reason"] = "NEW"
            await review_col.update_one({"fis_code": fis_code}, {"$set": athlete}, upsert=True)
            stats["new_to_review"] += 1
        else:
            existing_hash = generate_full_checksum(existing)
            if current_hash == existing_hash:
                await athletes_col.update_one({"fis_code": fis_code}, {"$set": {"last_checked": datetime.utcnow()}})
                stats["unchanged"] += 1
            else:
                athlete["review_reason"] = "MODIFIED"
                athlete["original_checksum"] = existing_hash
                await review_col.update_one({"fis_code": fis_code}, {"$set": athlete}, upsert=True)
                stats["modified_to_review"] += 1

    for fis_code, existing_athlete in current_athletes.items():
        if fis_code not in seen_fis_codes:
            existing_athlete["review_reason"] = "DELETED"
            await review_col.update_one({"fis_code": fis_code}, {"$set": existing_athlete}, upsert=True)
            stats["deleted_to_review"] += 1

    return {"message": "Analiza skoczków zakończona", "stats": stats}


@router.post("/refresh-results")
@limiter.limit("100/minute")
async def refresh_results(request: Request, payload: RefreshRequest):
    db = request.app.database
    sessions_col = db["sessions"]
    review_col = db["sessions_review"]
    hills_col = db["hills"]

    stats = {"inserted": 0, "sent_to_review": 0, "unchanged": 0, "combinations_processed": 0}
    hill_ops_dict = {}

    for season in payload.seasons:
        for category in payload.categories:
            stats["combinations_processed"] += 1
            scraped_events = run_scraper_pipeline(
                seasoncode=season,
                categorycode=category,
                gendercode="M",
                limit_events=None,
                mode=payload.mode
            )

            if not scraped_events:
                continue

            for event in scraped_events:
                place = event.get("place", "UNKNOWN")
                country = event.get("country", "UNKNOWN")

                for session in event.get("sessions", []):
                    hill_size = session.get('hillSize', 0)

                    # POPRAWKA: Prawidłowy fallback! Jeśli .get() zwróci None, operator 'or' wymusi 'HS'
                    hill_unit = session.get('hill_size_unit') or 'HS'

                    # Budujemy unikalny hill_id uwzględniając jednostkę skoczni
                    if hill_size:
                        hill_id = f"{place.upper()}_{hill_unit}{hill_size}"
                        hill_ops_dict[hill_id] = UpdateOne(
                            {"_id": hill_id},
                            {"$set": {
                                "place": place,
                                "country_code": country,
                                "size": hill_size,
                                "unit": hill_unit,
                                "type": session.get("hillType")
                            }},
                            upsert=True
                        )

                    codex = session.get("codex")
                    race_id = session.get("raceId")

                    if not codex or not race_id:
                        continue

                    session_composite_id = f"{race_id}_{codex}"

                    session["event_place"] = place
                    session["event_country"] = country
                    session["season"] = season
                    session["category_code"] = category

                    # Generowanie sumy kontrolnej
                    current_hash = generate_full_checksum(session)
                    session["checksum"] = current_hash
                    session["last_checked"] = datetime.utcnow()

                    existing_session = await sessions_col.find_one({"_id": session_composite_id})

                    if not existing_session:
                        session["_id"] = session_composite_id
                        await sessions_col.insert_one(session)
                        stats["inserted"] += 1
                    else:
                        existing_hash = generate_full_checksum(existing_session)
                        if existing_hash == current_hash:
                            await sessions_col.update_one({"_id": session_composite_id},
                                                          {"$set": {"last_checked": datetime.utcnow()}})
                            stats["unchanged"] += 1
                        else:
                            session["_id"] = session_composite_id
                            session["original_checksum"] = existing_hash
                            await review_col.update_one({"_id": session_composite_id}, {"$set": session}, upsert=True)
                            stats["sent_to_review"] += 1

    if hill_ops_dict:
        await hills_col.bulk_write(list(hill_ops_dict.values()))

    return {"message": "Pobieranie wariantów zakończone", "stats": stats}
# ==========================================
# MODUŁ WERYFIKACJI (KWARANTANNA) - SESJE
# ==========================================

@router.get("/reviews/sessions")
async def get_pending_session_reviews(request: Request):
    db = request.app.database
    reviews = await db["sessions_review"].find().to_list(100)

    enriched_reviews = []
    for review in reviews:
        codex = review["_id"]
        original = await db["sessions"].find_one({"_id": codex})
        enriched_reviews.append({
            "codex": codex,
            "new_data": review,
            "old_data": original or {}
        })

    return enriched_reviews


@router.post("/reviews/sessions/{session_id}/approve")
async def approve_session_review(request: Request, session_id: str):
    db = request.app.database
    review_session = await db["sessions_review"].find_one({"_id": session_id})

    if not review_session:
        raise HTTPException(status_code=404, detail="Brak sesji w kwarantannie")

    review_session.pop("original_checksum", None)
    await db["sessions"].replace_one({"_id": session_id}, review_session, upsert=True)
    await db["sessions_review"].delete_one({"_id": session_id})

    return {"message": f"Zmiany dla sesji {session_id} zostały zatwierdzone i zapisane w bazie."}


@router.post("/reviews/sessions/{session_id}/reject")
async def reject_session_review(request: Request, session_id: str):
    result = await request.app.database["sessions_review"].delete_one({"_id": session_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Brak sesji w kwarantannie")
    return {"message": f"Zmiany dla sesji {session_id} zostały odrzucone."}


# ==========================================
# MODUŁ WERYFIKACJI (KWARANTANNA) - SKOCZKOWIE
# ==========================================

@router.get("/reviews/athletes")
async def get_pending_athlete_reviews(request: Request):
    db = request.app.database
    reviews = await db["athletes_review"].find().to_list(1500)

    enriched_reviews = []
    for review in reviews:
        if "_id" in review:
            review["_id"] = str(review["_id"])

        fis_code = review.get("fis_code")

        original = await db["athletes"].find_one({"fis_code": fis_code})
        if original and "_id" in original:
            original["_id"] = str(original["_id"])

        enriched_reviews.append({
            "fis_code": fis_code,
            "new_data": review,
            "old_data": original or {}
        })

    return enriched_reviews


@router.post("/reviews/athletes/{fis_code}/approve")
async def approve_athlete_review(request: Request, fis_code: int):
    db = request.app.database
    athlete_in_review = await db["athletes_review"].find_one({"fis_code": fis_code})

    if not athlete_in_review:
        raise HTTPException(status_code=404, detail="Brak skoczka w kwarantannie")

    athlete_in_review.pop("_id", None)
    reason = athlete_in_review.pop("review_reason", None)
    athlete_in_review.pop("original_checksum", None)

    if reason == "DELETED":
        await db["athletes"].delete_one({"fis_code": fis_code})
        msg = f"Skoczek {fis_code} został usunięty z głównej bazy."
    else:
        await db["athletes"].update_one(
            {"fis_code": fis_code},
            {"$set": athlete_in_review},
            upsert=True
        )
        msg = f"Dane skoczka {fis_code} zostały zatwierdzone."

    await db["athletes_review"].delete_one({"fis_code": fis_code})
    return {"message": msg}


@router.post("/reviews/athletes/{fis_code}/reject")
async def reject_athlete_review(request: Request, fis_code: int):
    result = await request.app.database["athletes_review"].delete_one({"fis_code": fis_code})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Brak skoczka w kwarantannie")
    return {"message": f"Zmiany dla skoczka {fis_code} zostały zignorowane."}


@router.post("/reviews/athletes/approve-all")
async def approve_all_athlete_reviews(request: Request):
    db = request.app.database
    reviews = await db["athletes_review"].find().to_list(None)

    count = 0
    for athlete_in_review in reviews:
        fis_code = athlete_in_review.get("fis_code")

        athlete_in_review.pop("_id", None)
        reason = athlete_in_review.pop("review_reason", None)
        athlete_in_review.pop("original_checksum", None)

        if reason == "DELETED":
            await db["athletes"].delete_one({"fis_code": fis_code})
        else:
            await db["athletes"].update_one(
                {"fis_code": fis_code},
                {"$set": athlete_in_review},
                upsert=True
            )

        await db["athletes_review"].delete_one({"fis_code": fis_code})
        count += 1

    return {"message": f"Pomyślnie zatwierdzono {count} zmian w bazie skoczków."}


@router.post("/reviews/sessions/approve-all")
async def approve_all_session_reviews(request: Request):
    db = request.app.database
    reviews = await db["sessions_review"].find().to_list(None)

    count = 0
    for review_session in reviews:
        codex = review_session["_id"]
        review_session.pop("original_checksum", None)
        await db["sessions"].replace_one({"_id": codex}, review_session, upsert=True)
        await db["sessions_review"].delete_one({"_id": codex})
        count += 1

    return {"message": f"Pomyślnie zatwierdzono {count} zmian w sesjach."}