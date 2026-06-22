from fastapi.middleware.cors import CORSMiddleware
from app.controller.athletes_controller import router as athletes_router
from app.controller.admin_controller import router as admin_router
from app.controller.stats_controller import router as stats_router
from app.controller.pdf_scraper_controller import router as pdf_router
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.config import settings
from app.infrastructure.limiter import limiter  # <-- Import z nowego pliku
import csv
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient

def load_dictionaries():
    """
    Ładuje zlokalizowane słowniki aplikacyjne (kraje, sesje, zawody, skocznie) do pamięci RAM.
    """
    dictionaries = {
        "countries": {},
        "sessions": {
            "COMPETITION": {"pl": "Konkurs", "eng": "Competition"},
            "WC COMPETITION": {"pl": "Konkurs", "eng": "World Cup Competition"},
            "COC COMPETITION": {"pl": "Konkurs", "eng": "Continental Cup Competition"},
            "FC COMPETITION": {"pl": "Konkurs", "eng": "FIS Cup Competition"},
            "QUA": {"pl": "Kwalifikacje", "eng": "Qualifications"},
            "QUALIFICATION": {"pl": "Kwalifikacje", "eng": "Qualifications"},
            "TRAINING": {"pl": "Trening", "eng": "Training"},
            "TRIAL": {"pl": "Seria próbna", "eng": "Trial Round"}
        },
        "categories": {
            "WC": {"pl": "Puchar Świata", "eng": "World Cup"},
            "COC": {"pl": "Puchar Kontynentalny", "eng": "Continental Cup"},
            "FC": {"pl": "FIS Cup", "eng": "FIS Cup"},
            "GP": {"pl": "Letnie Grand Prix", "eng": "Grand Prix"},
            "OWG": {"pl": "Igrzyska Olimpijskie", "eng": "Olympic Winter Games"},
            "WSC": {"pl": "Mistrzostwa Świata", "eng": "World Ski Championships"},
            "SFWC": {"pl": "MŚ w Lotach", "eng": "Ski Flying World Championships"},
            "WJC": {"pl": "MŚ Juniorów", "eng": "World Junior Championships"}
        },
        # NOWY SŁOWNIK TYPÓW SKOCZNI
        "hill_type": {
            "LARGE": {"pl": "Duża", "eng": "Large"},
            "NORMAL": {"pl": "Normalna", "eng": "Normal"},
            "FLYING": {"pl": "Mamucia", "eng": "Flying"}
        }
    }

    # Ładowanie krajów z pliku CSV
    csv_path = Path(__file__).resolve().parent / "dictionaries" / "codes.csv"
    if csv_path.exists():
        with open(csv_path, mode="r", encoding="windows-1250") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                ioc = row.get("IOC", "").strip().upper()
                iso2 = row.get("ISO-2", "").strip()
                name_pl = row.get("PL", "").strip()
                name_eng = row.get("NAME_ENG", "").strip()  # Wyciągamy angielską nazwę z CSV

                if ioc:
                    dictionaries["countries"][ioc] = {
                        "iso2": iso2,
                        "pl": name_pl,
                        "eng": name_eng
                    }
        print(f" Loaded {len(dictionaries['countries'])} countries with PL/ENG names from CSV.")
    else:
        print(f"⚠️ Nie znaleziono pliku słownika krajów pod ścieżką: {csv_path}")

    return dictionaries

# ... funkcja lifespan i inicjalizacja app = FastAPI(lifespan=lifespan) bez zmian ...

# 2. Zarządzanie cyklem życia aplikacji (Lifespan) dla MongoDB
# --- FRAGMENT: Cykl życia aplikacji (Lifespan) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Uruchamiane przy starcie aplikacji
    app.mongodb_client = AsyncIOMotorClient(settings.MONGO_URL)
    app.database = app.mongodb_client[settings.MONGO_DB_NAME]
    print("Połączono z MongoDB!")

    app.state.dicts = load_dictionaries()

    # NOWOŚĆ: Agregacja w MongoDB do zbudowania hill_dictionary
    try:
        # $group zgrupujebnam unikalne kombinacje tych 4 pól z kolekcji sessions
        cursor = app.database["sessions"].aggregate([
            {
                "$group": {
                    "_id": {
                        "name": "$event_place",
                        "hill_country": "$event_country",
                        "hill_size": "$hillSize",
                        "hill_type": "$hillType"
                    }
                }
            }
        ])

        hills = []
        async for doc in cursor:
            h = doc["_id"]
            if h.get("name"):
                hills.append(h)

        # Sortowanie alfabetyczne po nazwie miejscowości
        hills.sort(key=lambda x: x["name"])
        app.state.dicts["hills"] = hills
        print(f"Załadowano {len(hills)} unikalnych konfiguracji skoczni z MongoDB do RAM.")

    except Exception as e:
        print(f"Błąd podczas budowania słownika skoczni z MongoDB: {e}")
        app.state.dicts["hills"] = []

    print("Wszystkie słowniki aplikacyjne zostały pomyślnie załadowane do RAM.")

    yield  # Tu aplikacja działa i obsługuje żądania

    # Uruchamiane przy wyłączaniu aplikacji
    app.mongodb_client.close()
    print("Rozłączono z MongoDB.")
app = FastAPI(lifespan=lifespan)

# Podpinamy limiter pod FastAPI
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_router)
app.include_router(athletes_router)
app.include_router(stats_router)
app.include_router(pdf_router, prefix="/api/v1")

