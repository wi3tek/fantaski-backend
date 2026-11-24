from pathlib import Path
from typing import List
import json
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    # MongoDB
    MONGO_URL: str = "mongodb://localhost:27017/"
    MONGO_DB_NAME: str = "FIS_DATA_DB"

    # Authentication & Security
    JWT_SECRET: str = "change-me-to-a-very-long-random-secret-in-production-123456789"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    GOOGLE_CLIENT_ID: str = ""
    INITIAL_ADMIN_EMAIL: str = ""

    # External APIs
    GEMINI_API_KEY: str = ""
    CONFIG_EMAIL: str = ""
    CONFIG_PASS: str = ""
    ESM_AUTH_URL: str = "https://esmdomainserviceproduction.azurewebsites.net/users/auth"
    ESM_DATA_URL: str = "https://esmdomainserviceproduction.azurewebsites.net/players/clientData/5f7f6aeb9ed6e90012549269"

    # AI Model
    AI_MODEL: str = "gemini-2.5-pro"

    # CORS & Web
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:4173,http://127.0.0.1:5173,http://127.0.0.1:4173"

    # Environment & Dev security
    ENVIRONMENT: str = "development"
    ENABLE_DEV_LOGIN: bool = True

    # Legacy basic auth users
    API_USERS_RAW: str = '{}'

    class Config:
        env_file = str(BASE_DIR / ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"

# Instancja ustawień, z której korzysta aplikacja
settings = Settings()

# Backwards compatibility aliases
GEMINI_API_KEY = settings.GEMINI_API_KEY
CONFIG_EMAIL = settings.CONFIG_EMAIL
CONFIG_PASS = settings.CONFIG_PASS
ESM_AUTH_URL = settings.ESM_AUTH_URL
ESM_DATA_URL = settings.ESM_DATA_URL
AI_MODEL = settings.AI_MODEL
INITIAL_ADMIN_EMAIL = settings.INITIAL_ADMIN_EMAIL

try:
    API_USERS = json.loads(settings.API_USERS_RAW)
except Exception:
    API_USERS = {}

FIS_BASE_URL = "https://www.fis-ski.com/DB/"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Punktacja Pucharu Świata - Indywidualna (miejsca 1-30)
COMPETITION_POINTS = {
    1: 100, 2: 80, 3: 60, 4: 50, 5: 45, 6: 40, 7: 36, 8: 32, 9: 29, 10: 26,
    11: 24, 12: 22, 13: 20, 14: 18, 15: 16, 16: 15, 17: 14, 18: 13, 19: 12, 20: 11,
    21: 10, 22: 9, 23: 8, 24: 7, 25: 6, 26: 5, 27: 4, 28: 3, 29: 2, 30: 1
}

# Punktacja Pucharu Świata - Drużynowa (miejsca 1-8)
TEAM_POINTS = {
    1: 400, 2: 350, 3: 300, 4: 250, 5: 200, 6: 150, 7: 100, 8: 50
}

# Punktacja Pucharu Świata - Duety / Super Team (miejsca 1-12)
SUPER_TEAM_POINTS = {
    1: 200, 2: 160, 3: 120, 4: 100, 5: 90, 6: 80, 7: 70, 8: 60, 9: 50, 10: 40, 11: 30, 12: 20
}

# --- FILTRY ---
EXCLUDED_TITLES_GLOBAL = [
    "Overall Standings"
]

TEAM_COMPETITION_PREFIXES = [
    "Super Team",
    "Team"
]
