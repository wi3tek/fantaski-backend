import asyncio
import copy
import json
import time
import logging
import datetime
from typing import Dict, Any, List, Optional, Tuple

from app.services.prediction.feature_extractor import SkiFeatureExtractor
from app.services.prediction.neural_network import (
    BACKTEST_PATH,
    MODEL_DIR,
    build_training_dataset,
    get_prediction_manager,
)
from app.services.prediction.backtester import get_walk_forward_backtester
from app.services.prediction.prediction_config import (
    SESSION_STAGE_ORDER,
    resolve_session_category,
    resolve_session_type,
)
from app.services.prediction.hyperparams_service import (
    DEFAULT_HYPERPARAMS,
    get_active_hyperparams,
    load_active_hyperparams_from_db,
    sanitize_hyperparams,
    save_active_hyperparams_to_db,
)

logger = logging.getLogger(__name__)
AUTOTUNE_PATH = MODEL_DIR / "autotune_results.json"


def compute_composite_score(overall: Dict[str, Any]) -> float:
    """
    Złożony cel rankingowy:
    niski błąd miejsca (MAE) + wysoka korelacja Spearmana + trafność TOP 10 i TOP 30 + Podium.
    """
    return round(
        40.0 * float(overall.get("spearman_corr", 0.0))
        + 0.55 * float(overall.get("top10_hit_pct", 0.0))
        + 0.45 * float(overall.get("top30_hit_pct", 0.0))
        + 0.20 * float(overall.get("podium_hit_pct", 0.0))
        - 4.0 * float(overall.get("avg_rank_mae", 15.0)),
        3,
    )


def _split_tune_and_holdout_seasons(sorted_sessions: List[Dict[str, Any]]) -> Tuple[List[str], List[str], Dict[str, int], List[str]]:
    elite_cats = {"WC", "OWG", "WSC", "SFWC"}
    season_elite_counts: Dict[str, int] = {}
    for s in sorted_sessions:
        if resolve_session_type(s) == "COMPETITION" and resolve_session_category(s) in elite_cats:
            sec = str(s.get("season"))
            season_elite_counts[sec] = season_elite_counts.get(sec, 0) + 1

    all_seasons = sorted(
        list({str(s.get("season")) for s in sorted_sessions if str(s.get("season", "")).isdigit()}),
        key=lambda y: int(y),
    )

    latest_season = all_seasons[-1] if all_seasons else None
    eligible = [
        y for idx, y in enumerate(all_seasons)
        if idx >= 2 and (
            season_elite_counts.get(y, 0) >= 4
            or (y == latest_season and season_elite_counts.get(y, 0) >= 1)
        )
    ]
    if len(eligible) < 2:
        raise ValueError("Za mało sezonów do przeprowadzenia strojenia z walidacją holdout.")

    # Holdout: ostatni pełny sezon (+ ewentualny niepełny bieżący sezon) — tuner nigdy na nich nie stroi!
    full_seasons = [y for y in eligible if season_elite_counts.get(y, 0) >= 4]
    holdout_anchor = int(full_seasons[-1]) if full_seasons else int(eligible[-1])
    holdout_seasons = [y for y in eligible if int(y) >= holdout_anchor]
    tune_seasons = [y for y in eligible if int(y) < holdout_anchor]
    if not tune_seasons:
        tune_seasons = eligible[:-1]
        holdout_seasons = eligible[-1:]

    return tune_seasons, holdout_seasons, season_elite_counts, all_seasons


def _build_stage1_candidates(base_hp: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Kandydaci Etapu 1: pamięć czasowa formy, dynamika treningów i wagi weekendowe."""
    variants = [
        ("Krótsza pamięć formy (24d / 21d)", {
            "comp_half_life_days": 24.0,
            "recent_form_window_days": 21,
        }),
        ("Dłuższa pamięć formy (45d / 35d)", {
            "comp_half_life_days": 45.0,
            "recent_form_window_days": 35,
        }),
        ("Mocniejszy bieżący weekend", {
            "weekend_qua_weight": 0.90,
            "weekend_trial_weight": 0.60,
            "weekend_training_weight": 0.45,
            "qua_half_life_days": 7.0,
        }),
        ("Szybki zanik treningów + konserwatywny weekend", {
            "qua_half_life_days": 6.0,
            "trial_half_life_days": 3.0,
            "training_half_life_days": 2.0,
            "weekend_qua_weight": 0.65,
            "weekend_trial_weight": 0.35,
            "weekend_training_weight": 0.20,
        }),
        ("Wysoka waga QUA + szybki zanik historii", {
            "comp_half_life_days": 28.0,
            "qua_half_life_days": 8.0,
            "trial_half_life_days": 3.0,
            "training_half_life_days": 2.0,
            "weekend_qua_weight": 0.85,
            "weekend_trial_weight": 0.50,
            "weekend_training_weight": 0.30,
        }),
        ("Krótsze okno ruchome (6 sezonów)", {
            "rolling_window_seasons": 6,
        }),
        ("Węższe okno ruchome (7 sezonów)", {
            "rolling_window_seasons": 7,
        }),
    ]
    out: List[Tuple[str, Dict[str, Any]]] = []
    for label, patch in variants:
        cand = copy.deepcopy(base_hp)
        cand.update(patch)
        out.append((label, sanitize_hyperparams(cand)))
    return out


def _build_stage2_candidates(stage1_hp: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Kandydaci Etapu 2: funkcja straty, blending punktów i hiperparametry sieci NN."""
    variants = [
        ("Nacisk na jakość rankingu (85/15, blend 80%)", {
            "loss_quality_weight": 0.85,
            "points_blend_quality_weight": 0.80,
        }),
        ("Maksymalna jakość rankingu (90/10, blend 90%)", {
            "loss_quality_weight": 0.90,
            "points_blend_quality_weight": 0.90,
        }),
        ("Większa rola dystansu FIS (65/35, blend 60%)", {
            "loss_quality_weight": 0.65,
            "points_blend_quality_weight": 0.60,
        }),
        ("Lekki dropout + szybsze uczenie", {
            "dropout": 0.08,
            "lr": 0.005,
            "epochs": 45,
        }),
        ("Mocniejsza regularyzacja (dropout 0.18, wd 5e-4)", {
            "dropout": 0.18,
            "weight_decay": 0.0005,
            "lr": 0.003,
            "epochs": 55,
        }),
        ("Ranking 85% + lekka regularyzacja", {
            "loss_quality_weight": 0.85,
            "points_blend_quality_weight": 0.85,
            "dropout": 0.10,
            "lr": 0.004,
            "epochs": 50,
        }),
        ("Ranking 85% + wyższy blend 90% + stabilizacja", {
            "loss_quality_weight": 0.85,
            "points_blend_quality_weight": 0.90,
            "dropout": 0.16,
            "weight_decay": 0.0004,
            "lr": 0.0035,
            "epochs": 55,
        }),
        ("Spokojniejsza zbieżność (lr 0.0025, epoki 65)", {
            "lr": 0.0025,
            "dropout": 0.10,
            "weight_decay": 0.0002,
            "epochs": 65,
            "loss_quality_weight": 0.80,
            "points_blend_quality_weight": 0.78,
        }),
    ]
    out: List[Tuple[str, Dict[str, Any]]] = []
    for label, patch in variants:
        cand = copy.deepcopy(stage1_hp)
        cand.update(patch)
        out.append((label, sanitize_hyperparams(cand)))
    return out


class AutoTunerJobManager:
    """
    2-etapowy Auto-Tuner hiperparametrów oparty o Walk-Forward Backtest
    z niezależnym zbiorem holdout, zapisem zwycięskich parametrów w MongoDB
    i automatycznym przetrenowaniem modelu produkcyjnego.
    """

    def __init__(self) -> None:
        self.is_running: bool = False
        self.stop_requested: bool = False
        self.progress_pct: float = 0.0
        self.stage: str = "idle"
        self.message: str = ""
        self.error: Optional[str] = None
        self.trials_done: int = 0
        self.trials_total: int = 0
        self.started_at: Optional[float] = None
        self.results: Optional[Dict[str, Any]] = None
        self._load_cached()

    def _load_cached(self) -> None:
        try:
            if AUTOTUNE_PATH.exists():
                with open(AUTOTUNE_PATH, "r", encoding="utf-8") as f:
                    self.results = json.load(f)
        except Exception as e:
            logger.warning(f"Nie udało się wczytać cache Auto-Tunera: {e}")

    def mark_started(self) -> None:
        self.is_running = True
        self.stop_requested = False
        self.progress_pct = 2.0
        self.stage = "preparing"
        self.message = "Ładowanie sesji i konfiguracji z MongoDB..."
        self.error = None
        self.trials_done = 0
        self.trials_total = 17
        self.started_at = time.time()

    def stop(self) -> None:
        if self.is_running:
            self.stop_requested = True
            self.message = "Zatrzymywanie Auto-Tunera po bieżącej próbie..."

    def get_status(self) -> Dict[str, Any]:
        elapsed = int(time.time() - self.started_at) if (self.is_running and self.started_at) else (
            int((self.results or {}).get("elapsed_seconds", 0))
        )
        return {
            "is_running": self.is_running,
            "stop_requested": self.stop_requested,
            "percent_complete": round(self.progress_pct, 1),
            "stage": self.stage,
            "message": self.message,
            "error": self.error,
            "trials_done": self.trials_done,
            "trials_total": self.trials_total,
            "elapsed_seconds": elapsed,
            "results": self.results,
        }

    def _precompute_feature_bundle(
        self,
        sorted_sessions: List[Dict[str, Any]],
        fe: SkiFeatureExtractor,
        hp: Dict[str, Any],
        all_seasons: List[str],
        season_elite_counts: Dict[str, int],
    ) -> Dict[str, Any]:
        _, X_all, y_all, w_all, s_all = build_training_dataset(
            sorted_sessions,
            feature_extractor=fe,
            hp=hp,
        )
        return {
            "sorted_sessions": sorted_sessions,
            "fe": fe,
            "X_all": X_all,
            "y_all": y_all,
            "w_all": w_all,
            "s_all": s_all,
            "all_seasons": all_seasons,
            "season_elite_counts": season_elite_counts,
        }

    async def run_job(self, db, sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self.is_running:
            self.mark_started()

        t0 = time.time()
        try:
            await load_active_hyperparams_from_db(db)
            baseline_hp = get_active_hyperparams()

            sorted_sessions = sorted(
                [s for s in sessions if s.get("date") and s.get("season")],
                key=lambda x: (
                    str(x.get("date") or "")[:10],
                    SESSION_STAGE_ORDER.get(resolve_session_type(x), 3),
                    str(x.get("time") or "12:00")[:5],
                    str(x.get("_id") or ""),
                ),
            )
            tune_seasons, holdout_seasons, season_elite_counts, all_seasons = _split_tune_and_holdout_seasons(sorted_sessions)

            fe = SkiFeatureExtractor()
            await asyncio.to_thread(fe.fit_from_sessions, sorted_sessions)

            bt = get_walk_forward_backtester()
            stage1_cands = _build_stage1_candidates(baseline_hp)
            stage2_preview = _build_stage2_candidates(baseline_hp)
            self.trials_total = 1 + len(stage1_cands) + len(stage2_preview)

            trials_log: List[Dict[str, Any]] = []

            # --- BASELINE ---
            self.stage = "baseline"
            self.message = f"Ocena obecnej konfiguracji bazowej na sezonach {tune_seasons[0]}–{tune_seasons[-1]}..."
            base_bundle = await asyncio.to_thread(
                self._precompute_feature_bundle,
                sorted_sessions, fe, baseline_hp, all_seasons, season_elite_counts,
            )
            base_tune_rep = await asyncio.to_thread(
                bt.run_backtest,
                sorted_sessions,
                test_seasons=tune_seasons,
                epochs_per_fold=34,
                hyperparams=baseline_hp,
                save_results=False,
                precomputed_data=base_bundle,
            )
            base_holdout_rep = await asyncio.to_thread(
                bt.run_backtest,
                sorted_sessions,
                test_seasons=holdout_seasons,
                epochs_per_fold=36,
                hyperparams=baseline_hp,
                save_results=False,
                precomputed_data=base_bundle,
            )
            base_tune_score = compute_composite_score(base_tune_rep["overall"])
            base_holdout_score = compute_composite_score(base_holdout_rep["overall"])

            trials_log.append({
                "stage": "baseline",
                "label": "Aktualna baza",
                "tune_score": base_tune_score,
                "overall": base_tune_rep["overall"],
            })
            self.trials_done = 1
            self.progress_pct = round(5.0 + (self.trials_done / self.trials_total) * 80.0, 1)

            best_hp = copy.deepcopy(baseline_hp)
            best_bundle = base_bundle
            best_tune_score = base_tune_score
            best_tune_overall = base_tune_rep["overall"]
            best_label = "Aktualna baza"

            # --- ETAP 1: CECHY CZASOWE I WAGI WEEKENDOWE ---
            self.stage = "stage1_features"
            for idx, (label, cand_hp) in enumerate(stage1_cands, 1):
                if self.stop_requested:
                    break
                self.message = f"Etap 1/2 (Cechy i pamięć czasu {idx}/{len(stage1_cands)}): {label}..."
                cand_bundle = await asyncio.to_thread(
                    self._precompute_feature_bundle,
                    sorted_sessions, fe, cand_hp, all_seasons, season_elite_counts,
                )
                rep = await asyncio.to_thread(
                    bt.run_backtest,
                    sorted_sessions,
                    test_seasons=tune_seasons,
                    epochs_per_fold=34,
                    hyperparams=cand_hp,
                    save_results=False,
                    precomputed_data=cand_bundle,
                )
                sc = compute_composite_score(rep["overall"])
                trials_log.append({"stage": "stage1", "label": label, "tune_score": sc, "overall": rep["overall"]})
                if sc > best_tune_score:
                    best_tune_score = sc
                    best_tune_overall = rep["overall"]
                    best_hp = copy.deepcopy(cand_hp)
                    best_bundle = cand_bundle
                    best_label = label

                self.trials_done += 1
                self.progress_pct = round(5.0 + (self.trials_done / self.trials_total) * 80.0, 1)

            # --- ETAP 2: HIPERPARAMETRY SIECI I FUNKCJI STRATY (TA SAMA MACIERZ CECH) ---
            if not self.stop_requested:
                self.stage = "stage2_network"
                stage2_cands = _build_stage2_candidates(best_hp)
                for idx, (label, cand_hp) in enumerate(stage2_cands, 1):
                    if self.stop_requested:
                        break
                    self.message = f"Etap 2/2 (Sieć i punktacja {idx}/{len(stage2_cands)}): {label}..."
                    rep = await asyncio.to_thread(
                        bt.run_backtest,
                        sorted_sessions,
                        test_seasons=tune_seasons,
                        epochs_per_fold=min(42, max(32, int(cand_hp["epochs"]) - 8)),
                        hyperparams=cand_hp,
                        save_results=False,
                        precomputed_data=best_bundle,
                    )
                    sc = compute_composite_score(rep["overall"])
                    trials_log.append({"stage": "stage2", "label": label, "tune_score": sc, "overall": rep["overall"]})
                    if sc > best_tune_score:
                        best_tune_score = sc
                        best_tune_overall = rep["overall"]
                        best_hp = copy.deepcopy(cand_hp)
                        best_label = label

                    self.trials_done += 1
                    self.progress_pct = round(5.0 + (self.trials_done / self.trials_total) * 80.0, 1)

            # --- WERYFIKACJA NA NIEZNANYM ZBIORZE HOLDOUT ---
            self.stage = "holdout_verification"
            self.progress_pct = 88.0
            self.message = f"Weryfikacja najlepszej konfiguracji ({best_label}) na holdout {holdout_seasons}..."

            if best_hp != baseline_hp:
                best_holdout_rep = await asyncio.to_thread(
                    bt.run_backtest,
                    sorted_sessions,
                    test_seasons=holdout_seasons,
                    epochs_per_fold=36,
                    hyperparams=best_hp,
                    save_results=False,
                    precomputed_data=best_bundle,
                )
                best_holdout_score = compute_composite_score(best_holdout_rep["overall"])
            else:
                best_holdout_rep = base_holdout_rep
                best_holdout_score = base_holdout_score

            # Promocja tylko gdy poprawia wynik strojenia I nie psuje niezaleznego holdoutu (>=-0.5 pkt)
            improved = (
                not self.stop_requested
                and best_tune_score > base_tune_score + 0.05
                and best_holdout_score >= base_holdout_score - 0.50
            )

            retrained_meta = None
            if improved:
                self.stage = "saving_and_retraining"
                self.progress_pct = 92.0
                self.message = "Znaleziono lepszą konfigurację! Zapis do MongoDB i przetrenowanie modelu produkcyjnego..."

                await save_active_hyperparams_to_db(
                    db=db,
                    params=best_hp,
                    source="autotuned",
                    tuning_metrics={**best_tune_overall, "composite_score": best_tune_score, "seasons": tune_seasons},
                    holdout_metrics={**best_holdout_rep["overall"], "composite_score": best_holdout_score, "seasons": holdout_seasons},
                    notes=f"Auto-Tuner: {best_label}",
                )

                # Zaktualizuj pełny raport Walk-Forward Backtest (2021-2027) dla nowej konfiguracji
                new_bt_report = await asyncio.to_thread(
                    bt.run_backtest,
                    sorted_sessions,
                    hyperparams=best_hp,
                    save_results=True,
                    precomputed_data=best_bundle,
                )

                # Automatycznie przetrenowuj model produkcyjny (zgodnie z wyborem użytkownika)
                pm = get_prediction_manager()
                retrained_meta = await asyncio.to_thread(pm.train, sorted_sessions, None, best_hp)
                from app.services.prediction.ml_storage import (
                    save_json_artifact_to_db,
                    save_nn_artifacts_to_db,
                )
                await save_json_artifact_to_db(db, "backtest_results", new_bt_report)
                await save_nn_artifacts_to_db(db, pm)
                winning_hp = best_hp
            else:
                winning_hp = baseline_hp

            elapsed = round(time.time() - t0, 1)
            report = {
                "run_at": datetime.datetime.now().isoformat(),
                "elapsed_seconds": elapsed,
                "stopped": self.stop_requested,
                "improved": improved,
                "best_candidate_label": best_label if improved else "Aktualna baza (bez zmian)",
                "tune_seasons": tune_seasons,
                "holdout_seasons": holdout_seasons,
                "baseline": {
                    "tune_score": base_tune_score,
                    "holdout_score": base_holdout_score,
                    "tune_overall": base_tune_rep["overall"],
                    "holdout_overall": base_holdout_rep["overall"],
                    "params": baseline_hp,
                },
                "best": {
                    "tune_score": best_tune_score,
                    "holdout_score": best_holdout_score,
                    "tune_overall": best_tune_overall,
                    "holdout_overall": best_holdout_rep["overall"],
                    "params": winning_hp,
                },
                "trials_evaluated": len(trials_log),
                "trials": sorted(trials_log, key=lambda x: x["tune_score"], reverse=True)[:10],
                "retrained_model_meta": retrained_meta,
            }

            AUTOTUNE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(AUTOTUNE_PATH, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)

            from app.services.prediction.ml_storage import save_json_artifact_to_db
            await save_json_artifact_to_db(db, "autotune_results", report)

            self.results = report
            self.progress_pct = 100.0
            self.stage = "completed"
            if self.stop_requested:
                self.message = f"Auto-Tuner zatrzymany po {len(trials_log)} próbach ({elapsed}s)."
            elif improved:
                self.message = (
                    f"✅ Zapisano lepsze parametry w MongoDB i przetrenowano model! "
                    f"Score: {base_tune_score} → {best_tune_score} (holdout: {base_holdout_score} → {best_holdout_score})."
                )
            else:
                self.message = (
                    f"Obecna konfiguracja w MongoDB pozostała najlepsza (score: {base_tune_score}, "
                    f"holdout: {base_holdout_score} po {len(trials_log)} próbach w {elapsed}s)."
                )
            return report
        except Exception as e:
            self.error = str(e)
            self.stage = "error"
            self.message = f"Błąd Auto-Tunera: {e}"
            logger.error(f"Błąd Auto-Tunera: {e}")
            raise
        finally:
            self.is_running = False


_auto_tuner_instance: Optional[AutoTunerJobManager] = None


def get_auto_tuner_manager() -> AutoTunerJobManager:
    global _auto_tuner_instance
    if _auto_tuner_instance is None:
        _auto_tuner_instance = AutoTunerJobManager()
    return _auto_tuner_instance
