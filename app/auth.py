from typing import Optional, List, Callable
from fastapi import Request, HTTPException, Depends, status, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from bson import ObjectId

from app.services.auth_service import decode_access_token, Role
from app.config import API_USERS

security = HTTPBearer(auto_error=False)

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> dict:
    """Weryfikuje nagłówek Authorization: Bearer <token> i pobiera użytkownika z bazy."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Brak tokenu autoryzacyjnego. Zaloguj się przez Google.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token autoryzacyjny wygasł lub jest nieprawidłowy. Zaloguj się ponownie.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nieprawidłowy token (brak identyfikatora użytkownika).",
            headers={"WWW-Authenticate": "Bearer"}
        )

    db = getattr(request.app, "database", None)
    if db is None:
        raise HTTPException(status_code=500, detail="Brak połączenia z bazą danych.")

    try:
        user = await db["users"].find_one({"_id": ObjectId(user_id)})
    except Exception:
        user = None

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Użytkownik nie istnieje w systemie.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Twoje konto zostało dezaktywowane."
        )

    user["_id"] = str(user["_id"])
    return user

async def get_current_user_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Optional[dict]:
    """Zwraca zalogowanego użytkownika lub None jeśli brak tokenu/token niepoprawny."""
    if not credentials:
        return None
    try:
        return await get_current_user(request, credentials)
    except HTTPException:
        return None

def require_role(*allowed_roles: str) -> Callable:
    """Zależność sprawdzająca czy zalogowany użytkownik posiada wymaganą rolę."""
    async def role_checker(current_user: dict = Depends(get_current_user)) -> dict:
        user_role = current_user.get("role", Role.USER)
        if user_role not in allowed_roles:
            if allowed_roles == (Role.ADMIN,):
                detail_msg = "Brak uprawnień do tego zasobu. Skontaktuj się z administratorem serwisu."
            elif Role.USER_LITE in allowed_roles and user_role == Role.USER:
                detail_msg = "Ta funkcja wymaga konta FantaSKI LITE lub PRO. Skontaktuj się z administratorem, aby aktywować pakiet."
            elif Role.USER_PRO in allowed_roles:
                detail_msg = "Ta sekcja jest dostępna wyłącznie w ramach konta FantaSKI PRO."
            else:
                detail_msg = "Brak wymaganych uprawnień. Skontaktuj się z administratorem serwisu."
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=detail_msg
            )
        return current_user
    return role_checker

# Legacy basic auth helper (kompatybilność wsteczna)
def verify_api_key(api_user: str = Header(None), api_secret: str = Header(None)):
    if api_user not in API_USERS:
        raise HTTPException(status_code=401, detail="Invalid user")
    if API_USERS[api_user] != api_secret:
        raise HTTPException(status_code=401, detail="Invalid secret")
    return True
