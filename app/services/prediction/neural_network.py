import os
import json
import math
import logging
import datetime
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from app.services.prediction.feature_extractor import SkiFeatureExtractor
from app.services.prediction.prediction_config import (
    INPUT_FEATURES_DIM,
    DEFAULT_TARGET_CATEGORY,
    resolve_session_category
)

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "model_weights"
MODEL_PATH = MODEL_DIR / "ski_jumping_nn.pt"
META_PATH = MODEL_DIR / "model_meta.json"
FEATURE_EXTRACTOR_PATH = MODEL_DIR / "feature_extractor_state.json"


def calculate_fis_jump_points(dist: float, hs: float, ht: str = "LARGE", headwind: float = 0.0) -> float:
    """
    Fizyczne obliczanie punktów FIS za skok narciarski:
    - Punkty za odległość (Distance Points): K_base + (dist - K) * meter_value
    - Punkty sędziowskie za styl (Style Points): 51.0 (krótkie) do 57.5 (długie z telemarkiem)
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
    ratio = dist / hs
    style_pts = 52.0 + 5.0 * max(0.0, min(1.0, (ratio - 0.85) / 0.15))
    wind_pts = -headwind * base_wind_rate
    return dist_pts + style_pts + wind_pts


class SkiJumpingNN(nn.Module):
    """
    Głęboka sieć neuronowa do przewidywania wyników skoków narciarskich.
    Wejście: 14 czystych cech fizyczno-sportowych.
    Wyjście: [pred_dist_ratio] (stosunek osiąganej odległości do rozmiaru skoczni).
    Punkty są wyliczane bezpośrednio z fizycznych reguł FIS.
    100% Permutation-Invariant (niezależna od kolejności na liście).
    """
    def __init__(self, input_dim: int = INPUT_FEATURES_DIM):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, 64),
            nn.LayerNorm(64),
            nn.GELU(),
            nn.Dropout(0.10),

            nn.Linear(64, 32),
            nn.LayerNorm(32),
            nn.GELU(),

            nn.Linear(32, 1)
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


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
            "started_at": self.training_started_at.isoformat() if self.training_started_at else None
        }

    def load_model(self) -> bool:
        """Ładuje wytrenowaną sieć neuronową i stan ekstraktora z dysku."""
        try:
            if MODEL_PATH.exists() and META_PATH.exists():
                self.model = SkiJumpingNN(input_dim=INPUT_FEATURES_DIM)
                self.model.load_state_dict(torch.load(MODEL_PATH, map_location=torch.device('cpu'), weights_only=True))
                self.model.eval()

                with open(META_PATH, "r", encoding="utf-8") as f:
                    self.metadata = json.load(f)

                if FEATURE_EXTRACTOR_PATH.exists():
                    with open(FEATURE_EXTRACTOR_PATH, "r", encoding="utf-8") as f:
                        saved_fe = json.load(f)
                        self.feature_extractor.from_dict(saved_fe)

                self.is_trained = True
                logger.info(f"Załadowano model sieci neuronowej 14 cech (wytrenowany: {self.metadata.get('trained_at')})")
                return True
        except Exception as e:
            logger.warning(f"Nie udało się załadować zapisanego modelu: {e}")
        return False

    def train(self, sessions: List[Dict[str, Any]], weather_service=None) -> Dict[str, Any]:
        """
        Trenuje sieć neuronową na danych konkursowych i kwalifikacyjnych z MongoDB.
        Wyklucza treningi (treningi to testy belek/sprzętu o znikomej wartości).
        Czysty wektor 14 cech, przewidywanie stosunku odległości bez target leakage.
        """
        logger.info(f"Rozpoczynanie treningu czystego modelu 14 cech na {len(sessions)} sesjach...")
        self.is_training = True
        self.training_stage = "extracting_features"
        self.training_progress_pct = 10.0
        self.current_epoch = 0
        self.total_epochs = 50
        self.training_error = None
        self.training_message = f"Analiza historii zawodników ({len(sessions)} sesji)..."
        if self.training_started_at is None:
            self.training_started_at = datetime.datetime.now()

        try:
            self.feature_extractor.fit_from_sessions(sessions)

            X_list = []
            y_list = []

            sorted_sessions = sorted(
                [s for s in sessions if s.get("date")],
                key=lambda x: (str(x.get("date") or ""), str(x.get("time") or ""))
            )
            total_sess = max(1, len(sorted_sessions))

            for idx_s, s in enumerate(sorted_sessions):
                if idx_s % 100 == 0:
                    self.training_progress_pct = 10.0 + (idx_s / total_sess) * 25.0
                    self.training_message = f"Ekstrakcja 14 cech ({idx_s}/{total_sess} sesji)..."

                stype = str(s.get("session_type") or "").upper()
                # Treningi mają znikome znaczenie - model zawodów uczy się na konkursach i kwalifikacjach
                if "TRAIN" in stype:
                    continue

                hill_size = float(s.get("hillSize") or 130)
                hill_type = (s.get("hillType") or "LARGE").upper()
                place = (s.get("event_place") or "").strip().title()
                date_str = str(s.get("date") or "").strip()[:10]

                try:
                    s_date = datetime.date.fromisoformat(date_str)
                except Exception:
                    s_date = datetime.date.today()

                w = s.get("weather") or {}
                weather = {
                    "headwind_component": float(w.get("headwind_component", 0.0) if w.get("headwind_component") is not None else 0.0),
                    "wind_speed": float(w.get("wind_speed", 1.5) if w.get("wind_speed") is not None else 1.5),
                }

                results = s.get("results_data", {}).get("results", [])
                for r in results:
                    fc = str(r.get("fis_code", "")).strip()
                    if not fc:
                        continue

                    jumps = r.get("jumps", [])
                    distances = [float(j["distance"]) for j in jumps if j.get("distance") and float(j["distance"]) > 0]
                    if not distances:
                        continue

                    best_dist = max(distances)
                    dist_ratio = best_dist / hill_size

                    vec = self.feature_extractor.extract_features(
                        fis_code=fc,
                        hill_size=hill_size,
                        hill_type=hill_type,
                        place=place,
                        weather=weather,
                        target_date=s_date,
                    )

                    X_list.append(vec)
                    y_list.append([dist_ratio])

            if len(X_list) < 50:
                raise ValueError(f"Zbyt mało danych treningowych do nauki sieci: {len(X_list)} próbek.")

            X = torch.tensor(np.array(X_list, dtype=np.float32))
            y = torch.tensor(np.array(y_list, dtype=np.float32))

            dataset_size = len(X)
            val_size = max(20, int(dataset_size * 0.15))
            train_size = dataset_size - val_size

            indices = torch.randperm(dataset_size)
            train_indices = indices[:train_size]
            val_indices = indices[train_size:]

            train_dataset = TensorDataset(X[train_indices], y[train_indices])
            val_dataset = TensorDataset(X[val_indices], y[val_indices])

            train_loader = DataLoader(train_dataset, batch_size=64, shuffle=True)
            val_loader = DataLoader(val_dataset, batch_size=128, shuffle=False)

            self.training_stage = "training_model"
            self.model = SkiJumpingNN(input_dim=INPUT_FEATURES_DIM)
            optimizer = torch.optim.AdamW(self.model.parameters(), lr=0.005, weight_decay=1e-4)
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=self.total_epochs, eta_min=1e-5)
            criterion = nn.MSELoss()

            best_val_loss = float("inf")
            best_model_weights = None

            for epoch in range(self.total_epochs):
                self.current_epoch = epoch + 1
                self.model.train()
                epoch_loss = 0.0

                for batch_X, batch_y in train_loader:
                    optimizer.zero_grad()
                    preds = self.model(batch_X)
                    loss = criterion(preds, batch_y)
                    loss.backward()
                    optimizer.step()
                    epoch_loss += loss.item() * len(batch_X)

                scheduler.step()
                train_loss = epoch_loss / train_size

                # Walidacja
                self.model.eval()
                val_loss = 0.0
                with torch.no_grad():
                    for val_X, val_y in val_loader:
                        v_preds = self.model(val_X)
                        v_loss = criterion(v_preds, val_y)
                        val_loss += v_loss.item() * len(val_X)
                val_loss /= val_size

                if val_loss < best_val_loss:
                    best_val_loss = val_loss
                    best_model_weights = {k: v.cpu().clone() for k, v in self.model.state_dict().items()}

                self.training_progress_pct = 35.0 + ((epoch + 1) / self.total_epochs) * 60.0
                self.training_message = f"Epoka {epoch + 1}/{self.total_epochs} — strata train: {train_loss:.4f}, val: {val_loss:.4f}"

            if best_model_weights is not None:
                self.model.load_state_dict(best_model_weights)
            self.model.eval()

            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            torch.save(self.model.state_dict(), MODEL_PATH)

            mae_dist = float(math.sqrt(best_val_loss) * 135.0)
            self.metadata = {
                "trained_at": datetime.datetime.now().isoformat(),
                "num_sessions": len(sessions),
                "num_samples": len(X),
                "epochs": self.total_epochs,
                "best_val_loss": round(float(best_val_loss), 5),
                "mae_distance_meters_approx": round(mae_dist, 2),
                "input_dim": INPUT_FEATURES_DIM,
                "model_architecture": "SkiJumpingNN(14->64->32->1, PhysicsPoints)"
            }

            with open(META_PATH, "w", encoding="utf-8") as f:
                json.dump(self.metadata, f, indent=2)

            with open(FEATURE_EXTRACTOR_PATH, "w", encoding="utf-8") as f:
                json.dump(self.feature_extractor.to_dict(), f)

            self.is_trained = True
            self.training_stage = "completed"
            self.training_progress_pct = 100.0
            self.training_message = f"Trening ukończony! MAE dystansu: ~{self.metadata['mae_distance_meters_approx']}m"
            logger.info(f"Trening ukończony! MAE: ~{self.metadata['mae_distance_meters_approx']}m")
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
        target_category: str = DEFAULT_TARGET_CATEGORY
    ) -> List[Dict[str, Any]]:
        """
        Generuje przewidywania dla listy zawodników:
        - 100% Permutation-Invariant (zmiana kolejności listy ma dokładnie 0.000000 pkt różnicy)
        - Fizyczne powiązanie odległości i punktów regułami FIS
        - Wylicza top30_probability, podium_probability, win_probability
        - Cena (ESM) jest informacyjna i NIE wpływa na wynik predykcji
        """
        if self.model is None or not self.is_trained:
            if not self.load_model():
                raise RuntimeError("Model sieci neuronowej nie jest jeszcze wytrenowany!")

        self.model.eval()
        hs = float(hill_size or 130.0)
        ht = (hill_type or "LARGE").upper()

        feature_matrix = []
        valid_athletes = []
        athlete_sigmas = []

        for ath in athletes:
            fis_code = str(ath.get("fis_code", "")).strip()
            if not fis_code:
                continue

            vec = self.feature_extractor.extract_features(
                fis_code=fis_code,
                hill_size=hs,
                hill_type=ht,
                place=place,
                weather=weather,
                target_date=target_date,
                target_category=target_category
            )
            feature_matrix.append(vec)
            valid_athletes.append(ath)

            stats = self.feature_extractor.get_athlete_stats(
                fis_code,
                hill_type=ht,
                place=place,
                target_date=target_date,
                target_category=target_category
            )
            # Indywidualna sigma punktowa w skokach
            sigma_pts = max(3.0, min(10.0, float(stats["consistency_sigma"]) * 140.0))
            athlete_sigmas.append(sigma_pts)

        if not valid_athletes:
            return []

        X_tensor = torch.tensor(np.array(feature_matrix), dtype=torch.float32)

        with torch.no_grad():
            preds = self.model(X_tensor).numpy()

        results = []
        base_points_list = []
        base_dist_list = []

        headwind = float(weather.get("headwind_component") if weather.get("headwind_component") is not None else (weather.get("headwind") if weather.get("headwind") is not None else 0.0))
        wind_spd = float(weather.get("wind_speed", 1.5))
        base_wind_rate = 4.8 if hs >= 200 else (2.4 if hs >= 110 else 1.8)

        for i, ath in enumerate(valid_athletes):
            pred_ratio = float(preds[i, 0])
            ath_code = str(ath.get("fis_code"))

            stats = self.feature_extractor.get_athlete_stats(
                ath_code,
                hill_type=ht,
                place=place,
                target_date=target_date,
                target_category=target_category
            )
            wind_profile = self.feature_extractor.get_athlete_wind_profile(
                ath_code,
                hill_size=hs,
                hill_type=ht,
                stats=stats
            )

            # Rzeczywisty wpływ wiatru bez zależności od kolejności
            if headwind >= 0:
                eff = wind_profile["headwind_efficiency"]
                wind_dist_delta = headwind * base_wind_rate * eff
            else:
                pen = wind_profile["tailwind_penalty_factor"]
                wind_dist_delta = headwind * base_wind_rate * pen

            # Łączenie wskaźników formy konkursowej z predykcją sieci
            # Sieć daje bazowy potencjał fizyczny, a świeża dyspozycja (recent_form) i aktywność zimowa decydują o formie dnia
            calibrated_ratio = 0.50 * pred_ratio + 0.30 * stats["recent_form_ratio"] + 0.10 * stats["venue_affinity"] + 0.10 * stats["weighted_form_ratio"]
            pred_dist = round(calibrated_ratio * hs + wind_dist_delta, 1)

            # Fizyczne obliczenie punktów za 1 skok wg zasad FIS
            jump_pts = calculate_fis_jump_points(pred_dist, hs, ht, headwind)
            # W konkursie 2-seryjnym: 2 skoki dla czołówki
            pred_pts = round(jump_pts * 2.0, 1)

            base_dist_list.append(pred_dist)
            base_points_list.append(pred_pts)

            profiles = self.feature_extractor.classify_athlete_profiles(ath_code, stats=stats)

            results.append({
                "fis_code": ath.get("fis_code"),
                "name": ath.get("full_name") or ath.get("name", "Nieznany"),
                "country": ath.get("country_code") or ath.get("country", ""),
                "price": ath.get("price"),
                "predicted_distance": pred_dist,
                "predicted_points": pred_pts,
                "wind_profile": wind_profile["profile_type"],
                "headwind_efficiency": wind_profile["headwind_efficiency"],
                "tailwind_penalty": wind_profile["tailwind_penalty_factor"],
                "form_score": round(stats["weighted_form_ratio"] * 100, 1),
                "recent_form_score": round(stats.get("recent_form_ratio", stats["weighted_form_ratio"]) * 100, 1),
                "consistency_score": stats["consistency_score"],
                "conversion_delta": round(stats["conversion_delta"], 1),
                "hill_affinity_score": round(stats["hill_type_affinity"] * 100, 1),
                "venue_affinity_score": round(stats["venue_affinity"] * 100, 1),
                "current_season_active": round(stats["current_season_activity"] * 100, 1),
                "profiles": profiles,
                "is_tier_debutant": bool(stats.get("is_tier_debutant", 0)),
                "tier_starts_count": stats.get("tier_starts_count", 0),
                "wc_starts_count": stats.get("wc_count", 0),
            })

        # Symulacje Monte Carlo dla prawdopodobieństwa zwycięstwa, podium i Top 30
        n_athletes = len(results)
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
            size=(n_athletes, num_sims)
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
