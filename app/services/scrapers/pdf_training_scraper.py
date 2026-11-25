# app/services/scrapers/pdf_training_scraper.py
import httpx
import fitz  # PyMuPDF
import io
import traceback
import re
import asyncio
import json
import hashlib
from datetime import datetime
from pymongo import ReplaceOne, UpdateOne
from app.config import DEFAULT_HEADERS


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


def normalize_athlete_name(name: str) -> str:
    """Normalizuje nazwisko skoczka do porównań (tylko wielkie litery A-Z i spacje)."""
    if not name:
        return ""
    clean = re.sub(r'[^A-Z\s]', '', name.upper())
    return re.sub(r'\s+', ' ', clean).strip()


def match_athlete_identity(name: str, country: str, session_athletes: list, global_athletes_map: dict) -> dict:
    """Dopasowuje skoczka z PDF do bazy danych w celu uzyskania fis_code i ujednolicenia danych."""
    norm_name = normalize_athlete_name(name)
    name_tokens = set(norm_name.split())
    country_up = (country or "").strip().upper()

    # 1. Sprawdź zawodników z sesji nadrzędnej (kwalifikacje/zawody tego samego weekendu)
    if session_athletes:
        for a in session_athletes:
            s_name = normalize_athlete_name(a.get("name", ""))
            s_country = (a.get("country", "") or "").strip().upper()
            s_tokens = set(s_name.split())

            if s_tokens == name_tokens or norm_name == s_name:
                if not country_up or not s_country or country_up == s_country:
                    return {
                        "fis_code": str(a.get("fis_code", "")),
                        "name": a.get("name", name),
                        "country": s_country or country_up,
                        "gender": a.get("gender", "M")
                    }

    # 2. Sprawdź globalną mapę skoczków z kolekcji athletes
    if global_athletes_map:
        if country_up and (norm_name, country_up) in global_athletes_map:
            a_info = global_athletes_map[(norm_name, country_up)]
            return {
                "fis_code": str(a_info.get("fis_code", "")),
                "name": a_info.get("name", name),
                "country": a_info.get("country", country_up),
                "gender": a_info.get("gender", "M")
            }

        if norm_name in global_athletes_map:
            a_info = global_athletes_map[norm_name]
            return {
                "fis_code": str(a_info.get("fis_code", "")),
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
            country = (a.get("country_code", "") or "").strip().upper()
            info = {
                "fis_code": a.get("fis_code"),
                "name": name.strip(),
                "country": country,
                "gender": a.get("gender", "M")
            }
            if country:
                athletes_map[(norm, country)] = info
            if norm not in athletes_map:
                athletes_map[norm] = info

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

                    nsa_match = re.search(r'\b([A-Z]{3})\b', full_name_raw)
                    nsa = nsa_match.group(1) if nsa_match else ""
                    clean_name = re.sub(r'\b[A-Z]{3}\b', '', full_name_raw).strip()

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
                        nsa_m = re.search(r'\b([A-Z]{3})\b', n_str)
                        if nsa_m:
                            c_str = nsa_m.group(1)
                            n_str = re.sub(r'\b[A-Z]{3}\b', '', n_str).strip()

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


def extract_athletes_via_fallback_anchors(full_text: str, num_rounds: int, round_names: list, sorted_anchors: list) -> list:
    """
    SILNIK 3 (Ratunkowy): Klasyczny parser z kotwicami, z restrykcyjnym odcięciem
    sekcji Jury/Weather i zabezpieczeniem przed ujemnymi odległościami.
    """
    # 1. Bezwzględnie odetnij sekcję Jury i dane pogodowe
    cutoff_match = re.search(r'(?:Competition Management\s*/\s*Jury|Jury\s*/\s*Competition Management|Judges\s*\n|Competition\s*/\s*Weather Information)', full_text, re.IGNORECASE)
    cleaned_text = full_text[:cutoff_match.start()] if cutoff_match else full_text

    lines = [line.strip() for line in cleaned_text.split('\n') if line.strip()]

    start_indices = []
    for i, line in enumerate(lines):
        line_to_check = re.sub(r'\s+', ' ', line.upper())
        line_to_check = re.sub(r'^\d+\.?\s*', '', line_to_check).strip()
        if not line_to_check:
            continue
        for name in sorted_anchors:
            if line_to_check == name or line_to_check.startswith(name + " "):
                start_indices.append((i, name))
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
# GŁÓWNY SCRAPER (JEDNA PACZKA = 500 SESJI)
# ==========================================
async def process_all_trainings_pdf(db, season=None):
    print("▶ Inicjalizacja: Pobieranie pełnej bazy skoczków do zakotwiczenia parsera...")
    sorted_anchors, global_athletes_map = await build_global_anchors_and_athletes(db)
    print(f"▶ Sukces: Załadowano {len(sorted_anchors)} wzorców skoczków z głównej bazy.")

    if len(sorted_anchors) == 0:
        return {"message": "BŁĄD: Załadowano 0 skoczków. Sprawdź czy kolekcja 'athletes' zawiera pole 'full_name'."}

    query = {
        "_id": {"$not": {"$regex": "_(TRAINING|TRIAL)", "$options": "i"}},
        "documents.type": {"$regex": "Results.*Training|Training.*Results|Results.*Trial|Official.*Training", "$options": "i"},
        "trainings_processed": {"$ne": True}
    }
    if season:
        query["season"] = season

    sessions_cursor = db["sessions"].find(query).limit(2000)
    sessions = await sessions_cursor.to_list(None)
    if not sessions:
        return {"message": "Wszystkie sesje treningowe są już przetworzone. Brak nowych danych do pobrania!"}

    results_report = []
    bulk_ops = []
    distinct_doc_types = set()
    unchanged_count = 0
    semaphore = asyncio.Semaphore(2)

    async def process_single_pdf(session, doc, client, db_instance):
        parent_id = session.get("_id")
        async with semaphore:
            doc_type = doc.get("type", "")
            pdf_url = doc.get("url")
            if pdf_url and "/competition-attachment/" in pdf_url and not pdf_url.lower().endswith(".pdf"):
                pdf_url = f"{pdf_url}.pdf"
            race_id = session.get("raceId", "UNKNOWN")
            codex = session.get("codex", "UNKNOWN")

            is_trial = "Trial" in doc_type
            session_type_str = "TRIAL" if is_trial else "TRAINING"
            base_suffix = "TRIAL" if is_trial else "TRAINING"

            num_match = re.search(r'\((\d+)\)', doc_type)
            file_idx = num_match.group(1) if num_match else ""
            suffix = f"{base_suffix}_{file_idx}" if file_idx else base_suffix
            new_session_id = f"{race_id}_{codex}_{suffix}"

            try:
                # Pobieranie pliku PDF z obsługą błędu 429 i ponawianiem (do 4 prób)
                response = None
                for attempt in range(4):
                    try:
                        await asyncio.sleep(0.5)
                        response = await client.get(pdf_url)
                        response.raise_for_status()
                        break
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
                        if attempt < 3:
                            await asyncio.sleep(2.0)
                            continue
                        raise req_err

                if b"%PDF" not in response.content[:10]:
                    return {"success": False, "ignored": True, "reason": "EMPTY", "parent_id": parent_id}

                pdf_stream = io.BytesIO(response.content)
                with fitz.open(stream=pdf_stream, filetype="pdf") as pdf_doc:
                    full_text = "\n".join([page.get_text("text") for page in pdf_doc])
                    hill_data = parse_hill_data_from_pdf_text(full_text)

                    start_time = "12:00"
                    time_match = re.search(r"Start Time:\s*(\d{1,2}:\d{2})", full_text)
                    if time_match:
                        start_time = time_match.group(1)

                    date_str = session.get("date", "9999-99-99")
                    date_match = re.search(r"(?:SUN|MON|TUE|WED|THU|FRI|SAT)\s+(\d{1,2}\s+[A-Z]{3}\s+\d{4})", full_text)
                    if date_match:
                        try:
                            parsed_date = datetime.strptime(date_match.group(1), "%d %b %Y")
                            date_str = parsed_date.strftime("%Y-%m-%d")
                        except:
                            pass

                    num_rounds = 3 if "3RD ROUND" in full_text else (2 if "2ND ROUND" in full_text else 1)
                    if is_trial:
                        round_names = ["Trial"]
                    else:
                        if file_idx:
                            round_names = [f"T{file_idx}"] if num_rounds == 1 else [f"T{file_idx}_{i + 1}" for i in range(num_rounds)]
                        else:
                            round_names = [f"T{i + 1}" for i in range(num_rounds)]

                    # Kaskada silników
                    athletes_results = []
                    used_engine = ""

                    # Silnik 1: Tabela (najwyższa dokładność)
                    athletes_results = extract_athletes_via_table_engine(pdf_doc, round_names)
                    if len(athletes_results) >= 10:
                        used_engine = "TABLE_ENGINE"

                    # Silnik 2: Dwukolumnowy (Trial _73C1)
                    if not athletes_results or len(athletes_results) < 10:
                        col_results = extract_athletes_via_2column_engine(pdf_doc, round_names)
                        if len(col_results) >= 10:
                            athletes_results = col_results
                            used_engine = "2COLUMN_ENGINE"

                    # Silnik 3: Kotwice tekstowe (awaryjny)
                    if not athletes_results or len(athletes_results) < 5:
                        fallback_results = extract_athletes_via_fallback_anchors(full_text, num_rounds, round_names, sorted_anchors)
                        if fallback_results:
                            athletes_results = fallback_results
                            used_engine = "ANCHOR_FALLBACK"

                if athletes_results:
                    # Dopasuj tożsamość i fis_code każdego zawodnika
                    parent_athletes = session.get("results_data", {}).get("results", [])
                    enriched_athletes = []
                    for a in athletes_results:
                        meta = match_athlete_identity(
                            name=a.get("name", ""),
                            country=a.get("country", ""),
                            session_athletes=parent_athletes,
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
                        "results_data": {"results": enriched_athletes}
                    }

                    # Wzbogacamy wersję skoczni w bazie o punkt K, rekord i synchronizujemy hierarchię
                    place = session.get("event_place")
                    if place and effective_hs:
                        try:
                            from app.services.hill_service import record_hill_version_from_session
                            await record_hill_version_from_session(
                                db=db_instance,
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
                                meter_value=hill_data.get("meter_value")
                            )
                        except Exception as e:
                            print(f"⚠️ Nie udało się zaktualizować wersji skoczni: {e}")

                    doc_to_hash = {k: v for k, v in new_session_doc.items() if k != "_id"}
                    new_checksum = hashlib.md5(json.dumps(doc_to_hash, sort_keys=True).encode('utf-8')).hexdigest()
                    existing_session = await db_instance["sessions"].find_one({"_id": new_session_id})

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
                        "parent_id": parent_id
                    }

                print(f"⚠️ Pusty Plik / Brak zawodników | CODEX={codex} RACE_ID={race_id} PDF_URL={pdf_url}")
                return {"success": False, "ignored": True, "reason": "EMPTY", "parent_id": parent_id}

            except Exception as e:
                print(f"❌ BŁĄD | CODEX={codex} RACE_ID={race_id} PDF_URL={pdf_url} | SZCZEGÓŁY: {str(e)}")
                return {"success": False, "error": str(e), "codex": codex, "type": doc_type, "parent_id": parent_id}

    sessions_to_process = []
    for session in sessions:
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

    results = []
    async with httpx.AsyncClient(timeout=30.0, headers=DEFAULT_HEADERS, follow_redirects=True,
                                 limits=httpx.Limits(max_connections=15)) as client:
        tasks = [process_single_pdf(s, d, client, db) for s, d in sessions_to_process]
        print(f"⏳ Oczekiwanie na pobranie i analizę {len(tasks)} plików z {len(sessions)} sesji...")
        results = await asyncio.gather(*tasks, return_exceptions=True)

    successful_parents = set(s["_id"] for s in sessions)
    failed_parents = {}
    for res in results:
        if isinstance(res, Exception) or not isinstance(res, dict):
            continue
        if "error" in res:
            pid = res.get("parent_id")
            if pid:
                successful_parents.discard(pid)
                failed_parents[pid] = res.get("error")
            results_report.append({"codex": res.get("codex"), "type": res.get("type"), "error": res.get("error")})
            continue

        if res.get("success"):
            bulk_ops.append(res["bulk_op"])
            results_report.append(res["report"])
        elif res.get("ignored"):
            if res.get("reason") == "UNCHANGED":
                unchanged_count += 1
            elif res.get("reason") == "EMPTY":
                pass

    if bulk_ops:
        await db["sessions_review"].bulk_write(bulk_ops)

    parent_ops = [UpdateOne({"_id": pid}, {"$set": {"trainings_processed": True}}) for pid in successful_parents if pid]

    for pid, err in failed_parents.items():
        parent_ops.append(UpdateOne(
            {"_id": pid},
            {
                "$inc": {"trainings_retry_count": 1},
                "$set": {"last_training_error": str(err)}
            }
        ))
        parent_ops.append(UpdateOne(
            {"_id": pid, "trainings_retry_count": {"$gte": 2}},
            {"$set": {"trainings_processed": True, "trainings_failed": True}}
        ))

    if parent_ops:
        await db["sessions"].bulk_write(parent_ops)

    return {
        "message": f"Przetworzono paczkę {len(sessions)} sesji. Uruchom ponownie, aby pobrać kolejne.",
        "processed_sessions_in_batch": len(sessions),
        "saved_to_review": len(bulk_ops),
        "unchanged_files": unchanged_count,
        "distinct_doc_types_in_batch": sorted(list(distinct_doc_types)),
        "report": results_report,
        "marked_as_processed": len(parent_ops)
    }


# ==========================================
# ENDPOINT CHECKER (DEBUGGER)
# ==========================================
async def process_single_pdf_debug(db, pdf_url: str, codex: str, race_id: str):
    debug_log = []
    debug_log.append("1. Budowanie bazy kotwic i indeksu skoczków...")
    sorted_anchors, global_athletes_map = await build_global_anchors_and_athletes(db)
    debug_log.append(f"Załadowano {len(sorted_anchors)} nazwisk z kolekcji athletes.")

    if pdf_url and "/competition-attachment/" in pdf_url and not pdf_url.lower().endswith(".pdf"):
        pdf_url = f"{pdf_url}.pdf"

    debug_log.append(f"2. Pobieranie pliku: {pdf_url}")
    async with httpx.AsyncClient(timeout=15.0, headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        try:
            response = await client.get(pdf_url)
            response.raise_for_status()

            if b"%PDF" not in response.content[:10]:
                return {"error": "Pobrany plik nie jest dokumentem PDF.", "debug_log": debug_log}

            pdf_stream = io.BytesIO(response.content)
            with fitz.open(stream=pdf_stream, filetype="pdf") as pdf_doc:
                full_text = "\n".join([page.get_text("text") for page in pdf_doc])
                raw_preview = full_text[:1000]
                hill_data = parse_hill_data_from_pdf_text(full_text)

                num_rounds = 3 if "3RD ROUND" in full_text else (2 if "2ND ROUND" in full_text else 1)
                round_names = [f"T{i + 1}" for i in range(num_rounds)]

                used_engine = ""
                debug_log.append("-> Uruchamiam Silnik 1: PyMuPDF find_tables()...")
                athletes_results = extract_athletes_via_table_engine(pdf_doc, round_names)
                if len(athletes_results) >= 10:
                    used_engine = "TABLE_ENGINE"
                    debug_log.append(f"✅ Sukces Silnika 1! Znaleziono {len(athletes_results)} zawodników.")
                else:
                    debug_log.append("-> Silnik 1 nie znalazł wystarczającej liczby zawodników. Uruchamiam Silnik 2: 2-kolumnowy...")
                    col_results = extract_athletes_via_2column_engine(pdf_doc, round_names)
                    if len(col_results) >= 10:
                        athletes_results = col_results
                        used_engine = "2COLUMN_ENGINE"
                        debug_log.append(f"✅ Sukces Silnika 2! Znaleziono {len(athletes_results)} zawodników.")
                    else:
                        debug_log.append("-> Uruchamiam Silnik 3: Kotwice tekstowe z odcięciem Jury...")
                        fallback_results = extract_athletes_via_fallback_anchors(full_text, num_rounds, round_names, sorted_anchors)
                        if fallback_results:
                            athletes_results = fallback_results
                            used_engine = "ANCHOR_FALLBACK"
                            debug_log.append(f"✅ Sukces Silnika 3! Znaleziono {len(athletes_results)} zawodników.")

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
            debug_log.append(f"BŁĄD KRYTYCZNY: {str(e)}")
            return {"error": str(e), "traceback": traceback.format_exc(), "debug_log": debug_log}
