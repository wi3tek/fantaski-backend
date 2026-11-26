import json
import math
import logging
import datetime
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional, Callable

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from app.services.prediction.feature_extractor import (
    SkiFeatureExtractor,
    compute_session_entries,
)
from app.services.prediction.prediction_config import (
    INPUT_FEATURES_DIM,
    DEFAULT_TARGET_CATEGORY,
    SESSION_STAGE_ORDER,
    TRAINING_COMPETITION_CATEGORY_WEIGHTS,
    TRAINING_ROLLING_WINDOW_SEASONS,
    resolve_session_category,
    resolve_session_type,
)
from app.services.prediction.hyperparams_service import (
    get_active_hyperparams,
    sanitize_hyperparams,
)

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "model_weights"
MODEL_PATH = MODEL_DIR / "ski_jumping_nn.pt"
META_PATH = MODEL_DIR / "model_meta.json"
FEATURE_EXTRACTOR_PATH = MODEL_DIR / "feature_extractor_state.json"
BACKTEST_PATH = MODEL_DIR / "backtest_results.json"


def calculate_fis_jump_points(dist: float, hs: float, ht: str = "LARGE", headwind: float = 0.0) -> float:
    """
    Fizyczne obliczanie punktów FIS za skok narciarski:
    - Punkty za odległość (Distance Points): K_base + (dist - K) * meter_value
    - Punkty sędziowskie za styl (Style Points): 52.0 do 57.0
    - Kompensacja za wiatr (Wind Compensation): -headwind * wind_pts_rate
    """
    if hs >= 200:
        k = 200.0
        meter_val = 1.2
        k_pts = 120.0
        base_wind_rate = 7.2
    elif hs >= 110:
        k = 120.0 if hs <= 135 else 125.0
        meter_val = 1.8
        k_pts = 60.0
        base_wind_rate = 5.4
    else:
        k = 90.0 if hs <= 100 else 95.0
        meter_val = 2.0
        k_pts = 60.0
        base_wind_rate = 3.6

    dist_pts = k_pts + (dist - k) * meter_val
    ratio = dist / max(1.0, hs)
    style_pts = 52.0 + 5.0 * max(0.0, min(1.0, (ratio - 0.85) / 0.15))
    wind_pts = -headwind * base_wind_rate
    return dist_pts + style_pts + wind_pts


class SkiJumpingNN(nn.Module):
    """
    Dwugłowicowa sieć neuronowa do prognozowania wyników konkursów skoków narciarskich.
    Wejście: 18 czystych cech (w tym bieżący weekend 0-4 dni, szybko wygasające treningi, doświadczenie do dnia t).
    Wyjście:
      - col 0: pred_quality w (0, 1) — znormalizowana jakość konkursowa (ranking + punkty relatywne)
      - col 1: pred_dist_ratio — stosunek długości skoku do rozmiaru skoczni HS
    100% Permutation-Invariant (niezależna od kolejności listy startowej).
    """

    def __init__(self, input_dim: int = INPUT_FEATURES_DIM, dropout: float = 0.12):
        super().__init__()
        self.backbone = nn.Sequential(
            nn.Linear(input_dim, 64),
            nn.LayerNorm(64),
            nn.GELU(),
            nn.Dropout(dropout),

            nn.Linear(64, 32),
            nn.LayerNorm(32),
            nn.GELU(),
        )
        self.head = nn.Linear(32, 2)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.backbone(x)
        raw = self.head(h)
        quality = torch.sigmoid(raw[:, 0:1])
        dist_ratio = raw[:, 1:2]
        return torch.cat([quality, dist_ratio], dim=1)


def build_training_dataset(
    sessions: List[Dict[str, Any]],
    feature_extractor: Optional[SkiFeatureExtractor] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    hp: Optional[Dict[str, Any]] = None,
) -> Tuple[SkiFeatureExtractor, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Buduje chronologiczny zbiór uczący wyłącznie na konkursach głównych (COMPETITION)
    z lig WC/OWG/WSC/SFWC (1.0), GP (0.60) i COC (0.35).
    Sesje TRAINING, TRIAL, QUA oraz FC/WJC/FESA trafiają do ekstraktora jako tło historii,
    ale nie tworzą zaszumionych etykiet docelowych (y).
    """
    if feature_extractor is None:
        feature_extractor = SkiFeatureExtractor()
        feature_extractor.fit_from_sessions(sessions)

    sorted_sessions = sorted(
        [s for s in sessions if s.get("date")],
        key=lambda x: (
            str(x.get("date") or "")[:10],
            SESSION_STAGE_ORDER.get(resolve_session_type(x), 3),
            str(x.get("time") or "12:00")[:5],
            str(x.get("_id") or ""),
        ),
    )

    X_list: List[np.ndarray] = []
    y_list: List[List[float]] = []
    w_list: List[float] = []
    sample_seasons: List[int] = []

    total_sess = max(1, len(sorted_sessions))
    for idx_s, s in enumerate(sorted_sessions):
        if progress_cb and idx_s % 100 == 0:
            progress_cb(idx_s, total_sess)

        stype = resolve_session_type(s)
        if stype != "COMPETITION":
            continue

        rcat = resolve_session_category(s)
        sample_weight = TRAINING_COMPETITION_CATEGORY_WEIGHTS.get(rcat)
        if sample_weight is None or sample_weight <= 0.0:
            continue

        try:
            hill_size = float(s.get("hillSize") or 130.0)
        except Exception:
            hill_size = 130.0
        if hill_size <= 0:
            hill_size = 130.0

        hill_type = (s.get("hillType") or "LARGE").upper()
        place = (s.get("event_place") or "").strip().title()
        date_str = str(s.get("date") or "").strip()[:10]
        time_str = str(s.get("time") or "12:00").strip()[:5]
        race_id = str(s.get("raceId") or "")
        session_id = str(s.get("_id") or "")

        try:
            s_date = datetime.date.fromisoformat(date_str)
        except Exception:
            continue

        season_str = str(s.get("season") or "")
        if season_str.isdigit():
            season_int = int(season_str)
        else:
            season_int = s_date.year + 1 if s_date.month >= 6 else s_date.year

        w_data = s.get("weather") or {}
        weather = {
            "headwind_component": float(w_data.get("headwind_component") if w_data.get("headwind_component") is not None else 0.0),
            "wind_speed": float(w_data.get("wind_speed") if w_data.get("wind_speed") is not None else 1.5),
        }

        entries = compute_session_entries(s)
        for fc, entry in entries:
            stats = feature_extractor.get_athlete_stats(
                fis_code=fc,
                hill_type=hill_type,
                place=place,
                target_date=s_date,
                target_category=rcat,
                target_time=time_str,
                target_race_id=race_id,
                target_session_id=session_id,
                hp=hp,
            )
            vec = feature_extractor.build_feature_vector_from_stats(stats, hill_size, weather)
            X_list.append(vec)
            y_list.append([float(entry["session_quality"]), float(entry["raw_ratio"])])
            w_list.append(float(sample_weight))
            sample_seasons.append(season_int)

    X_arr = np.array(X_list, dtype=np.float32) if X_list else np.empty((0, INPUT_FEATURES_DIM), dtype=np.float32)
    y_arr = np.array(y_list, dtype=np.float32) if y_list else np.empty((0, 2), dtype=np.float32)
    w_arr = np.array(w_list, dtype=np.float32) if w_list else np.empty((0,), dtype=np.float32)
    s_arr = np.array(sample_seasons, dtype=np.int32) if sample_seasons else np.empty((0,), dtype=np.int32)
    return feature_extractor, X_arr, y_arr, w_arr, s_arr


def fit_ski_jumping_nn(
    X_arr: np.ndarray,
    y_arr: np.ndarray,
    w_arr: np.ndarray,
    epochs: int = 50,
    batch_size: int = 256,
    lr: float = 0.004,
    dropout: float = 0.12,
    weight_decay: float = 2e-4,
    loss_quality_weight: float = 0.75,
    seed: int = 42,
    epoch_cb: Optional[Callable[[int, float, float], None]] = None,
) -> Tuple[SkiJumpingNN, float, int, float]:
    """
    Trenuje model SkiJumpingNN z chronologicznym podziałem Train (starsze 85%) / Val (najnowsze 15%),
    ważoną funkcją straty (loss_quality_weight * jakość + (1 - loss_quality_weight) * dystans).
    Używa wektorowego krojenia tensorów dla maksymalnej szybkości na CPU.
    """
    torch.manual_seed(seed)
    np.random.seed(seed)

    loss_q_w = max(0.1, min(0.95, float(loss_quality_weight)))
    loss_d_w = 1.0 - loss_q_w

    dataset_size = len(X_arr)
    if dataset_size < 50:
        raise ValueError(f"Zbyt mało próbek konkursowych do nauki sieci: {dataset_size}.")

    val_size = max(20, int(dataset_size * 0.15))
    train_size = dataset_size - val_size

    X_tr = torch.tensor(X_arr[:train_size], dtype=torch.float32)
    y_tr = torch.tensor(y_arr[:train_size], dtype=torch.float32)
    w_tr = torch.tensor(w_arr[:train_size], dtype=torch.float32)

    X_val = torch.tensor(X_arr[train_size:], dtype=torch.float32)
    y_val = torch.tensor(y_arr[train_size:], dtype=torch.float32)
    w_val = torch.tensor(w_arr[train_size:], dtype=torch.float32)
    val_w_sum = float(w_val.sum().clamp(min=1e-6).item())

    model = SkiJumpingNN(input_dim=INPUT_FEATURES_DIM, dropout=dropout)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs, eta_min=1e-5)
    loss_fn = nn.SmoothL1Loss(reduction="none", beta=0.08)

    best_val_loss = float("inf")
    best_val_dist_mse = 0.01
    best_epoch = 1
    best_weights = None

    for epoch in range(epochs):
        model.train()
        perm = torch.randperm(train_size)
        epoch_loss = 0.0
        for start in range(0, train_size, batch_size):
            idx = perm[start:start + batch_size]
            bx, by, bw = X_tr[idx], y_tr[idx], w_tr[idx]
            optimizer.zero_grad()
            preds = model(bx)
            lq = loss_fn(preds[:, 0], by[:, 0])
            ld = loss_fn(preds[:, 1], by[:, 1])
            loss = (bw * (loss_q_w * lq + loss_d_w * ld)).mean()
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item() * len(idx)

        scheduler.step()
        train_loss = epoch_loss / max(1, train_size)

        model.eval()
        with torch.no_grad():
            vp = model(X_val)
            lq = loss_fn(vp[:, 0], y_val[:, 0])
            ld = loss_fn(vp[:, 1], y_val[:, 1])
            val_loss = float((w_val * (loss_q_w * lq + loss_d_w * ld)).sum().item() / val_w_sum)
            val_dist_mse = float(((vp[:, 1] - y_val[:, 1]) ** 2).mean().item())

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_val_dist_mse = val_dist_mse
            best_epoch = epoch + 1
            best_weights = {k: v.cpu().clone() for k, v in model.state_dict().items()}

        if epoch_cb:
            epoch_cb(epoch + 1, train_loss, val_loss)

    if best_weights is not None:
        model.load_state_dict(best_weights)
    model.eval()
    return model, float(best_val_loss), best_epoch, float(best_val_dist_mse)


class PredictionModelManager:
    def __init__(self):
        self.model: Optional[SkiJumpingNN] = None
        self.feature_extractor: SkiFeatureExtractor = SkiFeatureExtractor()
        self.metadata: Dict[str, Any] = {}
        self.is_trained: bool = False

        # Stan treningu na żywo (dla panelu admina)
        self.is_training: bool = False
        self.training_stage: str = "idle"
        self.training_progress_pct: float = 0.0
        self.current_epoch: int = 0
        self.total_epochs: int = 50
        self.training_message: str = ""
        self.training_error: Optional[str] = None
        self.training_started_at: Optional[datetime.datetime] = None

        self.load_model()

    def mark_training_started(self, message: str = "Pobieranie sesji z bazy danych..."):
        self.is_training = True
        self.training_stage = "fetching_db"
        self.training_progress_pct = 5.0
        self.current_epoch = 0
        self.total_epochs = 50
        self.training_message = message
        self.training_error = None
        self.training_started_at = datetime.datetime.now()

    def mark_training_failed(self, error_msg: str):
        self.is_training = False
        self.training_stage = "error"
        self.training_error = error_msg
        self.training_message = f"Błąd treningu: {error_msg}"

    def get_training_status(self) -> Dict[str, Any]:
        return {
            "is_training": self.is_training,
            "stage": self.training_stage,
            "percent_complete": round(self.training_progress_pct, 1),
            "current_epoch": self.current_epoch,
            "total_epochs": self.total_epochs,
            "message": self.training_message,
            "error": self.training_error,
            "started_at": self.training_started_at.isoformat() if self.training_started_at else None,
        }

    def load_model(self) -> bool:
        """Ładuje wytrenowaną sieć neuronową i stan ekstraktora z dysku (z kontrolą kompatybilności cech)."""
        try:
            if MODEL_PATH.exists() and META_PATH.exists():
                with open(META_PATH, "r", encoding="utf-8") as f:
                    meta = json.load(f)

                saved_dim = meta.get("input_dim") or meta.get("input_features")
                if saved_dim != INPUT_FEATURES_DIM:
                    logger.info(f"Zapisany model ma {saved_dim} cech (wymagane {INPUT_FEATURES_DIM}) — wymagany ponowny trening.")
                    self.metadata = meta
                    self.is_trained = False
                    return False

                saved_dropout = float((meta.get("hyperparams") or {}).get("dropout", 0.12))
                model = SkiJumpingNN(input_dim=INPUT_FEATURES_DIM, dropout=saved_dropout)
                model.load_state_dict(torch.load(MODEL_PATH, map_location=torch.device("cpu"), weights_only=True))
                model.eval()
                self.model = model
                self.metadata = meta

                if FEATURE_EXTRACTOR_PATH.exists():
                    with open(FEATURE_EXTRACTOR_PATH, "r", encoding="utf-8") as f:
                        self.feature_extractor.from_dict(json.load(f))

                self.is_trained = True
                logger.info(f"Załadowano model SkiJumpingNN {INPUT_FEATURES_DIM} cech (wytrenowany: {self.metadata.get('trained_at')})")
                return True
        except Exception as e:
            logger.warning(f"Nie udało się załadować zapisanego modelu: {e}")
        return False

    def train(
        self,
        sessions: List[Dict[str, Any]],
        weather_service=None,
        hyperparams: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Trenuje sieć neuronową wyłącznie na konkursach głównych (COMPETITION: WC/OWG/WSC/SFWC/GP/COC)
        z wykorzystaniem aktywnych hiperparametrów z MongoDB/Auto-Tunera i chronologicznego podziału Train/Val.
        """
        hp = sanitize_hyperparams(hyperparams) if hyperparams else get_active_hyperparams()
        epochs = int(hp["epochs"])
        window_seasons = int(hp["rolling_window_seasons"])

        logger.info(f"Rozpoczynanie treningu modelu {INPUT_FEATURES_DIM} cech na {len(sessions)} sesjach...")
        self.is_training = True
        self.training_stage = "extracting_features"
        self.training_progress_pct = 10.0
        self.current_epoch = 0
        self.total_epochs = epochs
        self.training_error = None
        self.training_message = f"Analiza historii zawodników ({len(sessions)} sesji)..."
        if self.training_started_at is None:
            self.training_started_at = datetime.datetime.now()

        try:
            def _extract_progress(idx_s: int, total_s: int):
                self.training_progress_pct = 10.0 + (idx_s / total_s) * 25.0
                self.training_message = f"Ekstrakcja {INPUT_FEATURES_DIM} cech ({idx_s}/{total_s} sesji)..."

            fe, X_all, y_all, w_all, s_all = build_training_dataset(
                sessions,
                progress_cb=_extract_progress,
                hp=hp,
            )
            self.feature_extractor = fe

            # Ruchome okno treningowe: ostatnie window_seasons sezonów (np. 2020-2027)
            unique_seasons = sorted(list({int(s) for s in s_all})) if len(s_all) > 0 else []
            if len(unique_seasons) > window_seasons:
                cutoff_season = unique_seasons[-window_seasons]
                mask = s_all >= cutoff_season
                X_arr, y_arr, w_arr = X_all[mask], y_all[mask], w_all[mask]
                used_seasons = [s for s in unique_seasons if s >= cutoff_season]
            else:
                X_arr, y_arr, w_arr = X_all, y_all, w_all
                used_seasons = unique_seasons

            seasons_range_str = f"{used_seasons[0]}–{used_seasons[-1]}" if used_seasons else "N/A"
            self.training_stage = "training_model"

            def _epoch_progress(ep: int, tr_loss: float, va_loss: float):
                self.current_epoch = ep
                self.training_progress_pct = 35.0 + (ep / self.total_epochs) * 60.0
                self.training_message = f"Epoka {ep}/{self.total_epochs} — strata train: {tr_loss:.4f}, val: {va_loss:.4f}"

            model, best_val_loss, best_epoch, best_dist_mse = fit_ski_jumping_nn(
                X_arr,
                y_arr,
                w_arr,
                epochs=epochs,
                lr=float(hp["lr"]),
                dropout=float(hp["dropout"]),
                weight_decay=float(hp["weight_decay"]),
                loss_quality_weight=float(hp["loss_quality_weight"]),
                epoch_cb=_epoch_progress,
            )
            self.model = model

            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            torch.save(self.model.state_dict(), MODEL_PATH)

            mae_dist = float(math.sqrt(max(0.0, best_dist_mse)) * 135.0)
            num_samples = int(len(X_arr))
            self.metadata = {
                "trained_at": datetime.datetime.now().isoformat(),
                "num_sessions": len(sessions),
                "num_samples": num_samples,
                "sample_count": num_samples,
                "epochs": self.total_epochs,
                "best_epoch": best_epoch,
                "best_val_loss": round(best_val_loss, 5),
                "val_loss": round(best_val_loss, 5),
                "mae_distance_meters_approx": round(mae_dist, 2),
                "input_dim": INPUT_FEATURES_DIM,
                "input_features": INPUT_FEATURES_DIM,
                "training_window_seasons": window_seasons,
                "training_seasons_range": seasons_range_str,
                "hyperparams": hp,
                "validation_split": "chronological_85_15",
                "model_architecture": f"SkiJumpingNN({INPUT_FEATURES_DIM}->64->32->2, Quality+Distance)",
            }

            with open(META_PATH, "w", encoding="utf-8") as f:
                json.dump(self.metadata, f, indent=2)

            with open(FEATURE_EXTRACTOR_PATH, "w", encoding="utf-8") as f:
                json.dump(self.feature_extractor.to_dict(), f)

            self.is_trained = True
            self.training_stage = "completed"
            self.training_progress_pct = 100.0
            self.training_message = f"Trening ukończony! MAE dystansu (val): ~{self.metadata['mae_distance_meters_approx']}m"
            logger.info(f"Trening ukończony! Best epoch: {best_epoch}, val_loss: {best_val_loss:.5f}, MAE: ~{mae_dist:.2f}m")
            return self.metadata
        except Exception as e:
            self.mark_training_failed(str(e))
            raise
        finally:
            self.is_training = False

    def predict(
        self,
        athletes: List[Dict[str, Any]],
        hill_size: float,
        hill_type: str,
        place: Optional[str],
        weather: Dict[str, Any],
        target_date: Optional[datetime.date] = None,
        weekend_sessions: Optional[List[Dict[str, Any]]] = None,
        num_simulations: int = 100,
        target_category: str = DEFAULT_TARGET_CATEGORY,
        target_time: Optional[str] = None,
        target_race_id: Optional[str] = None,
        target_session_id: Optional[str] = None,
        model_override: Optional[SkiJumpingNN] = None,
        fe_override: Optional[SkiFeatureExtractor] = None,
        hyperparams: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generuje przewidywania dla listy zawodników:
        - Wykorzystuje na żywo weekend_sessions (treningi, próbną, QUA, wcześniejszy konkurs)
        - 100% Permutation-Invariant
        - Sortowanie i symulacje Monte Carlo oparte o wyuczoną jakość konkursową (pred_quality) + fizykę wiatru
        """
        active_hp = hyperparams if hyperparams is not None else get_active_hyperparams()
        blend_q_w = max(0.25, min(0.95, float(active_hp.get("points_blend_quality_weight", 0.70))))
        blend_d_w = 1.0 - blend_q_w

        active_model = model_override or self.model
        active_fe = fe_override or self.feature_extractor

        if active_model is None or (model_override is None and not self.is_trained):
            if not self.load_model() or self.model is None:
                raise RuntimeError("Model sieci neuronowej nie jest jeszcze wytrenowany!")
            active_model = self.model
            active_fe = self.feature_extractor

        active_model.eval()
        hs = float(hill_size or 130.0)
        ht = (hill_type or "LARGE").upper()

        weekend_overlay = active_fe.build_weekend_overlay(weekend_sessions)

        feature_matrix = []
        valid_athletes = []
        stats_list = []
        athlete_sigmas = []

        for ath in athletes:
            fis_code = str(ath.get("fis_code", "")).strip()
            if not fis_code:
                continue

            stats = active_fe.get_athlete_stats(
                fis_code=fis_code,
                hill_type=ht,
                place=place,
                target_date=target_date,
                target_category=target_category,
                target_time=target_time,
                target_race_id=target_race_id,
                target_session_id=target_session_id,
                extra_entries=weekend_overlay.get(fis_code),
                hp=active_hp,
            )
            vec = active_fe.build_feature_vector_from_stats(stats, hs, weather)

            feature_matrix.append(vec)
            valid_athletes.append(ath)
            stats_list.append(stats)

            # Indywidualna sigma punktowa (mniejsza jeśli skoczek ma pewne dane z tego weekendu)
            base_sigma = float(stats["consistency_sigma"]) * 135.0
            conf_factor = 1.0 - 0.15 * float(stats.get("weekend_presence_confidence", 0.0))
            sigma_pts = max(3.0, min(10.0, base_sigma * conf_factor))
            athlete_sigmas.append(sigma_pts)

        if not valid_athletes:
            return []

        X_tensor = torch.tensor(np.array(feature_matrix), dtype=torch.float32)

        with torch.no_grad():
            preds = active_model(X_tensor).numpy()

        results = []
        base_points_list = []

        headwind = float(
            weather.get("headwind_component")
            if weather.get("headwind_component") is not None
            else (weather.get("headwind") if weather.get("headwind") is not None else 0.0)
        )
        base_wind_rate = 4.8 if hs >= 200 else (2.4 if hs >= 110 else 1.8)

        for i, ath in enumerate(valid_athletes):
            pred_quality = float(preds[i, 0])
            pred_dist_ratio = float(preds[i, 1])
            ath_code = str(ath.get("fis_code"))
            stats = stats_list[i]

            wind_profile = active_fe.get_athlete_wind_profile(
                ath_code,
                hill_size=hs,
                hill_type=ht,
                stats=stats,
            )

            if headwind >= 0:
                eff = wind_profile["headwind_efficiency"]
                wind_dist_delta = headwind * base_wind_rate * eff
            else:
                pen = wind_profile["tailwind_penalty_factor"]
                wind_dist_delta = headwind * base_wind_rate * pen

            pred_dist = round(pred_dist_ratio * hs + wind_dist_delta, 1)
            jump_pts = calculate_fis_jump_points(pred_dist, hs, ht, headwind)

            # Wyuczona jakość konkursowa (pred_quality) jest głównym wyznacznikiem pozycji w konkursie,
            # wiatr profilowy i fizyczne punkty FIS stanowią lekki komponent aerodynamiczno-punktowy
            wind_score_adj = 0.015 * (wind_dist_delta / max(hs, 100.0))
            ranking_score = max(0.0, min(1.05, pred_quality + wind_score_adj))
            pred_pts = round(blend_q_w * (130.0 + ranking_score * 170.0) + blend_d_w * (jump_pts * 2.0), 1)

            base_points_list.append(pred_pts)
            profiles = active_fe.classify_athlete_profiles(ath_code, stats=stats)

            results.append({
                "fis_code": ath.get("fis_code"),
                "name": ath.get("full_name") or ath.get("name", "Nieznany"),
                "country": ath.get("country_code") or ath.get("country", ""),
                "price": ath.get("price"),
                "predicted_distance": pred_dist,
                "predicted_points": pred_pts,
                "predicted_quality": round(pred_quality * 100, 1),
                "wind_profile": wind_profile["profile_type"],
                "headwind_efficiency": wind_profile["headwind_efficiency"],
                "tailwind_penalty": wind_profile["tailwind_penalty_factor"],
                "form_score": round(stats["weighted_form_quality"] * 100, 1),
                "recent_form_score": round(stats["recent_form_quality"] * 100, 1),
                "weekend_form_score": round(stats["weekend_precomp_quality"] * 100, 1),
                "weekend_confidence": round(stats["weekend_presence_confidence"] * 100, 1),
                "consistency_score": stats["consistency_score"],
                "conversion_delta": round(stats["conversion_delta"], 1),
                "hill_affinity_score": round(stats["hill_type_affinity_quality"] * 100, 1),
                "venue_affinity_score": round(stats["venue_affinity_quality"] * 100, 1),
                "current_season_active": round(stats["current_season_activity"] * 100, 1),
                "profiles": profiles,
                "is_tier_debutant": bool(stats.get("is_tier_debutant", 0)),
                "tier_starts_count": stats.get("tier_starts_count", 0),
                "wc_starts_count": stats.get("wc_count", 0),
            })

        # Symulacje Monte Carlo dla prawdopodobieństwa zwycięstwa, podium, Top 10 i Top 30
        n_athletes = len(results)
        if num_simulations > 0:
            num_sims = max(50, min(1000, num_simulations))
            win_counts = np.zeros(n_athletes, dtype=int)
            podium_counts = np.zeros(n_athletes, dtype=int)
            top10_counts = np.zeros(n_athletes, dtype=int)
            top30_counts = np.zeros(n_athletes, dtype=int)

            base_pts_arr = np.array(base_points_list)
            sigmas_arr = np.array(athlete_sigmas)

            np.random.seed(42)
            simulated_scores = np.random.normal(
                loc=base_pts_arr[:, np.newaxis],
                scale=sigmas_arr[:, np.newaxis],
                size=(n_athletes, num_sims),
            )

            for s_idx in range(num_sims):
                sim_pts = simulated_scores[:, s_idx]
                order = np.argsort(-sim_pts)
                win_counts[order[0]] += 1
                for p in order[:3]:
                    podium_counts[p] += 1
                for t in order[:10]:
                    top10_counts[t] += 1
                for t in order[:30]:
                    top30_counts[t] += 1

            for i, r in enumerate(results):
                r["win_probability"] = round(float(win_counts[i] / num_sims) * 100, 1)
                r["podium_probability"] = round(float(podium_counts[i] / num_sims) * 100, 1)
                r["top10_probability"] = round(float(top10_counts[i] / num_sims) * 100, 1)
                r["top30_probability"] = round(float(top30_counts[i] / num_sims) * 100, 1)

        results.sort(key=lambda x: x["predicted_points"], reverse=True)
        for rank_idx, r in enumerate(results, 1):
            r["predicted_rank"] = rank_idx

        return results


_prediction_manager_instance: Optional[PredictionModelManager] = None


def get_prediction_manager() -> PredictionModelManager:
    global _prediction_manager_instance
    if _prediction_manager_instance is None:
        _prediction_manager_instance = PredictionModelManager()
    return _prediction_manager_instance
