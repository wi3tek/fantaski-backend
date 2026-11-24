from typing import Optional, List
from fastapi import APIRouter, Request, HTTPException, Depends, status
from pydantic import BaseModel
from bson import ObjectId

from app.services.auth_service import (
    verify_google_token,
    get_or_create_user,
    create_access_token,
    Role
)
from app.auth import get_current_user, require_role
from app.config import settings
from app.infrastructure.limiter import limiter

router = APIRouter(prefix="/auth", tags=["Authentication & RBAC"])

class GoogleLoginRequest(BaseModel):
    id_token: str

class DevLoginRequest(BaseModel):
    email: str
    name: Optional[str] = None

class UpdateRoleRequest(BaseModel):
    role: str

class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    picture: Optional[str] = ""
    role: str
    is_active: bool = True
    created_at: Optional[str] = None
    last_login: Optional[str] = None

def serialize_user(user: dict) -> dict:
    created = user.get("created_at")
    last_login = user.get("last_login")
    return {
        "id": str(user.get("_id", user.get("id"))),
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "picture": user.get("picture", ""),
        "role": user.get("role", Role.USER),
        "is_active": user.get("is_active", True),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created) if created else None,
        "last_login": last_login.isoformat() if hasattr(last_login, "isoformat") else str(last_login) if last_login else None
    }

@router.post("/google")
@limiter.limit("15/minute")
async def login_with_google(req: GoogleLoginRequest, request: Request):
    """Logowanie i rejestracja przez Google OAuth (ID Token)."""
    user_info = await verify_google_token(req.id_token)
    if not user_info:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nieprawidłowy lub wygasły Google ID Token."
        )

    db = request.app.database
    user = await get_or_create_user(db, user_info)

    token_payload = {
        "sub": str(user["_id"]),
        "email": user["email"],
        "name": user["name"],
        "picture": user.get("picture", ""),
        "role": user.get("role", Role.USER)
    }
    access_token = create_access_token(token_payload)

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": serialize_user(user)
    }

@router.post("/dev-login")
@limiter.limit("15/minute")
async def dev_login(req: DevLoginRequest, request: Request):
    """
    Logowanie deweloperskie (ułatwia testowanie bez konieczności konfiguracji Google Client ID w dev).
    Automatycznie przydziela uprawnienia administracyjne dla adresu wskazanego w INITIAL_ADMIN_EMAIL.
    """
    if getattr(settings, "ENVIRONMENT", "development") == "production" and not getattr(settings, "ENABLE_DEV_LOGIN", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Logowanie testowe jest wyłączone na środowisku produkcyjnym."
        )
    db = request.app.database
    user_info = {
        "sub": f"dev_{req.email}",
        "email": req.email,
        "name": req.name or req.email.split("@")[0],
        "picture": f"https://api.dicebear.com/7.x/identicon/svg?seed={req.email}",
        "email_verified": True
    }
    user = await get_or_create_user(db, user_info)

    token_payload = {
        "sub": str(user["_id"]),
        "email": user["email"],
        "name": user["name"],
        "picture": user.get("picture", ""),
        "role": user.get("role", Role.USER)
    }
    access_token = create_access_token(token_payload)

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": serialize_user(user)
    }

@router.get("/me")
async def get_my_profile(current_user: dict = Depends(get_current_user)):
    """Pobiera dane zalogowanego użytkownika i jego aktualną rolę."""
    return serialize_user(current_user)

@router.get("/users")
async def list_users(
    request: Request,
    admin: dict = Depends(require_role(Role.ADMIN))
):
    """Lista wszystkich użytkowników w systemie (dostęp tylko dla ADMIN)."""
    db = request.app.database
    users = []
    async for u in db["users"].find().sort("created_at", -1):
        users.append(serialize_user(u))
    return users

@router.put("/users/{user_id}/role")
async def update_user_role(
    user_id: str,
    req: UpdateRoleRequest,
    request: Request,
    admin: dict = Depends(require_role(Role.ADMIN))
):
    """Zmiana roli użytkownika (dostęp tylko dla ADMIN)."""
    new_role = req.role.strip().upper()
    if new_role not in Role.ALL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Nieprawidłowa rola. Dostępne role: {Role.ALL}"
        )

    db = request.app.database
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Nieprawidłowy format ID użytkownika.")

    target_user = await db["users"].find_one({"_id": oid})
    if not target_user:
        raise HTTPException(status_code=404, detail="Użytkownik nie został znaleziony.")

    # Zabezpieczenie przed odebraniem roli głównemu administratorowi
    if settings.INITIAL_ADMIN_EMAIL and target_user.get("email", "").lower() == settings.INITIAL_ADMIN_EMAIL.lower() and new_role != Role.ADMIN:
        raise HTTPException(
            status_code=400,
            detail="Nie można zmienić uprawnień głównego konta technicznego."
        )

    await db["users"].update_one({"_id": oid}, {"$set": {"role": new_role}})
    target_user["role"] = new_role
    return serialize_user(target_user)
