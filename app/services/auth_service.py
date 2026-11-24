import datetime
from datetime import timezone
from typing import Optional, Dict, Any, List
import jwt
import httpx
from bson import ObjectId
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from app.config import settings

class Role:
    USER = "USER"
    USER_LITE = "USER_LITE"
    USER_PRO = "USER_PRO"
    ADMIN = "ADMIN"
    ALL = [USER, USER_LITE, USER_PRO, ADMIN]

def create_access_token(data: dict, expires_delta: Optional[datetime.timedelta] = None) -> str:
    """Tworzy podpisany token JWT z payloadem użytkownika."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.datetime.now(timezone.utc) + datetime.timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str) -> Optional[dict]:
    """Weryfikuje i dekoduje token JWT."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None

async def verify_google_token(token_str: str) -> Optional[Dict[str, Any]]:
    """
    Weryfikuje ID Token z Google OAuth2.
    Wykorzystuje google-auth oraz fallback do Google Tokeninfo API.
    """
    # 1. Próba weryfikacji biblioteką google-auth jeśli jest GOOGLE_CLIENT_ID
    if settings.GOOGLE_CLIENT_ID:
        try:
            id_info = id_token.verify_oauth2_token(
                token_str, google_requests.Request(), settings.GOOGLE_CLIENT_ID
            )
            return {
                "sub": id_info.get("sub"),
                "email": id_info.get("email"),
                "name": id_info.get("name") or id_info.get("email", "").split("@")[0],
                "picture": id_info.get("picture", ""),
                "email_verified": id_info.get("email_verified", False)
            }
        except Exception as e:
            print(f"Błąd weryfikacji google-auth z client_id: {e}")

    # 2. Bezpośrednie zapytanie do endpointu Google tokeninfo (niezależne od client_id lub w fazie konfiguracji)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": token_str}
            )
            if resp.status_code == 200:
                data = resp.json()
                if "email" in data:
                    return {
                        "sub": data.get("sub"),
                        "email": data.get("email"),
                        "name": data.get("name") or data.get("email", "").split("@")[0],
                        "picture": data.get("picture", ""),
                        "email_verified": data.get("email_verified") in [True, "true"]
                    }
    except Exception as e:
        print(f"Błąd zapytania do tokeninfo Google: {e}")

    return None

async def get_or_create_user(db, user_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pobiera lub tworzy konto użytkownika w MongoDB.
    Gwarantuje, że initial admin zawsze otrzymuje rolę ADMIN.
    """
    email = user_info["email"].lower().strip()
    is_initial_admin = bool(settings.INITIAL_ADMIN_EMAIL and email == settings.INITIAL_ADMIN_EMAIL.lower().strip())
    now = datetime.datetime.now(timezone.utc)

    user = await db["users"].find_one({"email": email})
    if not user:
        role = Role.ADMIN if is_initial_admin else Role.USER
        new_user = {
            "email": email,
            "google_id": user_info.get("sub"),
            "name": user_info.get("name", email.split("@")[0]),
            "picture": user_info.get("picture", ""),
            "role": role,
            "created_at": now,
            "last_login": now,
            "is_active": True
        }
        res = await db["users"].insert_one(new_user)
        new_user["_id"] = str(res.inserted_id)
        return new_user
    else:
        # Aktualizacja logowania i ewentualne wymuszenie ADMIN dla głównego maila
        update_fields: Dict[str, Any] = {
            "last_login": now,
            "name": user_info.get("name") or user.get("name"),
            "picture": user_info.get("picture") or user.get("picture", "")
        }
        if is_initial_admin and user.get("role") != Role.ADMIN:
            update_fields["role"] = Role.ADMIN
            user["role"] = Role.ADMIN

        await db["users"].update_one({"_id": user["_id"]}, {"$set": update_fields})
        user["_id"] = str(user["_id"])
        user.update(update_fields)
        return user
