import math
import datetime
from typing import Dict, Any, List, Optional, Tuple
import numpy as np

# Wagi typów sesji w historii ogólnej
SESSION_TYPE_WEIGHTS: Dict[str, float] = {
    "COMPETITION": 1.0,
    "QUA": 0.65,
    "QUALIFICATION": 0.65,
    "QUALIFICATIONS": 0.65,
    "TRIAL": 0.40,
    "TRAINING": 0.20,
}

# Wagi rangi zawodów
RACE_CATEGORY_WEIGHTS: Dict[str, float] = {
    "WC": 1.0,
    "OWG": 1.0,
    "WSC": 1.0,
    "SFWC": 1.0,
    "GP": 0.75,
    "COC": 0.55,
    "FC": 0.35,
    "FESA": 0.30,
}

HALF_LIFE_DAYS = 150.0  # ~5 miesięcy (jeden sezon zimowy)


def calculate_time_weight(session_date_str: Optional[str], ref_date: Optional[datetime.date] = None) -> float:
    """Zanikanie wykładnicze (exponential decay): bieżący miesiąc ~1.0, 1 rok temu ~0.18, 5 lat temu ~0."""
    if not session_date_str:
        return 0.05
    if ref_date is None:
        ref_date = datetime.date.today()

    try:
        s_date = datetime.date.fromisoformat(str(session_date_str).strip()[:10])
        delta_days = (ref_date - s_date).days
        if delta_days < 0:
            delta_days = 0
        weight = math.exp(-0.693147 * delta_days / HALF_LIFE_DAYS)
        return float(max(0.0001, weight))
    except Exception:
        return 0.05


class SkiFeatureExtractor:
    """
    Ekstraktor cech skoczków FIS:
    - Normalizacja do siły obsady serii (kto skakał, a kto odpuścił)
    - Wskaźniki powtarzalności (odchylenie standardowe sigma)
    - Wskaźnik konwersji presji (trening/kwalifikacje -> konkurs)
    - Kontekst bieżącego weekendu (kwalifikacje, treningi, sobotni konkurs)
    - Wielowymiarowe profile psychologiczno-techniczne zawodników
    """
    def __init__(self):
        # fis_code -> lista wpisów historycznych
        self.athlete_history: Dict[str, List[Dict[str, Any]]] = {}
        # Zbiór kodów FIS zawodników z Pucharu Świata
        self.wc_athletes_fis: set = set()
        # Przybliżony rating bazowy zawodnika do oceny siły obsady
        self.athlete_base_ratings: Dict[str, float] = {}

    def fit_from_sessions(self, sessions: List[Dict[str, Any]]):
        """
        Przetwarza chronologicznie sesje z bazy danych, ocenia siłę obsady każdej sesji
        i buduje profile zawodników.
        """
        sorted_sessions = sorted(
            [s for s in sessions if s.get("date")],
            key=lambda x: (str(x.get("date") or ""), str(x.get("time") or ""))
        )

        self.athlete_history.clear()
        self.wc_athletes_fis.clear()
        self.athlete_base_ratings.clear()

        # KROK 1: Wstępna ocena ratingu bazowego zawodników (średnia historyczna)
        temp_athlete_ratios: Dict[str, List[float]] = {}
        for s in sorted_sessions:
            hill_size = float(s.get("hillSize") or 130)
            if hill_size <= 0:
                hill_size = 130.0

            results = s.get("results_data", {}).get("results", [])
            for r in results:
                raw_fis = r.get("fis_code")
                if not raw_fis:
                    continue
                fis_code = str(raw_fis).strip()
                jumps = r.get("jumps", [])
                distances = [float(j["distance"]) for j in jumps if j.get("distance") and float(j["distance"]) > 0]
                if distances:
                    ratio = max(distances) / hill_size
                    if fis_code not in temp_athlete_ratios:
                        temp_athlete_ratios[fis_code] = []
                    temp_athlete_ratios[fis_code].append(ratio)

        for code, ratios in temp_athlete_ratios.items():
            self.athlete_base_ratings[code] = float(np.mean(ratios[-15:]))

        # KROK 2: Budowanie historii z normalizacją do siły obsady serii
        for s in sorted_sessions:
            hill_size = float(s.get("hillSize") or 130)
            hill_type = (s.get("hillType") or "LARGE").upper()
            place = (s.get("event_place") or "").strip().title()
            date = str(s.get("date") or "").strip()[:10]
            stype = (s.get("session_type") or "COMPETITION").upper().strip()
            rcat = (s.get("race_category") or s.get("category_code") or "WC").upper().strip()

            s_weight = SESSION_TYPE_WEIGHTS.get(stype, 0.5)
            cat_weight = RACE_CATEGORY_WEIGHTS.get(rcat, 0.5)

            results = s.get("results_data", {}).get("results", [])
            if not results:
                continue

            # Ocena siły obsady serii (średni rating startujących zawodników)
            participating_ratings = [
                self.athlete_base_ratings.get(str(r.get("fis_code", "")).strip(), 0.88)
                for r in results if r.get("fis_code")
            ]
            field_strength = float(np.mean(participating_ratings)) if participating_ratings else 0.88
            # Normalizator: elita PŚ ma średnią ~0.94
            field_quality_factor = max(0.80, min(1.15, field_strength / 0.93))

            for r in results:
                raw_fis = r.get("fis_code")
                if not raw_fis:
                    continue
                fis_code = str(raw_fis).strip()

                if rcat == "WC":
                    self.wc_athletes_fis.add(fis_code)

                jumps = r.get("jumps", [])
                distances = [float(j["distance"]) for j in jumps if j.get("distance") and float(j["distance"]) > 0]
                if not distances:
                    continue

                best_dist = max(distances)
                total_pts = float(r.get("total_points") or 0.0)
                rank = float(r.get("rank") or 50)
                dist_ratio = best_dist / hill_size if hill_size > 0 else 0.9

                entry = {
                    "date": date,
                    "place": place,
                    "hill_size": hill_size,
                    "hill_type": hill_type,
                    "session_type": stype,
                    "race_category": rcat,
                    "session_weight": s_weight,
                    "category_weight": cat_weight,
                    "field_quality_factor": field_quality_factor,
                    "best_distance": best_dist,
                    "dist_ratio": dist_ratio,
                    "total_points": total_pts,
                    "rank": rank,
                }

                if fis_code not in self.athlete_history:
                    self.athlete_history[fis_code] = []

                self.athlete_history[fis_code].append(entry)

    def get_athlete_stats(
        self,
        fis_code: str,
        hill_type: str = "LARGE",
        place: Optional[str] = None,
        target_date: Optional[datetime.date] = None
    ) -> Dict[str, Any]:
        """
        Wylicza wyważone cechy zawodnika:
        - Forma z wagami czasowymi i wagami serii
        - Wskaźnik stabilności / powtarzalności (sigma odległości)
        - Wskaźnik konwersji presji (czy zyskuje/traci w konkursie względem treningów)
        - Afinitet do typu skoczni i konkretnego obiektu
        """
        code = str(fis_code).strip()
        history = self.athlete_history.get(code, [])

        if not history:
            return {
                "weighted_form_ratio": 0.88,
                "weighted_points": 180.0,
                "weighted_rank": 40.0,
                "consistency_sigma": 0.05,
                "consistency_score": 50.0,
                "conversion_delta": 0.0,
                "hill_type_affinity": 0.88,
                "venue_affinity": 0.88,
                "current_season_activity": 0.0,
                "form_trend": 0.0,
                "career_starts_log": 0.0,
                "has_wc_experience": 1.0 if code in self.wc_athletes_fis else 0.0
            }

        if target_date is None:
            target_date = datetime.date.today()

        ht = (hill_type or "LARGE").upper()
        target_place = (place or "").strip().title()

        total_weight = 0.0
        weighted_ratio_sum = 0.0
        weighted_pts_sum = 0.0
        weighted_rank_sum = 0.0

        ht_weight_sum = 0.0
        ht_ratio_sum = 0.0

        venue_weight_sum = 0.0
        venue_ratio_sum = 0.0

        recent_activity_score = 0.0
        recent_ratios = []

        # Do wskaźnika konwersji trening -> konkurs
        training_qualy_ranks = []
        competition_ranks = []

        relevant_history = history[-40:]

        for entry in relevant_history:
            t_weight = calculate_time_weight(entry.get("date"), target_date)
            s_weight = entry.get("session_weight", 0.5)
            c_weight = entry.get("category_weight", 0.5)
            f_quality = entry.get("field_quality_factor", 1.0)

            # Waga skoku skalowana jakością stawki
            w = t_weight * s_weight * c_weight * f_quality

            total_weight += w
            weighted_ratio_sum += entry["dist_ratio"] * w
            weighted_pts_sum += entry["total_points"] * w
            weighted_rank_sum += entry["rank"] * w

            recent_ratios.append(entry["dist_ratio"])

            stype = entry.get("session_type", "")
            if stype in ["QUA", "QUALIFICATION", "QUALIFICATIONS", "TRAINING", "TRIAL"]:
                training_qualy_ranks.append((entry["date"], entry["rank"]))
            elif stype == "COMPETITION":
                competition_ranks.append((entry["date"], entry["rank"]))

            if entry.get("hill_type") == ht:
                ht_weight_sum += w
                ht_ratio_sum += entry["dist_ratio"] * w

            if target_place and entry.get("place") == target_place:
                venue_weight_sum += w
                venue_ratio_sum += entry["dist_ratio"] * w

            if t_weight >= 0.4:
                recent_activity_score += w

        if total_weight > 0:
            weighted_form_ratio = weighted_ratio_sum / total_weight
            weighted_points = weighted_pts_sum / total_weight
            weighted_rank = weighted_rank_sum / total_weight
        else:
            weighted_form_ratio = 0.88
            weighted_points = 180.0
            weighted_rank = 40.0

        # POWTARZALNOŚĆ / STABILNOŚĆ (Odchylenie standardowe sigma ostatnich skoków)
        if len(recent_ratios) >= 4:
            sigma = float(np.std(recent_ratios[-15:]))
        else:
            sigma = 0.045
        # Wynik powtarzalności w skali 0-100% (im mniejsza sigma, tym wyższa powtarzalność)
        consistency_score = round(max(10.0, min(99.0, (1.0 - (sigma - 0.015) / 0.06) * 100)), 1)

        # KONWERSJA PRESJI (Trening vs Konkurs)
        # Porównujemy lokaty w konkursie vs trening/kwalifikacje z tych samych dni/weekendów
        conversion_deltas = []
        for c_date, c_rank in competition_ranks[-10:]:
            # Znajdź kwalifikacje/trening w obrębie 3 dni od konkursu
            matched_t = [
                t_rank for t_date, t_rank in training_qualy_ranks
                if abs((datetime.date.fromisoformat(c_date) - datetime.date.fromisoformat(t_date)).days) <= 3
            ]
            if matched_t:
                avg_t_rank = float(np.mean(matched_t))
                # Dodatnia wartość = zyskał pozycje w konkursie (np. był 15 w treningu, a 5 w konkursie -> delta = +10)
                conversion_deltas.append(avg_t_rank - c_rank)

        if conversion_deltas:
            conversion_delta = float(np.mean(conversion_deltas))
        else:
            conversion_delta = 0.0

        # Afinitet typu skoczni
        if ht_weight_sum > 0:
            hill_type_affinity = ht_ratio_sum / ht_weight_sum
        else:
            hill_type_affinity = weighted_form_ratio

        # Afinitet lokalny
        if venue_weight_sum > 0:
            raw_venue_aff = venue_ratio_sum / venue_weight_sum
            venue_blend = min(0.3, venue_weight_sum / (venue_weight_sum + 2.0))
            venue_affinity = (1.0 - venue_blend) * hill_type_affinity + venue_blend * raw_venue_aff
        else:
            venue_affinity = hill_type_affinity

        # Trend formy
        if len(history) >= 4:
            r3 = history[-3:]
            e3 = history[-7:-3]
            r3_avg = float(np.mean([x["dist_ratio"] for x in r3]))
            e3_avg = float(np.mean([x["dist_ratio"] for x in e3])) if e3 else r3_avg
            form_trend = r3_avg - e3_avg
        else:
            form_trend = 0.0

        career_starts = len(history)
        career_starts_log = float(math.log1p(career_starts))

        return {
            "weighted_form_ratio": float(weighted_form_ratio),
            "weighted_points": float(weighted_points),
            "weighted_rank": float(weighted_rank),
            "consistency_sigma": float(sigma),
            "consistency_score": float(consistency_score),
            "conversion_delta": float(conversion_delta),
            "hill_type_affinity": float(hill_type_affinity),
            "venue_affinity": float(venue_affinity),
            "current_season_activity": float(min(1.0, recent_activity_score / 3.0)),
            "form_trend": float(form_trend),
            "career_starts_log": career_starts_log,
            "has_wc_experience": 1.0 if code in self.wc_athletes_fis else 0.0
        }

    def classify_athlete_profiles(self, fis_code: str, stats: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
        """
        Zwraca listę profili wielowymiarowych z ikonami, kodami i pełnymi wyjaśnieniami dla użytkownika:
        - Wymiar 1: Konwersja Presji (Zwierzę konkursowe vs Mistrz treningów vs Stabilna konwersja)
        - Wymiar 2: Powtarzalność (Metronom vs Chwiejny)
        - Wymiar 3: Profil Skoczni (Lotnik vs Technik vs Uniwersalny)
        """
        if stats is None:
            stats = self.get_athlete_stats(fis_code)

        profiles = []
        conv = stats.get("conversion_delta", 0.0)
        c_score = stats.get("consistency_score", 50.0)
        flying_aff = stats.get("hill_type_affinity", 0.9)
        general_form = stats.get("weighted_form_ratio", 0.9)

        # 1. WYMIAR PRESJI (Trening vs Konkurs)
        if conv >= 2.5:
            profiles.append({
                "code": "clutch",
                "icon": "🔥",
                "label": "Zwierzę konkursowe",
                "tag_class": "badge-clutch",
                "description": f"Zyskuje średnio +{conv:.1f} pozycji w konkursie w porównaniu do treningów i kwalifikacji. Znakomicie radzi sobie z presją."
            })
        elif conv <= -3.0:
            profiles.append({
                "code": "training_champ",
                "icon": "📉",
                "label": "Syndrom treningów",
                "tag_class": "badge-choker",
                "description": f"W seriach próbnych osiąga wyższe lokaty niż w konkursie głównym (średni spadek o {abs(conv):.1f} pozycji pod presją)."
            })
        else:
            profiles.append({
                "code": "balanced_pressure",
                "icon": "⚖️",
                "label": "Równy poziom",
                "tag_class": "badge-balanced",
                "description": "Skacze na zbliżonym poziomie w seriach próbnych, kwalifikacyjnych i konkursowych."
            })

        # 2. WYMIAR POWTARZALNOŚCI
        if c_score >= 82.0:
            profiles.append({
                "code": "metronome",
                "icon": "🎯",
                "label": "Metronom",
                "tag_class": "badge-metronome",
                "description": f"Wysoka powtarzalność prób ({c_score:.0f}%). Minimalna wariancja odległości między seriami."
            })
        elif c_score <= 45.0:
            profiles.append({
                "code": "volatile",
                "icon": "🎲",
                "label": "Chwiejny",
                "tag_class": "badge-volatile",
                "description": "Duży rozrzut skoków i wysoka amplituda punktowa. Skoczek podatny na dyspozycję chwili i podmuchy wiatru."
            })

        # 3. WYMIAR SPECJALIZACJI SKOCZNI
        if flying_aff >= general_form + 0.035 and general_form > 0:
            profiles.append({
                "code": "flying_specialist",
                "icon": "🦅",
                "label": "Lotnik",
                "tag_class": "badge-flying",
                "description": "Specjalista od skoczni mamucich (HS 200+). Osiąga na nich wyraźnie większe odległości niż na obiektach tradycyjnych."
            })

        return profiles

    def process_weekend_context(
        self,
        fis_code: str,
        weekend_sessions: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """
        Przetwarza serie rozegrane w bieżący weekend przed konkursem (niedzielny konkurs uwzględnia
        sobotni konkurs, piątkowe kwalifikacje i treningi; sobotni uwzględnia kwalifikacje i treningi).
        """
        if not weekend_sessions:
            return {"has_weekend_data": 0.0, "weekend_perf_ratio": 0.0}

        code = str(fis_code).strip()
        weighted_ratios = []
        total_w = 0.0

        for s in weekend_sessions:
            stype = (s.get("session_type") or "").upper().strip()
            # Wagi weekendu: Sobotni konkurs (0.50), Kwalifikacje (0.35), Trening (0.15)
            if stype == "COMPETITION":
                w = 0.50
            elif stype in ["QUA", "QUALIFICATION", "QUALIFICATIONS"]:
                w = 0.35
            else:
                w = 0.15

            hill_size = float(s.get("hillSize") or 130)
            if hill_size <= 0:
                hill_size = 130.0

            results = s.get("results_data", {}).get("results", [])
            for r in results:
                if str(r.get("fis_code", "")).strip() == code:
                    jumps = r.get("jumps", [])
                    distances = [float(j["distance"]) for j in jumps if j.get("distance") and float(j["distance"]) > 0]
                    if distances:
                        ratio = max(distances) / hill_size
                        weighted_ratios.append(ratio * w)
                        total_w += w
                    break

        if total_w > 0:
            weekend_perf = sum(weighted_ratios) / total_w
            return {"has_weekend_data": 1.0, "weekend_perf_ratio": float(weekend_perf)}

        return {"has_weekend_data": 0.0, "weekend_perf_ratio": 0.0}

    def extract_features(
        self,
        fis_code: str,
        hill_size: float,
        hill_type: str,
        place: Optional[str],
        weather: Dict[str, Any],
        target_date: Optional[datetime.date] = None,
        weekend_sessions: Optional[List[Dict[str, Any]]] = None
    ) -> np.ndarray:
        """
        Tworzy 18-elementowy zaawansowany wektor cech:
        [
          0: norm_hill_size,
          1: is_normal,
          2: is_large,
          3: is_flying,
          4: weighted_form_ratio,
          5: weighted_points / 300,
          6: weighted_rank / 50,
          7: consistency_sigma * 10.0,
          8: conversion_delta / 10.0,
          9: hill_type_affinity,
          10: venue_affinity,
          11: current_season_activity,
          12: form_trend * 5.0,
          13: has_weekend_data,
          14: weekend_perf_ratio,
          15: headwind_component / 3.0,
          16: wind_speed / 5.0,
          17: (temperature - 0.0) / 15.0
        ]
        """
        ht = (hill_type or "LARGE").upper()
        hs = float(hill_size or 130.0)
        stats = self.get_athlete_stats(fis_code, ht, place=place, target_date=target_date)
        w_ctx = self.process_weekend_context(fis_code, weekend_sessions or [])

        is_normal = 1.0 if "NORM" in ht or hs < 110 else 0.0
        is_flying = 1.0 if "FLY" in ht or hs >= 200 else 0.0
        is_large = 1.0 if (not is_normal and not is_flying) else 0.0
        norm_hs = (hs - 130.0) / 40.0

        headwind = float(weather.get("headwind_component", 0.5))
        wind_spd = float(weather.get("wind_speed", 1.5))
        temp = float(weather.get("temperature", -2.0))

        vec = np.array([
            norm_hs,
            is_normal,
            is_large,
            is_flying,
            stats["weighted_form_ratio"],
            stats["weighted_points"] / 300.0,
            stats["weighted_rank"] / 50.0,
            stats["consistency_sigma"] * 10.0,
            stats["conversion_delta"] / 10.0,
            stats["hill_type_affinity"],
            stats["venue_affinity"],
            stats["current_season_activity"],
            stats["form_trend"] * 5.0,
            w_ctx["has_weekend_data"],
            w_ctx["weekend_perf_ratio"],
            headwind / 3.0,
            wind_spd / 5.0,
            temp / 15.0
        ], dtype=np.float32)

        return vec
