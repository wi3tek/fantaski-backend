# app/services/scrapers/pdf_training_scraper.py
import httpx
import fitz  # PyMuPDF
import io
import traceback
import re
import time
import threading
import unicodedata
import asyncio
import json
import hashlib
from datetime import datetime
from typing import Optional, List, Dict, Any
from pymongo import ReplaceOne, UpdateOne
from app.config import DEFAULT_HEADERS

if hasattr(fitz, "no_recommend_layout") and callable(fitz.no_recommend_layout):
    fitz.no_recommend_layout()


# ==========================================
# FUNKCJE POMOCNICZE: SKOCZNIA I TEKST
# ==========================================
def parse_hill_data_from_pdf_text(text: str) -> dict:
    """Ekstrahuje parametry skoczni (HS, punkt K, rekord, typ) z tekstu pierwszej strony PDF."""
    data = {}

    # 1. HILL SIZE (HS)
    hs_match = re.search(
        r'(?:HILL[\s-]*SIZE(?:\s*\(?HS\)?)?(?:\s*/\s*\(?\s*95%\s*\)?)?|(?<![A-Z])HS)\s*[:]?\s*\n?\s*(\d+(?:\.\d+)?)\s*m',
        text,
        re.IGNORECASE
    )
    if hs_match:
        data['size'] = int(float(hs_match.group(1)))
        data['unit'] = 'HS'

    # 2. PUNKT K (K-POINT)
    k_match = re.search(
        r'K[\s-]*POINT\s*[:]?\s*\n?\s*(\d+(?:\.\d+)?)\s*m?',
        text,
        re.IGNORECASE
    )
    if k_match:
        data['k_point'] = float(k_match.group(1))
        if 'size' not in data:
            data['size'] = int(float(k_match.group(1)))
            data['unit'] = 'K'

    # 3. METER VALUE
    mv_match = re.search(
        r'METER\s*VALUE\s*[:]?\s*\n?\s*(\d+(?:\.\d+)?)\s*(?:points|pts)?',
        text,
        re.IGNORECASE
    )
    if mv_match:
        data['meter_value'] = float(mv_match.group(1))

    # 4. REKORD SKOCZNI I REKORDZISTA
    rec_match = re.search(
        r'(?:HILL\s*RECORD|WC\s*HILL\s*RECORD|GP\s*HILL\s*RECORD)\s*[:]?\s*\n?\s*(\d+(?:\.\d+)?)\s*m',
        text,
        re.IGNORECASE
    )
    if rec_match:
        data['record'] = float(rec_match.group(1))
        rec_idx = rec_match.end()
        post_text = text[rec_idx:rec_idx + 200]
        holder_match = re.search(r'\([^\)]*?\d{4}\s+([A-Z\s\.\'-]+?)\s+[A-Z]{3}', post_text)
        if holder_match:
            data['record_holder'] = holder_match.group(1).strip()

    # 5. TYP SKOCZNI
    eff_size = data.get('size', 0)
    if eff_size >= 185:
        data['type'] = 'FLYING'
    elif eff_size >= 110:
        data['type'] = 'LARGE'
    elif eff_size >= 85:
        data['type'] = 'NORMAL'
    elif eff_size > 0:
        data['type'] = 'SMALL'

    return data


def parse_pdf_content_standard(content: bytes) -> str:
    """Silnik standardowy PyMuPDF get_text('text')."""
    pdf_stream = io.BytesIO(content)
    with fitz.open(stream=pdf_stream, filetype="pdf") as pdf_doc:
        return "\n".join([page.get_text("text") for page in pdf_doc])


def parse_pdf_content_optical(content: bytes) -> str:
    """Silnik optyczny sortujący słowa po współrzędnych pionowych."""
    pdf_stream = io.BytesIO(content)
    full_text = ""
    with fitz.open(stream=pdf_stream, filetype="pdf") as pdf_doc:
        for page in pdf_doc:
            words = page.get_text("words")
            words_sorted = sorted(words, key=lambda w: (round(w[1] / 5) * 5, w[0]))
            current_line_y = None
            line_words = []
            for w in words_sorted:
                y = round(w[1] / 5) * 5
                if current_line_y is None:
                    current_line_y = y
                if y != current_line_y:
                    full_text += " ".join(line_words) + "\n"
                    line_words = [w[4]]
                    current_line_y = y
                else:
                    line_words.append(w[4])
            if line_words:
                full_text += " ".join(line_words) + "\n"
    return full_text


def parse_round_cell(cell_text: str) -> dict:
    """
    Parsuje komórkę pojedynczej serii treningowej / próbnej.
    Zwraca słownik:
    {'distance': float or None, 'points': float or None, 'speed': float or None, 'status': str or None}
    """
    if not cell_text:
        return {'distance': None, 'points': None, 'speed': None, 'status': None}

    text = cell_text.strip()
    for st in ["DNS", "DSQ", "NPS", "SCR"]:
        if st in text:
            return {'distance': None, 'points': None, 'speed': None, 'status': st}

    lines = [l.strip() for l in text.split('\n') if l.strip()]
    first_line = lines[0] if lines else ""

    tokens = first_line.split()
    floats = []
    for t in tokens:
        clean_t = t.replace('=', '').replace('m', '').replace('©', '').replace('km/h', '')
        if '→' in clean_t:
            continue
        try:
            val = float(clean_t)
            floats.append(val)
        except ValueError:
            pass

    speed = None
    dist = None
    pts = None

    if len(floats) >= 4:
        # Typowy format FIS: [speed, distance, gate, wind, points]
        # lub [speed, distance, wind, points]
        speed = floats[0]
        dist = floats[1]
        pts = floats[-1]
    elif len(floats) == 3:
        speed = floats[0]
        dist = floats[1]
        pts = floats[2]
    elif len(floats) == 2:
        if floats[0] >= 70.0 and floats[1] >= 30.0:
            speed = floats[0]
            dist = floats[1]
        else:
            dist = floats[0]
            pts = floats[1]
    elif len(floats) == 1:
        dist = floats[0]

    # Zabezpieczenie fizyczne: odległość skoku musi być ściśle dodatnia i w granicach 30m - 260m
    if dist is not None:
        if dist < 30.0 or dist > 260.0:
            dist = None
    if speed is not None:
        if speed < 60.0 or speed > 120.0:
            speed = None

    return {
        'distance': dist,
        'points': pts,
        'speed': speed,
        'status': None
    }


_TRANSLIT_MAP = str.maketrans({
    "Ł": "L", "ł": "L",
    "Ø": "O", "ø": "O",
    "Æ": "AE", "æ": "AE",
    "Œ": "OE", "œ": "OE",
    "ß": "SS", "Ð": "D", "ð": "D", "Þ": "TH", "þ": "TH",
})


def normalize_athlete_name(name: str) -> str:
    """Normalizuje nazwisko skoczka do porównań (transliteracja znaków diakrytycznych + wielkie litery A-Z)."""
    if not name:
        return ""
    text = name.translate(_TRANSLIT_MAP)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    clean = re.sub(r'[^A-Z\s]', ' ', text.upper())
    return re.sub(r'\s+', ' ', clean).strip()


def match_athlete_identity(name: str, country: str, session_athletes: list, global_athletes_map: dict) -> dict:
    """Dopasowuje skoczka z PDF do bazy danych w celu uzyskania fis_code i ujednolicenia danych."""
    norm_name = normalize_athlete_name(name)
    name_tokens = frozenset(norm_name.split())
    country_up = (country or "").strip().upper()

    # 1. Sprawdź zawodników z sesji nadrzędnej / tego samego raceId (kwalifikacje/zawody weekendu)
    if session_athletes:
        for a in session_athletes:
            s_name = normalize_athlete_name(a.get("name", ""))
            s_country = (a.get("country", "") or "").strip().upper()
            s_tokens = frozenset(s_name.split())

            if (s_tokens and s_tokens == name_tokens) or (norm_name and norm_name == s_name):
                if not country_up or not s_country or country_up == s_country:
                    fc_val = str(a.get("fis_code") or "").strip()
                    if fc_val:
                        return {
                            "fis_code": fc_val,
                            "name": a.get("name", name),
                            "country": s_country or country_up,
                            "gender": a.get("gender", "M")
                        }

    # 2. Sprawdź globalną mapę skoczków z kolekcji athletes (dokładnie oraz przez zbiór tokenów)
    if global_athletes_map and norm_name:
        lookup_keys = []
        if country_up:
            lookup_keys.append((norm_name, country_up))
            if len(name_tokens) >= 2:
                lookup_keys.append((name_tokens, country_up))
        lookup_keys.append(norm_name)
        if len(name_tokens) >= 2:
            lookup_keys.append(name_tokens)

        for key in lookup_keys:
            if key in global_athletes_map:
                a_info = global_athletes_map[key]
                fc_val = str(a_info.get("fis_code") or "").strip()
                if fc_val:
                    return {
                        "fis_code": fc_val,
                        "name": a_info.get("name", name),
                        "country": a_info.get("country", country_up),
                        "gender": a_info.get("gender", "M")
                    }

    return {
        "fis_code": None,
        "name": name,
        "country": country_up,
        "gender": "M"
    }


def assign_training_ranks_and_points(athletes_list: list) -> list:
    """
    Oblicza klasyfikację skoków treningowych / serii próbnych:
    - Nadaje miejsca (rank) w każdej pojedynczej serii treningowej (np. T1, T2, T3) wg noty (a przy jej braku wg odległości).
    - Sumuje punkty (noty za odległość i rekompensaty) do total_points.
    - Nadaje łączną pozycję (rank) w sesji oraz sortuje listę od 1. miejsca.
    """
    if not athletes_list:
        return athletes_list

    max_rounds = max((len(a.get("jumps") or []) for a in athletes_list), default=0)

    # 1. Ranking w ramach każdej pojedynczej serii treningowej
    for r_idx in range(max_rounds):
        contenders = []
        for idx, ath in enumerate(athletes_list):
            jumps = ath.get("jumps") or []
            if r_idx < len(jumps):
                j = jumps[r_idx]
                dist = j.get("distance")
                pts = j.get("points")
                if dist is not None and dist > 0:
                    sort_pts = float(pts) if pts is not None else -9999.0
                    contenders.append((idx, sort_pts, float(dist)))

        contenders.sort(key=lambda x: (x[1], x[2]), reverse=True)
        prev_score = None
        curr_rank = 0
        for pos, (ath_idx, s_pts, s_dist) in enumerate(contenders, start=1):
            score_key = (s_pts, s_dist)
            if score_key != prev_score:
                curr_rank = pos
                prev_score = score_key
            athletes_list[ath_idx]["jumps"][r_idx]["rank"] = curr_rank

    # 2. Sumaryczna nota, status i ranking ogólny sesji
    classified = []
    unclassified = []
    for ath in athletes_list:
        jumps = ath.get("jumps") or []
        valid_pts = [float(j["points"]) for j in jumps if j.get("points") is not None]
        valid_dists = [float(j["distance"]) for j in jumps if j.get("distance") is not None and float(j["distance"]) > 0]

        total_pts = round(sum(valid_pts), 1) if valid_pts else None
        max_dist = max(valid_dists) if valid_dists else None

        ath["total_points"] = total_pts
        ath["competition_points"] = 0

        if valid_dists:
            ath["status"] = "Classified"
            classified.append((ath, total_pts if total_pts is not None else -9999.0, max_dist or 0.0))
        else:
            st = next((j.get("status") for j in jumps if j.get("status")), None) or "DNS"
            ath["status"] = st
            ath["rank"] = None
            unclassified.append(ath)

    classified.sort(key=lambda x: (x[1], x[2]), reverse=True)
    prev_total = None
    curr_rank = 0
    sorted_classified = []
    for pos, (ath, t_pts, m_dist) in enumerate(classified, start=1):
        score_key = (t_pts, m_dist)
        if score_key != prev_total:
            curr_rank = pos
            prev_total = score_key
        ath["rank"] = curr_rank
        sorted_classified.append(ath)

    return sorted_classified + unclassified


async def build_global_anchors_and_athletes(db) -> tuple[list, dict]:
    """Pobiera skoczków z bazy opierając się na kolekcji athletes."""
    athletes_cursor = db["athletes"].find({}, {"full_name": 1, "fis_code": 1, "country_code": 1, "gender": 1})
    global_known_names = set()
    athletes_map = {}
    async for a in athletes_cursor:
        name = a.get("full_name")
        if name:
            clean_name = re.sub(r'\s+', ' ', name.strip().upper())
            global_known_names.add(clean_name)
            norm = normalize_athlete_name(name)
            tokens = frozenset(norm.split())
            country = (a.get("country_code", "") or "").strip().upper()
            info = {
                "fis_code": a.get("fis_code"),
                "name": name.strip(),
                "country": country,
                "gender": a.get("gender", "M")
            }
            if country:
                athletes_map[(norm, country)] = info
                if len(tokens) >= 2:
                    athletes_map[(tokens, country)] = info
            if norm not in athletes_map:
                athletes_map[norm] = info
            if len(tokens) >= 2 and tokens not in athletes_map:
                athletes_map[tokens] = info

    sorted_anchors = sorted(list(global_known_names), key=len, reverse=True)
    return sorted_anchors, athletes_map


# ==========================================
# TRZYPOZIOMOWY SILNIK PARSERA PDF
# ==========================================
def extract_athletes_via_table_engine(doc: fitz.Document, round_names: list) -> list:
    """
    SILNIK 1: Ekstrakcja tabelaryczna za pomocą PyMuPDF find_tables().
    Najbardziej precyzyjny silnik dla oficjalnych plików FIS (Swiss Timing).
    Gwarantuje:
    - 0 błędnych ujemnych odległości przy DNS
    - 0 osób z Jury (Mathias Hafele) w wynikach
    - Precyzyjny podział na serię 1, 2, 3
    """
    athletes_parsed = []

    for page_idx, page in enumerate(doc):
        tabs = page.find_tables()
        if not tabs.tables:
            continue

        for t in tabs.tables:
            raw_rows = t.extract()
            if not raw_rows or len(raw_rows) < 2:
                continue

            # Sprawdź czy to tabela z wynikami zawodników
            header_text = " ".join([str(c) for r in raw_rows[:3] for c in r if c])
            is_athlete_table = any(k in header_text.upper() for k in ["BIB", "NAME", "NSA"])
            
            # Dla starszych formatów (np. 2010), gdzie nagłówek był poza tabelą
            if not is_athlete_table and len(raw_rows) > 5:
                # Sprawdź czy komórki pierwszych wierszy wyglądają jak dane zawodnika
                sample_cell = str(raw_rows[0][0]) if raw_rows[0] and raw_rows[0][0] else ""
                if re.search(r'\b[A-Z]{3}\b', sample_cell) and any(l.strip().isdigit() for l in sample_cell.split('\n')):
                    is_athlete_table = True

            if not is_athlete_table:
                continue

            # Ustal indeksy kolumn poszczególnych serii
            round_cols = []
            row0 = raw_rows[0]
            for col_idx, col_val in enumerate(row0):
                if col_val:
                    c_up = str(col_val).upper()
                    if "1ST" in c_up or "ROUND 1" in c_up or "TRIAL" in c_up:
                        round_cols.append((1, col_idx))
                    elif "2ND" in c_up or "ROUND 2" in c_up:
                        round_cols.append((2, col_idx))
                    elif "3RD" in c_up or "ROUND 3" in c_up:
                        round_cols.append((3, col_idx))

            if not round_cols and len(raw_rows) > 1:
                row1 = raw_rows[1]
                for col_idx, col_val in enumerate(row1):
                    if col_val:
                        c_up = str(col_val).upper()
                        if "SPEED" in c_up or "DIST" in c_up:
                            round_cols.append((len(round_cols) + 1, col_idx))

            if not round_cols:
                round_cols = [(1, 1)]

            # Parsuj wiersze zawodników
            for r in raw_rows:
                col0 = str(r[0]).strip() if r[0] is not None else ""

                # Format A: r[0] zawiera numer BIB (np. '1', '47')
                if col0.isdigit():
                    bib = int(col0)
                    name_cell = str(r[1]).strip() if len(r) > 1 and r[1] is not None else ""
                    name_lines = [l.strip() for l in name_cell.split('\n') if l.strip()]
                    full_name_raw = name_lines[0] if name_lines else ""

                    nsa_match = re.search(r'(?:^|\s)([A-Z]{3})\s*$', full_name_raw)
                    nsa = nsa_match.group(1) if nsa_match else ""
                    clean_name = full_name_raw[:nsa_match.start()].strip() if nsa_match else full_name_raw.strip()

                    jumps = []
                    for r_num, c_idx in round_cols:
                        c_val = str(r[c_idx]) if c_idx < len(r) and r[c_idx] is not None else ""
                        jump_data = parse_round_cell(c_val)
                        r_name = round_names[r_num - 1] if r_num - 1 < len(round_names) else f"T{r_num}"
                        jump_data['round'] = r_name
                        jumps.append(jump_data)

                    athletes_parsed.append({
                        'bib': bib,
                        'name': clean_name,
                        'country': nsa,
                        'jumps': jumps,
                        'raw_text': f"{bib} {clean_name} {nsa}"
                    })

                # Format B (starszy COC): r[0] łączy '1\nLOGINOV Aleksandr RUS\n...'
                elif '\n' in col0:
                    lines0 = [l.strip() for l in col0.split('\n') if l.strip()]
                    b_int = None
                    n_str = None
                    c_str = ""
                    for l in lines0:
                        if l.isdigit() and b_int is None:
                            b_int = int(l)
                        elif not b_int and re.match(r'^\d+\s+[A-Za-z]', l):
                            parts = l.split(maxsplit=1)
                            b_int = int(parts[0])
                            n_str = parts[1]
                        elif any(c.isupper() for c in l) and not n_str and not any(k in l.upper() for k in ["JURY", "JUDGES", "SKICLUB", "DATE"]):
                            n_str = l

                    if b_int and n_str:
                        nsa_m = re.search(r'(?:^|\s)([A-Z]{3})\s*$', n_str)
                        if nsa_m:
                            c_str = nsa_m.group(1)
                            n_str = n_str[:nsa_m.start()].strip()

                        col1 = str(r[1]).strip() if len(r) > 1 and r[1] is not None else ""
                        jump_data = parse_round_cell(col1)
                        jump_data['round'] = round_names[0] if round_names else "T1"
                        athletes_parsed.append({
                            'bib': b_int,
                            'name': n_str,
                            'country': c_str,
                            'jumps': [jump_data],
                            'raw_text': f"{b_int} {n_str} {c_str}"
                        })

    return athletes_parsed


def extract_athletes_via_2column_engine(doc: fitz.Document, round_names: list) -> list:
    """
    SILNIK 2: Ekstrakcja z dokumentów dwukolumnowych (np. Trial Round format _73C1).
    Dzieli stronę na kolumnę lewą (x < 300) i prawą (x >= 300) i rekonstruuje wiersze.
    """
    athletes_parsed = []
    ATHLETE_ROW_RE = re.compile(
        r'^\s*(\d{1,3})\s+([A-Z\xc0-\xdf][A-Za-z\xc0-\xff\.\'\-\s]+?)\s+([A-Z]{3})\b(?:\s+(.*))?$'
    )

    for page_idx, page in enumerate(doc):
        words = page.get_text("words")
        for col_name, col_filter in [("LEFT", lambda w: w[0] < 300), ("RIGHT", lambda w: w[0] >= 300)]:
            col_words = [w for w in words if col_filter(w) and 200 <= w[1] <= 700]
            tolerance = 3.5
            sorted_words = sorted(col_words, key=lambda w: (w[1], w[0]))
            lines = []
            curr = []
            curr_y = None
            for w in sorted_words:
                if curr_y is None:
                    curr_y = w[1]
                    curr.append(w)
                elif abs(w[1] - curr_y) <= tolerance:
                    curr.append(w)
                else:
                    curr.sort(key=lambda w: w[0])
                    lines.append(curr)
                    curr = [w]
                    curr_y = w[1]
            if curr:
                curr.sort(key=lambda w: w[0])
                lines.append(curr)

            for l in lines:
                l_str = " ".join([w[4] for w in l])
                if any(k in l_str for k in ["Jury", "Judges", "Competition Management", "Legend", "Weather", "Data Service by Swiss Timing"]):
                    break
                m = ATHLETE_ROW_RE.match(l_str)
                if m:
                    bib = int(m.group(1))
                    name = m.group(2).strip()
                    nsa = m.group(3).strip()
                    rest = m.group(4) or ""
                    jump_data = parse_round_cell(rest)
                    jump_data['round'] = round_names[0] if round_names else "T1"
                    athletes_parsed.append({
                        'bib': bib,
                        'name': name,
                        'country': nsa,
                        'jumps': [jump_data],
                        'raw_text': l_str
                    })

    return athletes_parsed


def extract_athletes_via_fallback_anchors(full_text: str, num_rounds: int, round_names: list, sorted_anchors: list, anchor_set: Optional[set] = None) -> list:
    """
    SILNIK 3 (Ratunkowy): Klasyczny parser z kotwicami, z restrykcyjnym odcięciem
    sekcji Jury/Weather i szybkim wyszukiwaniem prefiksów w O(1) zamiast O(5000) na linię.
    """
    # 1. Bezwzględnie odetnij sekcję Jury i dane pogodowe
    cutoff_match = re.search(r'(?:Competition Management\s*/\s*Jury|Jury\s*/\s*Competition Management|Judges\s*\n|Competition\s*/\s*Weather Information)', full_text, re.IGNORECASE)
    cleaned_text = full_text[:cutoff_match.start()] if cutoff_match else full_text

    lines = [line.strip() for line in cleaned_text.split('\n') if line.strip()]
    if anchor_set is None:
        anchor_set = set(sorted_anchors)

    start_indices = []
    for i, line in enumerate(lines):
        line_to_check = re.sub(r'\s+', ' ', line.upper())
        line_to_check = re.sub(r'^\d+\.?\s*', '', line_to_check).strip()
        if not line_to_check:
            continue
        if line_to_check in anchor_set:
            start_indices.append((i, line_to_check))
            continue
        tokens = line_to_check.split()
        max_k = min(5, len(tokens))
        for k in range(max_k, 1, -1):
            candidate = " ".join(tokens[:k])
            if candidate in anchor_set:
                start_indices.append((i, candidate))
                break

    start_indices = sorted(start_indices, key=lambda x: x[0])
    athletes_parsed = []

    for idx, (line_idx, athlete_name) in enumerate(start_indices):
        end_idx = start_indices[idx + 1][0] if idx + 1 < len(start_indices) else len(lines)
        chunk = lines[line_idx: end_idx]
        raw_text_block = " | ".join(chunk)

        temp_floats = []
        temp_statuses = []

        for line in chunk:
            tokens = line.split()
            for c in tokens:
                c_clean = c.replace("=", "").replace("m", "").strip()
                if c_clean in ["DNS", "DSQ", "NPS", "SCR"]:
                    temp_statuses.append(c_clean)
                elif re.match(r'^-?\d+\.\d+$', c_clean) and not c.strip().endswith('.'):
                    val = float(c_clean)
                    temp_floats.append(val)

        athlete_jumps = []
        for r in range(num_rounds):
            base_idx = r * 4
            dist = None
            pts = None
            status = None

            if len(temp_floats) >= base_idx + 2:
                candidate_dist = temp_floats[base_idx + 1]
                if 30.0 <= candidate_dist <= 260.0:
                    dist = candidate_dist
                    if len(temp_floats) >= base_idx + 4:
                        pts = temp_floats[base_idx + 3]
                    elif len(temp_floats) > base_idx + 2:
                        pts = temp_floats[base_idx + 2]
                else:
                    status = temp_statuses[r] if r < len(temp_statuses) else (temp_statuses[0] if temp_statuses else "DNS")
            else:
                status = temp_statuses[r] if r < len(temp_statuses) else (temp_statuses[0] if temp_statuses else "DNS")

            r_name = round_names[r] if r < len(round_names) else f"T{r + 1}"
            athlete_jumps.append({
                "round": r_name,
                "distance": dist,
                "points": pts,
                "status": status
            })

        athletes_parsed.append({
            "bib": None,
            "name": athlete_name,
            "country": "",
            "jumps": athlete_jumps,
            "raw_text": raw_text_block
        })

    return athletes_parsed


# ==========================================
# SYNCHRONICZNE PARSOWANIE PDF (WĄTEK ROBOCZY)
# ==========================================
def _parse_pdf_bytes_sync(
    pdf_bytes: bytes,
    is_trial: bool,
    file_idx: str,
    fallback_date: str,
    sorted_anchors: List[str],
    anchor_set: Optional[set] = None,
) -> Dict[str, Any]:
    """
    Synchroniczne (CPU-bound) parsowanie bajtów PDF przez PyMuPDF.
    Wykonywane przez asyncio.to_thread(), aby nie blokować pętli zdarzeń FastAPI.
    """
    engine_logs: List[str] = []
    pdf_stream = io.BytesIO(pdf_bytes)
    with fitz.open(stream=pdf_stream, filetype="pdf") as pdf_doc:
        full_text = "\n".join([page.get_text("text") for page in pdf_doc])
        hill_data = parse_hill_data_from_pdf_text(full_text)

        start_time = "12:00"
        time_match = re.search(r"Start Time:\s*(\d{1,2}:\d{2})", full_text)
        if time_match:
            start_time = time_match.group(1)

        date_str = fallback_date
        date_match = re.search(r"(?:SUN|MON|TUE|WED|THU|FRI|SAT)\s+(\d{1,2}\s+[A-Z]{3}\s+\d{4})", full_text)
        if date_match:
            try:
                parsed_date = datetime.strptime(date_match.group(1), "%d %b %Y")
                date_str = parsed_date.strftime("%Y-%m-%d")
            except Exception:
                pass

        num_rounds = 3 if "3RD ROUND" in full_text else (2 if "2ND ROUND" in full_text else 1)
        if is_trial:
            round_names = ["Trial"]
        elif file_idx:
            round_names = [f"T{file_idx}"] if num_rounds == 1 else [f"T{file_idx}_{i + 1}" for i in range(num_rounds)]
        else:
            round_names = [f"T{i + 1}" for i in range(num_rounds)]

        athletes_results: List[Dict[str, Any]] = []
        used_engine = ""

        # Silnik 1: Tabela (najwyższa dokładność)
        engine_logs.append("-> Uruchamiam Silnik 1: PyMuPDF find_tables()...")
        athletes_results = extract_athletes_via_table_engine(pdf_doc, round_names)
        if len(athletes_results) >= 10:
            used_engine = "TABLE_ENGINE"
            engine_logs.append(f"✅ Sukces Silnika 1! Znaleziono {len(athletes_results)} zawodników.")

        # Silnik 2: Dwukolumnowy (Trial _73C1)
        if not athletes_results or len(athletes_results) < 10:
            engine_logs.append("-> Silnik 1 nie znalazł wystarczającej liczby zawodników. Uruchamiam Silnik 2: 2-kolumnowy...")
            col_results = extract_athletes_via_2column_engine(pdf_doc, round_names)
            if len(col_results) >= 10:
                athletes_results = col_results
                used_engine = "2COLUMN_ENGINE"
                engine_logs.append(f"✅ Sukces Silnika 2! Znaleziono {len(athletes_results)} zawodników.")

        # Silnik 3: Kotwice tekstowe (awaryjny)
        if not athletes_results or len(athletes_results) < 5:
            engine_logs.append("-> Uruchamiam Silnik 3: Kotwice tekstowe z odcięciem Jury...")
            fallback_results = extract_athletes_via_fallback_anchors(
                full_text,
                num_rounds,
                round_names,
                sorted_anchors,
                anchor_set=anchor_set,
            )
            if fallback_results:
                athletes_results = fallback_results
                used_engine = "ANCHOR_FALLBACK"
                engine_logs.append(f"✅ Sukces Silnika 3! Znaleziono {len(athletes_results)} zawodników.")

    return {
        "full_text": full_text,
        "hill_data": hill_data,
        "start_time": start_time,
        "date_str": date_str,
        "num_rounds": num_rounds,
        "round_names": round_names,
        "athletes_results": athletes_results,
        "used_engine": used_engine,
        "engine_logs": engine_logs,
    }


# ==========================================
# MENEDŻER ZADAŃ W TLE (EKSTRAKCJA PDF)
# ==========================================
class PDFTrainingJobManager:
    """Zarządza asynchroniczną ekstrakcją plików PDF treningów z raportowaniem postępu i bezpiecznym zatrzymywaniem."""

    def __init__(self) -> None:
        self.is_running: bool = False
        self.stop_requested: bool = False
        self.total_sessions: int = 0
        self.total_files: int = 0
        self.files_done: int = 0
        self.saved_to_review: int = 0
        self.unchanged_files: int = 0
        self.empty_files: int = 0
        self.failed_files: int = 0
        self.current_item: Optional[str] = None
        self.last_message: Optional[str] = None
        self.started_at: Optional[float] = None
        self.finished_elapsed_seconds: int = 0
        self.distinct_doc_types_in_batch: List[str] = []
        self.errors: List[Dict[str, Any]] = []
        self.last_result: Optional[Dict[str, Any]] = None
        self._rate_lock: Optional[asyncio.Lock] = None
        self._last_request_ts: float = 0.0

    def mark_started(self) -> None:
        self.is_running = True
        self.stop_requested = False
        self.total_sessions = 0
        self.total_files = 0
        self.files_done = 0
        self.saved_to_review = 0
        self.unchanged_files = 0
        self.empty_files = 0
        self.failed_files = 0
        self.current_item = "Inicjalizacja bazy skoczków i wyszukiwanie plików PDF..."
        self.last_message = None
        self.started_at = time.time()
        self.finished_elapsed_seconds = 0
        self.distinct_doc_types_in_batch = []
        self.errors = []
        self.last_result = None
        self._rate_lock = asyncio.Lock()
        self._last_request_ts = 0.0

    def stop(self) -> None:
        if self.is_running:
            self.stop_requested = True
            self.last_message = "Zatrzymywanie po zakończeniu bieżących pobrań..."

    def get_status(self) -> Dict[str, Any]:
        if self.is_running and self.started_at is not None:
            elapsed = max(0, int(time.time() - self.started_at))
        else:
            elapsed = self.finished_elapsed_seconds

        if self.total_files > 0:
            pct = round((self.files_done / self.total_files) * 100.0, 1)
        elif not self.is_running and self.last_result is not None:
            pct = 100.0
        else:
            pct = 0.0

        return {
            "is_running": self.is_running,
            "stop_requested": self.stop_requested,
            "total_sessions": self.total_sessions,
            "total_files": self.total_files,
            "files_done": self.files_done,
            "saved_to_review": self.saved_to_review,
            "unchanged_files": self.unchanged_files,
            "empty_files": self.empty_files,
            "failed_files": self.failed_files,
            "percent_complete": min(100.0, pct),
            "current_item": self.current_item,
            "last_message": self.last_message,
            "elapsed_seconds": elapsed,
            "distinct_doc_types_in_batch": self.distinct_doc_types_in_batch,
            "errors": self.errors[-50:],
            "last_result": self.last_result,
        }

    async def _polite_wait_turn(self, min_interval: float = 0.55) -> None:
        """Gwarantuje kulturalny odstęp między żądaniami HTTP do fis-ski.com."""
        if self._rate_lock is None:
            self._rate_lock = asyncio.Lock()
        async with self._rate_lock:
            now = time.monotonic()
            wait_for = min_interval - (now - self._last_request_ts)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request_ts = time.monotonic()

    async def _download_pdf_polite(self, client: httpx.AsyncClient, pdf_url: str) -> httpx.Response:
        for attempt in range(4):
            if self.stop_requested:
                raise asyncio.CancelledError("Zatrzymano przez administratora.")
            try:
                await self._polite_wait_turn(0.55)
                response = await client.get(pdf_url)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as http_err:
                if attempt < 3:
                    if http_err.response.status_code == 429:
                        print(f"⚠️ [PDF 429] Limit zapytań FIS przy pobieraniu {pdf_url}. Czekam 4.5s przed próbą {attempt + 1}/3...")
                        await asyncio.sleep(4.5)
                        continue
                    elif http_err.response.status_code in [500, 502, 503, 504]:
                        await asyncio.sleep(2.0)
                        continue
                raise http_err
            except httpx.RequestError as req_err:
                if attempt < 3 and not self.stop_requested:
                    await asyncio.sleep(2.5)
                    continue
                raise req_err
        raise RuntimeError("Nieznany błąd pobierania PDF")

    async def run_job(
        self,
        db,
        season: str = None,
        season_from: int = None,
        season_to: int = None,
        categories: list = None,
        force_reprocess: bool = False,
    ) -> Dict[str, Any]:
        if not self.is_running:
            self.mark_started()

        try:
            print("▶ Inicjalizacja: Pobieranie pełnej bazy skoczków do zakotwiczenia parsera...")
            sorted_anchors, global_athletes_map = await build_global_anchors_and_athletes(db)
            anchor_set = set(sorted_anchors)
            print(f"▶ Sukces: Załadowano {len(sorted_anchors)} wzorców skoczków z głównej bazy.")

            if len(sorted_anchors) == 0:
                msg = "BŁĄD: Załadowano 0 skoczków. Sprawdź czy kolekcja 'athletes' zawiera pole 'full_name'."
                self.last_message = msg
                self.last_result = {"message": msg}
                return self.last_result

            query = {
                "_id": {"$not": {"$regex": "_(TRAINING|TRIAL)", "$options": "i"}},
                "documents.type": {"$regex": "Results.*Training|Training.*Results|Results.*Trial|Official.*Training", "$options": "i"},
            }
            if not force_reprocess:
                query["trainings_processed"] = {"$ne": True}

            if season_from is not None and season_to is not None:
                lo, hi = min(season_from, season_to), max(season_from, season_to)
                query["season"] = {"$in": [str(y) for y in range(lo, hi + 1)]}
            elif season:
                query["season"] = str(season)

            if categories and len(categories) > 0:
                query["category_code"] = {"$in": [str(c).upper().strip() for c in categories]}

            sessions_cursor = db["sessions"].find(query).limit(2000)
            sessions = await sessions_cursor.to_list(None)
            if not sessions:
                msg = "Brak nowych sesji treningowych do przetworzenia dla wybranych filtrów."
                self.last_message = msg
                self.last_result = {
                    "message": msg,
                    "processed_sessions_in_batch": 0,
                    "saved_to_review": 0,
                    "unchanged_files": 0,
                    "failed_files": 0,
                    "distinct_doc_types_in_batch": [],
                    "report": [],
                    "errors": [],
                    "marked_as_processed": 0,
                }
                return self.last_result

            # Zbuduj mapę zawodników z innych sesji tych samych raceId (na wypadek gdy sesja nadrzędna jest pusta)
            race_ids = list({str(s.get("raceId")) for s in sessions if s.get("raceId")})
            sibling_athletes_by_race: dict = {}
            if race_ids:
                sib_cursor = db["sessions"].find(
                    {
                        "raceId": {"$in": race_ids},
                        "_id": {"$not": {"$regex": "_(TRAINING|TRIAL)", "$options": "i"}},
                        "results_data.results": {"$exists": True, "$ne": []},
                    },
                    {"raceId": 1, "results_data.results": 1},
                )
                async for sib in sib_cursor:
                    rid = str(sib.get("raceId"))
                    res_list = sib.get("results_data", {}).get("results", [])
                    if rid and res_list:
                        sibling_athletes_by_race.setdefault(rid, []).extend(res_list)

            distinct_doc_types = set()
            sessions_to_process = []
            parent_expected_counts: Dict[Any, int] = {}

            for session in sessions:
                pid = session.get("_id")
                for doc in session.get("documents", []):
                    doc_type = doc.get("type", "")
                    if doc_type:
                        distinct_doc_types.add(doc_type)
                    doc_type_lower = doc_type.lower()
                    is_training_or_trial = (
                        ("training" in doc_type_lower or "trial" in doc_type_lower)
                        and "start list" not in doc_type_lower
                    )
                    if is_training_or_trial and doc.get("url"):
                        sessions_to_process.append((session, doc))
                        if pid:
                            parent_expected_counts[pid] = parent_expected_counts.get(pid, 0) + 1

            # Sesje, które pasowały do zapytania, ale np. miały tylko Start Listę bez wyników PDF -> oznacz jako przetworzone
            parent_ops_initial = [
                UpdateOne({"_id": s["_id"]}, {"$set": {"trainings_processed": True}})
                for s in sessions
                if s.get("_id") and s["_id"] not in parent_expected_counts
            ]
            if parent_ops_initial:
                await db["sessions"].bulk_write(parent_ops_initial)

            self.total_sessions = len(sessions)
            self.total_files = len(sessions_to_process)
            self.distinct_doc_types_in_batch = sorted(list(distinct_doc_types))

            if self.total_files == 0:
                msg = f"Sprawdzono {len(sessions)} sesji — brak wyników PDF do pobrania (tylko listy startowe)."
                self.last_message = msg
                self.last_result = {
                    "message": msg,
                    "processed_sessions_in_batch": len(sessions),
                    "saved_to_review": 0,
                    "unchanged_files": 0,
                    "failed_files": 0,
                    "distinct_doc_types_in_batch": self.distinct_doc_types_in_batch,
                    "report": [],
                    "errors": [],
                    "marked_as_processed": len(parent_ops_initial),
                }
                return self.last_result

            print(f"⏳ Rozpoczynam kulturalne pobieranie i analizę {self.total_files} plików PDF z {len(sessions)} sesji (Semaphore=2, wątek roboczy PyMuPDF)...")

            semaphore = asyncio.Semaphore(2)
            parent_completed_counts: Dict[Any, int] = {}
            parent_failed_errors: Dict[Any, str] = {}
            total_marked_parents = len(parent_ops_initial)
            parsed_reports: List[Dict[str, Any]] = []

            async def process_single_pdf(session, doc, client):
                parent_id = session.get("_id")
                doc_type = doc.get("type", "")
                pdf_url = doc.get("url")
                if pdf_url and "/competition-attachment/" in pdf_url and not pdf_url.lower().endswith(".pdf"):
                    pdf_url = f"{pdf_url}.pdf"
                race_id = session.get("raceId", "UNKNOWN")
                codex = session.get("codex", "UNKNOWN")
                place_label = session.get("event_place") or str(race_id)

                if self.stop_requested:
                    return {"stopped": True, "parent_id": parent_id}

                async with semaphore:
                    if self.stop_requested:
                        return {"stopped": True, "parent_id": parent_id}

                    is_trial = "Trial" in doc_type
                    session_type_str = "TRIAL" if is_trial else "TRAINING"
                    base_suffix = "TRIAL" if is_trial else "TRAINING"
                    num_match = re.search(r'\((\d+)\)', doc_type)
                    file_idx = num_match.group(1) if num_match else ""
                    suffix = f"{base_suffix}_{file_idx}" if file_idx else base_suffix
                    new_session_id = f"{race_id}_{codex}_{suffix}"

                    self.current_item = f"{place_label} • {doc_type} (Codex {codex})"

                    try:
                        response = await self._download_pdf_polite(client, pdf_url)

                        if b"%PDF" not in response.content[:10]:
                            return {"success": False, "ignored": True, "reason": "EMPTY", "parent_id": parent_id, "label": place_label}

                        # Parsowanie PyMuPDF w osobnym wątku, aby nie zamrażać pętli FastAPI
                        parsed = await asyncio.to_thread(
                            _parse_pdf_bytes_sync,
                            response.content,
                            is_trial,
                            file_idx,
                            session.get("date", "9999-99-99"),
                            sorted_anchors,
                            anchor_set,
                        )

                        athletes_results = parsed["athletes_results"]
                        used_engine = parsed["used_engine"]
                        hill_data = parsed["hill_data"]
                        start_time = parsed["start_time"]
                        date_str = parsed["date_str"]

                        if athletes_results:
                            parent_athletes = (
                                session.get("results_data", {}).get("results", [])
                                or sibling_athletes_by_race.get(str(race_id), [])
                            )
                            enriched_athletes = []
                            for a in athletes_results:
                                meta = match_athlete_identity(
                                    name=a.get("name", ""),
                                    country=a.get("country", ""),
                                    session_athletes=parent_athletes,
                                    global_athletes_map=global_athletes_map,
                                )
                                enriched_athletes.append({
                                    "bib": a.get("bib"),
                                    "name": meta["name"],
                                    "country": meta["country"],
                                    "fis_code": meta["fis_code"],
                                    "gender": meta["gender"],
                                    "jumps": a.get("jumps", []),
                                    "raw_text": a.get("raw_text", ""),
                                })

                            enriched_athletes = assign_training_ranks_and_points(enriched_athletes)

                            effective_hs = session.get("hillSize") or hill_data.get("size")
                            effective_unit = session.get("hill_size_unit") or hill_data.get("unit") or "HS"
                            effective_type = session.get("hillType") or hill_data.get("type")
                            k_point = hill_data.get("k_point")

                            session_title = f"{doc_type} - {session.get('event_place', '')}"
                            new_session_doc = {
                                "_id": new_session_id,
                                "codex": codex,
                                "raceId": race_id,
                                "title": session_title,
                                "event_place": session.get("event_place"),
                                "event_country": session.get("event_country"),
                                "season": session.get("season"),
                                "category_code": session.get("category_code"),
                                "session_type": session_type_str,
                                "date": date_str,
                                "time": start_time,
                                "hillSize": effective_hs,
                                "hill_size_unit": effective_unit,
                                "hillType": effective_type,
                                "k_point": k_point,
                                "results_data": {"results": enriched_athletes},
                            }

                            place = session.get("event_place")
                            if place and effective_hs:
                                try:
                                    from app.services.hill_service import record_hill_version_from_session
                                    await record_hill_version_from_session(
                                        db=db,
                                        place=place,
                                        country_code=session.get("event_country") or "UNKNOWN",
                                        hill_size=int(effective_hs),
                                        hill_unit=effective_unit,
                                        hill_type=effective_type,
                                        session_date=date_str,
                                        season=session.get("season"),
                                        k_point=k_point,
                                        record=hill_data.get("record"),
                                        record_holder=hill_data.get("record_holder"),
                                        meter_value=hill_data.get("meter_value"),
                                    )
                                except Exception as e:
                                    print(f"⚠️ Nie udało się zaktualizować wersji skoczni: {e}")

                            doc_to_hash = {k: v for k, v in new_session_doc.items() if k != "_id"}
                            new_checksum = hashlib.md5(json.dumps(doc_to_hash, sort_keys=True).encode('utf-8')).hexdigest()
                            existing_session = await db["sessions"].find_one({"_id": new_session_id})

                            if existing_session:
                                old_checksum = existing_session.get("original_checksum")
                                if not old_checksum:
                                    old_doc_to_hash = {k: v for k, v in existing_session.items() if k not in ["_id", "original_checksum"]}
                                    old_checksum = hashlib.md5(json.dumps(old_doc_to_hash, sort_keys=True).encode('utf-8')).hexdigest()
                                if old_checksum == new_checksum:
                                    return {"success": False, "ignored": True, "reason": "UNCHANGED", "parent_id": parent_id}

                            new_session_doc["original_checksum"] = new_checksum
                            print(f"✅ SUKCES ({used_engine}) | {race_id}_{codex}_{base_suffix} Zawodników: {len(enriched_athletes)} | {pdf_url}")
                            return {
                                "success": True,
                                "bulk_op": ReplaceOne({"_id": new_session_id}, new_session_doc, upsert=True),
                                "report": {"codex": codex, "type": doc_type, "athletes_parsed": len(enriched_athletes), "engine": used_engine},
                                "parent_id": parent_id,
                            }

                        print(f"⚠️ Pusty Plik / Brak zawodników | CODEX={codex} RACE_ID={race_id} PDF_URL={pdf_url}")
                        return {"success": False, "ignored": True, "reason": "EMPTY", "parent_id": parent_id}

                    except asyncio.CancelledError:
                        return {"stopped": True, "parent_id": parent_id}
                    except Exception as e:
                        err_detail = f"{type(e).__name__}: {e or repr(e)}"
                        print(f"❌ BŁĄD | CODEX={codex} RACE_ID={race_id} PDF_URL={pdf_url} | SZCZEGÓŁY: {err_detail}")
                        return {
                            "success": False,
                            "error": err_detail,
                            "codex": codex,
                            "race_id": race_id,
                            "type": doc_type,
                            "pdf_url": pdf_url,
                            "parent_id": parent_id,
                        }

            # Kulturalna pula połączeń (max 4 połączenia zamiast 15, timeout 45s read / 15s connect)
            timeout_cfg = httpx.Timeout(45.0, connect=15.0)
            limits_cfg = httpx.Limits(max_connections=4, max_keepalive_connections=2)
            chunk_size = 12

            async with httpx.AsyncClient(
                timeout=timeout_cfg,
                headers=DEFAULT_HEADERS,
                follow_redirects=True,
                limits=limits_cfg,
            ) as client:
                for start_idx in range(0, len(sessions_to_process), chunk_size):
                    if self.stop_requested:
                        break

                    chunk = sessions_to_process[start_idx:start_idx + chunk_size]
                    chunk_tasks = [asyncio.create_task(process_single_pdf(s, d, client)) for s, d in chunk]
                    chunk_bulk_ops = []

                    for fut in asyncio.as_completed(chunk_tasks):
                        res = await fut
                        if not isinstance(res, dict) or res.get("stopped"):
                            continue

                        pid = res.get("parent_id")
                        self.files_done += 1

                        if "error" in res:
                            self.failed_files += 1
                            err_entry = {
                                "codex": res.get("codex"),
                                "race_id": res.get("race_id"),
                                "type": res.get("type"),
                                "pdf_url": res.get("pdf_url"),
                                "error": res.get("error"),
                            }
                            self.errors.append(err_entry)
                            if pid:
                                parent_failed_errors[pid] = res.get("error", "")
                        elif res.get("success"):
                            self.saved_to_review += 1
                            chunk_bulk_ops.append(res["bulk_op"])
                            parsed_reports.append(res["report"])
                        elif res.get("ignored"):
                            if res.get("reason") == "UNCHANGED":
                                self.unchanged_files += 1
                            elif res.get("reason") == "EMPTY":
                                self.empty_files += 1

                        if pid:
                            parent_completed_counts[pid] = parent_completed_counts.get(pid, 0) + 1

                    # Natychmiastowy zapis kwarantanny po każdej porcji (chroni postęp przy zatrzymaniu)
                    if chunk_bulk_ops:
                        await db["sessions_review"].bulk_write(chunk_bulk_ops)

                    # Oznacz sesje nadrzędne, dla których ukończono już wszystkie załączniki PDF
                    chunk_parent_ops = []
                    for pid, done_cnt in list(parent_completed_counts.items()):
                        if done_cnt >= parent_expected_counts.get(pid, 1):
                            if pid in parent_failed_errors:
                                chunk_parent_ops.append(
                                    UpdateOne(
                                        {"_id": pid},
                                        {
                                            "$inc": {"trainings_retry_count": 1},
                                            "$set": {"last_training_error": str(parent_failed_errors[pid])},
                                        },
                                    )
                                )
                                chunk_parent_ops.append(
                                    UpdateOne(
                                        {"_id": pid, "trainings_retry_count": {"$gte": 2}},
                                        {"$set": {"trainings_processed": True, "trainings_failed": True}},
                                    )
                                )
                            else:
                                chunk_parent_ops.append(
                                    UpdateOne({"_id": pid}, {"$set": {"trainings_processed": True}})
                                )
                            total_marked_parents += 1
                            parent_completed_counts.pop(pid, None)
                            parent_failed_errors.pop(pid, None)

                    if chunk_parent_ops:
                        await db["sessions"].bulk_write(chunk_parent_ops)

            if self.stop_requested:
                msg = f"Zatrzymano bezpiecznie przez administratora ({self.files_done}/{self.total_files} plików)."
            else:
                msg = (
                    f"Zakończono ekstrakcję {self.files_done} plików PDF z {len(sessions)} sesji. "
                    f"Do kwarantanny: {self.saved_to_review}, bez zmian: {self.unchanged_files}, błędów: {self.failed_files}."
                )

            self.last_message = msg
            self.current_item = None
            self.last_result = {
                "message": msg,
                "stopped": self.stop_requested,
                "processed_sessions_in_batch": len(sessions),
                "total_files": self.total_files,
                "files_done": self.files_done,
                "saved_to_review": self.saved_to_review,
                "unchanged_files": self.unchanged_files,
                "empty_files": self.empty_files,
                "failed_files": self.failed_files,
                "distinct_doc_types_in_batch": self.distinct_doc_types_in_batch,
                "errors": self.errors,
                "report": self.errors,
                "marked_as_processed": total_marked_parents,
            }
            return self.last_result
        except Exception as fatal_err:
            err_msg = f"Krytyczny błąd ekstrakcji PDF: {type(fatal_err).__name__}: {fatal_err or repr(fatal_err)}"
            print(f"❌ {err_msg}")
            self.last_message = err_msg
            self.errors.append({"codex": "SYSTEM", "type": "JOB", "error": err_msg})
            self.last_result = {
                "message": err_msg,
                "saved_to_review": self.saved_to_review,
                "unchanged_files": self.unchanged_files,
                "failed_files": self.failed_files + 1,
                "errors": self.errors,
                "report": self.errors,
            }
            return self.last_result
        finally:
            if self.started_at is not None:
                self.finished_elapsed_seconds = max(0, int(time.time() - self.started_at))
            self.is_running = False
            self.current_item = None


_pdf_training_job_manager = PDFTrainingJobManager()


def get_pdf_training_job_manager() -> PDFTrainingJobManager:
    return _pdf_training_job_manager


# ==========================================
# GŁÓWNY SCRAPER (JEDNA PACZKA = 2000 SESJI)
# ==========================================
async def process_all_trainings_pdf(
    db,
    season: str = None,
    season_from: int = None,
    season_to: int = None,
    categories: list = None,
    force_reprocess: bool = False,
):
    manager = get_pdf_training_job_manager()
    manager.mark_started()
    return await manager.run_job(
        db=db,
        season=season,
        season_from=season_from,
        season_to=season_to,
        categories=categories,
        force_reprocess=force_reprocess,
    )


# ==========================================
# ENDPOINT CHECKER (DEBUGGER)
# ==========================================
async def process_single_pdf_debug(db, pdf_url: str, codex: str, race_id: str):
    debug_log = []
    debug_log.append("1. Budowanie bazy kotwic i indeksu skoczków...")
    sorted_anchors, global_athletes_map = await build_global_anchors_and_athletes(db)
    anchor_set = set(sorted_anchors)
    debug_log.append(f"Załadowano {len(sorted_anchors)} nazwisk z kolekcji athletes.")

    if pdf_url and "/competition-attachment/" in pdf_url and not pdf_url.lower().endswith(".pdf"):
        pdf_url = f"{pdf_url}.pdf"

    debug_log.append(f"2. Pobieranie pliku: {pdf_url}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0), headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        try:
            response = await client.get(pdf_url)
            response.raise_for_status()

            if b"%PDF" not in response.content[:10]:
                return {"error": "Pobrany plik nie jest dokumentem PDF.", "debug_log": debug_log}

            parsed = await asyncio.to_thread(
                _parse_pdf_bytes_sync,
                response.content,
                False,
                "",
                "9999-99-99",
                sorted_anchors,
                anchor_set,
            )
            full_text = parsed["full_text"]
            raw_preview = full_text[:1000]
            hill_data = parsed["hill_data"]
            athletes_results = parsed["athletes_results"]
            used_engine = parsed["used_engine"]
            debug_log.extend(parsed["engine_logs"])

            # Dopasuj tożsamość i fis_code
            enriched_athletes = []
            for a in athletes_results:
                meta = match_athlete_identity(
                    name=a.get("name", ""),
                    country=a.get("country", ""),
                    session_athletes=[],
                    global_athletes_map=global_athletes_map
                )
                enriched_athletes.append({
                    "bib": a.get("bib"),
                    "name": meta["name"],
                    "country": meta["country"],
                    "fis_code": meta["fis_code"],
                    "gender": meta["gender"],
                    "jumps": a.get("jumps", []),
                    "raw_text": a.get("raw_text", "")
                })

            enriched_athletes = assign_training_ranks_and_points(enriched_athletes)

            return {
                "success": True,
                "codex": codex,
                "race_id": race_id,
                "used_engine": used_engine,
                "debug_log": debug_log,
                "hill_data": hill_data,
                "athletes_found": len(enriched_athletes),
                "parsed_results": enriched_athletes,
                "raw_text_preview": raw_preview
            }

        except Exception as e:
            err_detail = f"{type(e).__name__}: {e or repr(e)}"
            debug_log.append(f"BŁĄD KRYTYCZNY: {err_detail}")
            return {"error": err_detail, "traceback": traceback.format_exc(), "debug_log": debug_log}
