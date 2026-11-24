import os
import json
import logging
import datetime
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from app.services.prediction.feature_extractor import SkiFeatureExtractor

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "model_weights"
MODEL_PATH = MODEL_DIR / "ski_jumping_nn.pt"
META_PATH = MODEL_DIR / "model_meta.json"
FEATURE_EXTRACTOR_PATH = MODEL_DIR / "feature_extractor_state.json"


class SkiJumpingNN(nn.Module):
    """
    Głęboka sieć neuronowa do przewidywania wyników skoków narciarskich.
    Wejście: 18 cech (skocznia, wagi sesji, time-decay, jakość obsady, powtarzalność, konwersja presji, kontekst weekendu, pogoda).
    Wyjście: [pred_dist_ratio, pred_points_normalized]
    """
    def __init__(self, input_dim: int = 18):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, 128),
            nn.LayerNorm(128),
            nn.ReLU(),
            nn.Dropout(0.12),

            nn.Linear(128, 64),
            nn.LayerNorm(64),
            nn.ReLU(),
            nn.Dropout(0.08),

            nn.Linear(64, 32),
            nn.LayerNorm(32),
            nn.ReLU(),

            nn.Linear(32, 2)
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class PredictionModelManager:
    def __init__(self):
        self.model: Optional[SkiJumpingNN] = None
        self.feature_extractor: SkiFeatureExtractor = SkiFeatureExtractor()
        self.metadata: Dict[str, Any] = {}
        self.is_trained: bool = False

        self.load_model()

    def load_model(self) -> bool:
        """Ładuje wytrenowaną sieć neuronową i metadane z dysku."""
        try:
            if MODEL_PATH.exists() and META_PATH.exists():
                self.model = SkiJumpingNN(input_dim=18)
                self.model.load_state_dict(torch.load(MODEL_PATH, map_location=torch.device('cpu'), weights_only=True))
                self.model.eval()

                with open(META_PATH, "r", encoding="utf-8") as f:
                    self.metadata = json.load(f)

                if FEATURE_EXTRACTOR_PATH.exists():
                    with open(FEATURE_EXTRACTOR_PATH, "r", encoding="utf-8") as f:
                        saved_fe = json.load(f)
                        self.feature_extractor.athlete_history = saved_fe.get("athlete_history", {})
                        self.feature_extractor.wc_athletes_fis = set(saved_fe.get("wc_athletes_fis", []))
                        self.feature_extractor.athlete_base_ratings = saved_fe.get("athlete_base_ratings", {})

                self.is_trained = True
                logger.info(f"Załadowano model sieci neuronowej (wytrenowany: {self.metadata.get('trained_at')})")
                return True
        except Exception as e:
            logger.warning(f"Nie udało się załadować zapisanego modelu: {e}")
        return False

    def train(self, sessions: List[Dict[str, Any]], weather_service=None) -> Dict[str, Any]:
        """
        Trenuje sieć neuronową na danych ze wszystkich sesji w MongoDB z uwzględnieniem
        jakości obsady serii, wag typów sesji, zanikania w czasie i wskaźników powtarzalności.
        """
        logger.info(f"Rozpoczynanie przygotowania 18 cech i treningu na {len(sessions)} sesjach...")

        self.feature_extractor.fit_from_sessions(sessions)

        X_list = []
        y_list = []

        sorted_sessions = sorted(
            [s for s in sessions if s.get("date")],
            key=lambda x: (str(x.get("date") or ""), str(x.get("time") or ""))
        )

        for s in sorted_sessions:
            hill_size = float(s.get("hillSize") or 130)
            hill_type = (s.get("hillType") or "LARGE").upper()
            place = (s.get("event_place") or "").strip().title()
            date_str = str(s.get("date") or "").strip()[:10]

            try:
                s_date = datetime.date.fromisoformat(date_str)
            except Exception:
                s_date = datetime.date.today()

            weather = {
                "headwind_component": float(np.random.uniform(-1.5, 2.0)),
                "wind_speed": float(np.random.uniform(0.5, 4.0)),
                "temperature": float(np.random.uniform(-10.0, 4.0)),
                "surface_pressure": float(np.random.uniform(940.0, 990.0)),
                "relative_humidity": float(np.random.uniform(50.0, 90.0)),
            }

            results = s.get("results_data", {}).get("results", [])
            for r in results:
                raw_fis = r.get("fis_code")
                if not raw_fis:
                    continue
                fis_code = str(raw_fis).strip()

                jumps = r.get("jumps", [])
                distances = [float(j["distance"]) for j in jumps if j.get("distance") and float(j["distance"]) > 0]
                if not distances:
                    continue

                best_dist = max(distances)
                total_pts = float(r.get("total_points") or 0.0)
                dist_ratio = best_dist / hill_size if hill_size > 0 else 0.9
                norm_pts = total_pts / 300.0

                vec = self.feature_extractor.extract_features(
                    fis_code=fis_code,
                    hill_size=hill_size,
                    hill_type=hill_type,
                    place=place,
                    weather=weather,
                    target_date=s_date,
                    weekend_sessions=[]
                )
                X_list.append(vec)
                y_list.append([dist_ratio, norm_pts])

        if len(X_list) < 100:
            raise ValueError(f"Zbyt mało próbek treningowych ({len(X_list)}). Wymagane co najmniej 100.")

        X = np.array(X_list, dtype=np.float32)
        y = np.array(y_list, dtype=np.float32)

        n_samples = len(X)
        indices = np.arange(n_samples)
        np.random.seed(42)
        np.random.shuffle(indices)

        val_size = int(n_samples * 0.15)
        train_idx, val_idx = indices[val_size:], indices[:val_size]

        X_train, y_train = torch.tensor(X[train_idx]), torch.tensor(y[train_idx])
        X_val, y_val = torch.tensor(X[val_idx]), torch.tensor(y[val_idx])

        train_dataset = TensorDataset(X_train, y_train)
        train_loader = DataLoader(train_dataset, batch_size=64, shuffle=True)

        self.model = SkiJumpingNN(input_dim=18)
        self.model.train()

        criterion = nn.SmoothL1Loss()
        optimizer = torch.optim.AdamW(self.model.parameters(), lr=0.002, weight_decay=1e-4)

        epochs = 20

        for epoch in range(epochs):
            for batch_x, batch_y in train_loader:
                optimizer.zero_grad()
                pred = self.model(batch_x)
                loss = criterion(pred, batch_y)
                loss.backward()
                optimizer.step()

        self.model.eval()

        with torch.no_grad():
            final_pred = self.model(X_val)
            val_loss = criterion(final_pred, y_val).item()
            mae_ratio = torch.mean(torch.abs(final_pred[:, 0] - y_val[:, 0])).item()
            mae_pts = torch.mean(torch.abs(final_pred[:, 1] - y_val[:, 1])).item() * 300.0

        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        torch.save(self.model.state_dict(), MODEL_PATH)

        self.metadata = {
            "trained_at": datetime.datetime.now().isoformat(),
            "sample_count": n_samples,
            "train_samples": len(train_idx),
            "val_samples": len(val_idx),
            "epochs": epochs,
            "val_loss": round(val_loss, 5),
            "mae_distance_meters_approx": round(mae_ratio * 130.0, 1),
            "mae_points": round(mae_pts, 1),
            "active_athletes_tracked": len(self.feature_extractor.athlete_history),
            "wc_athletes_count": len(self.feature_extractor.wc_athletes_fis)
        }

        with open(META_PATH, "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2, ensure_ascii=False)

        with open(FEATURE_EXTRACTOR_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "athlete_history": self.feature_extractor.athlete_history,
                "wc_athletes_fis": list(self.feature_extractor.wc_athletes_fis),
                "athlete_base_ratings": self.feature_extractor.athlete_base_ratings
            }, f)

        self.is_trained = True
        logger.info(f"Trening ukończony! MAE dystansu: ~{self.metadata['mae_distance_meters_approx']}m")
        return self.metadata

    def predict(
        self,
        athletes: List[Dict[str, Any]],
        hill_size: float,
        hill_type: str,
        place: Optional[str],
        weather: Dict[str, Any],
        target_date: Optional[datetime.date] = None,
        weekend_sessions: Optional[List[Dict[str, Any]]] = None,
        num_simulations: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Generuje przewidywania dla listy zawodników z uwzględnieniem indywidualnej
        wariancji powtarzalności (indywidualne sigma) w symulacji Monte Carlo.
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
                weekend_sessions=weekend_sessions or []
            )
            feature_matrix.append(vec)
            valid_athletes.append(ath)

            stats = self.feature_extractor.get_athlete_stats(
                fis_code, hill_type=ht, place=place, target_date=target_date
            )
            # Indywidualna sigma punktowa w skokach: metronom ~3.5 pkt, chwiejny ~8.5 pkt
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

        for i, ath in enumerate(valid_athletes):
            dist_ratio = float(preds[i, 0])
            norm_pts = float(preds[i, 1])

            headwind = float(weather.get("headwind_component", 0.0))
            wind_dist_delta = headwind * (3.5 if hs >= 200 else 2.2)
            wind_pts_delta = -headwind * (7.2 if hs >= 200 else 4.8)

            pred_dist = round(dist_ratio * hs + wind_dist_delta, 1)
            pred_pts = round(norm_pts * 300.0 + wind_pts_delta, 1)

            base_dist_list.append(pred_dist)
            base_points_list.append(pred_pts)

            stats = self.feature_extractor.get_athlete_stats(
                str(ath.get("fis_code")),
                hill_type=ht,
                place=place,
                target_date=target_date
            )

            profiles = self.feature_extractor.classify_athlete_profiles(
                str(ath.get("fis_code")),
                stats=stats
            )

            results.append({
                "fis_code": ath.get("fis_code"),
                "name": ath.get("full_name") or ath.get("name", "Nieznany"),
                "country": ath.get("country_code") or ath.get("country", ""),
                "price": ath.get("price"),
                "predicted_distance": pred_dist,
                "predicted_points": pred_pts,
                "form_score": round(stats["weighted_form_ratio"] * 100, 1),
                "consistency_score": stats["consistency_score"],
                "conversion_delta": round(stats["conversion_delta"], 1),
                "hill_affinity_score": round(stats["hill_type_affinity"] * 100, 1),
                "venue_affinity_score": round(stats["venue_affinity"] * 100, 1),
                "current_season_active": round(stats["current_season_activity"] * 100, 1),
                "career_starts": int(round(np.expm1(stats["career_starts_log"]))),
                "has_wc_experience": bool(stats["has_wc_experience"]),
                "profiles": profiles,
                "wins_count": 0,
                "podium_count": 0,
                "top10_count": 0,
                "top30_count": 0,
            })

        # --- SYMULACJA MONTE CARLO Z INDYWIDUALNĄ ZMIENNOŚCIĄ SKOCZKA (SIGMA) ---
        n_athletes = len(results)
        base_pts_arr = np.array(base_points_list)
        sigmas_arr = np.array(athlete_sigmas)

        for _ in range(num_simulations):
            jitter = np.random.normal(0, sigmas_arr, size=n_athletes)
            sim_pts = base_pts_arr + jitter
            sim_ranks = np.argsort(-sim_pts)

            results[sim_ranks[0]]["wins_count"] += 1

            for p_idx in sim_ranks[:3]:
                results[p_idx]["podium_count"] += 1

            for t_idx in sim_ranks[:10]:
                results[t_idx]["top10_count"] += 1

            for t30_idx in sim_ranks[:min(30, n_athletes)]:
                results[t30_idx]["top30_count"] += 1

        for r in results:
            r["win_probability"] = round((r.pop("wins_count") / num_simulations) * 100, 1)
            r["podium_probability"] = round((r.pop("podium_count") / num_simulations) * 100, 1)
            r["top10_probability"] = round((r.pop("top10_count") / num_simulations) * 100, 1)
            r["top30_probability"] = round((r.pop("top30_count") / num_simulations) * 100, 1)

        results.sort(key=lambda x: x["predicted_points"], reverse=True)

        for rank, r in enumerate(results, start=1):
            r["predicted_rank"] = rank

        return results


_manager_instance: Optional[PredictionModelManager] = None

def get_prediction_manager() -> PredictionModelManager:
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = PredictionModelManager()
    return _manager_instance
