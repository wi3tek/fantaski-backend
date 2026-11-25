import math
import datetime
from typing import Dict, Any, List, Optional, Tuple
import numpy as np

from app.services.prediction.prediction_config import (
    RACE_CATEGORY_WEIGHTS,
    SESSION_TYPE_WEIGHTS,
    HALF_LIFE_DAYS,
    RECENT_FORM_WINDOW_DAYS,
    DEFAULT_TARGET_CATEGORY,
    INPUT_FEATURES_DIM,
    resolve_session_category,
    resolve_session_type,
)


def calculate_time_weight(session_date_str: Optional[str], ref_date: Optional[datetime.date] = None, half_life: float = HALF_LIFE_DAYS) -> float:
    """Zanikanie wykładnicze (exponential decay): bieżący miesiąc ~1.0, 2 miesiące temu ~0.25, rok temu ~0.0001."""
    if not session_date_str:
        return 0.05
    if ref_date is None:
        ref_date = datetime.date.today()

    try:
        s_date = datetime.date.fromisoformat(str(session_date_str).strip()[:10])
        delta_days = (ref_date - s_date).days
        if delta_days < 0:
            return 0.0  # Sesja z przyszłości nie może wpływać na przeszłość!
        weight = math.exp(-0.693147 * delta_days / half_life)
        return float(max(0.0001, weight))
    except Exception:
        return 0.05


class SkiFeatureExtractor:
    """
    Ekstraktor cech skoczków FIS:
    - 14-elementowy wektor cech (100% niezależny od kolejności na liście)
    - Przeliczanie wyników niższych lig (COC, FC) na realia Pucharu Świata (WC)
    - Karane debiuty i brak startów w bieżącym sezonie zimowym
    - Czasowy rozpad formy (half-life = 35 dni)
    - Wykluczenie treningów z analizy formy konkursowej (treningi = testy sprzętu)
    - Wielowymiarowe profile psychologiczno-techniczne (zwierzę konkursowe, metronom itp.)
    """
    def __init__(self):
        # fis_code -> lista wpisów historycznych
        self.athlete_history: Dict[str, List[Dict[str, Any]]] = {}
        # Zbiór kodów FIS zawodników z Pucharu Świata
        self.wc_athletes_fis: set = set()
        # Liczba startów w konkursach głównych Pucharu Świata (i MŚ/IO): fis_code -> count
        self.wc_starts: Dict[str, int] = {}
        # Przybliżony rating bazowy zawodnika
        self.athlete_base_ratings: Dict[str, float] = {}
        # fis_code -> { "WC": count, "COC": count, ... }
        self.athlete_category_starts: Dict[str, Dict[str, int]] = {}

    def fit_from_sessions(self, sessions: List[Dict[str, Any]]):
        """
        Przetwarza chronologicznie sesje z bazy danych, standaryzuje punkty
        i buduje historię zawodników z uwzględnieniem lig FIS.
        """
        sorted_sessions = sorted(
            [s for s in sessions if s.get("date")],
            key=lambda x: (str(x.get("date") or ""), str(x.get("time") or ""))
        )

        self.athlete_history.clear()
        self.wc_athletes_fis.clear()
        self.wc_starts.clear()
        self.athlete_base_ratings.clear()
        self.athlete_category_starts.clear()

        for s in sorted_sessions:
            try:
                hill_size = float(s.get("hillSize") or 130.0)
            except Exception:
                hill_size = 130.0
            if hill_size <= 0:
                hill_size = 130.0

            hill_type = (s.get("hillType") or "LARGE").upper()
            place = (s.get("event_place") or "").strip().title()
            date_str = str(s.get("date") or "").strip()[:10]

            cat_code = str(s.get("category_code") or "").upper().strip()
            race_cat = str(s.get("race_category") or "").upper().strip()
            rcat = cat_code if cat_code in ["WC", "OWG", "WSC", "SFWC", "GP", "COC", "FC", "WJC", "FESA"] else (race_cat if race_cat != "QUA" else "WC")
            if not rcat:
                rcat = "WC"

            stype = str(s.get("session_type") or "").upper().strip()
            if "QUA" in race_cat or "QUA" in stype:
                stype = "QUA"
            elif "TRAIN" in stype:
                stype = "TRAINING"
            elif stype not in ["COMPETITION", "TRIAL"]:
                stype = "COMPETITION"

            results = s.get("results_data", {}).get("results", [])
            for r in results:
                raw_fis = r.get("fis_code")
                if not raw_fis:
                    continue
                fc = str(raw_fis).strip()

                jumps = r.get("jumps", [])
                distances = [float(j["distance"]) for j in jumps if j.get("distance") and float(j["distance"]) > 0]
                if not distances:
                    continue

                best_dist = max(distances)
                total_pts = float(r.get("total_points") or 0.0)
                rank = float(r.get("rank") or 50)

                # Standaryzacja punktów do skali 2-seryjnego konkursu:
                competition_scale_pts = total_pts * 2.0 if stype == "QUA" and total_pts < 160 else total_pts

                # Start w konkursie głównym PŚ znosi status debiutanta:
                if rcat in ["WC", "OWG", "WSC", "SFWC"]:
                    self.wc_athletes_fis.add(fc)
                    if stype == "COMPETITION":
                        self.wc_starts[fc] = self.wc_starts.get(fc, 0) + 1

                if fc not in self.athlete_category_starts:
                    self.athlete_category_starts[fc] = {}
                self.athlete_category_starts[fc][rcat] = self.athlete_category_starts[fc].get(rcat, 0) + 1

                raw_ratio = best_dist / hill_size

                entry = {
                    "date": date_str,
                    "place": place,
                    "hill_size": hill_size,
                    "hill_type": hill_type,
                    "rcat": rcat,
                    "stype": stype,
                    "raw_ratio": raw_ratio,
                    "total_pts": competition_scale_pts,
                    "rank": rank,
                    "is_wc": rcat in ["WC", "OWG", "WSC", "SFWC"]
                }
                if fc not in self.athlete_history:
                    self.athlete_history[fc] = []
                self.athlete_history[fc].append(entry)

        # Wyliczenie ratingów bazowych
        for fc, history in self.athlete_history.items():
            ratios = [h["raw_ratio"] for h in history[-15:]]
            self.athlete_base_ratings[fc] = float(np.mean(ratios)) if ratios else 0.85

    def get_athlete_stats(
        self,
        fis_code: str,
        hill_type: str = "LARGE",
        place: Optional[str] = None,
        target_date: Optional[datetime.date] = None,
        target_category: str = DEFAULT_TARGET_CATEGORY
    ) -> Dict[str, Any]:
        """
        Wylicza zagregowane wskaźniki sportowe skoczka przeliczone na realia ligi docelowej:
        - elite_form: zagregowany współczynnik formy z uwzględnieniem ligi (PŚ vs COC/FC)
        - recent_form: dyspozycja z ostatnich 28 dni (waga świeżej formy zimowej)
        - current_season_activity: aktywność w bieżącym sezonie zimowym (1.0 = skakał na śniegu, 0.0 = nieobecny/kontuzja)
        - is_tier_debutant: 1.0 jeśli zawodnik nie startował w konkursie głównym PŚ
        """
        fc = str(fis_code).strip()
        history = self.athlete_history.get(fc, [])
        if target_date is None:
            target_date = datetime.date.today()

        wc_count = self.wc_starts.get(fc, 0)
        is_wc_debutant = (wc_count == 0)

        # Dynamiczny początek sezonu zimowego względem target_date:
        if target_date.month >= 10:
            winter_season_start = datetime.date(target_date.year, 11, 1)
        else:
            winter_season_start = datetime.date(target_date.year - 1, 11, 1)

        if not history:
            return {
                "weighted_form_ratio": 0.75,
                "recent_form_ratio": 0.65,
                "weighted_points": 100.0,
                "weighted_rank": 55.0,
                "consistency_sigma": 0.06,
                "consistency_score": 40.0,
                "conversion_delta": 0.0,
                "hill_type_affinity": 0.75,
                "venue_affinity": 0.75,
                "current_season_activity": 0.0,
                "form_trend": 0.0,
                "career_starts_log": 0.0,
                "has_wc_experience": 0.0,
                "is_tier_debutant": 1.0,
                "tier_experience_ratio": 0.0,
                "tier_starts_count": 0,
                "wc_count": 0,
            }

        HALF_LIFE = HALF_LIFE_DAYS

        w_sum = 0.0
        ratio_sum = 0.0
        pts_sum = 0.0
        rank_sum = 0.0

        recent_w_sum = 0.0
        recent_ratio_sum = 0.0

        ht_w_sum = 0.0
        ht_ratio_sum = 0.0

        venue_w_sum = 0.0
        venue_ratio_sum = 0.0

        comp_ranks = []
        trial_ranks = []
        season_jumps_count = 0
        ratios_for_std = []

        clean_place = place.strip().lower() if place else ""

        for entry in history[-30:]:
            try:
                s_date = datetime.date.fromisoformat(entry["date"])
                delta_days = (target_date - s_date).days
                if delta_days < 0:
                    continue  # Wykluczamy sesje z przyszłości
            except Exception:
                delta_days = 90
                s_date = datetime.date(2026, 1, 1)

            time_w = math.exp(-0.693147 * delta_days / HALF_LIFE)

            # Wagi typów sesji (konkurs = 1.0, kwalifikacje = 0.35, trening = 0.0)
            if entry["stype"] == "COMPETITION":
                st_w = 1.00
            elif entry["stype"] == "QUA":
                st_w = 0.35
            else:  # TRAINING, TRIAL
                st_w = 0.05

            # Wagi ligi (Puchar Świata najważniejszy, niższe ligi mocno zredukowane)
            rcat = entry["rcat"]
            if target_category.upper() in ["WC", "OWG", "WSC", "SFWC"]:
                if entry["is_wc"]:
                    tier_w = 1.00
                    tier_dist_mult = 1.00
                    equiv_pts = entry["total_pts"]
                    equiv_rank = entry["rank"]
                elif rcat == "GP":
                    tier_w = 0.60
                    tier_dist_mult = 0.95
                    equiv_pts = entry["total_pts"] * 0.90
                    equiv_rank = entry["rank"] * 1.2 + 5.0
                elif rcat == "COC":
                    tier_w = 0.25
                    tier_dist_mult = 0.85
                    equiv_pts = entry["total_pts"] * 0.70
                    equiv_rank = entry["rank"] * 2.0 + 30.0
                else:  # FIS Cup / Alpen Cup / FC / WJC
                    tier_w = 0.08
                    tier_dist_mult = 0.75
                    equiv_pts = entry["total_pts"] * 0.55
                    equiv_rank = entry["rank"] * 2.5 + 45.0
            else:
                tier_w = 1.00
                tier_dist_mult = 1.00
                equiv_pts = entry["total_pts"]
                equiv_rank = entry["rank"]

            w = time_w * st_w * tier_w
            w_sum += w

            scaled_ratio = entry["raw_ratio"] * tier_dist_mult
            ratio_sum += scaled_ratio * w
            pts_sum += equiv_pts * w
            rank_sum += equiv_rank * w
            ratios_for_std.append(scaled_ratio)

            if entry["stype"] == "COMPETITION":
                comp_ranks.append(entry["rank"])
            elif entry["stype"] in ["TRIAL", "QUA", "TRAINING"]:
                trial_ranks.append(entry["rank"])

            # Aktywność w bieżącym sezonie zimowym (konkursy/kwalifikacje od 1 listopada):
            if winter_season_start <= s_date <= target_date and entry["stype"] in ["COMPETITION", "QUA"]:
                season_jumps_count += 1

            # Ostatnie 28 dni (świeża forma zimowa)
            if delta_days <= RECENT_FORM_WINDOW_DAYS:
                recent_w_sum += w
                recent_ratio_sum += scaled_ratio * w

            if entry["hill_type"] == hill_type.upper():
                ht_w_sum += w
                ht_ratio_sum += scaled_ratio * w

            if clean_place and clean_place in entry["place"].lower():
                venue_w_sum += w
                venue_ratio_sum += scaled_ratio * w

        elite_form = (ratio_sum / w_sum) if w_sum > 0 else 0.78
        equiv_wc_pts = (pts_sum / w_sum) if w_sum > 0 else 120.0
        equiv_wc_rank = (rank_sum / w_sum) if w_sum > 0 else 50.0

        current_season_activity = min(1.0, season_jumps_count / 2.0)

        if recent_w_sum > 0:
            recent_form = recent_ratio_sum / recent_w_sum
        else:
            recent_form = elite_form * 0.82

        # Kary dla zawodników bez startu zimowego lub debiutantów w PŚ:
        if current_season_activity == 0.0:
            # Zawodnik nie startował w tym sezonie zimowym (np. Schuster - kontuzja, Zniszczoł - brak powołania)
            recent_form *= 0.86
            elite_form *= 0.94

        # Tylko dla prawdziwych debiutantów bez żadnego startu w konkursie głównym PŚ (wc_count == 0):
        # Zawodnicy, którzy już punktowali/startowali w PŚ (np. Zimmermann z dwoma podiami w Lillehammer),
        # NIE MOGĄ być karani za brak doświadczenia — ich skoki w PŚ stanowią o ich realnej klasie!
        if target_category.upper() in ["WC", "OWG", "WSC", "SFWC"] and wc_count == 0:
            elite_form *= 0.94
            recent_form *= 0.92

        sigma = float(np.std(ratios_for_std[-10:])) if len(ratios_for_std) >= 3 else 0.045
        consistency_score = round(max(10.0, min(95.0, (1.0 - sigma / 0.08) * 100)), 1)

        if comp_ranks and trial_ranks:
            avg_comp = float(np.mean(comp_ranks[-5:]))
            avg_trial = float(np.mean(trial_ranks[-5:]))
            conversion_delta = avg_trial - avg_comp
        else:
            conversion_delta = 0.0

        htype_affinity = (ht_ratio_sum / ht_w_sum) if ht_w_sum > 0 else elite_form
        venue_affinity = (venue_ratio_sum / venue_w_sum) if venue_w_sum > 0 else (0.6 * recent_form + 0.4 * elite_form)

        if len(history) >= 4:
            r3 = history[-3:]
            e3 = history[-7:-3]
            r3_avg = float(np.mean([x["raw_ratio"] for x in r3]))
            e3_avg = float(np.mean([x["raw_ratio"] for x in e3])) if e3 else r3_avg
            form_trend = r3_avg - e3_avg
        else:
            form_trend = 0.0

        career_starts = len(history)
        career_starts_log = float(math.log1p(career_starts))

        cat_starts = self.athlete_category_starts.get(fc, {})
        tier_starts = cat_starts.get(target_category.upper(), 0)
        tier_exp_ratio = float(min(1.0, math.log1p(tier_starts) / 4.5))

        return {
            "weighted_form_ratio": float(elite_form),
            "recent_form_ratio": float(recent_form),
            "weighted_points": float(equiv_wc_pts),
            "weighted_rank": float(equiv_wc_rank),
            "consistency_sigma": float(sigma),
            "consistency_score": float(consistency_score),
            "conversion_delta": float(conversion_delta),
            "hill_type_affinity": float(htype_affinity),
            "venue_affinity": float(venue_affinity),
            "current_season_activity": float(current_season_activity),
            "form_trend": float(form_trend),
            "career_starts_log": career_starts_log,
            "has_wc_experience": 1.0 if wc_count > 0 else 0.0,
            "is_tier_debutant": 1.0 if is_wc_debutant else 0.0,
            "tier_experience_ratio": tier_exp_ratio,
            "tier_starts_count": tier_starts,
            "wc_count": int(wc_count),
        }

    def classify_athlete_profiles(self, fis_code: str, stats: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
        """Profile wielowymiarowe dla interfejsu użytkownika."""
        if stats is None:
            stats = self.get_athlete_stats(fis_code)

        profiles = []
        conv = stats.get("conversion_delta", 0.0)
        c_score = stats.get("consistency_score", 50.0)

        if conv >= 2.5:
            profiles.append({
                "code": "clutch",
                "icon": "🔥",
                "label": "Zwierzę konkursowe",
                "tag_class": "badge-clutch",
                "description": f"Zyskuje średnio +{conv:.1f} pozycji w konkursie w porównaniu do serii próbnych."
            })
        elif conv <= -3.0:
            profiles.append({
                "code": "training_champ",
                "icon": "📉",
                "label": "Syndrom treningów",
                "tag_class": "badge-choker",
                "description": f"W seriach próbnych osiąga wyższe lokaty niż pod presją konkursową."
            })
        else:
            profiles.append({
                "code": "balanced_pressure",
                "icon": "⚖️",
                "label": "Równy poziom",
                "tag_class": "badge-balanced",
                "description": "Skacze na zbliżonym poziomie w seriach próbnych i konkursowych."
            })

        if c_score >= 80.0:
            profiles.append({
                "code": "metronome",
                "icon": "🎯",
                "label": "Metronom",
                "tag_class": "badge-metronome",
                "description": f"Wysoka powtarzalność prób ({c_score:.0f}%). Minimalna wariancja odległości."
            })
        elif c_score <= 45.0:
            profiles.append({
                "code": "volatile",
                "icon": "🎲",
                "label": "Chwiejny",
                "tag_class": "badge-volatile",
                "description": f"Duża wariancja skoków ({c_score:.0f}% stabilności) — potrafi odpalić petardę lub zepsuć skok."
            })

        return profiles

    def get_athlete_wind_profile(
        self,
        fis_code: str,
        hill_size: float = 130.0,
        hill_type: str = "LARGE",
        stats: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Aero-profil zawodnika (lotnik vs siłowy)."""
        if stats is None:
            stats = self.get_athlete_stats(fis_code, hill_type=hill_type)

        c_score = stats.get("consistency_score", 50.0)
        history = self.athlete_history.get(str(fis_code).strip(), [])
        flying_jumps = [x["raw_ratio"] for x in history if (x.get("hill_type") or "").upper() == "FLYING"]
        trad_jumps = [x["raw_ratio"] for x in history if (x.get("hill_type") or "").upper() in ["LARGE", "NORMAL"]]

        if len(flying_jumps) >= 3 and len(trad_jumps) >= 5:
            flying_delta = float(np.mean(flying_jumps)) - float(np.mean(trad_jumps))
        else:
            flying_delta = 0.0

        if flying_delta >= 0.012:
            headwind_efficiency = 1.30
            tailwind_penalty = 1.35
            profile_type = "GLIDER"
        elif flying_delta <= -0.010:
            headwind_efficiency = 0.90
            tailwind_penalty = 0.80
            profile_type = "POWER_RESILIENT"
        else:
            headwind_efficiency = 1.0
            tailwind_penalty = 1.0
            profile_type = "BALANCED"

        gust_sens = max(0.6, min(1.6, (100.0 - c_score) / 45.0))
        return {
            "headwind_efficiency": float(headwind_efficiency),
            "tailwind_penalty_factor": float(tailwind_penalty),
            "gust_sensitivity": float(gust_sens),
            "profile_type": profile_type
        }

    def extract_features(
        self,
        fis_code: str,
        hill_size: float,
        hill_type: str,
        place: Optional[str],
        weather: Dict[str, Any],
        target_date: Optional[datetime.date] = None,
        weekend_sessions: Optional[List[Dict[str, Any]]] = None,
        jump_sequence_index: float = 0.5,
        target_category: str = DEFAULT_TARGET_CATEGORY,
    ) -> np.ndarray:
        """
        Czysty, 14-elementowy wektor cech (100% Permutation-Invariant!):
        [
          0: norm_hill_size,
          1: is_normal,
          2: is_large,
          3: is_flying,
          4: weighted_form_ratio (elite_form),
          5: recent_form_ratio (forma z ostatnich 28 dni),
          6: current_season_activity (1.0 = aktywny w zimie, 0.0 = nieobecny),
          7: hill_type_affinity,
          8: venue_affinity,
          9: wc_experience (logarytmiczne doświadczenie w PŚ),
          10: headwind_component / 3.0,
          11: wind_speed / 5.0,
          12: consistency_score / 100.0,
          13: conversion_delta / 10.0
        ]
        ZERO target leakage (brak cech punktowych w wektorze wejściowym)!
        """
        ht = (hill_type or "LARGE").upper()
        hs = float(hill_size or 130.0)
        stats = self.get_athlete_stats(
            fis_code,
            hill_type=ht,
            place=place,
            target_date=target_date,
            target_category=target_category
        )

        norm_hs = (hs - 130.0) / 40.0
        is_normal = 1.0 if hs < 110 else 0.0
        is_large = 1.0 if 110 <= hs < 185 else 0.0
        is_flying = 1.0 if hs >= 185 else 0.0

        headwind = float(weather.get("headwind_component") if weather.get("headwind_component") is not None else (weather.get("headwind") if weather.get("headwind") is not None else 0.0))
        wind_spd = float(weather.get("wind_speed", 1.5))

        vec = np.array([
            norm_hs,
            is_normal,
            is_large,
            is_flying,
            stats["weighted_form_ratio"],
            stats["recent_form_ratio"],
            stats["current_season_activity"],
            stats["hill_type_affinity"],
            stats["venue_affinity"],
            min(1.0, math.log1p(stats["wc_count"]) / 5.0),
            headwind / 3.0,
            wind_spd / 5.0,
            stats["consistency_score"] / 100.0,
            stats["conversion_delta"] / 10.0
        ], dtype=np.float32)

        return vec

    def to_dict(self) -> Dict[str, Any]:
        """Serializacja stanu ekstraktora do JSON."""
        return {
            "athlete_history": self.athlete_history,
            "wc_athletes_fis": list(self.wc_athletes_fis),
            "wc_starts": self.wc_starts,
            "athlete_base_ratings": self.athlete_base_ratings,
            "athlete_category_starts": self.athlete_category_starts,
        }

    def from_dict(self, data: Dict[str, Any]):
        """Deserializacja stanu ekstraktora z JSON."""
        self.athlete_history = data.get("athlete_history", {})
        self.wc_athletes_fis = set(data.get("wc_athletes_fis", []))
        self.wc_starts = data.get("wc_starts", {})
        self.athlete_base_ratings = data.get("athlete_base_ratings", {})
        self.athlete_category_starts = data.get("athlete_category_starts", {})
