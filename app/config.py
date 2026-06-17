from pathlib import Path
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    MONGO_URL: str
    MONGO_DB_NAME: str

    class Config:
        env_file = str(BASE_DIR / ".env")

# Instancja ustawień, z której będziesz korzystać w aplikacji
settings = Settings()


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
# Wykluczane zawsze (niezależnie od trybu)
EXCLUDED_TITLES_GLOBAL = [
    "Overall Standings"
]

# Prefiksy oznaczające zawody drużynowe
TEAM_COMPETITION_PREFIXES = [
    "Super Team",
    "Team"
]


API_USERS = {
    "fantaski2": "supersekret123"
}