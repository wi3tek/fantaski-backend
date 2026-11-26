import json
import math
import time
import logging
import datetime
from typing import Dict, Any, List, Optional

import numpy as np

from app.services.prediction.feature_extractor import SkiFeatureExtractor
from app.services.prediction.neural_network import (
    BACKTEST_PATH,
    PredictionModelManager,
    build_training_dataset,
    fit_ski_jumping_nn,
)
from app.services.prediction.prediction_config import (
    SESSION_STAGE_ORDER,
    resolve_session_category,
    resolve_session_type,
)
from app.services.prediction.hyperparams_service import (
    get_active_hyperparams,
    sanitize_hyperparams,
)

logger = logging.getLogger(__name__)


def _spearman_corr(x: List[float], y: List[float]) -> float:
    """Wylicza korelację rang Spearmana między pozycją przewidywaną a rzeczywistą."""
    n = len(x)
    if n < 3:
        return 0.0
    rx = np.argsort(np.argsort(np.array(x, dtype=float))).astype(float)
    ry = np.argsort(np.argsort(np.array(y, dtype=float))).astype(float)
    rx -= rx.mean()
    ry -= ry.mean()
    denom = float(np.sqrt((rx ** 2).sum() * (ry ** 2).sum()))
    if denom <= 1e-9:
        return 0.0
    return float((rx * ry).sum() / denom)


class WalkForwardBacktester:
    """
    Kroczący (Expanding-Window) backtester historyczny po sezonach.
    Dla każdego sezonu testowego T:
      - uczy model wyłącznie na konkursach ze sezonów < T,
      - symuluje dzień po dniu każdy elitarny konkurs w sezonie T (z jego ówczesną pogodą,
        treningami, próbami, QUA i wcześniejszymi startami — zero przecieku przyszłości),
      - porównuje wygenerowane miejsca, podium, TOP 10 i TOP 30 z rzeczywistymi wynikami FIS.
    """

    def __init__(self) -> None:
        self.is_running: bool = False
        self.progress_pct: float = 0.0
        self.current_stage: str = "idle"
        self.message: str = ""
        self.error: Optional[str] = None
        self.results: Optional[Dict[str, Any]] = None
        self._load_cached()

    def _load_cached(self) -> None:
        try:
            if BACKTEST_PATH.exists():
                with open(BACKTEST_PATH, "r", encoding="utf-8") as f:
                    self.results = json.load(f)
        except Exception as e:
            logger.warning(f"Nie udało się wczytać cache backtestu: {e}")

    def mark_started(self) -> None:
        self.is_running = True
        self.progress_pct = 2.0
        self.current_stage = "loading_sessions"
        self.message = "Ładowanie sesji z bazy danych do kroczącego backtestu..."
        self.error = None

    def get_status(self) -> Dict[str, Any]:
        return {
            "is_running": self.is_running,
            "percent_complete": round(self.progress_pct, 1),
            "stage": self.current_stage,
            "message": self.message,
            "error": self.error,
            "results": self.results,
        }

    def run_backtest(
        self,
        sessions: List[Dict[str, Any]],
        test_seasons: Optional[List[str]] = None,
        epochs_per_fold: Optional[int] = None,
        hyperparams: Optional[Dict[str, Any]] = None,
        save_results: bool = True,
        precomputed_data: Optional[Dict[str, Any]] = None,
        stop_check: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Uruchamia synchroniczny walk-forward backtest na liście sesji z bazy."""
        t0 = time.time()
        hp = sanitize_hyperparams(hyperparams) if hyperparams else get_active_hyperparams()
        fold_epochs = int(epochs_per_fold if epochs_per_fold is not None else min(45, max(30, int(hp["epochs"]) - 10)))

        if save_results:
            self.is_running = True
            self.error = None

        try:
            if precomputed_data is not None:
                sorted_sessions = precomputed_data["sorted_sessions"]
                fe = precomputed_data["fe"]
                X_all = precomputed_data["X_all"]
                y_all = precomputed_data["y_all"]
                w_all = precomputed_data["w_all"]
                s_all = precomputed_data["s_all"]
                all_seasons = precomputed_data["all_seasons"]
                season_elite_counts = precomputed_data["season_elite_counts"]
            else:
                sorted_sessions = sorted(
                    [s for s in sessions if s.get("date") and s.get("season")],
                    key=lambda x: (
                        str(x.get("date") or "")[:10],
                        SESSION_STAGE_ORDER.get(resolve_session_type(x), 3),
                        str(x.get("time") or "12:00")[:5],
                        str(x.get("_id") or ""),
                    ),
                )

                elite_cats = {"WC", "OWG", "WSC", "SFWC"}
                season_elite_counts = {}
                for s in sorted_sessions:
                    if resolve_session_type(s) == "COMPETITION" and resolve_session_category(s) in elite_cats:
                        sec = str(s.get("season"))
                        season_elite_counts[sec] = season_elite_counts.get(sec, 0) + 1

                all_seasons = sorted(
                    list({str(s.get("season")) for s in sorted_sessions if str(s.get("season", "")).isdigit()}),
                    key=lambda y: int(y),
                )

                fe = SkiFeatureExtractor()
                fe.fit_from_sessions(sorted_sessions)
                if save_results:
                    self.message = "Budowanie macierzy cech chronologicznych dla wszystkich sezonów..."
                _, X_all, y_all, w_all, s_all = build_training_dataset(
                    sorted_sessions,
                    feature_extractor=fe,
                    hp=hp,
                )

            elite_cats = {"WC", "OWG", "WSC", "SFWC"}
            if test_seasons is None:
                latest_season = all_seasons[-1] if all_seasons else None
                test_seasons = [
                    y for idx, y in enumerate(all_seasons)
                    if idx >= 2 and (
                        season_elite_counts.get(y, 0) >= 4
                        or (y == latest_season and season_elite_counts.get(y, 0) >= 1)
                    )
                ]

            if not test_seasons:
                raise ValueError("Za mało sezonów z konkursami PŚ/MŚ/IO do przeprowadzenia kroczącego backtestu.")

            pm = PredictionModelManager()
            folds_report: List[Dict[str, Any]] = []
            all_comp_records: List[Dict[str, Any]] = []
            recent_samples: List[Dict[str, Any]] = []

            rolling_win = int(hp["rolling_window_seasons"])
            total_folds = len(test_seasons)
            for fold_idx, test_season in enumerate(test_seasons):
                if stop_check and stop_check():
                    break

                test_year_int = int(test_season)
                if save_results:
                    self.current_stage = f"fold_{test_season}"
                    self.progress_pct = 15.0 + (fold_idx / total_folds) * 80.0
                    self.message = f"Walk-Forward ({fold_idx + 1}/{total_folds}): Trenowanie na sezonach < {test_season}, test na {test_season}..."

                min_train_year = test_year_int - rolling_win
                mask_tr = (s_all < test_year_int) & (s_all >= min_train_year)
                X_tr, y_tr, w_tr = X_all[mask_tr], y_all[mask_tr], w_all[mask_tr]
                if len(X_tr) < 100:
                    continue

                fold_model, fold_val_loss, best_ep, _ = fit_ski_jumping_nn(
                    X_tr,
                    y_tr,
                    w_tr,
                    epochs=fold_epochs,
                    lr=float(hp["lr"]),
                    dropout=float(hp["dropout"]),
                    weight_decay=float(hp["weight_decay"]),
                    loss_quality_weight=float(hp["loss_quality_weight"]),
                    seed=42 + fold_idx,
                )

                # Konkursy elity z sezonu testowego
                test_comps = [
                    s for s in sorted_sessions
                    if str(s.get("season")) == str(test_season)
                    and resolve_session_type(s) == "COMPETITION"
                    and resolve_session_category(s) in elite_cats
                ]

                fold_records: List[Dict[str, Any]] = []
                for comp in test_comps:
                    raw_res = comp.get("results_data", {}).get("results", [])
                    actual_rows = []
                    for r in raw_res:
                        fc = str(r.get("fis_code") or "").strip()
                        jumps = r.get("jumps", [])
                        dists = [float(j["distance"]) for j in jumps if j.get("distance") and float(j["distance"]) > 0]
                        rank_raw = r.get("rank")
                        if fc and dists and rank_raw is not None:
                            try:
                                actual_rows.append({
                                    "fis_code": fc,
                                    "full_name": r.get("name", ""),
                                    "country_code": r.get("country", ""),
                                    "actual_rank": int(rank_raw),
                                    "actual_best_dist": max(dists),
                                })
                            except Exception:
                                pass

                    if len(actual_rows) < 15:
                        continue

                    try:
                        comp_date = datetime.date.fromisoformat(str(comp.get("date"))[:10])
                    except Exception:
                        continue

                    comp_time = str(comp.get("time") or "12:00")[:5]
                    comp_place = (comp.get("event_place") or "").strip().title()
                    comp_hs = float(comp.get("hillSize") or 130.0)
                    comp_ht = (comp.get("hillType") or "LARGE").upper()
                    comp_cat = resolve_session_category(comp)
                    w_data = comp.get("weather") or {}
                    weather = {
                        "headwind_component": float(w_data.get("headwind_component") if w_data.get("headwind_component") is not None else 0.0),
                        "wind_speed": float(w_data.get("wind_speed") if w_data.get("wind_speed") is not None else 1.5),
                    }

                    preds = pm.predict(
                        athletes=actual_rows,
                        hill_size=comp_hs,
                        hill_type=comp_ht,
                        place=comp_place,
                        weather=weather,
                        target_date=comp_date,
                        weekend_sessions=None,  # eval_pool_sessions w fe już zawiera treningi/QUA tej skoczni!
                        num_simulations=0,
                        target_category=comp_cat,
                        target_time=comp_time,
                        target_race_id=str(comp.get("raceId") or ""),
                        target_session_id=str(comp.get("_id") or ""),
                        model_override=fold_model,
                        fe_override=fe,
                        hyperparams=hp,
                    )
                    if len(preds) < 15:
                        continue

                    pred_rank_map = {str(p["fis_code"]): p["predicted_rank"] for p in preds}
                    pred_dist_map = {str(p["fis_code"]): p["predicted_distance"] for p in preds}

                    matched = [a for a in actual_rows if a["fis_code"] in pred_rank_map]
                    if len(matched) < 15:
                        continue

                    rank_errors = [abs(pred_rank_map[a["fis_code"]] - a["actual_rank"]) for a in matched]
                    dist_errors = [abs(pred_dist_map[a["fis_code"]] - a["actual_best_dist"]) for a in matched]
                    spearman = _spearman_corr(
                        [pred_rank_map[a["fis_code"]] for a in matched],
                        [float(a["actual_rank"]) for a in matched],
                    )

                    actual_sorted = sorted(matched, key=lambda x: x["actual_rank"])
                    actual_top1 = actual_sorted[0]["fis_code"]
                    actual_top3 = {x["fis_code"] for x in actual_sorted[:3]}
                    actual_top10 = {x["fis_code"] for x in actual_sorted[:10]}
                    actual_top30 = {x["fis_code"] for x in actual_sorted[:30]}

                    pred_top3_codes = {str(p["fis_code"]) for p in preds[:3]}
                    pred_top10_codes = {str(p["fis_code"]) for p in preds[:10]}
                    pred_top30_codes = {str(p["fis_code"]) for p in preds[:30]}

                    top30_denom = min(30, len(matched))
                    rec = {
                        "season": test_season,
                        "session_id": str(comp.get("_id")),
                        "date": str(comp.get("date"))[:10],
                        "place": comp_place,
                        "category": comp_cat,
                        "field_size": len(matched),
                        "rank_mae": float(np.mean(rank_errors)),
                        "dist_mae_m": float(np.mean(dist_errors)),
                        "spearman": spearman,
                        "winner_top1": 1.0 if str(preds[0]["fis_code"]) == actual_top1 else 0.0,
                        "winner_top3": 1.0 if actual_top1 in pred_top3_codes else 0.0,
                        "podium_hit_pct": (len(actual_top3 & pred_top3_codes) / 3.0) * 100.0,
                        "top10_hit_pct": (len(actual_top10 & pred_top10_codes) / 10.0) * 100.0,
                        "top30_hit_pct": (len(actual_top30 & pred_top30_codes) / max(1, top30_denom)) * 100.0,
                    }
                    fold_records.append(rec)
                    all_comp_records.append(rec)

                    if fold_idx == total_folds - 1 or str(test_season) in ("2026", "2027"):
                        recent_samples.append({
                            "date": rec["date"],
                            "place": comp_place,
                            "category": comp_cat,
                            "podium_hits": len(actual_top3 & pred_top3_codes),
                            "top10_hits": len(actual_top10 & pred_top10_codes),
                            "rank_mae": round(rec["rank_mae"], 1),
                            "predicted_top3": [p["name"] for p in preds[:3]],
                            "actual_top3": [x["full_name"] for x in actual_sorted[:3]],
                        })

                if fold_records:
                    folds_report.append({
                        "season": test_season,
                        "train_samples": int(len(X_tr)),
                        "best_epoch": best_ep,
                        "competitions_evaluated": len(fold_records),
                        "avg_rank_mae": round(float(np.mean([r["rank_mae"] for r in fold_records])), 2),
                        "dist_mae_m": round(float(np.mean([r["dist_mae_m"] for r in fold_records])), 2),
                        "spearman_corr": round(float(np.mean([r["spearman"] for r in fold_records])), 3),
                        "winner_top1_pct": round(float(np.mean([r["winner_top1"] for r in fold_records]) * 100.0), 1),
                        "winner_top3_pct": round(float(np.mean([r["winner_top3"] for r in fold_records]) * 100.0), 1),
                        "podium_hit_pct": round(float(np.mean([r["podium_hit_pct"] for r in fold_records])), 1),
                        "top10_hit_pct": round(float(np.mean([r["top10_hit_pct"] for r in fold_records])), 1),
                        "top30_hit_pct": round(float(np.mean([r["top30_hit_pct"] for r in fold_records])), 1),
                    })

            if not all_comp_records:
                raise ValueError("Brak konkursów spełniających kryteria ewaluacji w wybranych sezonach.")

            overall = {
                "competitions_evaluated": len(all_comp_records),
                "avg_rank_mae": round(float(np.mean([r["rank_mae"] for r in all_comp_records])), 2),
                "dist_mae_m": round(float(np.mean([r["dist_mae_m"] for r in all_comp_records])), 2),
                "spearman_corr": round(float(np.mean([r["spearman"] for r in all_comp_records])), 3),
                "winner_top1_pct": round(float(np.mean([r["winner_top1"] for r in all_comp_records]) * 100.0), 1),
                "winner_top3_pct": round(float(np.mean([r["winner_top3"] for r in all_comp_records]) * 100.0), 1),
                "podium_hit_pct": round(float(np.mean([r["podium_hit_pct"] for r in all_comp_records])), 1),
                "top10_hit_pct": round(float(np.mean([r["top10_hit_pct"] for r in all_comp_records])), 1),
                "top30_hit_pct": round(float(np.mean([r["top30_hit_pct"] for r in all_comp_records])), 1),
            }

            elapsed = round(time.time() - t0, 1)
            report = {
                "run_at": datetime.datetime.now().isoformat(),
                "elapsed_seconds": elapsed,
                "test_seasons": test_seasons,
                "hyperparams": hp,
                "overall": overall,
                "folds": folds_report,
                "recent_competitions": recent_samples[-8:][::-1],
            }

            if save_results:
                BACKTEST_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(BACKTEST_PATH, "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=2, ensure_ascii=False)

                self.results = report
                self.progress_pct = 100.0
                self.current_stage = "completed"
                self.message = (
                    f"Backtest ukończony w {elapsed}s ({overall['competitions_evaluated']} konkursów): "
                    f"TOP 30: {overall['top30_hit_pct']}%, TOP 10: {overall['top10_hit_pct']}%, "
                    f"Podium: {overall['podium_hit_pct']}%, MAE miejsca: {overall['avg_rank_mae']}"
                )
            return report
        except Exception as e:
            if save_results:
                self.error = str(e)
                self.current_stage = "error"
                self.message = f"Błąd backtestu: {e}"
            raise
        finally:
            if save_results:
                self.is_running = False


_backtester_instance: Optional[WalkForwardBacktester] = None


def get_walk_forward_backtester() -> WalkForwardBacktester:
    global _backtester_instance
    if _backtester_instance is None:
        _backtester_instance = WalkForwardBacktester()
    return _backtester_instance
