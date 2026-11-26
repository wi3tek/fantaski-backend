import datetime
import uuid
from typing import Any, Dict, List, Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth import get_current_user, require_role
from app.infrastructure.limiter import limiter
from app.services.auth_service import Role
from app.services.draft_service import create_notification

router = APIRouter(prefix="/support", tags=["Support & Admin Messenger"])


class SendSupportMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1500)
    context: Optional[str] = Field(default=None, max_length=120)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _serialize_thread(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not doc:
        return {
            "id": None,
            "user_id": None,
            "user_name": "",
            "user_email": "",
            "user_picture": "",
            "unread_by_admin": 0,
            "unread_by_user": 0,
            "last_message_preview": "",
            "last_sender_role": None,
            "updated_at": None,
            "messages": [],
        }
    return {
        "id": str(doc.get("_id") or ""),
        "user_id": str(doc.get("user_id") or ""),
        "user_name": doc.get("user_name") or "Gracz",
        "user_email": doc.get("user_email") or "",
        "user_picture": doc.get("user_picture") or "",
        "unread_by_admin": int(doc.get("unread_by_admin") or 0),
        "unread_by_user": int(doc.get("unread_by_user") or 0),
        "last_message_preview": doc.get("last_message_preview") or "",
        "last_sender_role": doc.get("last_sender_role"),
        "updated_at": doc.get("updated_at"),
        "messages": list(doc.get("messages") or []),
    }


@router.get("/my-thread")
@limiter.limit("60/minute")
async def get_my_support_thread(
    request: Request,
    mark_read: bool = Query(default=False),
    current_user: dict = Depends(get_current_user),
):
    """Zwraca wątek rozmowy zalogowanego użytkownika z administratorem."""
    db = request.app.database
    uid = str(current_user["_id"])
    thread = await db["support_threads"].find_one({"user_id": uid})

    if thread and mark_read and int(thread.get("unread_by_user") or 0) > 0:
        await db["support_threads"].update_one(
            {"_id": thread["_id"]},
            {"$set": {"unread_by_user": 0}},
        )
        thread["unread_by_user"] = 0
        await db["notifications"].update_many(
            {"user_id": uid, "type": "SUPPORT_REPLY", "is_read": False},
            {"$set": {"is_read": True}},
        )

    out = _serialize_thread(thread)
    out["user_id"] = uid
    return out


@router.post("/messages")
@limiter.limit("20/minute")
async def send_message_to_admin(
    payload: SendSupportMessageRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Użytkownik wysyła wiadomość do administratora; administratorzy otrzymują powiadomienie."""
    clean_text = (payload.message or "").strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="Wiadomość nie może być pusta.")

    clean_ctx = (payload.context or "").strip() or None
    db = request.app.database
    uid = str(current_user["_id"])
    uname = current_user.get("name") or current_user.get("email") or "Gracz"
    uemail = current_user.get("email") or ""
    upic = current_user.get("picture") or ""
    now_iso = _now_iso()

    msg_obj = {
        "id": uuid.uuid4().hex[:12],
        "sender_id": uid,
        "sender_name": uname,
        "sender_role": "USER",
        "text": clean_text,
        "context": clean_ctx,
        "created_at": now_iso,
    }

    preview = clean_text if len(clean_text) <= 90 else f"{clean_text[:87]}..."
    await db["support_threads"].update_one(
        {"user_id": uid},
        {
            "$set": {
                "user_id": uid,
                "user_name": uname,
                "user_email": uemail,
                "user_picture": upic,
                "last_message_preview": preview,
                "last_sender_role": "USER",
                "updated_at": now_iso,
            },
            "$setOnInsert": {"created_at": now_iso, "unread_by_user": 0},
            "$inc": {"unread_by_admin": 1},
            "$push": {"messages": {"$each": [msg_obj], "$slice": -100}},
        },
        upsert=True,
    )

    admins = await db["users"].find(
        {"role": Role.ADMIN, "is_active": {"$ne": False}},
        {"_id": 1},
    ).to_list(length=20)

    notif_title = f"💬 Wiadomość od: {uname}"
    notif_msg = f"[{clean_ctx}] {preview}" if clean_ctx else preview
    for adm in admins:
        adm_id = str(adm["_id"])
        if adm_id == uid:
            continue
        await create_notification(
            db=db,
            user_id=adm_id,
            notif_type="SUPPORT_MESSAGE",
            title=notif_title,
            message=notif_msg,
            link=f"/admin?tab=messages&user_id={uid}",
        )

    fresh = await db["support_threads"].find_one({"user_id": uid})
    return _serialize_thread(fresh)


@router.get("/admin/threads")
@limiter.limit("60/minute")
async def list_support_threads_admin(
    request: Request,
    admin: dict = Depends(require_role(Role.ADMIN)),
):
    """Lista wszystkich rozmów z użytkownikami dla panelu administratora."""
    db = request.app.database
    docs = await db["support_threads"].find({}).sort([("updated_at", -1)]).to_list(length=200)
    threads = [_serialize_thread(d) for d in docs]
    total_unread = sum(t["unread_by_admin"] for t in threads)
    return {
        "unread_count": total_unread,
        "threads": threads,
    }


@router.post("/admin/threads/{user_id}/read")
@limiter.limit("60/minute")
async def mark_thread_read_by_admin(
    user_id: str,
    request: Request,
    admin: dict = Depends(require_role(Role.ADMIN)),
):
    """Oznacza wątek użytkownika jako przeczytany przez administratora."""
    db = request.app.database
    clean_uid = str(user_id).strip()
    await db["support_threads"].update_one(
        {"user_id": clean_uid},
        {"$set": {"unread_by_admin": 0}},
    )
    return {"ok": True, "user_id": clean_uid}


@router.post("/admin/threads/{user_id}/reply")
@limiter.limit("30/minute")
async def reply_to_user_thread(
    user_id: str,
    payload: SendSupportMessageRequest,
    request: Request,
    admin: dict = Depends(require_role(Role.ADMIN)),
):
    """Administrator odpowiada w wątku użytkownika; użytkownik otrzymuje powiadomienie."""
    clean_text = (payload.message or "").strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="Odpowiedź nie może być pusta.")

    clean_uid = str(user_id).strip()
    db = request.app.database
    thread = await db["support_threads"].find_one({"user_id": clean_uid})
    if not thread:
        # Pozwól też zainicjować wątek jeśli istnieje użytkownik
        try:
            u_doc = await db["users"].find_one({"_id": ObjectId(clean_uid)}, {"name": 1, "email": 1, "picture": 1})
        except Exception:
            u_doc = None
        if not u_doc:
            raise HTTPException(status_code=404, detail="Nie znaleziono wątku ani użytkownika.")
        base_name = u_doc.get("name") or u_doc.get("email") or "Gracz"
        base_email = u_doc.get("email") or ""
        base_pic = u_doc.get("picture") or ""
    else:
        base_name = thread.get("user_name") or "Gracz"
        base_email = thread.get("user_email") or ""
        base_pic = thread.get("user_picture") or ""

    now_iso = _now_iso()
    adm_id = str(admin["_id"])
    adm_name = admin.get("name") or "Administrator"

    msg_obj = {
        "id": uuid.uuid4().hex[:12],
        "sender_id": adm_id,
        "sender_name": adm_name,
        "sender_role": "ADMIN",
        "text": clean_text,
        "context": None,
        "created_at": now_iso,
    }
    preview = clean_text if len(clean_text) <= 90 else f"{clean_text[:87]}..."

    await db["support_threads"].update_one(
        {"user_id": clean_uid},
        {
            "$set": {
                "user_id": clean_uid,
                "user_name": base_name,
                "user_email": base_email,
                "user_picture": base_pic,
                "unread_by_admin": 0,
                "last_message_preview": preview,
                "last_sender_role": "ADMIN",
                "updated_at": now_iso,
            },
            "$setOnInsert": {"created_at": now_iso},
            "$inc": {"unread_by_user": 1},
            "$push": {"messages": {"$each": [msg_obj], "$slice": -100}},
        },
        upsert=True,
    )

    await create_notification(
        db=db,
        user_id=clean_uid,
        notif_type="SUPPORT_REPLY",
        title="💬 Odpowiedź od Administratora",
        message=preview,
        link="/profile?tab=messages",
    )

    fresh = await db["support_threads"].find_one({"user_id": clean_uid})
    return _serialize_thread(fresh)
