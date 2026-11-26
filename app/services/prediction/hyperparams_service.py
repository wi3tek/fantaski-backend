import copy
import datetime
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

HYPERPARAMS_COLLECTION = "prediction_hyperparams"
HYPERPARAMS_HISTORY_COLLECTION = "prediction_hyperparams_history"
ACTIVE_DOC_ID = "active"

DEFAULT_HYPERPARAMS: Dict[str, Any] = {
    "comp_half_life_days": 35.0,
    "qua_half_life_days": 10.0,
    "trial_half_life_days": 4.0,
    "training_half_life_days": 3.0,
    "recent_form_window_days": 28,
    "weekend_comp_weight": 1.00,
    "weekend_qua_weight": 0.75,
    "weekend_trial_weight": 0.45,
    "weekend_training_weight": 0.30,
    "rolling_window_seasons": 8,
    "loss_quality_weight": 0.75,
    "points_blend_quality_weight": 0.70,
    "lr": 0.004,
    "dropout": 0.12,
    "weight_decay": 0.0002,
    "epochs": 50,
}

_ACTIVE_HYPERPARAMS_CACHE: Dict[str, Any] = copy.deepcopy(DEFAULT_HYPERPARAMS)
_ACTIVE_META_CACHE: Dict[str, Any] = {
    "source": "default",
    "updated_at": None,
    "tuning_metrics": None,
    "holdout_metrics": None,
}


def sanitize_hyperparams(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Łączy podane parametry z domyślnymi i pilnuje bezpiecznych zakresów liczbowych."""
    merged = copy.deepcopy(DEFAULT_HYPERPARAMS)
    if raw:
        for k, default_val in DEFAULT_HYPERPARAMS.items():
            if k in raw and raw[k] is not None:
                try:
                    merged[k] = type(default_val)(raw[k])
                except Exception:
                    pass

    merged["comp_half_life_days"] = max(7.0, min(90.0, float(merged["comp_half_life_days"])))
    merged["qua_half_life_days"] = max(2.0, min(45.0, float(merged["qua_half_life_days"])))
    merged["trial_half_life_days"] = max(1.0, min(21.0, float(merged["trial_half_life_days"])))
    merged["training_half_life_days"] = max(1.0, min(14.0, float(merged["training_half_life_days"])))
    merged["recent_form_window_days"] = max(7, min(60, int(merged["recent_form_window_days"])))
    merged["weekend_comp_weight"] = max(0.3, min(1.5, float(merged["weekend_comp_weight"])))
    merged["weekend_qua_weight"] = max(0.1, min(1.2, float(merged["weekend_qua_weight"])))
    merged["weekend_trial_weight"] = max(0.05, min(1.0, float(merged["weekend_trial_weight"])))
    merged["weekend_training_weight"] = max(0.05, min(1.0, float(merged["weekend_training_weight"])))
    merged["rolling_window_seasons"] = max(3, min(12, int(merged["rolling_window_seasons"])))
    merged["loss_quality_weight"] = max(0.25, min(0.95, float(merged["loss_quality_weight"])))
    merged["points_blend_quality_weight"] = max(0.30, min(0.95, float(merged["points_blend_quality_weight"])))
    merged["lr"] = max(0.0005, min(0.02, float(merged["lr"])))
    merged["dropout"] = max(0.0, min(0.4, float(merged["dropout"])))
    merged["weight_decay"] = max(1e-6, min(0.01, float(merged["weight_decay"])))
    merged["epochs"] = max(15, min(100, int(merged["epochs"])))
    return merged


def get_active_hyperparams() -> Dict[str, Any]:
    """Zwraca kopię aktualnie aktywnych hiperparametrów modelu w RAM."""
    return copy.deepcopy(_ACTIVE_HYPERPARAMS_CACHE)


def get_active_hyperparams_bundle() -> Dict[str, Any]:
    """Zwraca aktywne hiperparametry wraz z metadanymi źródła i metrykami tuningu."""
    return {
        "params": copy.deepcopy(_ACTIVE_HYPERPARAMS_CACHE),
        "defaults": copy.deepcopy(DEFAULT_HYPERPARAMS),
        "source": _ACTIVE_META_CACHE.get("source", "default"),
        "updated_at": _ACTIVE_META_CACHE.get("updated_at"),
        "tuning_metrics": _ACTIVE_META_CACHE.get("tuning_metrics"),
        "holdout_metrics": _ACTIVE_META_CACHE.get("holdout_metrics"),
    }


def set_active_hyperparams_cache(
    params: Dict[str, Any],
    source: str = "manual",
    updated_at: Optional[str] = None,
    tuning_metrics: Optional[Dict[str, Any]] = None,
    holdout_metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    global _ACTIVE_HYPERPARAMS_CACHE, _ACTIVE_META_CACHE
    clean = sanitize_hyperparams(params)
    _ACTIVE_HYPERPARAMS_CACHE = clean
    _ACTIVE_META_CACHE = {
        "source": source,
        "updated_at": updated_at or datetime.datetime.now().isoformat(),
        "tuning_metrics": tuning_metrics,
        "holdout_metrics": holdout_metrics,
    }
    return clean


async def load_active_hyperparams_from_db(db) -> Dict[str, Any]:
    """Wczytuje aktywne hiperparametry z kolekcji prediction_hyperparams w MongoDB."""
    try:
        doc = await db[HYPERPARAMS_COLLECTION].find_one({"_id": ACTIVE_DOC_ID})
        if doc and isinstance(doc.get("params"), dict):
            clean = set_active_hyperparams_cache(
                params=doc["params"],
                source=doc.get("source", "mongodb"),
                updated_at=doc.get("updated_at"),
                tuning_metrics=doc.get("tuning_metrics"),
                holdout_metrics=doc.get("holdout_metrics"),
            )
            logger.info(f"Załadowano hiperparametry modelu z MongoDB (źródło: {_ACTIVE_META_CACHE['source']}).")
            return clean
    except Exception as e:
        logger.warning(f"Nie udało się wczytać hiperparametrów z MongoDB (używam domyślnych): {e}")

    return get_active_hyperparams()


async def save_active_hyperparams_to_db(
    db,
    params: Dict[str, Any],
    source: str = "autotuned",
    tuning_metrics: Optional[Dict[str, Any]] = None,
    holdout_metrics: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Zapisuje zwycięskie hiperparametry do MongoDB (active + historia) oraz aktualizuje RAM."""
    now_iso = datetime.datetime.now().isoformat()
    clean = set_active_hyperparams_cache(
        params=params,
        source=source,
        updated_at=now_iso,
        tuning_metrics=tuning_metrics,
        holdout_metrics=holdout_metrics,
    )

    payload = {
        "_id": ACTIVE_DOC_ID,
        "source": source,
        "updated_at": now_iso,
        "params": clean,
        "tuning_metrics": tuning_metrics,
        "holdout_metrics": holdout_metrics,
        "notes": notes,
    }
    await db[HYPERPARAMS_COLLECTION].replace_one({"_id": ACTIVE_DOC_ID}, payload, upsert=True)

    history_doc = {
        "created_at": now_iso,
        "source": source,
        "params": clean,
        "tuning_metrics": tuning_metrics,
        "holdout_metrics": holdout_metrics,
        "notes": notes,
    }
    await db[HYPERPARAMS_HISTORY_COLLECTION].insert_one(history_doc)
    return get_active_hyperparams_bundle()


async def reset_hyperparams_to_default(db) -> Dict[str, Any]:
    """Przywraca domyślne hiperparametry w MongoDB i RAM."""
    return await save_active_hyperparams_to_db(
        db=db,
        params=DEFAULT_HYPERPARAMS,
        source="default",
        tuning_metrics=None,
        holdout_metrics=None,
        notes="Przywrócono ustawienia domyślne",
    )


async def get_hyperparams_history(db, limit: int = 10) -> List[Dict[str, Any]]:
    """Zwraca ostatnie zapisane konfiguracje hiperparametrów z MongoDB."""
    cursor = db[HYPERPARAMS_HISTORY_COLLECTION].find({}, {"_id": 0}).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)
