import math
import datetime
import unicodedata
from typing import Dict, Any, List, Optional, Set
import numpy as np

from app.services.prediction.prediction_config import (
    RACE_CATEGORY_WEIGHTS,
    SESSION_TYPE_WEIGHTS,
    SESSION_HALF_LIFE_DAYS,
    SESSION_MAX_AGE_DAYS,
    WEEKEND_LOOKBACK_DAYS,
    WEEKEND_SESSION_WEIGHTS,
    SESSION_STAGE_ORDER,
    HALF_LIFE_DAYS,
    RECENT_FORM_WINDOW_DAYS,
    LOWER_TIER_DOMINANCE_WINDOW_DAYS,
    DEFAULT_TARGET_CATEGORY,
    INPUT_FEATURES_DIM,
    resolve_session_category,
    resolve_session_type,
)
from app.services.prediction.hyperparams_service import get_active_hyperparams

# Przeliczniki siły ligowej na ekwiwalent Pucharu Świata
_LEAGUE_QUALITY_MULT: Dict[str, float] = {
    "WC": 1.00,
    "OWG": 1.00,
    "WSC": 1.00,
    "SFWC": 1.00,
    "GP": 0.88,
    "COC": 0.65,
    "FC": 0.45,
    "WJC": 0.42,
    "FESA": 0.40,
}

_LEAGUE_DIST_MULT: Dict[str, float] = {
    "WC": 1.00,
    "OWG": 1.00,
    "WSC": 1.00,
    "SFWC": 1.00,
    "GP": 0.95,
    "COC": 0.85,
    "FC": 0.75,
    "WJC": 0.75,
    "FESA": 0.75,
}


def normalize_place_name(place: Optional[str]) -> str:
    """Normalizuje nazwę miejscowości skoczni (usuwa polskie/specjalne znaki i ujednolica warianty)."""
    if not place:
        return ""
    raw = unicodedata.normalize("NFKD", str(place))
    ascii_str = "".join(ch for ch in raw if not unicodedata.combining(ch)).lower().strip()
    ascii_str = ascii_str.replace("od radhostem", "pod radhostem").replace("kulm mitterndorf", "bad mitterndorf")
    cleaned = " ".join("".join(c if c.isalnum() else " " for c in ascii_str).split())
    return cleaned


def places_match_norm(norm_a: str, tok_a: str, norm_b: str) -> bool:
    """Szybkie porównanie już znormalizowanych nazw miejscowości skoczni (bez unicodedata w pętli)."""
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b:
        return True
    if len(norm_a) >= 4 and len(norm_b) >= 4 and (norm_a in norm_b or norm_b in norm_a):
        return True
    tok_b = norm_b.split()[0] if " " in norm_b else norm_b
    return len(tok_a) >= 4 and tok_a == tok_b


def places_match(place_a: Optional[str], place_b: Optional[str]) -> bool:
    """Sprawdza czy dwie nazwy miejscowości dotyczą tej samej skoczni."""
    na = normalize_place_name(place_a)
    nb = normalize_place_name(place_b)
    tok_a = na.split()[0] if " " in na else na
    return places_match_norm(na, tok_a, nb)


def calculate_time_weight(
    session_date_str: Optional[str],
    ref_date: Optional[datetime.date] = None,
    half_life: float = HALF_LIFE_DAYS,
) -> float:
    """Zanikanie wykładnicze (exponential decay) względem daty docelowej."""
    if not session_date_str:
        return 0.05
    if ref_date is None:
        ref_date = datetime.date.today()

    try:
        s_date = datetime.date.fromisoformat(str(session_date_str).strip()[:10])
        delta_days = (ref_date - s_date).days
        if delta_days < 0:
            return 0.0
        weight = math.exp(-0.693147 * delta_days / max(0.5, half_life))
        return float(weight)
    except Exception:
        return 0.05


def compute_session_entries(session: Dict[str, Any]) -> List[tuple]:
    """
    Przetwarza pojedynczą sesję z bazy i wylicza znormalizowaną jakość każdego zawodnika
    w obrębie tej sesji (comp_quality w [0, 1]), stosunek dystansu do HS oraz meta-pola.
    Zwraca listę krotek (fis_code_str, entry_dict).
    """
    date_str = str(session.get("date") or "").strip()[:10]
    if not date_str:
        return []
    try:
        date_ord = datetime.date.fromisoformat(date_str).toordinal()
    except Exception:
        return []

    try:
        hill_size = float(session.get("hillSize") or 130.0)
    except Exception:
        hill_size = 130.0
    if hill_size <= 0:
        hill_size = 130.0

    hill_type = (session.get("hillType") or "LARGE").upper()
    place = (session.get("event_place") or "").strip().title()
    place_norm = normalize_place_name(place)
    rcat = resolve_session_category(session)
    stype = resolve_session_type(session)
    time_str = str(session.get("time") or "12:00").strip()[:5]
    session_id = str(session.get("_id") or "")
    race_id = str(session.get("raceId") or "")

    results = session.get("results_data", {}).get("results", [])
    if not results:
        return []

    # Krok 1: Zbierz surowe parametry skoków do wyliczenia znormalizowanej jakości w sesji
    parsed_rows = []
    explicit_ranks = 0
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
        jump_pts = [float(j["points"]) for j in jumps if j.get("points") is not None]
        avg_jump_pts = float(np.mean(jump_pts)) if jump_pts else best_dist
        total_pts = float(r.get("total_points") or 0.0)

        rank_val: Optional[float] = None
        if r.get("rank") is not None:
            try:
                rv = float(r["rank"])
                if rv > 0:
                    rank_val = rv
                    explicit_ranks += 1
            except Exception:
                pass

        parsed_rows.append({
            "fc": fc,
            "best_dist": best_dist,
            "avg_jump_pts": avg_jump_pts,
            "total_pts": total_pts,
            "rank": rank_val,
        })

    n_rows = len(parsed_rows)
    if n_rows == 0:
        return []

    # Jeśli w sesji treningowej brak jawnych miejsc, wyznacz ranking na podstawie pkt/odległości
    use_explicit = explicit_ranks >= max(1, int(0.5 * n_rows))
    if not use_explicit:
        sorted_idx = sorted(range(n_rows), key=lambda i: (-parsed_rows[i]["avg_jump_pts"], -parsed_rows[i]["best_dist"]))
        for pos, orig_idx in enumerate(sorted_idx, 1):
            parsed_rows[orig_idx]["eff_rank"] = float(pos)
    else:
        for row in parsed_rows:
            row["eff_rank"] = row["rank"] if row["rank"] is not None else float(n_rows)

    pts_arr = np.array([row["avg_jump_pts"] for row in parsed_rows], dtype=float)
    p_low = float(np.percentile(pts_arr, 10)) if n_rows >= 5 else float(np.min(pts_arr))
    p_high = float(np.percentile(pts_arr, 98)) if n_rows >= 5 else float(np.max(pts_arr))
    pts_span = max(1e-3, p_high - p_low)

    is_wc_tier = rcat in ("WC", "OWG", "WSC", "SFWC")
    out: List[tuple] = []

    for row in parsed_rows:
        eff_rank = row["eff_rank"]
        rank_pct = max(0.0, min(1.0, 1.0 - (eff_rank - 1.0) / max(1.0, float(n_rows - 1))))
        pts_norm = max(0.0, min(1.0, (row["avg_jump_pts"] - p_low) / pts_span))
        session_quality = float(0.75 * rank_pct + 0.25 * pts_norm)

        total_pts = row["total_pts"]
        competition_scale_pts = total_pts * 2.0 if stype == "QUA" and 0 < total_pts < 160 else total_pts
        raw_ratio = row["best_dist"] / hill_size

        entry = {
            "session_id": session_id,
            "race_id": race_id,
            "date": date_str,
            "date_ord": date_ord,
            "time": time_str,
            "place": place,
            "place_norm": place_norm,
            "hill_size": hill_size,
            "hill_type": hill_type,
            "rcat": rcat,
            "stype": stype,
            "raw_ratio": float(raw_ratio),
            "session_quality": session_quality,
            "total_pts": competition_scale_pts,
            "rank": float(eff_rank),
            "is_wc": is_wc_tier,
        }
        out.append((row["fc"], entry))

    return out


def _is_entry_strictly_before_target(
    entry: Dict[str, Any],
    target_ord: int,
    target_time: Optional[str] = None,
    target_race_id: Optional[str] = None,
    target_session_id: Optional[str] = None,
) -> Optional[int]:
    """
    Zwraca delta_days >= 0 jeśli wpis wystąpił PRZED sesją docelową (zero wycieku z przyszłości),
    albo None jeśli wpis jest z przyszłości / tego samego konkursu / po sesji docelowej tego samego dnia.
    """
    e_ord = entry.get("date_ord")
    if e_ord is None:
        try:
            e_ord = datetime.date.fromisoformat(str(entry.get("date") or "")[:10]).toordinal()
            entry["date_ord"] = e_ord
        except Exception:
            return None

    delta_days = target_ord - e_ord
    if delta_days < 0:
        return None

    eid = entry.get("session_id")
    if target_session_id and eid and eid == target_session_id:
        return None

    if delta_days == 0:
        estype = entry.get("stype", "COMPETITION")
        etime = str(entry.get("time") or "12:00")[:5]
        if estype == "COMPETITION":
            if target_time and etime < target_time:
                return 0
            return None
        if target_time is None:
            return 0
        if target_race_id and entry.get("race_id") and str(entry.get("race_id")) == str(target_race_id):
            return 0
        if etime <= target_time:
            return 0
        if estype in ("TRAINING", "TRIAL") and etime == "12:00":
            return 0
        return None

    return delta_days


class SkiFeatureExtractor:
    """
    Ekstraktor cech skoczków FIS (wersja 18-cechowa, chronologiczna bez wycieku danych):
    - Rozróżnia bieżący weekend na tej skoczni (0-4 dni przed konkursem) od wygasającej historii
    - Szybki rozpad czasowy sesji niekonkursowych (TRAINING 3d, TRIAL 4d, QUA 10d) vs COMPETITION (35d)
    - Pełne okno czasowe do 730 dni bez ucinania historii przez rundy treningowe
    - Obsługa sesji weekendowych przekazywanych na żywo w momencie predykcji
    """

    def __init__(self):
        self.athlete_history: Dict[str, List[Dict[str, Any]]] = {}
        self.wc_athletes_fis: Set[str] = set()
        self.wc_starts: Dict[str, int] = {}
        self.athlete_base_ratings: Dict[str, float] = {}
        self.athlete_category_starts: Dict[str, Dict[str, int]] = {}

    def fit_from_sessions(self, sessions: List[Dict[str, Any]]):
        """
        Przetwarza chronologicznie sesje z bazy danych i buduje czystą historię zawodników.
        """
        sorted_sessions = sorted(
            [s for s in sessions if s.get("date")],
            key=lambda x: (
                str(x.get("date") or "")[:10],
                SESSION_STAGE_ORDER.get(resolve_session_type(x), 3),
                str(x.get("time") or "12:00")[:5],
                str(x.get("_id") or ""),
            ),
        )

        self.athlete_history.clear()
        self.wc_athletes_fis.clear()
        self.wc_starts.clear()
        self.athlete_base_ratings.clear()
        self.athlete_category_starts.clear()

        for s in sorted_sessions:
            entries = compute_session_entries(s)
            for fc, entry in entries:
                if entry["is_wc"]:
                    self.wc_athletes_fis.add(fc)
                    if entry["stype"] == "COMPETITION":
                        self.wc_starts[fc] = self.wc_starts.get(fc, 0) + 1

                rcat = entry["rcat"]
                if fc not in self.athlete_category_starts:
                    self.athlete_category_starts[fc] = {}
                self.athlete_category_starts[fc][rcat] = self.athlete_category_starts[fc].get(rcat, 0) + 1

                if fc not in self.athlete_history:
                    self.athlete_history[fc] = []
                self.athlete_history[fc].append(entry)

        for fc, history in self.athlete_history.items():
            comp_ratios = [h["raw_ratio"] for h in history if h["stype"] == "COMPETITION"][-15:]
            if not comp_ratios:
                comp_ratios = [h["raw_ratio"] for h in history[-15:]]
            self.athlete_base_ratings[fc] = float(np.mean(comp_ratios)) if comp_ratios else 0.85

    @staticmethod
    def build_weekend_overlay(weekend_sessions: Optional[List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
        """Konwertuje surowe sesje weekendowe z bazy na indeks fis_code -> lista wpisów historycznych."""
        overlay: Dict[str, List[Dict[str, Any]]] = {}
        if not weekend_sessions:
            return overlay
        for s in weekend_sessions:
            for fc, entry in compute_session_entries(s):
                overlay.setdefault(fc, []).append(entry)
        return overlay

    def get_athlete_stats(
        self,
        fis_code: str,
        hill_type: str = "LARGE",
        place: Optional[str] = None,
        target_date: Optional[datetime.date] = None,
        target_category: str = DEFAULT_TARGET_CATEGORY,
        target_time: Optional[str] = None,
        target_race_id: Optional[str] = None,
        target_session_id: Optional[str] = None,
        extra_entries: Optional[List[Dict[str, Any]]] = None,
        hp: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Wylicza zagregowane wskaźniki sportowe skoczka na dzień i godzinę docelową
        (100% chronologicznie, bez wycieku danych z przyszłości).
        """
        active_hp = hp if hp is not None else get_active_hyperparams()
        fc = str(fis_code).strip()
        base_history = self.athlete_history.get(fc, [])
        if extra_entries:
            seen_ids = {e.get("session_id") for e in base_history if e.get("session_id")}
            combined_history = list(base_history)
            for ex in extra_entries:
                ex_id = ex.get("session_id")
                if not ex_id or ex_id not in seen_ids:
                    combined_history.append(ex)
                    if ex_id:
                        seen_ids.add(ex_id)
        else:
            combined_history = base_history

        if target_date is None:
            target_date = datetime.date.today()

        # Dynamiczny początek sezonu zimowego względem target_date:
        if target_date.month >= 10:
            winter_season_start = datetime.date(target_date.year, 11, 1)
        else:
            winter_season_start = datetime.date(target_date.year - 1, 11, 1)

        if not combined_history:
            return {
                "weighted_form_ratio": 0.75,
                "recent_form_ratio": 0.65,
                "weighted_form_quality": 0.20,
                "recent_form_quality": 0.18,
                "weekend_precomp_quality": 0.18,
                "weekend_presence_confidence": 0.0,
                "weekend_vs_form_delta": 0.0,
                "weighted_points": 100.0,
                "weighted_rank": 55.0,
                "consistency_sigma": 0.06,
                "consistency_score": 40.0,
                "conversion_delta": 0.0,
                "hill_type_affinity": 0.75,
                "venue_affinity": 0.75,
                "hill_type_affinity_quality": 0.20,
                "venue_affinity_quality": 0.20,
                "current_season_activity": 0.0,
                "form_trend": 0.0,
                "career_starts_log": 0.0,
                "has_wc_experience": 0.0,
                "is_tier_debutant": 1.0,
                "tier_experience_ratio": 0.0,
                "tier_starts_count": 0,
                "wc_count": 0,
                "lower_tier_dominance": 0.0,
                "coc_recent_count": 0,
                "coc_top6_count": 0,
                "coc_avg_rank": None,
            }

        target_cat_upper = (target_category or DEFAULT_TARGET_CATEGORY).upper()
        is_elite_target = target_cat_upper in ("WC", "OWG", "WSC", "SFWC")
        target_ord = target_date.toordinal()
        winter_start_ord = winter_season_start.toordinal()
        target_ht_upper = (hill_type or "LARGE").upper()
        target_place_norm = normalize_place_name(place)
        target_place_tok = target_place_norm.split()[0] if " " in target_place_norm else target_place_norm
        has_extra = bool(extra_entries)

        w_sum = 0.0
        ratio_sum = 0.0
        qual_sum = 0.0
        pts_sum = 0.0
        rank_sum = 0.0

        recent_w_sum = 0.0
        recent_ratio_sum = 0.0
        recent_qual_sum = 0.0

        ht_w_sum = 0.0
        ht_ratio_sum = 0.0
        ht_qual_sum = 0.0

        venue_w_sum = 0.0
        venue_ratio_sum = 0.0
        venue_qual_sum = 0.0

        weekend_w_sum = 0.0
        weekend_qual_sum = 0.0
        weekend_max_conf = 0.0

        comp_ranks_recent: List[float] = []
        trial_ranks_recent: List[float] = []
        comp_ratios_for_std: List[float] = []
        qua_ratios_for_std: List[float] = []

        season_jumps_count = 0
        wc_count_until_target = 0
        tier_starts_until_target = 0
        total_starts_until_target = 0

        comp_qual_recent3: List[float] = []
        comp_qual_prev4: List[float] = []

        lt_w_sum = 0.0
        lt_qual_sum = 0.0
        coc_recent_count = 0
        coc_top6_count = 0
        coc_ranks_list: List[float] = []

        for entry in combined_history:
            e_ord = entry.get("date_ord")
            if not has_extra and e_ord is not None and e_ord > target_ord:
                break

            delta_days = _is_entry_strictly_before_target(
                entry,
                target_ord=target_ord,
                target_time=target_time,
                target_race_id=target_race_id,
                target_session_id=target_session_id,
            )
            if delta_days is None:
                continue

            total_starts_until_target += 1
            stype = entry.get("stype", "COMPETITION")
            rcat = entry.get("rcat", "WC")

            # Zliczanie doświadczenia wyłącznie DO dnia konkursu docelowego (zero leakage z przyszłości)
            if entry.get("is_wc") and stype == "COMPETITION":
                wc_count_until_target += 1
            if rcat == target_cat_upper and stype == "COMPETITION":
                tier_starts_until_target += 1

            # Ograniczenie okna długoterminowego (730 dni dla konkursów; szybkie wygasanie niekonkursowych)
            max_age = SESSION_MAX_AGE_DAYS.get(stype, 730)
            if delta_days > max_age:
                continue

            if stype == "COMPETITION":
                half_life = float(active_hp["comp_half_life_days"])
            elif stype == "QUA":
                half_life = float(active_hp["qua_half_life_days"])
            elif stype == "TRIAL":
                half_life = float(active_hp["trial_half_life_days"])
            else:
                half_life = float(active_hp["training_half_life_days"])
            time_w = math.exp(-0.693147 * delta_days / max(0.5, half_life))
            st_w = SESSION_TYPE_WEIGHTS.get(stype, 0.25)

            if is_elite_target:
                tier_w = RACE_CATEGORY_WEIGHTS.get(rcat, 0.20)
                tier_dist_mult = _LEAGUE_DIST_MULT.get(rcat, 0.75)
                tier_qual_mult = _LEAGUE_QUALITY_MULT.get(rcat, 0.45)
                if entry.get("is_wc"):
                    equiv_pts = entry["total_pts"]
                    equiv_rank = entry["rank"]
                elif rcat == "GP":
                    equiv_pts = entry["total_pts"] * 0.90
                    equiv_rank = entry["rank"] * 1.2 + 5.0
                elif rcat == "COC":
                    equiv_pts = entry["total_pts"] * 0.70
                    equiv_rank = entry["rank"] * 2.0 + 30.0
                else:
                    equiv_pts = entry["total_pts"] * 0.55
                    equiv_rank = entry["rank"] * 2.5 + 45.0
            else:
                tier_w = 1.00
                tier_dist_mult = 1.00
                tier_qual_mult = 1.00
                equiv_pts = entry["total_pts"]
                equiv_rank = entry["rank"]

            w = time_w * st_w * tier_w
            scaled_ratio = entry["raw_ratio"] * tier_dist_mult
            scaled_qual = float(entry.get("session_quality", 0.5)) * tier_qual_mult

            w_sum += w
            ratio_sum += scaled_ratio * w
            qual_sum += scaled_qual * w
            pts_sum += equiv_pts * w
            rank_sum += equiv_rank * w

            if stype == "COMPETITION":
                comp_ratios_for_std.append(scaled_ratio)
                if delta_days <= 365:
                    comp_ranks_recent.append(entry["rank"])
                comp_qual_prev4.append(scaled_qual)
                if len(comp_qual_prev4) > 7:
                    comp_qual_prev4.pop(0)
            elif stype in ("QUA", "TRIAL") and delta_days <= 365:
                trial_ranks_recent.append(entry["rank"])
                if stype == "QUA":
                    qua_ratios_for_std.append(scaled_ratio)

            # Dominacja w niższych ligach (COC/FC w ostatnich 120 dniach PRZED przelicznikiem ligowym)
            if stype == "COMPETITION" and rcat in ("COC", "FC") and delta_days <= LOWER_TIER_DOMINANCE_WINDOW_DAYS:
                lt_league_w = 1.0 if rcat == "COC" else 0.50
                lt_tw = math.exp(-0.693147 * delta_days / 45.0) * lt_league_w
                lt_w_sum += lt_tw
                lt_qual_sum += float(entry.get("session_quality", 0.5)) * lt_tw
                if rcat == "COC":
                    coc_recent_count += 1
                    rk = float(entry.get("rank", 50.0))
                    coc_ranks_list.append(rk)
                    if rk <= 6.0:
                        coc_top6_count += 1

            # Aktywność w bieżącym sezonie zimowym (konkursy/kwalifikacje od 1 listopada)
            if e_ord is not None and winter_start_ord <= e_ord <= target_ord and stype in ("COMPETITION", "QUA"):
                season_jumps_count += 1

            # Ostatnie N dni (świeża dyspozycja)
            if delta_days <= int(active_hp["recent_form_window_days"]):
                recent_w_sum += w
                recent_ratio_sum += scaled_ratio * w
                recent_qual_sum += scaled_qual * w

            # Afinitet typu skoczni (dłuższe okno 180d, głównie konkursy i QUA, bez starych treningów)
            if entry["hill_type"] == target_ht_upper and stype in ("COMPETITION", "QUA"):
                ht_tw = math.exp(-0.693147 * delta_days / 180.0) * (1.0 if stype == "COMPETITION" else 0.45) * tier_w
                ht_w_sum += ht_tw
                ht_ratio_sum += scaled_ratio * ht_tw
                ht_qual_sum += scaled_qual * ht_tw

            # Afinitet konkretnej skoczni (konkursy do 730d z half-life 240d; treningi/próbne tylko do 14d)
            entry_pnorm = entry.get("place_norm") or ""
            same_venue = places_match_norm(target_place_norm, target_place_tok, entry_pnorm)
            if same_venue:
                if stype in ("COMPETITION", "QUA"):
                    v_tw = math.exp(-0.693147 * delta_days / 240.0) * (1.0 if stype == "COMPETITION" else 0.5) * tier_w
                else:
                    v_tw = math.exp(-0.693147 * delta_days / 4.0) * 0.25 * tier_w
                venue_w_sum += v_tw
                venue_ratio_sum += scaled_ratio * v_tw
                venue_qual_sum += scaled_qual * v_tw

                # Dedykowany sygnał BIEŻĄCEGO WEEKENDU na tej skoczni (0-4 dni przed konkursem)
                if 0 <= delta_days <= WEEKEND_LOOKBACK_DAYS:
                    if stype == "COMPETITION":
                        wk_base_w = float(active_hp["weekend_comp_weight"])
                    elif stype == "QUA":
                        wk_base_w = float(active_hp["weekend_qua_weight"])
                    elif stype == "TRIAL":
                        wk_base_w = float(active_hp["weekend_trial_weight"])
                    else:
                        wk_base_w = float(active_hp["weekend_training_weight"])
                    wk_recency = math.exp(-0.15 * delta_days)
                    wk_w = wk_base_w * wk_recency
                    weekend_w_sum += wk_w
                    weekend_qual_sum += float(entry.get("session_quality", 0.5)) * wk_w
                    if stype in ("COMPETITION", "QUA"):
                        weekend_max_conf = max(weekend_max_conf, 1.0)
                    elif stype == "TRIAL":
                        weekend_max_conf = max(weekend_max_conf, 0.65)
                    else:
                        weekend_max_conf = max(weekend_max_conf, 0.45)

        elite_form_ratio = (ratio_sum / w_sum) if w_sum > 0 else 0.78
        elite_form_qual = (qual_sum / w_sum) if w_sum > 0 else 0.22
        equiv_wc_pts = (pts_sum / w_sum) if w_sum > 0 else 120.0
        equiv_wc_rank = (rank_sum / w_sum) if w_sum > 0 else 50.0

        # Q5 (A1): Start w bieżącym weekendzie (TRAINING/TRIAL/QUA na tej skoczni) potwierdza dyspozycję na śniegu i znosi karę braku startów zimowych
        if weekend_max_conf > 0.0:
            current_season_activity = 1.0
        else:
            current_season_activity = min(1.0, season_jumps_count / 2.0)

        if recent_w_sum > 0:
            recent_form_ratio = recent_ratio_sum / recent_w_sum
            recent_form_qual = recent_qual_sum / recent_w_sum
        else:
            recent_form_ratio = elite_form_ratio * 0.85
            recent_form_qual = elite_form_qual * 0.80

        lower_tier_dominance = float(lt_qual_sum / lt_w_sum) if lt_w_sum > 0 else 0.0
        coc_avg_rank = round(float(np.mean(coc_ranks_list)), 1) if coc_ranks_list else None

        htype_affinity_ratio = (ht_ratio_sum / ht_w_sum) if ht_w_sum > 0 else elite_form_ratio
        htype_affinity_qual = (ht_qual_sum / ht_w_sum) if ht_w_sum > 0 else elite_form_qual

        venue_affinity_ratio = (venue_ratio_sum / venue_w_sum) if venue_w_sum > 0 else (0.6 * recent_form_ratio + 0.4 * elite_form_ratio)
        venue_affinity_qual = (venue_qual_sum / venue_w_sum) if venue_w_sum > 0 else (0.6 * recent_form_qual + 0.4 * elite_form_qual)

        if weekend_w_sum > 0:
            weekend_precomp_qual = float(weekend_qual_sum / weekend_w_sum)
            weekend_vs_form_delta = float(weekend_precomp_qual - elite_form_qual)
        else:
            weekend_precomp_qual = float(0.70 * recent_form_qual + 0.30 * venue_affinity_qual)
            weekend_vs_form_delta = 0.0

        # Kary przy całkowitym braku aktywności tej zimy (ani wcześniejszych startów, ani bieżącego weekendu)
        if current_season_activity == 0.0:
            recent_form_ratio *= 0.88
            recent_form_qual *= 0.82
            elite_form_ratio *= 0.95
            elite_form_qual *= 0.90

        is_wc_debutant = (wc_count_until_target == 0)
        if is_elite_target and is_wc_debutant:
            # Złagodzenie kary debiutanta, jeśli dominuje w PK (COC) i dobrze skacze w tym weekendzie PŚ
            is_verified_gem = (
                (coc_recent_count >= 2 or coc_top6_count >= 1)
                and lower_tier_dominance >= 0.82
                and weekend_max_conf > 0.0
                and weekend_precomp_qual >= 0.72
            )
            if is_verified_gem:
                # Minimalny bufor ostrożności zamiast ostrego -10%/-12%
                elite_form_ratio *= 0.99
                elite_form_qual *= 0.98
                recent_form_ratio *= 0.99
                recent_form_qual *= 0.98
                weekend_vs_form_delta = float(weekend_precomp_qual - elite_form_qual) if weekend_w_sum > 0 else 0.0
            elif weekend_max_conf > 0.0 and weekend_precomp_qual >= 0.80:
                elite_form_ratio *= 0.98
                elite_form_qual *= 0.96
                recent_form_ratio *= 0.97
                recent_form_qual *= 0.95
                weekend_vs_form_delta = float(weekend_precomp_qual - elite_form_qual) if weekend_w_sum > 0 else 0.0
            else:
                elite_form_ratio *= 0.95
                elite_form_qual *= 0.90
                recent_form_ratio *= 0.93
                recent_form_qual *= 0.88

        std_source = comp_ratios_for_std[-10:] if len(comp_ratios_for_std) >= 3 else (comp_ratios_for_std + qua_ratios_for_std)[-10:]
        sigma = float(np.std(std_source)) if len(std_source) >= 3 else 0.045
        consistency_score = round(max(10.0, min(95.0, (1.0 - sigma / 0.08) * 100)), 1)

        if comp_ranks_recent and trial_ranks_recent:
            conversion_delta = float(np.mean(trial_ranks_recent[-5:])) - float(np.mean(comp_ranks_recent[-5:]))
        else:
            conversion_delta = 0.0

        if len(comp_qual_prev4) >= 4:
            comp_qual_recent3 = comp_qual_prev4[-3:]
            earlier = comp_qual_prev4[:-3]
            form_trend = float(np.mean(comp_qual_recent3) - np.mean(earlier))
        else:
            form_trend = 0.0

        career_starts_log = float(math.log1p(total_starts_until_target))
        tier_exp_ratio = float(min(1.0, math.log1p(tier_starts_until_target) / 4.5))

        return {
            "weighted_form_ratio": float(elite_form_ratio),
            "recent_form_ratio": float(recent_form_ratio),
            "weighted_form_quality": float(elite_form_qual),
            "recent_form_quality": float(recent_form_qual),
            "weekend_precomp_quality": float(weekend_precomp_qual),
            "weekend_presence_confidence": float(weekend_max_conf),
            "weekend_vs_form_delta": float(weekend_vs_form_delta),
            "weighted_points": float(equiv_wc_pts),
            "weighted_rank": float(equiv_wc_rank),
            "consistency_sigma": float(sigma),
            "consistency_score": float(consistency_score),
            "conversion_delta": float(conversion_delta),
            "hill_type_affinity": float(htype_affinity_ratio),
            "venue_affinity": float(venue_affinity_ratio),
            "hill_type_affinity_quality": float(htype_affinity_qual),
            "venue_affinity_quality": float(venue_affinity_qual),
            "current_season_activity": float(current_season_activity),
            "form_trend": float(form_trend),
            "career_starts_log": career_starts_log,
            "has_wc_experience": 1.0 if wc_count_until_target > 0 else 0.0,
            "is_tier_debutant": 1.0 if is_wc_debutant else 0.0,
            "tier_experience_ratio": tier_exp_ratio,
            "tier_starts_count": int(tier_starts_until_target),
            "wc_count": int(wc_count_until_target),
            "lower_tier_dominance": lower_tier_dominance,
            "coc_recent_count": int(coc_recent_count),
            "coc_top6_count": int(coc_top6_count),
            "coc_avg_rank": coc_avg_rank,
        }

    def classify_athlete_profiles(self, fis_code: str, stats: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
        """Profile wielowymiarowe dla interfejsu użytkownika."""
        if stats is None:
            stats = self.get_athlete_stats(fis_code)

        profiles = []
        wc_cnt = int(stats.get("wc_count", 0))
        lt_dom = float(stats.get("lower_tier_dominance", 0.0))
        coc_cnt = int(stats.get("coc_recent_count", 0))
        coc_top6 = int(stats.get("coc_top6_count", 0))
        coc_avg_rk = stats.get("coc_avg_rank")
        if wc_cnt <= 5 and coc_cnt >= 2 and (lt_dom >= 0.82 or coc_top6 >= 2):
            avg_txt = f", śr. miejsce {coc_avg_rk}" if coc_avg_rk is not None else ""
            profiles.append({
                "code": "lower_tier_gem",
                "icon": "🌟",
                "label": "Perełka PK",
                "tag_class": "badge-gem",
                "description": (
                    f"Dominator zaplecza w ost. 120 dniach ({coc_top6}x TOP 6 w {coc_cnt} startach PK"
                    f"{avg_txt}, indeks PK {lt_dom * 100:.0f}%) przy małym stażu w PŚ ({wc_cnt} startów)."
                ),
            })
        conv = stats.get("conversion_delta", 0.0)
        c_score = stats.get("consistency_score", 50.0)

        if conv >= 2.5:
            profiles.append({
                "code": "clutch",
                "icon": "🔥",
                "label": "Zwierzę konkursowe",
                "tag_class": "badge-clutch",
                "description": f"Zyskuje średnio +{conv:.1f} pozycji w konkursie w porównaniu do serii próbnych.",
            })
        elif conv <= -3.0:
            profiles.append({
                "code": "training_champ",
                "icon": "📉",
                "label": "Syndrom treningów",
                "tag_class": "badge-choker",
                "description": "W seriach próbnych osiąga wyższe lokaty niż pod presją konkursową.",
            })
        else:
            profiles.append({
                "code": "balanced_pressure",
                "icon": "⚖️",
                "label": "Równy poziom",
                "tag_class": "badge-balanced",
                "description": "Skacze na zbliżonym poziomie w seriach próbnych i konkursowych.",
            })

        if c_score >= 80.0:
            profiles.append({
                "code": "metronome",
                "icon": "🎯",
                "label": "Metronom",
                "tag_class": "badge-metronome",
                "description": f"Wysoka powtarzalność prób ({c_score:.0f}%). Minimalna wariancja odległości.",
            })
        elif c_score <= 45.0:
            profiles.append({
                "code": "volatile",
                "icon": "🎲",
                "label": "Chwiejny",
                "tag_class": "badge-volatile",
                "description": f"Duża wariancja skoków ({c_score:.0f}% stabilności) — potrafi odpalić petardę lub zepsuć skok.",
            })

        return profiles

    def get_athlete_wind_profile(
        self,
        fis_code: str,
        hill_size: float = 130.0,
        hill_type: str = "LARGE",
        stats: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Aero-profil zawodnika (lotnik vs siłowy)."""
        if stats is None:
            stats = self.get_athlete_stats(fis_code, hill_type=hill_type)

        c_score = stats.get("consistency_score", 50.0)
        history = self.athlete_history.get(str(fis_code).strip(), [])
        flying_jumps = [x["raw_ratio"] for x in history if x.get("stype") == "COMPETITION" and (x.get("hill_type") or "").upper() == "FLYING"]
        trad_jumps = [x["raw_ratio"] for x in history if x.get("stype") == "COMPETITION" and (x.get("hill_type") or "").upper() in ("LARGE", "NORMAL")]

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
            "profile_type": profile_type,
        }

    def build_feature_vector_from_stats(
        self,
        stats: Dict[str, Any],
        hill_size: float,
        weather: Dict[str, Any],
    ) -> np.ndarray:
        """Składa 18-elementowy wektor cech na podstawie gotowych statystyk i warunków pogodowych."""
        hs = float(hill_size or 130.0)
        norm_hs = (hs - 130.0) / 40.0
        is_normal = 1.0 if hs < 110 else 0.0
        is_large = 1.0 if 110 <= hs < 185 else 0.0
        is_flying = 1.0 if hs >= 185 else 0.0

        headwind = float(
            weather.get("headwind_component")
            if weather.get("headwind_component") is not None
            else (weather.get("headwind") if weather.get("headwind") is not None else 0.0)
        )
        wind_spd = float(weather.get("wind_speed", 1.5))

        return np.array([
            norm_hs,
            is_normal,
            is_large,
            is_flying,
            stats["weighted_form_quality"],
            stats["recent_form_quality"],
            stats["weekend_precomp_quality"],
            stats["weekend_presence_confidence"],
            stats["weekend_vs_form_delta"],
            stats["current_season_activity"],
            stats["hill_type_affinity_quality"],
            stats["venue_affinity_quality"],
            min(1.0, math.log1p(stats["wc_count"]) / 5.0),
            stats["tier_experience_ratio"],
            stats["consistency_score"] / 100.0,
            stats["conversion_delta"] / 10.0,
            headwind / 3.0,
            wind_spd / 5.0,
        ], dtype=np.float32)

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
        target_time: Optional[str] = None,
        target_race_id: Optional[str] = None,
        target_session_id: Optional[str] = None,
        weekend_overlay: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> np.ndarray:
        """Ekstrahuje 18-elementowy wektor cech (z opcjonalnym żywym overlayem weekendowym)."""
        fc = str(fis_code).strip()
        if weekend_overlay is None and weekend_sessions:
            weekend_overlay = self.build_weekend_overlay(weekend_sessions)

        extra = weekend_overlay.get(fc) if weekend_overlay else None
        ht = (hill_type or "LARGE").upper()
        hs = float(hill_size or 130.0)

        stats = self.get_athlete_stats(
            fc,
            hill_type=ht,
            place=place,
            target_date=target_date,
            target_category=target_category,
            target_time=target_time,
            target_race_id=target_race_id,
            target_session_id=target_session_id,
            extra_entries=extra,
        )
        return self.build_feature_vector_from_stats(stats, hs, weather)

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
        """Deserializacja stanu ekstraktora z JSON (z uzupełnieniem date_ord i place_norm)."""
        self.athlete_history = data.get("athlete_history", {})
        for hist in self.athlete_history.values():
            for entry in hist:
                if "date_ord" not in entry and entry.get("date"):
                    try:
                        entry["date_ord"] = datetime.date.fromisoformat(str(entry["date"])[:10]).toordinal()
                    except Exception:
                        entry["date_ord"] = None
                if "place_norm" not in entry and entry.get("place"):
                    entry["place_norm"] = normalize_place_name(entry["place"])
        self.wc_athletes_fis = set(data.get("wc_athletes_fis", []))
        self.wc_starts = data.get("wc_starts", {})
        self.athlete_base_ratings = data.get("athlete_base_ratings", {})
        self.athlete_category_starts = data.get("athlete_category_starts", {})
