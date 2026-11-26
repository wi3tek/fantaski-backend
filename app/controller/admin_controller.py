# app/controller/admin_controller.py
import io
import asyncio
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from pymongo import UpdateOne, ReplaceOne, DeleteOne

import httpx
import pdfplumber

from app import config
from app.infrastructure.limiter import limiter
from app.auth import require_role
from app.services.auth_service import Role

from app.services.scrapers.generate_results import run_scraper_pipeline
from app.services.scrapers.results_job_manager import (
    generate_full_checksum,
    get_results_job_manager,
    save_scraped_events_to_db,
)
from app.services.scrapers.fis_athletes_scraper import fetch_all_athletes_by_chunks
from app.services.athletes_esm_prices import update_esm_prices
from app.services.scrapers.pdf_training_scraper import (
    get_pdf_training_job_manager,
    process_all_trainings_pdf,
    process_single_pdf_debug,
)
from app.services.weather.weather_enricher import get_weather_enricher
from app.services.scrapers.nightly_batch_scraper import get_nightly_batch_scraper

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(require_role(Role.ADMIN))]
)


# --- MODELE PYDANTIC ---

class RefreshRequest(BaseModel):
    seasons: List[str]
    categories: List[str]
    mode: str = "IND"
    only_missing: bool = True


class RetryMissingRequest(BaseModel):
    season: Optional[str] = None
    only_429: bool = False


# --- ENDPOINTY ---

CATEGORY_LABELS = {
    "WC": "Puchar Świata",
    "GP": "Letnie Grand Prix",
    "COC": "Puchar Kontynentalny",
    "FC": "FIS Cup",
    "OWG": "Igrzyska Olimpijskie",
    "WSC": "Mistrzostwa Świata",
    "SFWC": "MŚ w Lotach",
    "WJC": "MŚ Juniorów",
    "FESA": "Puchar OPA / FESA"
}

@router.get("/config")
async def get_admin_config(request: Request):
    return {"categories": request.app.state.dicts.get("categories", {})}


@router.get("/seasons-summary")
async def get_seasons_summary(request: Request):
    """
    Zwraca szczegółowe zestawienie wszystkich wgranych sezonów i kategorii w bazie danych:
    - Liczbę pobranych sesji i sesji z wynikami
    - Procent i status kompletności (COMPLETE, PARTIAL, EMPTY)
    - Kategorie i miejscowości w każdym sezonie
    - Datę ostatniej aktualizacji / scrapowania
    """
    db = request.app.database
    pipeline = [
        {"$match": {"season": {"$ne": None}}},
        {
            "$group": {
                "_id": {
                    "season": "$season",
                    "category": "$category_code"
                },
                "total_sessions": {"$sum": 1},
                "sessions_with_results": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$isArray": "$results_data.results"},
                                    {"$gt": [{"$size": "$results_data.results"}, 0]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "cancelled_sessions": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$or": [
                                            {"$eq": ["$is_cancelled", True]},
                                            {"$eq": ["$results_data.is_cancelled", True]}
                                        ]
                                    },
                                    {
                                        "$or": [
                                            {"$not": ["$results_data.results"]},
                                            {"$eq": [{"$size": {"$ifNull": ["$results_data.results", []]}}, 0]}
                                        ]
                                    }
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "failed_429_sessions": {
                    "$sum": {
                        "$cond": [{"$eq": ["$scrape_status", "FAILED_429"]}, 1, 0]
                    }
                },
                "places": {"$addToSet": "$event_place"},
                "min_date": {"$min": "$date"},
                "max_date": {"$max": "$date"},
                "last_checked": {"$max": "$last_checked"}
            }
        },
        {"$sort": {"_id.season": -1, "_id.category": 1}}
    ]

    records = await db["sessions"].aggregate(pipeline).to_list(None)

    seasons_map = {}
    for r in records:
        season = r["_id"]["season"]
        cat_code = r["_id"]["category"] or "INNE"
        total = r["total_sessions"]
        with_res = r["sessions_with_results"]
        cancelled = r.get("cancelled_sessions", 0)
        failed_429 = r.get("failed_429_sessions", 0)
        missing = max(0, total - with_res - cancelled)
        places = [p for p in r.get("places", []) if p]

        # Kompletność uwzględnia sesje rozegrane oraz odwołane (bo one też są kompletnie obsłużone)
        cat_pct = round(((with_res + cancelled) / total) * 100) if total > 0 else 0

        cat_info = {
            "category_code": cat_code,
            "category_name": CATEGORY_LABELS.get(cat_code, cat_code),
            "total_sessions": total,
            "sessions_with_results": with_res,
            "cancelled_sessions": cancelled,
            "missing_sessions": missing,
            "failed_429_sessions": failed_429,
            "completeness_pct": cat_pct,
            "places_count": len(places),
            "places": sorted(places),
            "min_date": r.get("min_date"),
            "max_date": r.get("max_date"),
            "last_checked": r.get("last_checked").isoformat() if r.get("last_checked") else None
        }

        if season not in seasons_map:
            seasons_map[season] = {
                "season": season,
                "total_sessions": 0,
                "sessions_with_results": 0,
                "cancelled_sessions": 0,
                "missing_sessions": 0,
                "failed_429_sessions": 0,
                "categories": [],
                "places_set": set(),
                "last_checked": None
            }

        s_obj = seasons_map[season]
        s_obj["total_sessions"] += total
        s_obj["sessions_with_results"] += with_res
        s_obj["cancelled_sessions"] += cancelled
        s_obj["missing_sessions"] += missing
        s_obj["failed_429_sessions"] += failed_429
        s_obj["categories"].append(cat_info)
        s_obj["places_set"].update(places)

        lc = r.get("last_checked")
        if lc and (not s_obj["last_checked"] or lc > s_obj["last_checked"]):
            s_obj["last_checked"] = lc

    result = []
    for s_code, s_data in sorted(seasons_map.items(), key=lambda x: str(x[0]), reverse=True):
        total = s_data["total_sessions"]
        with_res = s_data["sessions_with_results"]
        cancelled = s_data["cancelled_sessions"]
        missing = s_data["missing_sessions"]
        failed_429 = s_data["failed_429_sessions"]
        pct = round(((with_res + cancelled) / total) * 100) if total > 0 else 0

        if total == 0 or (with_res == 0 and cancelled == 0):
            status_str = "EMPTY"
        elif missing == 0:
            status_str = "COMPLETE"
        else:
            status_str = "PARTIAL"

        result.append({
            "season": s_code,
            "total_sessions": total,
            "sessions_with_results": with_res,
            "cancelled_sessions": cancelled,
            "missing_sessions": missing,
            "failed_429_sessions": failed_429,
            "completeness_pct": pct,
            "status": status_str,
            "unique_places_count": len(s_data["places_set"]),
            "categories": s_data["categories"],
            "last_checked": s_data["last_checked"].isoformat() if s_data["last_checked"] else None
        })

    return {
        "total_seasons": len(result),
        "total_seasons_in_db": len(result),
        "seasons": result
    }


@router.get("/seasons-summary/{season}/missing")
async def get_season_missing_sessions(request: Request, season: str):
    """
    Zwraca szczegółową listę sesji w danym sezonie, które nie mają kompletnych wyników:
    - Odwołane przez jury (np. wiatr/pogoda)
    - Brakujące w bazie lub oznaczone błędem 429 do wznowienia
    """
    db = request.app.database
    cursor = db["sessions"].find({
        "season": season,
        "$or": [
            {"results_data.results": {"$in": [None, []]}},
            {"results_data": None},
            {"results_data.results": {"$exists": False}},
            {"is_cancelled": True},
            {"results_data.is_cancelled": True}
        ]
    }).sort([("date", 1), ("_id", 1)])

    missing_list = []
    cancelled_list = []
    failed_429_count = 0

    async for doc in cursor:
        results_arr = (doc.get("results_data") or {}).get("results", [])
        has_results = bool(results_arr and len(results_arr) > 0)
        is_cancelled = bool(
            (doc.get("is_cancelled") is True or (doc.get("results_data") or {}).get("is_cancelled") is True)
            and not has_results
        )

        if has_results:
            continue

        scrape_status = doc.get("scrape_status")
        is_429 = scrape_status == "FAILED_429"
        if not is_cancelled and is_429:
            failed_429_count += 1

        if is_cancelled:
            status_val = "CANCELLED"
            status_label = "Odwołane przez jury (wiatr / pogoda)"
        elif is_429:
            status_val = "FAILED_429"
            status_label = "Limit FIS (429) — do wznowienia"
        else:
            status_val = "MISSING"
            status_label = "Brak wyników w bazie (do pobrania)"

        item = {
            "id": doc.get("_id"),
            "codex": doc.get("codex"),
            "race_id": doc.get("raceId"),
            "date": doc.get("date"),
            "time": doc.get("time"),
            "place": doc.get("event_place", "Nieznane"),
            "country": doc.get("event_country", ""),
            "hill_size": doc.get("hillSize"),
            "hill_unit": doc.get("hill_size_unit", "HS"),
            "title": doc.get("title", "Konkurs"),
            "category_code": doc.get("category_code"),
            "category_name": CATEGORY_LABELS.get(doc.get("category_code", ""), doc.get("category_code")),
            "race_url": doc.get("raceUrl"),
            "is_cancelled": is_cancelled,
            "scrape_status": scrape_status,
            "scrape_error": doc.get("scrape_error"),
            "status": status_val,
            "status_label": status_label
        }

        if is_cancelled:
            cancelled_list.append(item)
        else:
            missing_list.append(item)

    return {
        "season": season,
        "total_unresolved": len(missing_list) + len(cancelled_list),
        "missing_count": len(missing_list),
        "failed_429_count": failed_429_count,
        "cancelled_count": len(cancelled_list),
        "missing_sessions": missing_list,
        "cancelled_sessions": cancelled_list
    }


@router.post("/update-prices")
@limiter.limit("100/minute")
async def update_prices(request: Request):
    db = request.app.database
    countries_dict = request.app.state.dicts.get("countries", {})
    prices = await update_esm_prices(db,countries_dict)

    return prices


class RefreshAthletesRequest(BaseModel):
    onlyPriceUpdate: bool = False


@router.post("/refresh-athletes")
@limiter.limit("100/minute")
async def refresh_athletes(request: Request, payload: RefreshAthletesRequest):
    db = request.app.database
    athletes_col = db["athletes"]
    review_col = db["athletes_review"]
    countries_dict = request.app.state.dicts.get("countries", {})

    current_athletes_cursor = athletes_col.find({})
    current_athletes = {}
    async for a in current_athletes_cursor:
        if "fis_code" in a:
            current_athletes[int(a["fis_code"])] = a

    stats = {"new_to_review": 0, "modified_to_review": 0, "deleted_to_review": 0, "unchanged": 0}

    # ====================================================
    # TRYB 1: TYLKO AKTUALIZACJA CEN (KIERUNEK -> KWARANTANNA)
    # ====================================================
    if payload.onlyPriceUpdate:
        print("Pobieranie cen z ESM (tryb dry_run - weryfikacja do kwarantanny)...")
        prices_result = await update_esm_prices(db, countries_dict, dry_run=True)
        esm_updates = prices_result.get("details", {}).get("esm_updates", {})
        review_bulk_ops = []

        for fis_code, updates in esm_updates.items():
            existing = current_athletes.get(fis_code)
            if existing:
                current_hash = generate_full_checksum(existing)

                # Tworzymy symulowanego skoczka z nową ceną
                updated_athlete = existing.copy()
                updated_athlete.update(updates)

                new_hash = generate_full_checksum(updated_athlete)

                if current_hash != new_hash:
                    updated_athlete["review_reason"] = "MODIFIED"
                    updated_athlete["original_checksum"] = current_hash
                    updated_athlete["checksum"] = new_hash
                    updated_athlete["last_checked"] = datetime.utcnow()
                    review_bulk_ops.append(UpdateOne({"fis_code": fis_code}, {"$set": updated_athlete}, upsert=True))
                    stats["modified_to_review"] += 1
                else:
                    stats["unchanged"] += 1

        if review_bulk_ops:
            await review_col.bulk_write(review_bulk_ops)

        return {
            "message": "Pobrano ceny z ESM. Zmiany wysłane do kwarantanny.",
            "stats": stats,
            "esmPricesData": prices_result
        }

    # ====================================================
    # TRYB 2: PEŁNY REFRESH FIS (1970 - 2014)
    # ====================================================
    else:
        all_scraped_athletes_dict = {}

        for year in range(1970, 2015):
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
            raise HTTPException(status_code=500, detail="Scraper FIS zwrócił 0 wyników dla wszystkich lat.")

        seen_fis_codes = set()
        athletes_bulk_ops = []
        review_bulk_ops = []

        for fis_code, athlete in all_scraped_athletes_dict.items():
            seen_fis_codes.add(fis_code)
            current_hash = generate_full_checksum(athlete)
            athlete["checksum"] = current_hash
            athlete["last_checked"] = datetime.utcnow()

            existing = current_athletes.get(fis_code)
            if not existing:
                athlete["review_reason"] = "NEW"
                review_bulk_ops.append(UpdateOne({"fis_code": fis_code}, {"$set": athlete}, upsert=True))
                stats["new_to_review"] += 1
            else:
                existing_hash = generate_full_checksum(existing)
                if current_hash == existing_hash:
                    athletes_bulk_ops.append(
                        UpdateOne({"fis_code": fis_code}, {"$set": {"last_checked": datetime.utcnow()}}))
                    stats["unchanged"] += 1
                else:
                    athlete["review_reason"] = "MODIFIED"
                    athlete["original_checksum"] = existing_hash
                    review_bulk_ops.append(UpdateOne({"fis_code": fis_code}, {"$set": athlete}, upsert=True))
                    stats["modified_to_review"] += 1

        for fis_code, existing_athlete in current_athletes.items():
            if fis_code not in seen_fis_codes:
                existing_athlete["review_reason"] = "DELETED"
                review_bulk_ops.append(UpdateOne({"fis_code": fis_code}, {"$set": existing_athlete}, upsert=True))
                stats["deleted_to_review"] += 1

        if athletes_bulk_ops:
            await athletes_col.bulk_write(athletes_bulk_ops)
        if review_bulk_ops:
            await review_col.bulk_write(review_bulk_ops)

        # Standardowe wstrzyknięcie cen bez dry_run dla pełnego cyklu
        prices_result = await update_esm_prices(db, countries_dict, dry_run=False)

        return {"message": "Analiza skoczków zakończona", "stats": stats, "esmPricesData": prices_result}


async def _run_refresh_results_background(db, seasons: List[str], categories: List[str], mode: str, only_missing: bool):
    manager = get_results_job_manager()
    await manager.run_results_scrape(db, seasons, categories, mode=mode, only_missing=only_missing)


async def _run_retry_missing_background(db, season: Optional[str], only_429: bool):
    manager = get_results_job_manager()
    await manager.run_retry_missing(db, season=season, only_429=only_429)


@router.get("/refresh-results/status")
@limiter.limit("120/minute")
async def get_refresh_results_status(request: Request):
    """Zwraca bieżący postęp zadania scrapowania lub wznawiania brakujących sesji."""
    return get_results_job_manager().get_status()


@router.post("/refresh-results/start")
@limiter.limit("30/minute")
async def start_refresh_results(
    request: Request,
    payload: RefreshRequest,
    background_tasks: BackgroundTasks
):
    """Uruchamia pobieranie wyników FIS w tle z bieżącym raportowaniem postępu."""
    manager = get_results_job_manager()
    if manager.is_running:
        raise HTTPException(status_code=400, detail="Zadanie scrapowania wyników jest już w toku.")

    db = request.app.database
    total_combos = len(payload.seasons) * len(payload.categories)
    manager.mark_started("scrape", total_combinations=total_combos)
    background_tasks.add_task(
        _run_refresh_results_background,
        db=db,
        seasons=payload.seasons,
        categories=payload.categories,
        mode=payload.mode,
        only_missing=payload.only_missing,
    )
    return {"message": "Scrapowanie wyników zostało uruchomione w tle.", "total_combinations": total_combos}


@router.post("/retry-missing/start")
@limiter.limit("30/minute")
async def start_retry_missing(
    request: Request,
    payload: RetryMissingRequest,
    background_tasks: BackgroundTasks
):
    """
    Uruchamia szybkie dociąganie WYŁĄCZNIE brakujących sesji / błędów 429 bezpośrednio z bazy
    (bez ponownego przeszukiwania kalendarzy i kompletnych eventów na serwerach FIS).
    """
    manager = get_results_job_manager()
    if manager.is_running:
        raise HTTPException(status_code=400, detail="Zadanie scrapowania wyników jest już w toku.")

    db = request.app.database
    manager.mark_started("retry_missing", total_combinations=1)
    background_tasks.add_task(
        _run_retry_missing_background,
        db=db,
        season=payload.season,
        only_429=payload.only_429,
    )
    return {
        "message": f"Uruchomiono dociąganie brakujących sesji (sezon: {payload.season or 'wszystkie'}).",
        "season": payload.season,
        "only_429": payload.only_429,
    }


@router.post("/refresh-results/stop")
@limiter.limit("30/minute")
async def stop_refresh_results(request: Request):
    """Bezpiecznie zatrzymuje aktywne zadanie scrapowania wyników."""
    manager = get_results_job_manager()
    if not manager.is_running:
        return {"message": "Żadne zadanie scrapowania nie jest obecnie aktywne."}
    manager.stop()
    return {"message": "Wysłano sygnał zatrzymania scrapowania."}


@router.post("/refresh-results")
@limiter.limit("100/minute")
async def refresh_results(request: Request, payload: RefreshRequest):
    db = request.app.database
    manager = get_results_job_manager()
    stats = {
        "inserted": 0,
        "sent_to_review": 0,
        "unchanged": 0,
        "skipped_complete": 0,
        "recovered_missing": 0,
        "failed_429": 0,
        "combinations_processed": 0,
    }

    existing_complete_ids = set()
    existing_event_ids_by_session: Dict[str, Optional[str]] = {}
    complete_event_ids = set()
    if payload.only_missing:
        existing_complete_ids, existing_event_ids_by_session, complete_event_ids = await manager._build_complete_sets(
            db, payload.seasons, payload.categories
        )

    for season in payload.seasons:
        for category in payload.categories:
            stats["combinations_processed"] += 1
            try:
                scraped_events = await asyncio.to_thread(
                    run_scraper_pipeline,
                    seasoncode=season,
                    categorycode=category,
                    gendercode="M",
                    limit_events=None,
                    mode=payload.mode,
                    existing_complete_ids=existing_complete_ids if payload.only_missing else None,
                    existing_complete_event_ids=complete_event_ids if payload.only_missing else None,
                )
            except Exception as e:
                print(f"⚠️ Błąd podczas pobierania kombinacji (Sezon={season}, Kategoria={category}): {e}")
                continue

            if not scraped_events:
                continue

            batch_stats = await save_scraped_events_to_db(
                db=db,
                scraped_events=scraped_events,
                season=season,
                category=category,
                only_missing=payload.only_missing,
                existing_event_ids_by_session=existing_event_ids_by_session if payload.only_missing else None,
            )
            for k in ("inserted", "sent_to_review", "unchanged", "skipped_complete", "recovered_missing", "failed_429"):
                stats[k] += batch_stats.get(k, 0)

    return {"message": "Pobieranie wariantów zakończone", "stats": stats}
# ==========================================
# MODUŁ WERYFIKACJI (KWARANTANNA) - SESJE
# ==========================================

@router.get("/reviews/sessions")
async def get_pending_session_reviews(request: Request):
    db = request.app.database
    # Sortujemy od najnowszych dat, aby świeżo pobrane sesje były na górze listy
    reviews = await db["sessions_review"].find().sort([("date", -1), ("_id", -1)]).to_list(500)

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
    """Zatwierdza wszystkich skoczków z kwarantanny za pomocą zoptymalizowanego bulk_write."""
    db = request.app.database
    reviews = await db["athletes_review"].find().to_list(None)

    if not reviews:
        return {"message": "Brak skoczków do zatwierdzenia."}

    athletes_ops = []
    review_ids_to_del = []

    for athlete in reviews:
        fis_code = athlete.get("fis_code")
        if not fis_code:
            continue

        athlete.pop("_id", None)
        reason = athlete.pop("review_reason", None)
        athlete.pop("original_checksum", None)

        if reason == "DELETED":
            athletes_ops.append(DeleteOne({"fis_code": fis_code}))
        else:
            athletes_ops.append(UpdateOne({"fis_code": fis_code}, {"$set": athlete}, upsert=True))

        review_ids_to_del.append(fis_code)

    if athletes_ops:
        await db["athletes"].bulk_write(athletes_ops, ordered=False)
    if review_ids_to_del:
        await db["athletes_review"].delete_many({"fis_code": {"$in": review_ids_to_del}})

    return {"message": f"Pomyślnie zatwierdzono {len(reviews)} zmian w bazie skoczków."}


@router.post("/reviews/sessions/approve-all")
async def approve_all_session_reviews(request: Request):
    """Zatwierdza wszystkie sesje z kwarantanny za pomocą zoptymalizowanego bulk_write."""
    db = request.app.database
    reviews = await db["sessions_review"].find().to_list(None)

    if not reviews:
        return {"message": "Brak sesji do zatwierdzenia."}

    sessions_ops = []
    review_ids_to_del = []

    for review_session in reviews:
        codex = review_session["_id"]
        review_session.pop("original_checksum", None)
        sessions_ops.append(ReplaceOne({"_id": codex}, review_session, upsert=True))
        review_ids_to_del.append(codex)

    if sessions_ops:
        # Porcjowanie po 1000 operacji dla bezpieczeństwa pamięci
        batch_size = 1000
        for i in range(0, len(sessions_ops), batch_size):
            await db["sessions"].bulk_write(sessions_ops[i:i + batch_size], ordered=False)
        await db["sessions_review"].delete_many({"_id": {"$in": review_ids_to_del}})

    return {"message": f"Pomyślnie zatwierdzono {len(reviews)} sesji."}


@router.post("/reviews/sessions/reject-all")
async def reject_all_session_reviews(request: Request):
    """Odrzuca (usuwa) wszystkie oczekujące sesje z kwarantanny."""
    db = request.app.database
    result = await db["sessions_review"].delete_many({})
    return {"message": f"Pomyślnie odrzucono i usunięto {result.deleted_count} sesji z kwarantanny."}


# Na samym dole kontrolera dodaj:
# ==========================================
# MODUŁ PDF - TRENINGI I SERIE PRÓBNE (DEBUG)
# ==========================================

# ==========================================
# MODUŁ PDF - TRENINGI I SERIE PRÓBNE
# ==========================================
class RefreshTrainingsRequest(BaseModel):
    season: Optional[str] = None
    season_from: Optional[int] = None
    season_to: Optional[int] = None
    categories: Optional[List[str]] = None
    force_reprocess: bool = False


async def _run_refresh_trainings_background(
    db,
    season: Optional[str],
    season_from: Optional[int],
    season_to: Optional[int],
    categories: Optional[List[str]],
    force_reprocess: bool,
):
    manager = get_pdf_training_job_manager()
    await manager.run_job(
        db=db,
        season=season,
        season_from=season_from,
        season_to=season_to,
        categories=categories,
        force_reprocess=force_reprocess,
    )


@router.get("/refresh-trainings/status")
@limiter.limit("120/minute")
async def get_refresh_trainings_status(request: Request):
    """Zwraca bieżący postęp asynchronicznej ekstrakcji PDF treningów."""
    return get_pdf_training_job_manager().get_status()


@router.post("/refresh-trainings/start")
@limiter.limit("30/minute")
async def start_refresh_trainings(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: RefreshTrainingsRequest = None,
):
    """Uruchamia pobieranie i analizę plików PDF treningów w tle z bezpiecznym zatrzymywaniem."""
    manager = get_pdf_training_job_manager()
    if manager.is_running:
        raise HTTPException(status_code=400, detail="Ekstrakcja plików PDF treningów jest już w toku.")

    db = request.app.database
    manager.mark_started()
    background_tasks.add_task(
        _run_refresh_trainings_background,
        db=db,
        season=payload.season if payload else None,
        season_from=payload.season_from if payload else None,
        season_to=payload.season_to if payload else None,
        categories=payload.categories if payload else None,
        force_reprocess=payload.force_reprocess if payload else False,
    )
    return {"message": "Ekstrakcja plików PDF treningów została uruchomiona w tle."}


@router.post("/refresh-trainings/stop")
@limiter.limit("30/minute")
async def stop_refresh_trainings(request: Request):
    """Bezpiecznie zatrzymuje aktywną ekstrakcję plików PDF treningów."""
    manager = get_pdf_training_job_manager()
    if not manager.is_running:
        return {"message": "Żadne zadanie ekstrakcji PDF nie jest obecnie aktywne."}
    manager.stop()
    return {"message": "Wysłano sygnał bezpiecznego zatrzymania ekstrakcji PDF."}


@router.post("/refresh-trainings")
@limiter.limit("5/minute")
async def extract_trainings_pdf(request: Request, payload: RefreshTrainingsRequest = None):
    """Przeszukuje bazę, pobiera pliki PDF z treningami i tworzy nowe sesje w MongoDB."""
    db = request.app.database
    return await process_all_trainings_pdf(
        db,
        season=payload.season if payload else None,
        season_from=payload.season_from if payload else None,
        season_to=payload.season_to if payload else None,
        categories=payload.categories if payload else None,
        force_reprocess=payload.force_reprocess if payload else False,
    )


class CheckerRequest(BaseModel):
    url: str
    codex: str
    race_id: str

@router.post("/refresh-trainings-checker")
async def check_single_training(req: CheckerRequest, request: Request):
    """Endpoint DEBUG: Sprawdza jeden konkretny PDF i wypluwa krok po kroku jak go zrozumiał."""
    db = request.app.database
    result = await process_single_pdf_debug(db, req.url, req.codex, req.race_id)
    return result


# Model zapytania
class PdfplumberRequest(BaseModel):
    url: str


# Funkcja synchroniczna do parsowania (pdfplumber mocno obciąża procesor)
def analyze_pdf_with_pdfplumber(pdf_bytes: bytes):
    results = []
    # pdfplumber otwiera strumień bajtów bezpośrednio z pamięci RAM
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            page_data = {
                "page": page_num + 1,
                "status": "",
                "tables_found": 0,
                "extracted_rows": []
            }

            # Próbujemy wyciągnąć tabele
            tables = page.extract_tables()

            if len(tables) > 0:
                page_data["status"] = "ZNALEZIONO TABELE"
                page_data["tables_found"] = len(tables)
                # Spłaszczamy tabele do jednej listy wierszy dla łatwiejszego podglądu
                for table in tables:
                    for row in table:
                        page_data["extracted_rows"].append(row)
            else:
                page_data["status"] = "BRAK TABEL (pdfplumber widzi to tylko jako zwykły tekst)"

            results.append(page_data)

    return results


@router.post("/test-pdfplumber")
async def test_pdfplumber_endpoint(req: PdfplumberRequest):
    """
    Endpoint debugujący. Pobiera PDF z podanego publicznego URL i sprawdza,
    jak pdfplumber widzi w nim tabele.
    """
    from urllib.parse import urlparse
    import ipaddress
    import socket

    parsed = urlparse((req.url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {"success": False, "error": "Dozwolone są wyłącznie publiczne adresy HTTP/HTTPS."}

    try:
        resolved_ip = ipaddress.ip_address(socket.gethostbyname(parsed.hostname))
        if resolved_ip.is_private or resolved_ip.is_loopback or resolved_ip.is_link_local or resolved_ip.is_reserved:
            return {"success": False, "error": "Niedozwolony adres docelowy."}
    except Exception:
        return {"success": False, "error": "Nie można zweryfikować nazwy hosta."}

    try:
        # 1. Pobieramy plik z URL
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(req.url, headers=config.DEFAULT_HEADERS)
            response.raise_for_status()

            # Sprawdzamy czy to faktycznie PDF
            if b"%PDF" not in response.content[:10]:
                return {"error": "Pobrany plik nie jest prawidłowym dokumentem PDF."}

            pdf_bytes = response.content

        # 2. Odpalamy pdfplumber w osobnym wątku (żeby nie zablokować FastAPI)
        analysis_result = await asyncio.to_thread(analyze_pdf_with_pdfplumber, pdf_bytes)

        return {
            "success": True,
            "url": req.url,
            "pages_analyzed": len(analysis_result),
            "results": analysis_result
        }

    except Exception:
        return {
            "success": False,
            "error": "Nie udało się pobrać lub przeanalizować wskazanego dokumentu PDF."
        }


# =====================================================================
# MODUŁ WZBOGACANIA POGODĄ & NOCNEGO PRZETWARZANIA BAZY (BATCH SCRAPE)
# =====================================================================

class EnrichWeatherRequest(BaseModel):
    target: str = "all"  # "all" (DEV + TEST), "dev", "test", "current"
    force: bool = False

class NightlyScrapeRequest(BaseModel):
    seasons: Optional[List[str]] = None
    categories: Optional[List[str]] = None
    delay_seconds: float = 1.8
    mode: str = "IND"


async def _run_weather_enrichment_task(db_client, target: str, force: bool):
    enricher = get_weather_enricher()
    try:
        if target in ["all", "both"]:
            # 1. TEST najpierw (szybka weryfikacja)
            db_test = db_client["FIS_DATA_DB_TEST"]
            await enricher.enrich_database(db_test, force=force)
            # 2. DEV (pełna baza)
            if not enricher.stop_requested:
                db_dev = db_client["FIS_DATA_DB_DEV"]
                await enricher.enrich_database(db_dev, force=force)
        elif target == "test":
            db_test = db_client["FIS_DATA_DB_TEST"]
            await enricher.enrich_database(db_test, force=force)
        elif target == "dev":
            db_dev = db_client["FIS_DATA_DB_DEV"]
            await enricher.enrich_database(db_dev, force=force)
        else:
            db_cur = db_client[config.settings.MONGO_DB_NAME]
            await enricher.enrich_database(db_cur, force=force)
    except Exception as e:
        print(f"Błąd zadania w tle wzbogacania pogody: {e}")


async def _run_nightly_scrape_background(db, seasons: List[str], categories: List[str], delay_seconds: float, mode: str):
    scraper = get_nightly_batch_scraper()
    await scraper.run_nightly_scrape(db, seasons, categories, delay_seconds=delay_seconds, mode=mode)


@router.get("/weather-status")
@limiter.limit("60/minute")
async def get_weather_enrichment_status(request: Request):
    """Zwraca stan pokrycia bazy danymi pogodowymi dla profilu DEV oraz TEST oraz live status enrichera."""
    client = getattr(request.app, "mongodb_client", None) or getattr(request.app, "db_client", None)
    enricher = get_weather_enricher()

    if client is not None:
        stats_dev = await enricher.get_db_stats(client["FIS_DATA_DB_DEV"])
        stats_test = await enricher.get_db_stats(client["FIS_DATA_DB_TEST"])
    else:
        db = getattr(request.app, "database", None)
        cur_stats = await enricher.get_db_stats(db) if db is not None else {}
        stats_dev = cur_stats
        stats_test = cur_stats

    return {
        "status": enricher.get_status(),
        "dev": stats_dev,
        "test": stats_test,
        "active_env": config.APP_ENV,
        "active_db": config.settings.MONGO_DB_NAME
    }


@router.post("/enrich-weather/start")
@limiter.limit("10/minute")
async def start_weather_enrichment(
    request: Request,
    payload: EnrichWeatherRequest,
    background_tasks: BackgroundTasks
):
    """Uruchamia w tle wzbogacanie sesji historycznych o dane pogodowe z Open-Meteo Archive API."""
    enricher = get_weather_enricher()
    if enricher.is_running:
        raise HTTPException(status_code=400, detail="Zadanie wzbogacania bazy pogodą jest już w toku.")

    client = getattr(request.app, "mongodb_client", None) or getattr(request.app, "db_client", None)
    background_tasks.add_task(_run_weather_enrichment_task, client, payload.target, payload.force)

    return {
        "message": f"Wzbogacanie bazy pogodą zostało uruchomione w tle (cel: {payload.target.upper()}).",
        "target": payload.target,
        "force": payload.force
    }


@router.post("/enrich-weather/stop")
@limiter.limit("10/minute")
async def stop_weather_enrichment(request: Request):
    """Zatrzymuje trwające zadanie wzbogacania bazy pogodą."""
    enricher = get_weather_enricher()
    if not enricher.is_running:
        return {"message": "Żadne zadanie wzbogacania bazy nie jest obecnie aktywne."}

    enricher.stop()
    return {"message": "Wysłano sygnał bezpiecznego zatrzymania wzbogacania."}


@router.get("/nightly-scrape/status")
@limiter.limit("60/minute")
async def get_nightly_scrape_status(request: Request):
    """Zwraca bieżący stan nocnego przetwarzania bazy."""
    scraper = get_nightly_batch_scraper()
    return scraper.get_status()


@router.post("/nightly-scrape/start")
@limiter.limit("10/minute")
async def start_nightly_scrape(
    request: Request,
    payload: NightlyScrapeRequest,
    background_tasks: BackgroundTasks
):
    """
    Uruchamia nocne powolne przetwarzanie (batch scrape) ze sztucznym opóźnieniem chroniącym przed kodem 429.
    Automatycznie wzbogaca każdą sesję o historyczne dane pogodowe z Open-Meteo.
    """
    scraper = get_nightly_batch_scraper()
    if scraper.is_running:
        raise HTTPException(status_code=400, detail="Nocne przetwarzanie bazy jest już w toku.")

    db = request.app.database

    # Domyślnie wszystkie dostępne sezony z kalendarza lub bazy
    seasons = payload.seasons
    if not seasons or len(seasons) == 0:
        db_seasons = await db["sessions"].distinct("season")
        if db_seasons:
            seasons = sorted(db_seasons, reverse=True)
        else:
            seasons = [str(y) for y in range(2026, 1999, -1)]

    categories = payload.categories or ["WC", "GP", "COC", "FC"]

    background_tasks.add_task(
        _run_nightly_scrape_background,
        db=db,
        seasons=seasons,
        categories=categories,
        delay_seconds=payload.delay_seconds,
        mode=payload.mode
    )

    return {
        "message": f"Nocne przetwarzanie zostało uruchomione w tle ({len(seasons)} sezonów, {len(categories)} kategorii).",
        "seasons_count": len(seasons),
        "categories": categories,
        "delay_seconds": payload.delay_seconds
    }


@router.post("/nightly-scrape/stop")
@limiter.limit("10/minute")
async def stop_nightly_scrape(request: Request):
    """Bezpiecznie zatrzymuje nocne przetwarzanie bazy."""
    scraper = get_nightly_batch_scraper()
    if not scraper.is_running:
        return {"message": "Żadne nocne przetwarzanie nie jest obecnie aktywne."}

    scraper.stop()
    return {"message": "Wysłano sygnał zatrzymania nocnego przetwarzania."}