from fastapi.middleware.cors import CORSMiddleware
from app.controller.auth_controller import router as auth_router
from app.controller.athletes_controller import router as athletes_router
from app.controller.admin_controller import router as admin_router
from app.controller.stats_controller import router as stats_router
from app.controller.pdf_scraper_controller import router as pdf_router
from app.controller.hills_controller import router as hills_router
from app.controller.prediction_controller import router as prediction_router
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from fastapi import Request
from app.config import settings
from app.infrastructure.limiter import limiter
import csv
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient
try:
    import torch
    torch.set_num_threads(1)
except Exception:
    pass

def load_dictionaries():
    """
    Ładuje zlokalizowane słowniki aplikacyjne (kraje, sesje, zawody, skocznie) do pamięci RAM.
    """
    dictionaries = {
        "countries": {},
        "sessions": {
            "COMPETITION": {"pl": "Konkurs", "eng": "Competition"},
            "QUA": {"pl": "Kwalifikacje", "eng": "Qualifications"},
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
                        "ioc": ioc,
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
    # Uruchamiane przy starcie aplikacji (optymalizacja puli połączeń dla darmowych klastrów Atlas M0)
    app.mongodb_client = AsyncIOMotorClient(
        settings.MONGO_URL,
        maxPoolSize=10,
        minPoolSize=1,
        serverSelectionTimeoutMS=5000
    )
    app.database = app.mongodb_client[settings.MONGO_DB_NAME]
    print("Połączono z MongoDB!")

    # Indeksy w bazie dla użytkowników
    try:
        await app.database["users"].create_index("email", unique=True)
        await app.database["users"].create_index("created_at")
    except Exception as e:
        print(f"Błąd podczas tworzenia indeksów dla users: {e}")

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
app = FastAPI(
    title="FantaSKI API",
    description="Backend API platformy FantaSKI — wyniki, analityka i predykcje skoków narciarskich",
    version="1.0.0",
    lifespan=lifespan
)

# Podpinamy limiter pod FastAPI
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
cors_list = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
is_wildcard = "*" in cors_list or not cors_list

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if is_wildcard else cors_list,
    allow_credentials=not is_wildcard,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Nagłówki bezpieczeństwa HTTP (Security Headers)
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    if getattr(settings, "ENVIRONMENT", "development") == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# Healthcheck
@app.get("/health", tags=["Health"])
async def healthcheck():
    return {"status": "ok", "service": "FantaSKI API"}

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(athletes_router)
app.include_router(stats_router)
app.include_router(pdf_router, prefix="/api/v1")
app.include_router(hills_router)
app.include_router(prediction_router)

