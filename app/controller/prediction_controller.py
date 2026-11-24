import logging
import datetime
from typing import Optional, List, Dict, Any, Union
from fastapi import APIRouter, Request, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel

from app.infrastructure.limiter import limiter
from app.services.weather.weather_service import get_weather_service
from app.services.prediction.neural_network import get_prediction_manager

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/prediction",
    tags=["Prediction"],
)


# --- MODELE PYDANTIC ---

class WeatherPayload(BaseModel):
    temperature: float = -2.0
    wind_speed: float = 1.5
    wind_direction: float = 180.0
    wind_gusts: float = 2.5
    surface_pressure: float = 960.0
    relative_humidity: float = 75.0
    precipitation: float = 0.0
    headwind_component: float = 0.5


class PredictionRequest(BaseModel):
    hill_place: str
    hill_size: float = 130.0
    hill_type: str = "LARGE"  # NORMAL, LARGE, FLYING
    hill_id: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = "15:00"
    weather: Optional[WeatherPayload] = None
    fis_codes: Optional[List[Union[int, str]]] = None  # Własna lista startowa (jeśli podano)
    num_simulations: int = 100


# --- ENDPOINTY ---

@router.get("/weather")
@limiter.limit("60/minute")
async def get_hill_weather(
    request: Request,
    place: str = Query(..., description="Nazwa miejscowości skoczni (np. Zakopane, Planica)"),
    date: Optional[str] = Query(None, description="Data w formacie YYYY-MM-DD"),
    time: Optional[str] = Query("15:00", description="Godzina w formacie HH:MM")
):
    """Pobiera prognozę lub dane historyczne z darmowego API Open-Meteo."""
    ws = get_weather_service()
    return ws.get_weather(place, date_str=date, time_str=time)


@router.get("/available-athletes")
@limiter.limit("60/minute")
async def get_available_athletes(request: Request):
    """
    Zwraca listę aktywnych skoczków wraz z parametrem ceny (price) i flagą Pucharu Świata,
    używaną przez selektor i listę startową.
    """
    db = request.app.database
    manager = get_prediction_manager()
    wc_set = manager.feature_extractor.wc_athletes_fis

    athletes = await db["athletes"].find({"status": "Active"}).sort("full_name", 1).to_list(1500)

    result = []
    for a in athletes:
        code_str = str(a.get("fis_code", "")).strip()
        price_val = a.get("price")
        result.append({
            "fis_code": a.get("fis_code"),
            "full_name": a.get("full_name"),
            "country_code": a.get("country_code", ""),
            "price": price_val,
            "has_price": price_val is not None,
            "has_wc": code_str in wc_set
        })

    return result


@router.post("/predict")
@limiter.limit("60/minute")
async def predict_competition(
    request: Request,
    payload: PredictionRequest
):
    """
    Generuje przewidywania sieci neuronowej:
    - Domyślnie dla skoczków z wyceną (price != None),
    - Z automatycznym wykrywaniem rozegranych serii weekendowych (kwalifikacje, treningi, wcześniejszy konkurs),
    - Z wielowymiarowymi profilami zawodników (konwersja presji, powtarzalność, afinitet skoczni).
    """
    db = request.app.database
    manager = get_prediction_manager()

    if not manager.is_trained:
        if not manager.load_model():
            raise HTTPException(
                status_code=503,
                detail="Sieć neuronowa nie jest jeszcze wytrenowana. Uruchom trening modelu w panelu administratora."
            )

    # 1. Warunki pogodowe
    if payload.weather:
        weather_dict = payload.weather.model_dump()
    else:
        ws = get_weather_service()
        weather_dict = ws.get_weather(payload.hill_place, payload.date, payload.time)

    # 2. Data konkursu
    target_date = datetime.date.today()
    if payload.date:
        try:
            target_date = datetime.date.fromisoformat(payload.date[:10])
        except Exception:
            target_date = datetime.date.today()

    target_date_str = target_date.isoformat()
    date_minus_3_str = (target_date - datetime.timedelta(days=3)).isoformat()

    # 3. Automatyczne wykrycie sesji z bieżącego weekendu (kwalifikacje, treningi, sobotni konkurs)
    weekend_sessions_info = []
    weekend_sessions = []
    try:
        clean_place = payload.hill_place.strip()
        weekend_query = {
            "event_place": {"$regex": f"^{clean_place}$", "$options": "i"},
            "date": {"$gte": date_minus_3_str, "$lte": target_date_str},
            "results_data.results": {"$exists": True, "$ne": []}
        }
        cursor = db["sessions"].find(weekend_query).sort([("date", 1), ("time", 1)])
        found_sessions = await cursor.to_list(15)

        # Odfiltruj ewentualną sesję tego samego konkursu o tej samej godzinie
        for s in found_sessions:
            s_time = str(s.get("time") or "")
            s_date = str(s.get("date") or "")
            # Jeśli to dokładnie ta sama godzina i data, pomiń
            if s_date == target_date_str and s_time == str(payload.time or "") and s.get("session_type") == "COMPETITION":
                continue

            weekend_sessions.append(s)
            weekend_sessions_info.append({
                "id": str(s.get("_id")),
                "date": s.get("date"),
                "time": s.get("time"),
                "type": s.get("session_type"),
                "title": s.get("title", s.get("session_type")),
                "jumpers_count": len(s.get("results_data", {}).get("results", []))
            })
    except Exception as e:
        logger.warning(f"Błąd podczas wyszukiwania sesji weekendowych: {e}")

    # 4. Pobierz skoczków z bazy
    athletes_col = db["athletes"]
    athletes = []

    if payload.fis_codes and len(payload.fis_codes) > 0:
        int_codes = []
        str_codes = []
        for c in payload.fis_codes:
            try:
                int_codes.append(int(c))
            except ValueError:
                pass
            str_codes.append(str(c).strip())

        query = {
            "$or": [
                {"fis_code": {"$in": int_codes}},
                {"fis_code": {"$in": str_codes}}
            ]
        }
        athletes = await athletes_col.find(query).to_list(1000)
    else:
        # NOWOŚĆ: Domyślnie aktywni skoczkowie z polem price != None!
        query = {
            "status": "Active",
            "price": {"$ne": None}
        }
        athletes = await athletes_col.find(query).sort("price", -1).to_list(500)

        # Fallback jeśli brak wycenionych
        if not athletes:
            athletes = await athletes_col.find({"status": "Active"}).limit(100).to_list(100)

    # 5. Uruchom inferencję sieci neuronowej
    try:
        predictions = manager.predict(
            athletes=athletes,
            hill_size=payload.hill_size,
            hill_type=payload.hill_type,
            place=payload.hill_place,
            weather=weather_dict,
            target_date=target_date,
            weekend_sessions=weekend_sessions,
            num_simulations=payload.num_simulations
        )
    except Exception as e:
        logger.error(f"Błąd podczas generowania predykcji: {e}")
        raise HTTPException(status_code=500, detail=f"Błąd sieci neuronowej: {str(e)}")

    return {
        "hill": {
            "place": payload.hill_place,
            "size": payload.hill_size,
            "type": payload.hill_type,
            "id": payload.hill_id
        },
        "weather": weather_dict,
        "weekend_sessions_detected": weekend_sessions_info,
        "total_athletes": len(predictions),
        "predictions": predictions,
        "model_meta": manager.metadata
    }


@router.get("/model-info")
@limiter.limit("60/minute")
async def get_model_info(request: Request):
    """Zwraca aktualny stan i metryki sieci neuronowej."""
    manager = get_prediction_manager()
    return {
        "is_trained": manager.is_trained,
        "model_path": str(manager.model is not None),
        "metadata": manager.metadata,
        "tracked_athletes_count": len(manager.feature_extractor.athlete_history),
        "wc_athletes_count": len(manager.feature_extractor.wc_athletes_fis)
    }


async def _run_retraining_task(db):
    try:
        manager = get_prediction_manager()
        cursor = db["sessions"].find(
            {"results_data.results": {"$exists": True, "$ne": []}},
            {"date": 1, "time": 1, "hillSize": 1, "hillType": 1, "event_place": 1, "session_type": 1, "race_category": 1, "category_code": 1, "results_data": 1}
        )
        sessions = await cursor.to_list(length=3500)
        manager.train(sessions)
    except Exception as e:
        logger.error(f"Błąd zadania w tle trenowania modelu: {e}")


@router.post("/retrain")
@limiter.limit("5/minute")
async def trigger_retrain(request: Request, background_tasks: BackgroundTasks):
    """Uruchamia ponowny trening sieci neuronowej w tle."""
    db = request.app.database
    background_tasks.add_task(_run_retraining_task, db)
    return {"message": "Trening sieci neuronowej został uruchomiony w tle."}
