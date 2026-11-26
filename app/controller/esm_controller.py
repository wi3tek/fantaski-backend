from typing import Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.auth import get_current_user, require_role
from app.infrastructure.limiter import limiter
from app.services.auth_service import Role
from app.services.esm_user_service import (
    DEFAULT_ESM_LEAGUE_ID,
    build_user_esm_status_payload,
    sync_team_for_user_doc,
)

router = APIRouter(prefix="/esm", tags=["esmManager & FantaSKI Friends"])


class AdminAssignEsmRequest(BaseModel):
    team_id: str
    league_id: Optional[str] = DEFAULT_ESM_LEAGUE_ID


def _oid_or_400(raw_id: str, label: str = "użytkownika") -> ObjectId:
    try:
        return ObjectId(str(raw_id).strip())
    except Exception:
        raise HTTPException(status_code=400, detail=f"Nieprawidłowy identyfikator {label}.")


@router.get("/status")
@limiter.limit("60/minute")
async def get_my_esm_status(request: Request, current_user: dict = Depends(get_current_user)):
    """Zwraca status integracji ESM przypisanej przez admina oraz listę znajomych FantaSKI i zaproszeń."""
    db = request.app.database
    countries_dict = (getattr(request.app.state, "dicts", {}) or {}).get("countries", {})
    return await build_user_esm_status_payload(
        db=db,
        user_doc=current_user,
        countries_dict=countries_dict,
        refresh_friends_live=False,
    )


@router.post("/sync")
@limiter.limit("20/minute")
async def sync_my_and_friends_esm(request: Request, current_user: dict = Depends(get_current_user)):
    """Na żądanie odświeża z esmManager skład użytkownika (jeśli przypisany) oraz składy zaakceptowanych znajomych."""
    db = request.app.database
    countries_dict = (getattr(request.app.state, "dicts", {}) or {}).get("countries", {})
    existing = current_user.get("esm_integration") or {}

    if not existing.get("team_id") and not (current_user.get("fantaski_friends") or []):
        raise HTTPException(
            status_code=400,
            detail="Konto nie ma jeszcze przypisanego ID drużyny ESM przez administratora ani zaakceptowanych znajomych.",
        )

    try:
        if existing.get("team_id"):
            updated_esm = await sync_team_for_user_doc(
                db=db,
                user_doc=current_user,
                countries_dict=countries_dict,
                team_id=existing.get("team_id"),
                league_id=existing.get("league_id"),
                strict_league_check=False,
            )
            current_user["esm_integration"] = updated_esm

        status_payload = await build_user_esm_status_payload(
            db=db,
            user_doc=current_user,
            countries_dict=countries_dict,
            refresh_friends_live=True,
        )
        return {"message": "Składy z esmManager zostały zaktualizowane.", **status_payload}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Błąd komunikacji z esmManager: {e}")


# =========================================================
# PANEL ADMINA: PRZYPISYWANIE ESM TEAM ID DO UŻYTKOWNIKÓW
# =========================================================

@router.put("/admin/users/{user_id}")
@limiter.limit("30/minute")
async def admin_assign_esm_team(
    user_id: str,
    payload: AdminAssignEsmRequest,
    request: Request,
    admin: dict = Depends(require_role(Role.ADMIN)),
):
    """Administrator przypisuje lub aktualizuje ESM Team ID wskazanego użytkownika FantaSKI."""
    clean_tid = payload.team_id.strip()
    if not clean_tid:
        raise HTTPException(status_code=400, detail="Podaj poprawne ESM Team ID drużyny.")

    db = request.app.database
    countries_dict = (getattr(request.app.state, "dicts", {}) or {}).get("countries", {})
    target_oid = _oid_or_400(user_id)
    target_user = await db["users"].find_one({"_id": target_oid})
    if not target_user:
        raise HTTPException(status_code=404, detail="Nie znaleziono użytkownika FantaSKI.")

    try:
        esm_doc = await sync_team_for_user_doc(
            db=db,
            user_doc=target_user,
            countries_dict=countries_dict,
            team_id=clean_tid,
            league_id=(payload.league_id or DEFAULT_ESM_LEAGUE_ID).strip(),
            assigned_by=admin.get("email"),
            strict_league_check=True,
        )
        my_team = esm_doc.get("my_team") or {}
        return {
            "message": f"Przypisano drużynę ESM '{my_team.get('team_name')}' do konta {target_user.get('email')}.",
            "user_id": str(target_oid),
            "esm_summary": {
                "connected": True,
                "team_id": esm_doc.get("team_id"),
                "league_id": esm_doc.get("league_id"),
                "team_name": my_team.get("team_name"),
                "value": my_team.get("value"),
                "total_points": my_team.get("total_points"),
                "last_synced_at": esm_doc.get("last_synced_at"),
            },
        }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Błąd pobierania drużyny z esmManager: {e}")


@router.delete("/admin/users/{user_id}")
@limiter.limit("30/minute")
async def admin_unlink_esm_team(
    user_id: str,
    request: Request,
    admin: dict = Depends(require_role(Role.ADMIN)),
):
    """Administrator odpina drużynę ESM od wskazanego użytkownika FantaSKI."""
    db = request.app.database
    target_oid = _oid_or_400(user_id)
    res = await db["users"].update_one({"_id": target_oid}, {"$unset": {"esm_integration": ""}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Nie znaleziono użytkownika.")
    return {"message": "Odpięto integrację ESM od wybranego użytkownika.", "user_id": str(target_oid)}


# =========================================================
# ZNAJOMI WEWNĄTRZ FANTASKI (SYSTEM ZAPROSZEŃ + PRYWATNOŚĆ)
# =========================================================

@router.get("/friends/directory")
@limiter.limit("40/minute")
async def search_fantaski_players_directory(
    request: Request,
    q: str = Query(default="", max_length=80),
    current_user: dict = Depends(get_current_user),
):
    """
    Zwraca listę graczy FantaSKI z podpiętym ESM (bez adresów e-mail!),
    umożliwiając wysłanie zaproszenia do znajomych.
    """
    db = request.app.database
    me_id = str(current_user["_id"])
    friends_set = set(str(x) for x in (current_user.get("fantaski_friends") or []))
    in_set = set(str(x) for x in (current_user.get("friend_requests_in") or []))
    out_set = set(str(x) for x in (current_user.get("friend_requests_out") or []))

    query_filter = {
        "_id": {"$ne": _oid_or_400(me_id)},
        "is_active": {"$ne": False},
        "esm_integration.connected": True,
        "esm_integration.team_id": {"$exists": True, "$ne": None},
    }
    cursor = db["users"].find(query_filter, {"name": 1, "picture": 1, "esm_integration": 1}).sort("name", 1)

    needle = q.strip().lower()
    results = []
    async for doc in cursor:
        uid = str(doc["_id"])
        esm = doc.get("esm_integration") or {}
        team = esm.get("my_team") or {}
        u_name = doc.get("name") or "Gracz FantaSKI"
        t_name = team.get("team_name") or ""

        if needle and needle not in u_name.lower() and needle not in t_name.lower():
            continue

        if uid in friends_set:
            relation = "friend"
        elif uid in in_set:
            relation = "incoming"
        elif uid in out_set:
            relation = "outgoing"
        else:
            relation = "none"

        results.append({
            "user_id": uid,
            "name": u_name,
            "picture": doc.get("picture") or "",
            "team_name": t_name or "Drużyna ESM",
            "value": team.get("value"),
            "total_points": team.get("total_points"),
            "relation": relation,
        })

    return results[:50]


@router.post("/friends/request/{target_user_id}")
@limiter.limit("30/minute")
async def send_friend_request(
    target_user_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Wysyła zaproszenie do znajomych w FantaSKI (lub akceptuje od razu, jeśli druga strona już zaprosiła)."""
    me_id = str(current_user["_id"])
    target_oid = _oid_or_400(target_user_id, "adresata")
    target_id = str(target_oid)
    if target_id == me_id:
        raise HTTPException(status_code=400, detail="Nie możesz zaprosić samego siebie.")

    db = request.app.database
    target_user = await db["users"].find_one({"_id": target_oid, "is_active": {"$ne": False}})
    if not target_user:
        raise HTTPException(status_code=404, detail="Wybrany użytkownik nie istnieje.")

    if target_id in (current_user.get("fantaski_friends") or []):
        raise HTTPException(status_code=400, detail="Ten gracz jest już na Twojej liście znajomych.")

    me_oid = _oid_or_400(me_id)

    # Jeśli adresat już wysłał nam zaproszenie — od razu akceptujemy obustronnie
    if target_id in (current_user.get("friend_requests_in") or []):
        await db["users"].update_one(
            {"_id": me_oid},
            {"$pull": {"friend_requests_in": target_id}, "$addToSet": {"fantaski_friends": target_id}},
        )
        await db["users"].update_one(
            {"_id": target_oid},
            {"$pull": {"friend_requests_out": me_id}, "$addToSet": {"fantaski_friends": me_id}},
        )
        updated_me = await db["users"].find_one({"_id": me_oid})
        return {
            "message": f"Zaakceptowano zaproszenie od gracza {target_user.get('name')}!",
            **(await build_user_esm_status_payload(db, updated_me)),
        }

    await db["users"].update_one({"_id": me_oid}, {"$addToSet": {"friend_requests_out": target_id}})
    await db["users"].update_one({"_id": target_oid}, {"$addToSet": {"friend_requests_in": me_id}})
    updated_me = await db["users"].find_one({"_id": me_oid})
    return {
        "message": f"Wysłano zaproszenie do gracza {target_user.get('name')}.",
        **(await build_user_esm_status_payload(db, updated_me)),
    }


@router.post("/friends/accept/{requester_user_id}")
@limiter.limit("30/minute")
async def accept_friend_request(
    requester_user_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Akceptuje przychodzące zaproszenie do znajomych (nawiązuje obustronną relację)."""
    me_id = str(current_user["_id"])
    req_oid = _oid_or_400(requester_user_id, "zapraszającego")
    req_id = str(req_oid)

    if req_id not in (current_user.get("friend_requests_in") or []):
        raise HTTPException(status_code=400, detail="Brak aktywnego zaproszenia od tego użytkownika.")

    db = request.app.database
    me_oid = _oid_or_400(me_id)

    await db["users"].update_one(
        {"_id": me_oid},
        {"$pull": {"friend_requests_in": req_id, "friend_requests_out": req_id}, "$addToSet": {"fantaski_friends": req_id}},
    )
    await db["users"].update_one(
        {"_id": req_oid},
        {"$pull": {"friend_requests_out": me_id, "friend_requests_in": me_id}, "$addToSet": {"fantaski_friends": me_id}},
    )

    updated_me = await db["users"].find_one({"_id": me_oid})
    return {
        "message": "Zaproszenie zostało zaakceptowane! Od teraz możecie porównywać swoje składy.",
        **(await build_user_esm_status_payload(db, updated_me)),
    }


@router.post("/friends/reject/{requester_user_id}")
@limiter.limit("30/minute")
async def reject_friend_request(
    requester_user_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Odrzuca przychodzące zaproszenie do znajomych."""
    me_id = str(current_user["_id"])
    req_oid = _oid_or_400(requester_user_id, "zapraszającego")
    req_id = str(req_oid)

    db = request.app.database
    me_oid = _oid_or_400(me_id)
    await db["users"].update_one({"_id": me_oid}, {"$pull": {"friend_requests_in": req_id}})
    await db["users"].update_one({"_id": req_oid}, {"$pull": {"friend_requests_out": me_id}})

    updated_me = await db["users"].find_one({"_id": me_oid})
    return {
        "message": "Odrzucono zaproszenie.",
        **(await build_user_esm_status_payload(db, updated_me)),
    }


@router.delete("/friends/request/{target_user_id}")
@limiter.limit("30/minute")
async def cancel_friend_request(
    target_user_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Anuluje wysłane przez siebie zaproszenie do znajomych."""
    me_id = str(current_user["_id"])
    target_oid = _oid_or_400(target_user_id, "adresata")
    target_id = str(target_oid)

    db = request.app.database
    me_oid = _oid_or_400(me_id)
    await db["users"].update_one({"_id": me_oid}, {"$pull": {"friend_requests_out": target_id}})
    await db["users"].update_one({"_id": target_oid}, {"$pull": {"friend_requests_in": me_id}})

    updated_me = await db["users"].find_one({"_id": me_oid})
    return {
        "message": "Anulowano zaproszenie.",
        **(await build_user_esm_status_payload(db, updated_me)),
    }


@router.delete("/friends/{friend_user_id}")
@limiter.limit("30/minute")
async def remove_fantaski_friend(
    friend_user_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Usuwa obustronną znajomość w FantaSKI."""
    me_id = str(current_user["_id"])
    fr_oid = _oid_or_400(friend_user_id, "znajomego")
    fr_id = str(fr_oid)

    db = request.app.database
    me_oid = _oid_or_400(me_id)
    await db["users"].update_one({"_id": me_oid}, {"$pull": {"fantaski_friends": fr_id}})
    await db["users"].update_one({"_id": fr_oid}, {"$pull": {"fantaski_friends": me_id}})

    updated_me = await db["users"].find_one({"_id": me_oid})
    return {
        "message": "Usunięto ze znajomych.",
        **(await build_user_esm_status_payload(db, updated_me)),
    }
