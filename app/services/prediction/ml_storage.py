import asyncio
import datetime
import gzip
import io
import json
import logging
from typing import Any, Dict, Optional

import torch
from bson import Binary

from app.services.prediction.neural_network import (
    BACKTEST_PATH,
    FEATURE_EXTRACTOR_PATH,
    INPUT_FEATURES_DIM,
    META_PATH,
    MODEL_PATH,
    SkiJumpingNN,
    get_prediction_manager,
)

logger = logging.getLogger(__name__)

ML_ARTIFACTS_COLLECTION = "ml_artifacts"


def _serialize_nn_state_bytes(model: SkiJumpingNN) -> bytes:
    buf = io.BytesIO()
    torch.save(model.state_dict(), buf)
    return buf.getvalue()


def _compress_json_dict(data: Dict[str, Any]) -> bytes:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, compresslevel=6)


def _decompress_json_dict(blob: bytes) -> Dict[str, Any]:
    raw = gzip.decompress(blob)
    return json.loads(raw.decode("utf-8"))


async def save_nn_artifacts_to_db(db, manager=None) -> bool:
    """Zapisuje wagi sieci neuronowej, metadane i stan ekstraktora cech (gzip) do MongoDB."""
    if db is None:
        return False
    pm = manager or get_prediction_manager()
    if not pm.is_trained or pm.model is None:
        return False

    try:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        weights_bytes, fe_gz = await asyncio.to_thread(
            lambda: (
                _serialize_nn_state_bytes(pm.model),
                _compress_json_dict(pm.feature_extractor.to_dict()),
            )
        )

        await db[ML_ARTIFACTS_COLLECTION].replace_one(
            {"_id": "nn_weights"},
            {
                "_id": "nn_weights",
                "updated_at": now_iso,
                "input_dim": INPUT_FEATURES_DIM,
                "meta": pm.metadata or {},
                "weights": Binary(weights_bytes),
            },
            upsert=True,
        )

        # Dokument BSON ma limit 16 MB; po gzip ~3.8 MB mieści się z dużym zapasem.
        if len(fe_gz) < 14 * 1024 * 1024:
            await db[ML_ARTIFACTS_COLLECTION].replace_one(
                {"_id": "feature_extractor_state"},
                {
                    "_id": "feature_extractor_state",
                    "updated_at": now_iso,
                    "encoding": "gzip_json",
                    "size_bytes": len(fe_gz),
                    "data": Binary(fe_gz),
                },
                upsert=True,
            )
        else:
            logger.warning(
                "Stan feature_extractor po kompresji przekracza 14 MB (%d B) — pominięto zapis w 1 dokumencie MongoDB.",
                len(fe_gz),
            )

        logger.info("Zapisano artefakty modelu SkiJumpingNN do MongoDB (%d KB fe_gz).", len(fe_gz) // 1024)
        return True
    except Exception as exc:
        logger.warning("Nie udało się zapisać artefaktów NN do MongoDB: %s", exc)
        return False


async def save_json_artifact_to_db(db, artifact_id: str, payload: Optional[Dict[str, Any]]) -> bool:
    """Zapisuje raport JSON (np. backtest_results, autotune_results) do kolekcji ml_artifacts w MongoDB."""
    if db is None or not payload:
        return False
    try:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        await db[ML_ARTIFACTS_COLLECTION].replace_one(
            {"_id": artifact_id},
            {
                "_id": artifact_id,
                "updated_at": now_iso,
                "payload": payload,
            },
            upsert=True,
        )
        return True
    except Exception as exc:
        logger.warning("Nie udało się zapisać artefaktu %s do MongoDB: %s", artifact_id, exc)
        return False


async def load_ml_artifacts_from_db(db) -> None:
    """
    Ładuje model NN, stan ekstraktora cech, backtest i raport auto-tunera z MongoDB przy starcie.
    Jeśli w MongoDB nie ma jeszcze artefaktów, a istnieją na dysku lokalnym, automatycznie wykonuje jednorazowy backfill.
    """
    if db is None:
        return

    pm = get_prediction_manager()
    try:
        nn_doc = await db[ML_ARTIFACTS_COLLECTION].find_one({"_id": "nn_weights"})
        fe_doc = await db[ML_ARTIFACTS_COLLECTION].find_one({"_id": "feature_extractor_state"})

        if nn_doc and isinstance(nn_doc.get("weights"), (bytes, bytearray)):
            meta = nn_doc.get("meta") or {}
            saved_dim = meta.get("input_dim") or meta.get("input_features") or nn_doc.get("input_dim")
            if saved_dim == INPUT_FEATURES_DIM:
                saved_dropout = float((meta.get("hyperparams") or {}).get("dropout", 0.12))

                def _restore():
                    model = SkiJumpingNN(input_dim=INPUT_FEATURES_DIM, dropout=saved_dropout)
                    buf = io.BytesIO(bytes(nn_doc["weights"]))
                    model.load_state_dict(torch.load(buf, map_location=torch.device("cpu"), weights_only=True))
                    model.eval()
                    fe_dict = None
                    if fe_doc and isinstance(fe_doc.get("data"), (bytes, bytearray)):
                        fe_dict = _decompress_json_dict(bytes(fe_doc["data"]))
                    return model, fe_dict

                model, fe_dict = await asyncio.to_thread(_restore)
                pm.model = model
                pm.metadata = meta
                if fe_dict:
                    pm.feature_extractor.from_dict(fe_dict)
                pm.is_trained = True
                logger.info("Załadowano model SkiJumpingNN z MongoDB (wytrenowany: %s).", meta.get("trained_at"))
            else:
                logger.info("Model w MongoDB ma inną liczbę cech (%s vs %s).", saved_dim, INPUT_FEATURES_DIM)
        else:
            # Backfill z lokalnych plików jeśli istnieją na dysku (np. po pierwszym deployu)
            if MODEL_PATH.exists() and META_PATH.exists() and FEATURE_EXTRACTOR_PATH.exists():
                loaded = await asyncio.to_thread(pm.load_model)
                if loaded:
                    await save_nn_artifacts_to_db(db, pm)
    except Exception as exc:
        logger.warning("Błąd ładowania modelu NN z MongoDB: %s", exc)

    # Backtest & Auto-Tuner
    try:
        from app.services.prediction.backtester import get_walk_forward_backtester
        from app.services.prediction.auto_tuner import AUTOTUNE_PATH, get_auto_tuner_manager

        bt = get_walk_forward_backtester()
        bt_doc = await db[ML_ARTIFACTS_COLLECTION].find_one({"_id": "backtest_results"})
        if bt_doc and isinstance(bt_doc.get("payload"), dict):
            bt.results = bt_doc["payload"]
        elif bt.results is None and BACKTEST_PATH.exists():
            await asyncio.to_thread(bt._load_cached)
            if bt.results:
                await save_json_artifact_to_db(db, "backtest_results", bt.results)
        elif bt.results:
            await save_json_artifact_to_db(db, "backtest_results", bt.results)

        tuner = get_auto_tuner_manager()
        at_doc = await db[ML_ARTIFACTS_COLLECTION].find_one({"_id": "autotune_results"})
        if at_doc and isinstance(at_doc.get("payload"), dict):
            tuner.results = at_doc["payload"]
        elif tuner.results is None and AUTOTUNE_PATH.exists():
            await asyncio.to_thread(tuner._load_cached)
            if tuner.results:
                await save_json_artifact_to_db(db, "autotune_results", tuner.results)
        elif tuner.results:
            await save_json_artifact_to_db(db, "autotune_results", tuner.results)
    except Exception as exc:
        logger.warning("Błąd ładowania raportów backtest/autotuner z MongoDB: %s", exc)
