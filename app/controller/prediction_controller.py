import asyncio
import logging
import datetime
import re
from typing import Optional, List, Dict, Any, Union
from fastapi import APIRouter, Request, HTTPException, Query, BackgroundTasks, Depends
from pydantic import BaseModel, Field

from app.infrastructure.cache import get_cached_value, set_cached_value
from app.infrastructure.limiter import limiter
from app.services.weather.weather_service import get_weather_service
from app.services.prediction.neural_network import get_prediction_manager
from app.services.prediction.backtester import get_walk_forward_backtester
from app.services.prediction.auto_tuner import get_auto_tuner_manager
from app.services.prediction.hyperparams_service import (
    get_active_hyperparams_bundle,
    get_hyperparams_history,
    load_active_hyperparams_from_db,
    reset_hyperparams_to_default,
)
from app.services.prediction.prediction_config import WEEKEND_LOOKBACK_DAYS
from app.services.prediction.calendar_service import get_next_upcoming_competition
from app.auth import require_role
from app.services.auth_service import Role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/prediction",
    tags=["Prediction"],
    dependencies=[Depends(require_role(Role.USER_PRO, Role.ADMIN))]
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
    wind_trend_slope: Optional[float] = 0.0
    wind_volatility_index: Optional[float] = None
    temperature_drift: Optional[float] = 0.0
    direction_shift: Optional[float] = 0.0


class PredictionRequest(BaseModel):
    hill_place: str
    hill_size: float = 130.0
    hill_type: str = "LARGE"  # NORMAL, LARGE, FLYING
    hill_id: Optional[str] = None
    competition_type: Optional[str] = "WC"  # WC, GP, COC, FC
    date: Optional[str] = None
    time: Optional[str] = "15:00"
    weather: Optional[WeatherPayload] = None
    fis_codes: Optional[List[Union[int, str]]] = Field(default=None, max_length=150)  # Własna lista startowa (jeśli podano)
    num_simulations: int = Field(default=100, ge=10, le=400)


# --- ENDPOINTY ---

@router.get("/next-competition")
@limiter.limit("60/minute")
async def get_next_competition(
    request: Request,
    category: str = Query("WC", description="Kategoria zawodów (WC, GP, COC)"),
    gender: str = Query("M", description="Płeć (M lub W)"),
    force_refresh: bool = Query(False, description="Wymuś pobranie z serwerów FIS")
):
    """
    Automatycznie wykrywa i podpowiada najbliższe nadchodzące zawody (np. Ruka, Wisła)
    wraz z datą, godziną, parametrami skoczni i statusem kwalifikacji na bazie kalendarza FIS.
    """
    db = request.app.database
    return await get_next_upcoming_competition(db, target_category=category, gender=gender, force_refresh=force_refresh)


async def _get_weather_cached_async(place: str, date_str: Optional[str], time_str: Optional[str]) -> Dict[str, Any]:
    cache_key = f"weather_{(place or '').strip().lower()}_{date_str or ''}_{time_str or '15:00'}"
    cached = get_cached_value("short", cache_key)
    if cached is not None:
        return cached
    ws = get_weather_service()
    res = await asyncio.to_thread(ws.get_weather, place, date_str=date_str, time_str=time_str)
    set_cached_value("short", cache_key, res)
    return res


@router.get("/weather")
@limiter.limit("60/minute")
async def get_hill_weather(
    request: Request,
    place: str = Query(..., description="Nazwa miejscowości skoczni (np. Zakopane, Planica)"),
    date: Optional[str] = Query(None, description="Data w formacie YYYY-MM-DD"),
    time: Optional[str] = Query("15:00", description="Godzina w formacie HH:MM")
):
    """Pobiera prognozę lub dane historyczne z darmowego API Open-Meteo (cache 1 min)."""
    return await _get_weather_cached_async(place, date, time)


@router.get("/available-athletes")
@limiter.limit("60/minute")
async def get_available_athletes(request: Request):
    """
    Zwraca listę aktywnych skoczków wraz z parametrem ceny (price), flagą Pucharu Świata
    oraz informacją, czy zawodnik punktował w AKTUALNYM sezonie (TOP 30 w konkursie).
    """
    db = request.app.database
    manager = get_prediction_manager()
    wc_set = manager.feature_extractor.wc_athletes_fis

    # Znajdź zawodników, którzy zdobyli punkty (TOP 30) w bieżącym sezonie zimowym
    scored_set = set()
    try:
        cursor_lat = db["sessions"].find({"date": {"$ne": None}}).sort("date", -1).limit(1)
        lat_list = await cursor_lat.to_list(1)
        if lat_list:
            latest_date = str(lat_list[0].get("date") or "2026-11-22")
            season_year = int(latest_date[:4])
            month = int(latest_date[5:7]) if len(latest_date) >= 7 else 11
            winter_start = f"{season_year}-11-01" if month >= 10 else f"{season_year-1}-11-01"

            comp_sessions = await db["sessions"].find({
                "date": {"$gte": winter_start},
                "session_type": "COMPETITION",
                "results_data.results": {"$exists": True, "$ne": []}
            }, {"results_data.results": 1}).to_list(100)

            for s in comp_sessions:
                for r in s.get("results_data", {}).get("results", []):
                    try:
                        if 1 <= int(r.get("rank", 999)) <= 30 and r.get("fis_code"):
                            scored_set.add(str(r["fis_code"]).strip())
                    except Exception:
                        pass
    except Exception as e:
        logger.warning(f"Błąd podczas wyliczania punktujących w bieżącym sezonie: {e}")

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
            "has_wc": code_str in wc_set,
            "scored_current_season": code_str in scored_set
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
        weather_dict = await _get_weather_cached_async(payload.hill_place, payload.date, payload.time)

    # 2. Data konkursu
    target_date = datetime.date.today()
    if payload.date:
        try:
            target_date = datetime.date.fromisoformat(payload.date[:10])
        except Exception:
            target_date = datetime.date.today()

    target_date_str = target_date.isoformat()
    date_minus_window_str = (target_date - datetime.timedelta(days=WEEKEND_LOOKBACK_DAYS)).isoformat()

    # 3. Automatyczne wykrycie sesji z bieżącego weekendu (kwalifikacje, treningi, wcześniejszy konkurs)
    weekend_sessions_info = []
    weekend_sessions = []
    try:
        clean_place = payload.hill_place.strip()
        weekend_query = {
            "event_place": {"$regex": f"^{re.escape(clean_place)}$", "$options": "i"},
            "date": {"$gte": date_minus_window_str, "$lte": target_date_str},
            "results_data.results": {"$exists": True, "$ne": []}
        }
        cursor = db["sessions"].find(weekend_query).sort([("date", 1), ("time", 1)])
        found_sessions = await cursor.to_list(25)

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
        raw_athletes = await athletes_col.find(query).to_list(1000)
        # Zachowaj dokładną kolejność wyjścia na belkę podaną przez użytkownika / listę startową
        code_to_ath = {str(a.get("fis_code")): a for a in raw_athletes}
        for c in payload.fis_codes:
            str_c = str(c).strip()
            if str_c in code_to_ath:
                athletes.append(code_to_ath[str_c])
    else:
        # Domyślnie aktywni skoczkowie z wyceną:
        # W Pucharze Świata zawodnicy o niższym rankingu/cenie skaczą na początku, a liderzy na końcu
        query = {
            "status": "Active",
            "price": {"$ne": None}
        }
        athletes = await athletes_col.find(query).sort("price", 1).to_list(500)

        # Fallback jeśli brak wycenionych
        if not athletes:
            athletes = await athletes_col.find({"status": "Active"}).limit(100).to_list(100)

    # 5. Uruchom inferencję sieci neuronowej w wątku roboczym (nie blokuje pętli asyncio)
    target_tier = (payload.competition_type or "WC").upper().strip()
    try:
        predictions = await asyncio.to_thread(
            manager.predict,
            athletes=athletes,
            hill_size=payload.hill_size,
            hill_type=payload.hill_type,
            place=payload.hill_place,
            weather=weather_dict,
            target_date=target_date,
            weekend_sessions=weekend_sessions,
            num_simulations=payload.num_simulations,
            target_category=target_tier,
            target_time=(payload.time or None),
        )
    except Exception as e:
        logger.exception("Błąd podczas generowania predykcji")
        raise HTTPException(status_code=500, detail="Błąd podczas generowania predykcji sieci neuronowej.")

    volatility_val = weather_dict.get("wind_volatility_index")
    if volatility_val is None:
        spd = float(weather_dict.get("wind_speed", 1.5))
        gst = float(weather_dict.get("wind_gusts", spd * 1.5))
        volatility_val = round(max(0.0, (gst - spd) / max(0.5, spd)), 2)

    return {
        "hill": {
            "place": payload.hill_place,
            "size": payload.hill_size,
            "type": payload.hill_type,
            "id": payload.hill_id
        },
        "competition_type": target_tier,
        "weather": weather_dict,
        "corridor": {
            "wind_volatility_index": float(volatility_val),
            "lottery_risk_level": weather_dict.get("lottery_risk_level", "HIGH" if volatility_val >= 0.85 else ("MODERATE" if volatility_val >= 0.35 else "LOW")),
            "wind_trend_slope": float(weather_dict.get("wind_trend_slope", 0.0)),
            "temperature_drift": float(weather_dict.get("temperature_drift", 0.0)),
            "direction_shift": float(weather_dict.get("direction_shift", 0.0)),
        },
        "weekend_sessions_detected": weekend_sessions_info,
        "total_athletes": len(predictions),
        "predictions": predictions,
        "model_meta": manager.metadata
    }


@router.get("/model-info")
@limiter.limit("60/minute")
async def get_model_info(request: Request):
    """Zwraca aktualny stan, postęp treningu i metryki sieci neuronowej."""
    manager = get_prediction_manager()
    return {
        "is_trained": manager.is_trained,
        "model_path": str(manager.model is not None),
        "metadata": manager.metadata,
        "training_status": manager.get_training_status(),
        "tracked_athletes_count": len(manager.feature_extractor.athlete_history),
        "wc_athletes_count": len(manager.feature_extractor.wc_athletes_fis)
    }


_SESSION_PROJECTION = {
    "_id": 1,
    "raceId": 1,
    "season": 1,
    "date": 1,
    "time": 1,
    "hillSize": 1,
    "hillType": 1,
    "event_place": 1,
    "session_type": 1,
    "race_category": 1,
    "category_code": 1,
    "results_data": 1,
    "weather": 1,
}


async def _run_retraining_task(db):
    manager = get_prediction_manager()
    try:
        manager.training_message = "Wczytywanie aktywnych hiperparametrów z MongoDB..."
        await load_active_hyperparams_from_db(db)
        manager.training_message = "Pobieranie sesji z bazy MongoDB..."
        cursor = db["sessions"].find(
            {"results_data.results": {"$exists": True, "$ne": []}},
            _SESSION_PROJECTION,
        )
        sessions = await cursor.to_list(length=10000)
        await asyncio.to_thread(manager.train, sessions)
        from app.services.prediction.ml_storage import save_nn_artifacts_to_db
        await save_nn_artifacts_to_db(db, manager)
    except Exception as e:
        manager.mark_training_failed(str(e))
        logger.error(f"Błąd zadania w tle trenowania modelu: {e}")


@router.post("/retrain")
@limiter.limit("5/minute")
async def trigger_retrain(
    request: Request,
    background_tasks: BackgroundTasks,
    admin: dict = Depends(require_role(Role.ADMIN))
):
    """Uruchamia ponowny trening sieci neuronowej w tle (wymaga uprawnień ADMIN)."""
    manager = get_prediction_manager()
    if manager.is_training:
        raise HTTPException(status_code=400, detail="Trening sieci neuronowej jest już w toku.")

    db = request.app.database
    manager.mark_training_started()
    background_tasks.add_task(_run_retraining_task, db)
    return {"message": "Trening sieci neuronowej został uruchomiony w tle."}


async def _run_backtest_task(db):
    backtester = get_walk_forward_backtester()
    try:
        await load_active_hyperparams_from_db(db)
        backtester.message = "Pobieranie sesji historycznych do walidacji Walk-Forward..."
        cursor = db["sessions"].find(
            {"results_data.results": {"$exists": True, "$ne": []}},
            _SESSION_PROJECTION,
        )
        sessions = await cursor.to_list(length=10000)
        report = await asyncio.to_thread(backtester.run_backtest, sessions)
        from app.services.prediction.ml_storage import save_json_artifact_to_db
        await save_json_artifact_to_db(db, "backtest_results", report)
    except Exception as e:
        backtester.is_running = False
        backtester.error = str(e)
        backtester.current_stage = "error"
        backtester.message = f"Błąd backtestu: {e}"
        logger.error(f"Błąd zadania w tle Walk-Forward Backtest: {e}")


@router.get("/backtest/status")
@limiter.limit("60/minute")
async def get_backtest_status(request: Request):
    """Zwraca bieżący postęp oraz ostatnie zapisane wyniki walidacji Walk-Forward."""
    return get_walk_forward_backtester().get_status()


@router.post("/backtest/start")
@limiter.limit("5/minute")
async def trigger_backtest(
    request: Request,
    background_tasks: BackgroundTasks,
    admin: dict = Depends(require_role(Role.ADMIN))
):
    """Uruchamia kroczącą walidację historyczną Walk-Forward po sezonach (wymaga uprawnień ADMIN)."""
    backtester = get_walk_forward_backtester()
    if backtester.is_running:
        raise HTTPException(status_code=400, detail="Backtest Walk-Forward jest już w toku.")

    db = request.app.database
    backtester.mark_started()
    background_tasks.add_task(_run_backtest_task, db)
    return {"message": "Krocząca walidacja Walk-Forward została uruchomiona w tle."}


# --- HIPERPARAMETRY MONGODB & AUTO-TUNER ---

@router.get("/hyperparams")
@limiter.limit("60/minute")
async def get_prediction_hyperparams(request: Request):
    """Zwraca aktywne hiperparametry sieci z MongoDB oraz historię strojeń."""
    db = request.app.database
    await load_active_hyperparams_from_db(db)
    bundle = get_active_hyperparams_bundle()
    bundle["history"] = await get_hyperparams_history(db, limit=6)
    return bundle


@router.post("/hyperparams/reset")
@limiter.limit("10/minute")
async def reset_prediction_hyperparams(
    request: Request,
    admin: dict = Depends(require_role(Role.ADMIN)),
):
    """Przywraca domyślne hiperparametry predykcji w MongoDB (wymaga ADMIN)."""
    db = request.app.database
    bundle = await reset_hyperparams_to_default(db)
    bundle["history"] = await get_hyperparams_history(db, limit=6)
    return {"message": "Przywrócono domyślne hiperparametry w MongoDB.", **bundle}


async def _run_autotune_task(db):
    tuner = get_auto_tuner_manager()
    try:
        tuner.message = "Pobieranie sesji z MongoDB do 2-etapowego Auto-Tuningu..."
        cursor = db["sessions"].find(
            {"results_data.results": {"$exists": True, "$ne": []}},
            _SESSION_PROJECTION,
        )
        sessions = await cursor.to_list(length=10000)
        await tuner.run_job(db=db, sessions=sessions)
    except Exception as e:
        tuner.is_running = False
        tuner.error = str(e)
        tuner.stage = "error"
        tuner.message = f"Błąd Auto-Tunera: {e}"
        logger.error(f"Błąd zadania w tle Auto-Tunera: {e}")


@router.get("/autotune/status")
@limiter.limit("60/minute")
async def get_autotune_status(request: Request):
    """Zwraca bieżący postęp oraz ostatnie wyniki 2-etapowego Auto-Tunera."""
    return get_auto_tuner_manager().get_status()


@router.post("/autotune/start")
@limiter.limit("5/minute")
async def trigger_autotune(
    request: Request,
    background_tasks: BackgroundTasks,
    admin: dict = Depends(require_role(Role.ADMIN)),
):
    """Uruchamia automatyczne 2-etapowe strojenie parametrów z walidacją holdout i zapisem w MongoDB."""
    tuner = get_auto_tuner_manager()
    if tuner.is_running:
        raise HTTPException(status_code=400, detail="Auto-Tuning sieci neuronowej jest już w toku.")
    if get_prediction_manager().is_training or get_walk_forward_backtester().is_running:
        raise HTTPException(status_code=400, detail="Poczekaj na zakończenie aktywnego treningu lub backtestu.")

    db = request.app.database
    tuner.mark_started()
    background_tasks.add_task(_run_autotune_task, db)
    return {"message": "Uruchomiono 2-etapowy Auto-Tuning z zapisem w MongoDB."}


@router.post("/autotune/stop")
@limiter.limit("30/minute")
async def stop_autotune(
    request: Request,
    admin: dict = Depends(require_role(Role.ADMIN)),
):
    """Bezpiecznie zatrzymuje Auto-Tuner po zakończeniu bieżącej próby."""
    tuner = get_auto_tuner_manager()
    if not tuner.is_running:
        return {"message": "Auto-Tuner nie jest obecnie uruchomiony."}
    tuner.stop()
    return {"message": "Wysłano sygnał zatrzymania Auto-Tunera."}
