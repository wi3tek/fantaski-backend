from fastapi import Header, HTTPException
from app.config import API_USERS

def verify_api_key(api_user: str = Header(None), api_secret: str = Header(None)):
    if api_user not in API_USERS:
        raise HTTPException(status_code=401, detail="Invalid user")
    if API_USERS[api_user] != api_secret:
        raise HTTPException(status_code=401, detail="Invalid secret")
    return True
