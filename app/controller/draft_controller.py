import asyncio
import json
import re
from typing import List, Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth import get_current_user
from app.infrastructure.limiter import limiter
from app.services.auth_service import Role, decode_access_token, is_pro_role
from app.services.draft_service import (
    ALLOWED_DRAFT_CATEGORIES,
    DEFAULT_DRAFT_SEASON,
    DEFAULT_PICKS_PER_PLAYER,
    MAX_PICKS_PER_PLAYER,
    MIN_PICKS_PER_PLAYER,
    _now_iso,
    build_draft_tournament_calendar,
    calculate_league_standings,
    check_user_draft_eligibility,
    create_notification,
    draft_broker,
    ensure_league_invite_code,
    generate_unique_invite_code,
    get_active_athlete_countries,
    get_current_wc_season_points_map,
    get_finished_competitions_for_cutoff,
    get_snake_turn_user_id,
    make_random_order,
    mark_turn_notifications_read,
    publish_league_refresh_to_participants,
    serialize_league,
    sync_leagues_user_profiles,
)

router = APIRouter(prefix="/draft", tags=["Fantasy Draft & Notifications"])


class CreateDraftRequest(BaseModel):
    name: str
    category_code: str = "WC"
    season: str = DEFAULT_DRAFT_SEASON
    picks_per_player: int = DEFAULT_PICKS_PER_PLAYER
    order_mode: str = "CUSTOM"  # CUSTOM | RANDOM
    scoring_from_date: Optional[str] = None
    scoring_to_date: Optional[str] = None


class UpdateDraftConfigRequest(BaseModel):
    name: Optional[str] = None
    category_code: Optional[str] = None
    season: Optional[str] = None
    picks_per_player: Optional[int] = None
    order_mode: Optional[str] = None  # CUSTOM | RANDOM
    ordered_user_ids: Optional[List[str]] = None
    scoring_from_date: Optional[str] = None
    scoring_to_date: Optional[str] = None


class InviteUserRequest(BaseModel):
    user_id: str


class RespondInviteRequest(BaseModel):
    action: str  # accept | decline


class MakePickRequest(BaseModel):
    fis_code: int


class JoinByCodeRequest(BaseModel):
    code: str


def _oid_or_400(raw: str, label: str = "rekordu") -> ObjectId:
    try:
        return ObjectId(str(raw).strip())
    except Exception:
        raise HTTPException(status_code=400, detail=f"Nieprawidłowy identyfikator {label}.")


def _require_draft_plan(current_user: dict) -> None:
    # Każdy zalogowany użytkownik ma dostęp do Drafta (FREE: max 1 liga, PRO/ADMIN: bez limitu)
    return None


def _validate_picks_count(val: int) -> int:
    if val < MIN_PICKS_PER_PLAYER or val > MAX_PICKS_PER_PLAYER:
        raise HTTPException(
            status_code=400,
            detail=f"Liczba zawodników w drafcie musi mieścić się w zakresie od {MIN_PICKS_PER_PLAYER} do {MAX_PICKS_PER_PLAYER}.",
        )
    return int(val)


# =========================================================
# POWIADOMIENIA + STRUMIEŃ NA ŻYWO (SSE)
# =========================================================

@router.get("/events/stream")
async def stream_user_events(request: Request):
    """Strumień Server-Sent Events (SSE) dla powiadomień i odświeżania drafta (autoryzacja wyłącznie przez nagłówek Bearer)."""
    auth_header = request.headers.get("authorization") or ""
    raw_token = None
    if auth_header.lower().startswith("bearer "):
        raw_token = auth_header.split(" ", 1)[1].strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="Brak nagłówka autoryzacyjnego Bearer dla strumienia SSE.")

    payload = decode_access_token(raw_token)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Nieprawidłowy lub wygasły token.")

    uid = str(payload["sub"])
    db = request.app.database
    try:
        user_doc = await db["users"].find_one({"_id": ObjectId(uid)}, {"is_active": 1})
    except Exception:
        user_doc = None
    if not user_doc or not user_doc.get("is_active", True):
        raise HTTPException(status_code=401, detail="Użytkownik nieaktywny lub nie istnieje.")

    queue = draft_broker.subscribe(uid)

    async def event_generator():
        try:
            yield f"data: {json.dumps({'event': 'connected'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            draft_broker.unsubscribe(uid, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/notifications")
@limiter.limit("90/minute")
async def get_my_notifications(
    request: Request,
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    db = request.app.database
    uid = str(current_user["_id"])
    unread_count = await db["notifications"].count_documents({"user_id": uid, "is_read": False})
    flt = {"user_id": uid}
    if unread_only:
        flt["is_read"] = False
    docs = await db["notifications"].find(flt).sort("created_at", -1).to_list(length=limit)
    items = [
        {
            "id": str(d["_id"]),
            "type": d.get("type"),
            "title": d.get("title"),
            "message": d.get("message"),
            "league_id": d.get("league_id"),
            "link": d.get("link") or "/draft",
            "is_read": bool(d.get("is_read")),
            "created_at": d.get("created_at"),
        }
        for d in docs
    ]
    return {"unread_count": unread_count, "items": items}


@router.post("/notifications/{notif_id}/read")
@limiter.limit("60/minute")
async def mark_notification_read(
    notif_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    db = request.app.database
    uid = str(current_user["_id"])
    oid = _oid_or_400(notif_id, "powiadomienia")
    await db["notifications"].update_one({"_id": oid, "user_id": uid}, {"$set": {"is_read": True}})
    return {"ok": True}


@router.post("/notifications/read-all")
@limiter.limit("30/minute")
async def mark_all_notifications_read(request: Request, current_user: dict = Depends(get_current_user)):
    db = request.app.database
    uid = str(current_user["_id"])
    await db["notifications"].update_many({"user_id": uid, "is_read": False}, {"$set": {"is_read": True}})
    return {"ok": True}


# =========================================================
# POMOCNICZE: ZAWODNICY AKTYWNI, UŻYTKOWNICY, KONKURSY GRANICZNE
# =========================================================

@router.get("/cutoff-preview")
@limiter.limit("40/minute")
async def preview_cutoff_competitions(
    request: Request,
    season: str = Query(default=DEFAULT_DRAFT_SEASON),
    category_code: str = Query(default="WC"),
    current_user: dict = Depends(get_current_user),
):
    """Zwraca już rozegrane konkursy dla (sezon, turniej) i rekomendowaną datę graniczną od kolejnego konkursu."""
    _require_draft_plan(current_user)
    cat = category_code.strip().upper()
    if cat not in ALLOWED_DRAFT_CATEGORIES:
        raise HTTPException(status_code=400, detail="Obsługiwane turnieje to: WC, COC, FC.")
    db = request.app.database
    return await get_finished_competitions_for_cutoff(db, season=season.strip(), category_code=cat)


@router.get("/athletes/countries")
@limiter.limit("60/minute")
async def get_draft_athlete_countries(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Zwraca listę wszystkich narodowości aktywnych skoczków do wielokrotnego filtra w Drafcie."""
    _require_draft_plan(current_user)
    db = request.app.database
    return await get_active_athlete_countries(db)


@router.get("/athletes/active")
@limiter.limit("60/minute")
async def get_active_athletes_pool(
    request: Request,
    q: str = Query(default="", max_length=60),
    country: str = Query(default="", max_length=350),
    countries: str = Query(default="", max_length=350),
    sort_by: str = Query(default="wc_points", max_length=20),
    current_user: dict = Depends(get_current_user),
):
    """Zwraca pulę aktywnych skoczków do wyboru w Drafcie (tylko status=Active) z punktami bieżącego sezonu PŚ."""
    _require_draft_plan(current_user)
    db = request.app.database
    flt: dict = {"status": "Active"}

    raw_countries = f"{country},{countries}"
    selected_countries = list(dict.fromkeys(
        c.strip().upper() for c in raw_countries.split(",") if c.strip()
    ))
    if selected_countries:
        flt["country_code"] = {"$in": selected_countries}

    docs = await db["athletes"].find(
        flt,
        {
            "fis_code": 1,
            "full_name": 1,
            "country_code": 1,
            "price": 1,
            "wc_points": 1,
            "competitor_id_from_url": 1,
        },
    ).to_list(length=2000)

    wc_season, wc_points_map = await get_current_wc_season_points_map(db)

    needle = q.strip().lower()
    out = []
    for a in docs:
        name = a.get("full_name") or ""
        cc = str(a.get("country_code") or "").strip().upper()
        fis = a.get("fis_code")
        if needle and needle not in name.lower() and needle not in cc.lower() and needle not in str(fis or ""):
            continue
        try:
            fis_int = int(fis) if fis is not None else None
        except (ValueError, TypeError):
            fis_int = None

        if fis_int is not None and fis_int in wc_points_map:
            raw_wc = wc_points_map[fis_int]
        else:
            raw_wc = float(a.get("wc_points") or 0)
        wc_clean = int(raw_wc) if float(raw_wc).is_integer() else round(float(raw_wc), 1)

        out.append({
            "fis_code": fis,
            "full_name": name,
            "country_code": cc,
            "price": a.get("price"),
            "wc_points": wc_clean,
            "wc_season": wc_season,
            "competitor_id_from_url": a.get("competitor_id_from_url"),
        })

    sort_mode = sort_by.strip().lower()
    if sort_mode == "name":
        out.sort(key=lambda x: (str(x.get("full_name") or "").lower(), str(x.get("fis_code") or "")))
    elif sort_mode == "price":
        out.sort(key=lambda x: (
            -(float(x["price"]) if x.get("price") is not None else -1.0),
            -float(x.get("wc_points") or 0),
            str(x.get("full_name") or "").lower(),
        ))
    else:
        out.sort(key=lambda x: (
            -float(x.get("wc_points") or 0),
            -(float(x["price"]) if x.get("price") is not None else -1.0),
            str(x.get("full_name") or "").lower(),
        ))

    return out[:200]


@router.get("/users/search")
@limiter.limit("40/minute")
async def search_users_to_invite(
    request: Request,
    q: str = Query(default="", max_length=60),
    current_user: dict = Depends(get_current_user),
):
    """Wyszukuje graczy (LITE, PRO i ADMIN) po nazwie do zaproszenia do ligi drafta (bez N+1 zapytań)."""
    _require_draft_plan(current_user)
    db = request.app.database
    me_id = str(current_user["_id"])

    user_filter: dict = {"_id": {"$ne": _oid_or_400(me_id)}, "is_active": {"$ne": False}}
    needle = q.strip()
    if needle:
        user_filter["name"] = {"$regex": re.escape(needle), "$options": "i"}

    users = await db["users"].find(
        user_filter,
        {"name": 1, "picture": 1, "role": 1},
    ).sort("name", 1).limit(60).to_list(length=60)

    if not users:
        return []

    user_ids = [str(u["_id"]) for u in users]
    counts_map: dict = {}
    agg_cursor = db["draft_leagues"].aggregate([
        {"$match": {"participants": {"$elemMatch": {"user_id": {"$in": user_ids}, "status": "ACCEPTED"}}}},
        {"$unwind": "$participants"},
        {"$match": {"participants.user_id": {"$in": user_ids}, "participants.status": "ACCEPTED"}},
        {"$group": {"_id": "$participants.user_id", "cnt": {"$sum": 1}}},
    ])
    async for doc in agg_cursor:
        counts_map[str(doc["_id"])] = int(doc.get("cnt") or 0)

    items = []
    for u in users:
        uid_str = str(u["_id"])
        uname = u.get("name") or "Gracz"
        role = str(u.get("role") or Role.USER).upper()
        active_cnt = counts_map.get(uid_str, 0)
        if is_pro_role(role):
            eligible, reason = True, ""
        elif active_cnt >= 1:
            eligible = False
            reason = "Gracz osiągnął limit aktywnych lig."
        else:
            eligible, reason = True, ""

        items.append({
            "user_id": uid_str,
            "name": uname,
            "picture": u.get("picture") or "",
            "eligible": eligible,
            "ineligible_reason": reason,
        })

    items.sort(key=lambda x: (0 if x["eligible"] else 1, x["name"].lower()))
    return items[:60]


# =========================================================
# ZARZĄDZANIE LIGAMI DRAFTA I PRZEBIEG DRAFTA
# =========================================================

@router.get("/leagues")
@limiter.limit("60/minute")
async def list_my_draft_leagues(request: Request, current_user: dict = Depends(get_current_user)):
    """Lista lig drafta, w których użytkownik bierze udział lub ma oczekujące zaproszenie."""
    _require_draft_plan(current_user)
    db = request.app.database
    uid = str(current_user["_id"])
    eligible, reason, active_count = await check_user_draft_eligibility(db, current_user)

    leagues = await db["draft_leagues"].find(
        {"participants.user_id": uid}
    ).sort("updated_at", -1).to_list(length=50)

    await sync_leagues_user_profiles(db, leagues)
    for lg in leagues:
        await ensure_league_invite_code(db, lg)

    serialized = [serialize_league(lg) for lg in leagues]
    return {
        "eligibility": {
            "eligible": eligible,
            "reason": reason,
            "active_leagues_count": active_count,
            "role": str(current_user.get("role") or Role.USER).upper(),
        },
        "leagues": serialized,
    }


@router.post("/leagues")
@limiter.limit("20/minute")
async def create_draft_league(
    payload: CreateDraftRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Tworzy nową ligę drafta w stanie LOBBY (z wyborem liczby zawodników 3-8 oraz wykluczeniem przeszłych konkursów)."""
    _require_draft_plan(current_user)
    db = request.app.database

    eligible, reason, _ = await check_user_draft_eligibility(db, current_user)
    if not eligible:
        raise HTTPException(status_code=403, detail=reason)

    clean_name = payload.name.strip()
    if len(clean_name) < 2 or len(clean_name) > 80:
        raise HTTPException(status_code=400, detail="Nazwa ligi musi mieć od 2 do 80 znaków.")

    cat = payload.category_code.strip().upper()
    if cat not in ALLOWED_DRAFT_CATEGORIES:
        raise HTTPException(status_code=400, detail="Wybierz turniej: WC, COC lub FC.")

    picks_count = _validate_picks_count(payload.picks_per_player)

    order_mode = payload.order_mode.strip().upper()
    if order_mode not in {"CUSTOM", "RANDOM"}:
        order_mode = "CUSTOM"

    season = (payload.season or DEFAULT_DRAFT_SEASON).strip()
    cutoff = await get_finished_competitions_for_cutoff(db, season=season, category_code=cat)
    scoring_from = payload.scoring_from_date.strip() if payload.scoring_from_date is not None else cutoff["recommended_scoring_from_date"]
    scoring_to = payload.scoring_to_date.strip() if payload.scoring_to_date is not None else cutoff["recommended_scoring_to_date"]

    uid = str(current_user["_id"])
    now = _now_iso()
    invite_code = await generate_unique_invite_code(db)
    league_doc = {
        "name": clean_name,
        "invite_code": invite_code,
        "creator_id": uid,
        "creator_name": current_user.get("name") or "Gracz",
        "status": "LOBBY",
        "category_code": cat,
        "season": season,
        "picks_per_player": picks_count,
        "order_mode": order_mode,
        "scoring_from_date": scoring_from,
        "scoring_to_date": scoring_to,
        "participants": [
            {
                "user_id": uid,
                "name": current_user.get("name") or "Gracz",
                "picture": current_user.get("picture") or "",
                "role": str(current_user.get("role") or Role.USER).upper(),
                "status": "ACCEPTED",
                "draft_order": 0,
                "picks": [],
            }
        ],
        "draft_order_user_ids": [uid],
        "current_pick_index": 0,
        "current_turn_user_id": None,
        "started_at": None,
        "completed_at": None,
        "league_started_at": None,
        "created_at": now,
        "updated_at": now,
    }
    res = await db["draft_leagues"].insert_one(league_doc)
    league_doc["_id"] = res.inserted_id
    standings_data = await calculate_league_standings(db, league_doc)
    return serialize_league(league_doc, standings_data)


@router.post("/leagues/join-by-code")
@limiter.limit("20/minute")
async def join_draft_league_by_code(
    payload: JoinByCodeRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Dołącza zalogowanego gracza do ligi drafta na podstawie 8-znakowego kodu alfanumerycznego."""
    _require_draft_plan(current_user)
    clean_code = (payload.code or "").strip().upper()
    if len(clean_code) < 6 or len(clean_code) > 12 or not clean_code.isalnum():
        raise HTTPException(status_code=400, detail="Podaj prawidłowy 8-znakowy kod ligi (litery i cyfry).")

    db = request.app.database
    league = await db["draft_leagues"].find_one({"invite_code": clean_code})
    if not league:
        raise HTTPException(status_code=404, detail="Nie znaleziono ligi o podanym kodzie.")

    if league.get("status") != "LOBBY":
        raise HTTPException(status_code=400, detail="Draft w tej lidze już się rozpoczął — nie można dołączyć.")

    uid = str(current_user["_id"])
    participants = list(league.get("participants") or [])
    existing_idx = next((i for i, p in enumerate(participants) if p.get("user_id") == uid), None)

    if existing_idx is not None and participants[existing_idx].get("status") == "ACCEPTED":
        standings_data = await calculate_league_standings(db, league)
        return serialize_league(league, standings_data)

    eligible, reason, _ = await check_user_draft_eligibility(db, current_user)
    if not eligible:
        raise HTTPException(status_code=403, detail=reason)

    order_ids = list(league.get("draft_order_user_ids") or [])
    if uid not in order_ids:
        order_ids.append(uid)

    if existing_idx is not None:
        participants[existing_idx]["status"] = "ACCEPTED"
        participants[existing_idx]["draft_order"] = order_ids.index(uid)
    else:
        participants.append({
            "user_id": uid,
            "name": current_user.get("name") or "Gracz",
            "picture": current_user.get("picture") or "",
            "role": str(current_user.get("role") or Role.USER).upper(),
            "status": "ACCEPTED",
            "draft_order": order_ids.index(uid),
            "picks": [],
        })

    await db["draft_leagues"].update_one(
        {"_id": league["_id"]},
        {
            "$set": {
                "participants": participants,
                "draft_order_user_ids": order_ids,
                "updated_at": _now_iso(),
            }
        },
    )
    await mark_turn_notifications_read(db, uid, str(league["_id"]))

    creator_id = league.get("creator_id")
    if creator_id and creator_id != uid:
        await create_notification(
            db=db,
            user_id=str(creator_id),
            notif_type="DRAFT_JOINED",
            title="Nowy gracz dołączył do Drafta!",
            message=f"{current_user.get('name') or 'Gracz'} dołączył do ligi '{league.get('name')}' kodem {clean_code}.",
            league_id=str(league["_id"]),
        )

    fresh = await db["draft_leagues"].find_one({"_id": league["_id"]})
    publish_league_refresh_to_participants(fresh)
    standings_data = await calculate_league_standings(db, fresh)
    return serialize_league(fresh, standings_data)


@router.get("/leagues/{league_id}")
@limiter.limit("60/minute")
async def get_draft_league_detail(
    league_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Zwraca szczegóły ligi drafta, kolejkę Snake oraz tabelę klasyfikacji generalnej."""
    _require_draft_plan(current_user)
    db = request.app.database
    lid = _oid_or_400(league_id, "ligi")
    league = await db["draft_leagues"].find_one({"_id": lid})
    if not league:
        raise HTTPException(status_code=404, detail="Nie znaleziono ligi drafta.")

    uid = str(current_user["_id"])
    if not any(p.get("user_id") == uid for p in (league.get("participants") or [])):
        raise HTTPException(status_code=403, detail="Nie jesteś uczestnikiem tej ligi.")

    await ensure_league_invite_code(db, league)
    standings_data = await calculate_league_standings(db, league)
    return serialize_league(league, standings_data)


@router.get("/leagues/{league_id}/calendar")
@limiter.limit("40/minute")
async def get_draft_league_calendar(
    league_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Zwraca kalendarz turnieju dla danej ligi (rozegrane i przyszłe konkursy wraz z flagą udziału)."""
    _require_draft_plan(current_user)
    db = request.app.database
    lid = _oid_or_400(league_id, "ligi")
    league = await db["draft_leagues"].find_one({"_id": lid})
    if not league:
        raise HTTPException(status_code=404, detail="Nie znaleziono ligi drafta.")

    uid = str(current_user["_id"])
    if not any(p.get("user_id") == uid for p in (league.get("participants") or [])):
        raise HTTPException(status_code=403, detail="Nie jesteś uczestnikiem tej ligi.")

    return await build_draft_tournament_calendar(
        db=db,
        season=str(league.get("season") or DEFAULT_DRAFT_SEASON),
        category_code=str(league.get("category_code") or "WC"),
        scoring_from_date=str(league.get("scoring_from_date") or ""),
        scoring_to_date=str(league.get("scoring_to_date") or ""),
    )


@router.put("/leagues/{league_id}/config")
@limiter.limit("30/minute")
async def update_draft_league_config(
    league_id: str,
    payload: UpdateDraftConfigRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Aktualizuje konfigurację ligi w stanie LOBBY przed kliknięciem START."""
    _require_draft_plan(current_user)
    db = request.app.database
    lid = _oid_or_400(league_id, "ligi")
    league = await db["draft_leagues"].find_one({"_id": lid})
    if not league:
        raise HTTPException(status_code=404, detail="Nie znaleziono ligi.")

    uid = str(current_user["_id"])
    if league.get("creator_id") != uid:
        raise HTTPException(status_code=403, detail="Tylko założyciel ligi może zmieniać jej ustawienia.")
    if league.get("status") != "LOBBY":
        raise HTTPException(status_code=400, detail="Konfigurację można zmieniać tylko przed rozpoczęciem drafta (START).")

    updates: dict = {"updated_at": _now_iso()}
    new_season = str(payload.season.strip() if payload.season else league.get("season") or DEFAULT_DRAFT_SEASON)
    new_cat = str(payload.category_code.strip().upper() if payload.category_code else league.get("category_code") or "WC")

    if new_cat not in ALLOWED_DRAFT_CATEGORIES:
        raise HTTPException(status_code=400, detail="Wybierz turniej: WC, COC lub FC.")

    if payload.picks_per_player is not None:
        updates["picks_per_player"] = _validate_picks_count(payload.picks_per_player)

    if payload.name is not None:
        clean_n = payload.name.strip()
        if len(clean_n) < 2 or len(clean_n) > 80:
            raise HTTPException(status_code=400, detail="Nazwa ligi musi mieć od 2 do 80 znaków.")
        updates["name"] = clean_n

    cat_or_season_changed = (new_cat != league.get("category_code")) or (new_season != str(league.get("season")))
    updates["category_code"] = new_cat
    updates["season"] = new_season

    if cat_or_season_changed and payload.scoring_from_date is None and payload.scoring_to_date is None:
        cutoff = await get_finished_competitions_for_cutoff(db, season=new_season, category_code=new_cat)
        updates["scoring_from_date"] = cutoff["recommended_scoring_from_date"]
        updates["scoring_to_date"] = cutoff["recommended_scoring_to_date"]
    else:
        if payload.scoring_from_date is not None:
            updates["scoring_from_date"] = payload.scoring_from_date.strip()
        if payload.scoring_to_date is not None:
            updates["scoring_to_date"] = payload.scoring_to_date.strip()

    if payload.order_mode is not None:
        om = payload.order_mode.strip().upper()
        if om in {"CUSTOM", "RANDOM"}:
            updates["order_mode"] = om

    participants = list(league.get("participants") or [])
    if payload.ordered_user_ids is not None:
        accepted_ids = [p["user_id"] for p in participants if p.get("status") == "ACCEPTED"]
        new_order = [x for x in payload.ordered_user_ids if x in accepted_ids]
        for x in accepted_ids:
            if x not in new_order:
                new_order.append(x)
        order_idx_map = {u: i for i, u in enumerate(new_order)}
        for p in participants:
            if p["user_id"] in order_idx_map:
                p["draft_order"] = order_idx_map[p["user_id"]]
        updates["participants"] = participants
        updates["draft_order_user_ids"] = new_order

    await db["draft_leagues"].update_one({"_id": lid}, {"$set": updates})
    fresh = await db["draft_leagues"].find_one({"_id": lid})
    publish_league_refresh_to_participants(fresh)
    standings_data = await calculate_league_standings(db, fresh)
    return serialize_league(fresh, standings_data)


@router.post("/leagues/{league_id}/invite")
@limiter.limit("30/minute")
async def invite_user_to_draft_league(
    league_id: str,
    payload: InviteUserRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Zaprasza użytkownika FantaSKI (LITE, PRO lub ADMIN) do ligi drafta i wysyła powiadomienie."""
    _require_draft_plan(current_user)
    db = request.app.database
    lid = _oid_or_400(league_id, "ligi")
    league = await db["draft_leagues"].find_one({"_id": lid})
    if not league:
        raise HTTPException(status_code=404, detail="Nie znaleziono ligi.")

    uid = str(current_user["_id"])
    if league.get("creator_id") != uid:
        raise HTTPException(status_code=403, detail="Tylko założyciel ligi może zapraszać graczy.")
    if league.get("status") != "LOBBY":
        raise HTTPException(status_code=400, detail="Nie można zapraszać po rozpoczęciu drafta.")

    target_oid = _oid_or_400(payload.user_id, "użytkownika")
    target_user = await db["users"].find_one({"_id": target_oid, "is_active": {"$ne": False}})
    if not target_user:
        raise HTTPException(status_code=404, detail="Nie znaleziono użytkownika.")

    target_uid = str(target_user["_id"])
    eligible, reason, _ = await check_user_draft_eligibility(db, target_user)
    if not eligible:
        raise HTTPException(status_code=400, detail=f"{target_user.get('name')}: {reason}")

    participants = list(league.get("participants") or [])
    existing_idx = next((i for i, p in enumerate(participants) if p.get("user_id") == target_uid), None)
    if existing_idx is not None:
        curr_st = participants[existing_idx].get("status")
        if curr_st == "ACCEPTED":
            raise HTTPException(status_code=400, detail="Ten gracz już uczestniczy w lidze.")
        if curr_st == "INVITED":
            raise HTTPException(status_code=400, detail="Zaproszenie zostało już wysłane.")
        participants[existing_idx]["status"] = "INVITED"
        participants[existing_idx]["invited_by"] = uid
    else:
        participants.append({
            "user_id": target_uid,
            "name": target_user.get("name") or "Gracz",
            "picture": target_user.get("picture") or "",
            "role": str(target_user.get("role") or Role.USER).upper(),
            "status": "INVITED",
            "invited_by": uid,
            "draft_order": len(participants),
            "picks": [],
        })

    await db["draft_leagues"].update_one(
        {"_id": lid},
        {"$set": {"participants": participants, "updated_at": _now_iso()}},
    )
    await create_notification(
        db=db,
        user_id=target_uid,
        notif_type="DRAFT_INVITE",
        title="Zaproszenie do ligi Draft!",
        message=f"{current_user.get('name')} zaprosił Cię do ligi '{league.get('name')}' ({league.get('category_code')} {league.get('season')}).",
        league_id=str(lid),
    )

    fresh = await db["draft_leagues"].find_one({"_id": lid})
    publish_league_refresh_to_participants(fresh)
    standings_data = await calculate_league_standings(db, fresh)
    return serialize_league(fresh, standings_data)


@router.post("/leagues/{league_id}/respond")
@limiter.limit("30/minute")
async def respond_to_draft_invite(
    league_id: str,
    payload: RespondInviteRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Akceptacja lub odrzucenie zaproszenia do ligi drafta (z kontrolą limitu LITE/PRO/ADMIN)."""
    _require_draft_plan(current_user)
    db = request.app.database
    lid = _oid_or_400(league_id, "ligi")
    league = await db["draft_leagues"].find_one({"_id": lid})
    if not league:
        raise HTTPException(status_code=404, detail="Nie znaleziono ligi.")
    if league.get("status") != "LOBBY":
        raise HTTPException(status_code=400, detail="Draft w tej lidze już się rozpoczął.")

    uid = str(current_user["_id"])
    participants = list(league.get("participants") or [])
    p_idx = next((i for i, p in enumerate(participants) if p.get("user_id") == uid), None)
    if p_idx is None:
        raise HTTPException(status_code=403, detail="Nie masz zaproszenia do tej ligi.")

    action = payload.action.strip().lower()
    if action == "accept":
        was_accepted = participants[p_idx].get("status") == "ACCEPTED"
        if not was_accepted:
            eligible, reason, _ = await check_user_draft_eligibility(db, current_user)
            if not eligible:
                raise HTTPException(status_code=403, detail=reason)
        participants[p_idx]["status"] = "ACCEPTED"
        order_ids = list(league.get("draft_order_user_ids") or [])
        if uid not in order_ids:
            order_ids.append(uid)
        participants[p_idx]["draft_order"] = order_ids.index(uid)
        await db["draft_leagues"].update_one(
            {"_id": lid},
            {"$set": {"participants": participants, "draft_order_user_ids": order_ids, "updated_at": _now_iso()}},
        )
        if not was_accepted:
            inviter_id = str(participants[p_idx].get("invited_by") or league.get("creator_id") or "")
            if inviter_id and inviter_id != uid:
                await create_notification(
                    db=db,
                    user_id=inviter_id,
                    notif_type="DRAFT_INVITE_ACCEPTED",
                    title="Zaproszenie zaakceptowane!",
                    message=f"{current_user.get('name') or 'Gracz'} przyjął zaproszenie do ligi '{league.get('name')}'.",
                    league_id=str(lid),
                )
    elif action == "decline":
        if league.get("creator_id") == uid:
            raise HTTPException(status_code=400, detail="Założyciel nie może odrzucić własnej ligi.")
        participants = [p for p in participants if p.get("user_id") != uid]
        order_ids = [x for x in (league.get("draft_order_user_ids") or []) if x != uid]
        await db["draft_leagues"].update_one(
            {"_id": lid},
            {"$set": {"participants": participants, "draft_order_user_ids": order_ids, "updated_at": _now_iso()}},
        )
    else:
        raise HTTPException(status_code=400, detail="Nieznana akcja (dozwolone: accept, decline).")

    await mark_turn_notifications_read(db, uid, str(lid))
    fresh = await db["draft_leagues"].find_one({"_id": lid})
    publish_league_refresh_to_participants(fresh)
    standings_data = await calculate_league_standings(db, fresh)
    return serialize_league(fresh, standings_data)


@router.post("/leagues/{league_id}/leave")
@limiter.limit("20/minute")
async def leave_draft_league(
    league_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Pozwala zaakceptowanemu uczestnikowi (innemu niż założyciel) dobrowolnie opuścić ligę drafta."""
    _require_draft_plan(current_user)
    db = request.app.database
    lid = _oid_or_400(league_id, "ligi")
    league = await db["draft_leagues"].find_one({"_id": lid})
    if not league:
        raise HTTPException(status_code=404, detail="Nie znaleziono ligi.")

    uid = str(current_user["_id"])
    if str(league.get("creator_id")) == uid:
        raise HTTPException(
            status_code=400,
            detail="Założyciel nie może opuścić własnej ligi — możesz ją usunąć.",
        )

    participants = list(league.get("participants") or [])
    leaving = next((p for p in participants if p.get("user_id") == uid), None)
    if not leaving:
        raise HTTPException(status_code=403, detail="Nie jesteś uczestnikiem tej ligi.")

    new_participants = [p for p in participants if p.get("user_id") != uid]
    accepted_remaining = [p for p in new_participants if p.get("status") == "ACCEPTED"]
    accepted_ids = {p["user_id"] for p in accepted_remaining}
    new_order_ids = [x for x in (league.get("draft_order_user_ids") or []) if x in accepted_ids]
    for p in accepted_remaining:
        if p["user_id"] not in new_order_ids:
            new_order_ids.append(p["user_id"])

    order_map = {u: idx for idx, u in enumerate(new_order_ids)}
    for p in new_participants:
        if p.get("status") == "ACCEPTED" and p["user_id"] in order_map:
            p["draft_order"] = order_map[p["user_id"]]

    now = _now_iso()
    updates: dict = {
        "participants": new_participants,
        "draft_order_user_ids": new_order_ids,
        "updated_at": now,
    }

    curr_status = str(league.get("status") or "LOBBY")
    if curr_status == "ACTIVE_DRAFT":
        picks_per_player = int(league.get("picks_per_player") or DEFAULT_PICKS_PER_PLAYER)
        if len(new_order_ids) < 2:
            for p in new_participants:
                p["picks"] = []
            updates["status"] = "LOBBY"
            updates["current_pick_index"] = 0
            updates["current_turn_user_id"] = None
        else:
            total_done = sum(len(p.get("picks") or []) for p in accepted_remaining)
            total_picks = len(new_order_ids) * picks_per_player
            if total_done >= total_picks:
                updates["status"] = "DRAFT_FINISHED"
                updates["current_pick_index"] = total_picks
                updates["current_turn_user_id"] = None
                updates["completed_at"] = now
            else:
                next_uid = get_snake_turn_user_id(new_order_ids, total_done)
                updates["current_pick_index"] = total_done
                updates["current_turn_user_id"] = next_uid

    await db["draft_leagues"].update_one({"_id": lid}, {"$set": updates})
    await mark_turn_notifications_read(db, uid, str(lid))

    creator_id = str(league.get("creator_id") or "")
    if creator_id and creator_id != uid:
        await create_notification(
            db=db,
            user_id=creator_id,
            notif_type="DRAFT_LEFT",
            title="Gracz opuścił ligę",
            message=f"{current_user.get('name') or 'Gracz'} opuścił ligę '{league.get('name')}'.",
            league_id=str(lid),
        )

    fresh = await db["draft_leagues"].find_one({"_id": lid})
    publish_league_refresh_to_participants(fresh)
    return {"ok": True, "message": "Opuszczono ligę drafta."}


@router.delete("/leagues/{league_id}/participants/{target_user_id}")
@limiter.limit("30/minute")
async def remove_participant_from_lobby(
    league_id: str,
    target_user_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Usuwa uczestnika lub cofnięte zaproszenie w lobby przed startem drafta."""
    _require_draft_plan(current_user)
    db = request.app.database
    lid = _oid_or_400(league_id, "ligi")
    league = await db["draft_leagues"].find_one({"_id": lid})
    if not league:
        raise HTTPException(status_code=404, detail="Nie znaleziono ligi.")
    uid = str(current_user["_id"])
    if league.get("creator_id") != uid:
        raise HTTPException(status_code=403, detail="Tylko założyciel ligi może usuwać graczy.")
    if league.get("status") != "LOBBY":
        raise HTTPException(status_code=400, detail="Nie można usuwać graczy po starcie drafta.")
    if target_user_id == uid:
        raise HTTPException(status_code=400, detail="Założyciel nie może usunąć samego siebie.")

    participants = [p for p in (league.get("participants") or []) if p.get("user_id") != target_user_id]
    order_ids = [x for x in (league.get("draft_order_user_ids") or []) if x != target_user_id]
    await db["draft_leagues"].update_one(
        {"_id": lid},
        {"$set": {"participants": participants, "draft_order_user_ids": order_ids, "updated_at": _now_iso()}},
    )
    fresh = await db["draft_leagues"].find_one({"_id": lid})
    publish_league_refresh_to_participants(fresh)
    standings_data = await calculate_league_standings(db, fresh)
    return serialize_league(fresh, standings_data)


@router.post("/leagues/{league_id}/start")
@limiter.limit("20/minute")
async def start_draft_league(
    league_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Rozpoczyna DRAFT (kolejność Snake, blokuje ustawienia i wysyła powiadomienie pierwszemu graczowi)."""
    _require_draft_plan(current_user)
    db = request.app.database
    lid = _oid_or_400(league_id, "ligi")
    league = await db["draft_leagues"].find_one({"_id": lid})
    if not league:
        raise HTTPException(status_code=404, detail="Nie znaleziono ligi.")

    uid = str(current_user["_id"])
    if league.get("creator_id") != uid:
        raise HTTPException(status_code=403, detail="Tylko założyciel ligi może kliknąć START.")
    if league.get("status") != "LOBBY":
        raise HTTPException(status_code=400, detail="Draft został już uruchomiony.")

    accepted = [p for p in (league.get("participants") or []) if p.get("status") == "ACCEPTED"]
    if len(accepted) < 2:
        raise HTTPException(status_code=400, detail="Do rozpoczęcia drafta potrzeba minimum 2 zaakceptowanych graczy.")

    accepted_ids = [p["user_id"] for p in accepted]
    existing_order = [x for x in (league.get("draft_order_user_ids") or []) if x in accepted_ids]
    for x in accepted_ids:
        if x not in existing_order:
            existing_order.append(x)

    if str(league.get("order_mode") or "CUSTOM").upper() == "RANDOM":
        final_order = make_random_order(existing_order)
    else:
        final_order = existing_order

    order_map = {u: idx for idx, u in enumerate(final_order)}
    for p in accepted:
        p["draft_order"] = order_map[p["user_id"]]
        p["picks"] = []
    accepted.sort(key=lambda x: x["draft_order"])

    first_user_id = get_snake_turn_user_id(final_order, 0)
    picks_per_player = int(league.get("picks_per_player") or DEFAULT_PICKS_PER_PLAYER)
    now = _now_iso()
    await db["draft_leagues"].update_one(
        {"_id": lid},
        {
            "$set": {
                "status": "ACTIVE_DRAFT",
                "participants": accepted,
                "draft_order_user_ids": final_order,
                "current_pick_index": 0,
                "current_turn_user_id": first_user_id,
                "started_at": now,
                "updated_at": now,
            }
        },
    )

    if first_user_id:
        first_participant = next((p for p in accepted if p["user_id"] == first_user_id), None)
        first_name = (first_participant or {}).get("name") or "Gracz"
        await create_notification(
            db=db,
            user_id=first_user_id,
            notif_type="DRAFT_TURN",
            title="Twoja kolej w Drafcie!",
            message=f"Draft w lidze '{league.get('name')}' wystartował! Wybierz 1. zawodnika (Runda 1/{picks_per_player}).",
            league_id=str(lid),
        )
        for p in accepted:
            if p["user_id"] != first_user_id:
                await create_notification(
                    db=db,
                    user_id=p["user_id"],
                    notif_type="DRAFT_STARTED",
                    title="Draft rozpoczęty!",
                    message=f"Gospodarz rozpoczął draft w lidze '{league.get('name')}'. Jako pierwszy wybiera {first_name}.",
                    league_id=str(lid),
                )

    fresh = await db["draft_leagues"].find_one({"_id": lid})
    publish_league_refresh_to_participants(fresh)
    standings_data = await calculate_league_standings(db, fresh)
    return serialize_league(fresh, standings_data)


@router.post("/leagues/{league_id}/pick")
@limiter.limit("60/minute")
async def make_draft_pick(
    league_id: str,
    payload: MakePickRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Zatwierdza wybór 1 aktywnego zawodnika w kolejności Snake i powiadamia kolejnego gracza."""
    _require_draft_plan(current_user)
    db = request.app.database
    lid = _oid_or_400(league_id, "ligi")
    league = await db["draft_leagues"].find_one({"_id": lid})
    if not league:
        raise HTTPException(status_code=404, detail="Nie znaleziono ligi.")
    if league.get("status") != "ACTIVE_DRAFT":
        raise HTTPException(status_code=400, detail="Ta liga nie jest obecnie w trakcie drafta.")

    uid = str(current_user["_id"])
    if league.get("current_turn_user_id") != uid:
        raise HTTPException(status_code=403, detail="Teraz nie jest Twoja kolej wyboru w tym drafcie.")

    athlete = await db["athletes"].find_one(
        {"fis_code": payload.fis_code, "status": "Active"},
        {"fis_code": 1, "full_name": 1, "country_code": 1, "status": 1, "competitor_id_from_url": 1},
    )
    if not athlete:
        raise HTTPException(status_code=400, detail="Wybrany zawodnik nie istnieje lub nie ma statusu aktywnego (Active).")

    participants = list(league.get("participants") or [])
    for p in participants:
        for pk in p.get("picks") or []:
            if int(pk.get("fis_code")) == int(payload.fis_code):
                raise HTTPException(status_code=400, detail=f"Zawodnik {athlete.get('full_name')} został już wybrany przez gracza {p.get('name')}.")

    order_ids = league.get("draft_order_user_ids") or []
    n_players = len(order_ids)
    picks_per_player = int(league.get("picks_per_player") or DEFAULT_PICKS_PER_PLAYER)
    curr_idx = int(league.get("current_pick_index") or 0)
    round_num = (curr_idx // n_players) + 1
    pick_num = curr_idx + 1

    now = _now_iso()
    for p in participants:
        if p.get("user_id") == uid:
            p.setdefault("picks", []).append({
                "round": round_num,
                "pick_number": pick_num,
                "fis_code": athlete["fis_code"],
                "full_name": athlete.get("full_name") or str(athlete["fis_code"]),
                "country_code": athlete.get("country_code") or "",
                "competitor_id_from_url": athlete.get("competitor_id_from_url"),
                "picked_at": now,
            })
            break

    await mark_turn_notifications_read(db, uid, str(lid))

    next_idx = curr_idx + 1
    total_picks = n_players * picks_per_player

    if next_idx >= total_picks:
        await db["draft_leagues"].update_one(
            {"_id": lid},
            {
                "$set": {
                    "status": "DRAFT_FINISHED",
                    "participants": participants,
                    "current_pick_index": next_idx,
                    "current_turn_user_id": None,
                    "completed_at": now,
                    "updated_at": now,
                }
            },
        )
        for p in participants:
            await create_notification(
                db=db,
                user_id=p["user_id"],
                notif_type="DRAFT_COMPLETED",
                title="Draft zakończony!",
                message=f"Wszyscy gracze w lidze '{league.get('name')}' wybrali po {picks_per_player} zawodników. Gospodarz może teraz kliknąć 'Rozpocznij Ligę'.",
                league_id=str(lid),
            )
    else:
        next_user_id = get_snake_turn_user_id(order_ids, next_idx)
        next_round = (next_idx // n_players) + 1
        await db["draft_leagues"].update_one(
            {"_id": lid},
            {
                "$set": {
                    "participants": participants,
                    "current_pick_index": next_idx,
                    "current_turn_user_id": next_user_id,
                    "updated_at": now,
                }
            },
        )
        if next_user_id:
            await create_notification(
                db=db,
                user_id=next_user_id,
                notif_type="DRAFT_TURN",
                title="Twoja kolej w Drafcie!",
                message=f"W lidze '{league.get('name')}' nadszedł Twój ruch (Runda {next_round}/{picks_per_player}). Ostatni wybór: {athlete.get('full_name')}.",
                league_id=str(lid),
            )

    fresh = await db["draft_leagues"].find_one({"_id": lid})
    publish_league_refresh_to_participants(fresh)
    standings_data = await calculate_league_standings(db, fresh)
    return serialize_league(fresh, standings_data)


@router.post("/leagues/{league_id}/launch-league")
@limiter.limit("20/minute")
async def launch_draft_season_league(
    league_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Gospodarz klika 'Rozpocznij Ligę' po zakończeniu drafta — otwiera oficjalną tabelę klasyfikacji generalnej."""
    _require_draft_plan(current_user)
    db = request.app.database
    lid = _oid_or_400(league_id, "ligi")
    league = await db["draft_leagues"].find_one({"_id": lid})
    if not league:
        raise HTTPException(status_code=404, detail="Nie znaleziono ligi.")

    uid = str(current_user["_id"])
    if league.get("creator_id") != uid:
        raise HTTPException(status_code=403, detail="Tylko gospodarz (twórca ligi) może rozpocząć ligę.")

    curr_status = league.get("status")
    is_finished = curr_status == "DRAFT_FINISHED" or (curr_status == "COMPLETED" and not league.get("league_started_at"))
    if not is_finished:
        raise HTTPException(status_code=400, detail="Ligę można rozpocząć dopiero po zakończeniu wszystkich wyborów w drafcie.")

    now = _now_iso()
    await db["draft_leagues"].update_one(
        {"_id": lid},
        {"$set": {"status": "LEAGUE_ACTIVE", "league_started_at": now, "updated_at": now}},
    )

    for p in league.get("participants") or []:
        if p.get("status") == "ACCEPTED" and p.get("user_id"):
            await create_notification(
                db=db,
                user_id=p["user_id"],
                notif_type="LEAGUE_STARTED",
                title="Liga Drafta rozpoczęta!",
                message=f"Liga '{league.get('name')}' wystartowała! Tabela klasyfikacji generalnej jest już aktywna.",
                league_id=str(lid),
            )

    fresh = await db["draft_leagues"].find_one({"_id": lid})
    publish_league_refresh_to_participants(fresh)
    standings_data = await calculate_league_standings(db, fresh)
    if (standings_data or {}).get("counted_competitions"):
        claimed = await db["draft_leagues"].find_one_and_update(
            {"_id": lid, "season_started_notified": {"$ne": True}},
            {"$set": {"season_started_notified": True}},
        )
        if claimed:
            first_c = standings_data["counted_competitions"][0]
            for p in fresh.get("participants") or []:
                if p.get("status") == "ACCEPTED" and p.get("user_id"):
                    await create_notification(
                        db=db,
                        user_id=p["user_id"],
                        notif_type="SEASON_STARTED",
                        title="Start punktowania sezonu!",
                        message=f"Sezon w lidze '{fresh.get('name')}' jest już w grze — pierwsze wliczone zawody: {first_c.get('event_place')} ({first_c.get('date')}).",
                        league_id=str(lid),
                    )
            fresh = await db["draft_leagues"].find_one({"_id": lid})
    return serialize_league(fresh, standings_data)


@router.delete("/leagues/{league_id}")
@limiter.limit("20/minute")
async def delete_draft_league(
    league_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Usuwa ligę drafta (tylko założyciel lub administrator)."""
    _require_draft_plan(current_user)
    db = request.app.database
    lid = _oid_or_400(league_id, "ligi")
    league = await db["draft_leagues"].find_one({"_id": lid})
    if not league:
        raise HTTPException(status_code=404, detail="Nie znaleziono ligi.")

    uid = str(current_user["_id"])
    if league.get("creator_id") != uid and str(current_user.get("role") or "").upper() != Role.ADMIN:
        raise HTTPException(status_code=403, detail="Tylko założyciel ligi lub administrator może ją usunąć.")

    publish_league_refresh_to_participants(league, event_kind="league_deleted")
    await db["draft_leagues"].delete_one({"_id": lid})
    await db["notifications"].delete_many({"league_id": str(lid)})
    return {"message": "Usunięto ligę drafta."}
