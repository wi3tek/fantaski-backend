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
# FUNKCJE POMOCNICZE (DWA NIEZALEŻNE SILNIKI)
# ==========================================
def parse_pdf_content_standard(content):
    """Główny, niezawodny silnik, który poprawnie obsługuje 95% plików (w tym kolumny z HALF.pdf)"""
    pdf_stream = io.BytesIO(content)
    with fitz.open(stream=pdf_stream, filetype="pdf") as pdf_doc:
        return "\n".join([page.get_text("text") for page in pdf_doc])


def parse_pdf_content_optical(content):
    """Silnik zapasowy (ratunkowy) dla plików zepsutych przez FIS (np. 1s.pdf z odciętymi liczbami)"""
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


async def build_global_anchors(db):
    """Pobiera skoczków z bazy opierając się WYŁĄCZNIE na kluczu full_name."""
    athletes_cursor = db["athletes"].find({}, {"full_name": 1})
    global_known_names = set()
    async for a in athletes_cursor:
        name = a.get("full_name")
        if name:
            clean_name = re.sub(r'\s+', ' ', name.strip().upper())
            global_known_names.add(clean_name)
    return global_known_names


# ==========================================
# GŁÓWNY SCRAPER (JEDNA PACZKA = 500 SESJI)
# ==========================================
async def process_all_trainings_pdf(db, season=None):
    print("▶ Inicjalizacja: Pobieranie pełnej bazy skoczków do zakotwiczenia parsera...")
    global_known_names = await build_global_anchors(db)
    print(f"▶ Sukces: Załadowano {len(global_known_names)} wzorców skoczków z głównej bazy.")

    if len(global_known_names) == 0:
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
    # TODO POBIERZMY SESJE JEDYNIE DLA DO SEZONU 2023. DO SEZONU 2022 SCRAPUJEMY DANE Z BAZY W CSV KTÓRĄ POBRALIŚMY.
    if not sessions:
        return {"message": "Wszystkie sesje treningowe są już przetworzone. Brak nowych danych do pobrania!"}

    results_report = []
    bulk_ops = []
    distinct_doc_types = set()
    unchanged_count = 0
    semaphore = asyncio.Semaphore(15)

    async def process_single_pdf(session, doc, client, db_instance):
        parent_id = session.get("_id")
        async with semaphore:
            doc_type = doc.get("type", "")
            pdf_url = doc.get("url")
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
                local_anchors = set(global_known_names)
                for r in session.get("results_data", {}).get("results", []):
                    if r.get("name"):
                        clean_name = re.sub(r'\s+', ' ', r["name"].strip().upper())
                        local_anchors.add(clean_name)

                sorted_anchors = sorted(list(local_anchors), key=len, reverse=True)

                # Pobieranie pliku PDF z ponawianiem (retry do 2 prób)
                response = None
                for attempt in range(2):
                    try:
                        response = await client.get(pdf_url)
                        response.raise_for_status()
                        break
                    except httpx.HTTPStatusError as http_err:
                        if attempt == 0 and http_err.response.status_code in [500, 502, 503, 504]:
                            await asyncio.sleep(1.0)
                            continue
                        raise http_err
                    except httpx.RequestError as req_err:
                        if attempt == 0:
                            await asyncio.sleep(1.0)
                            continue
                        raise req_err

                if b"%PDF" not in response.content[:10]:
                    return {"success": False, "ignored": True, "reason": "EMPTY", "parent_id": parent_id}

                athletes_results = []
                used_parser_name = ""

                # 🚀 WZORZEC FALLBACK: Najpierw Standardowy, potem ew. Optyczny
                for parser_name, parser_func in [("STANDARD", parse_pdf_content_standard),
                                                 ("OPTICAL", parse_pdf_content_optical)]:
                    athletes_results = []  # Resetujemy wyniki dla każdego podejścia
                    full_text = await asyncio.to_thread(parser_func, response.content)

                    start_time = "12:00"
                    time_match = re.search(r"Start Time:\s*(\d{1,2}:\d{2})", full_text)
                    if time_match: start_time = time_match.group(1)

                    date_str = session.get("date", "9999-99-99")
                    date_match = re.search(r"(?:SUN|MON|TUE|WED|THU|FRI|SAT)\s+(\d{1,2}\s+[A-Z]{3}\s+\d{4})", full_text)
                    if date_match:
                        try:
                            parsed_date = datetime.strptime(date_match.group(1), "%d %b %Y")
                            date_str = parsed_date.strftime("%Y-%m-%d")
                        except:
                            pass

                    num_rounds = 3 if "3RD ROUND" in full_text else 2 if "2ND ROUND" in full_text else 1
                    if is_trial:
                        round_names = ["Trial"]
                    else:
                        if file_idx:
                            if num_rounds == 1:
                                round_names = [f"T{file_idx}"]
                            else:
                                round_names = [f"T{file_idx}_{i + 1}" for i in range(num_rounds)]
                        else:
                            round_names = [f"T{i + 1}" for i in range(num_rounds)]

                    lines = [line.strip() for line in full_text.split('\n') if line.strip()]

                    start_indices = []
                    for i, line in enumerate(lines):
                        line_to_check = re.sub(r'\s+', ' ', line.upper())
                        line_to_check = re.sub(r'^\d+\.?\s*', '', line_to_check).strip()
                        if not line_to_check: continue
                        for name in sorted_anchors:
                            if line_to_check == name or line_to_check.startswith(name + " "):
                                start_indices.append((i, name))
                                break

                    start_indices = sorted(start_indices, key=lambda x: x[0])

                    athlete_chunks_map = {}
                    for idx, (line_idx, athlete_name) in enumerate(start_indices):
                        end_idx = start_indices[idx + 1][0] if idx + 1 < len(start_indices) else len(lines)
                        chunk = lines[line_idx: end_idx]
                        if athlete_name not in athlete_chunks_map:
                            athlete_chunks_map[athlete_name] = []
                        athlete_chunks_map[athlete_name].append(chunk)

                    for athlete_name, chunks in athlete_chunks_map.items():
                        best_floats = []
                        best_statuses = []
                        best_raw_text = ""
                        all_raw_texts = []
                        max_float_count = -1

                        for chunk in chunks:
                            raw_text_block = " | ".join(chunk)
                            all_raw_texts.append(raw_text_block)

                            temp_floats = []
                            temp_statuses = []

                            for line in chunk:
                                tokens = line.split()
                                for c in tokens:
                                    c_clean = c.replace("=", "").replace("m", "").strip()
                                    if c_clean in ["DNS", "DSQ", "NPS", "SCR"]:
                                        temp_statuses.append(c_clean)
                                    elif re.match(r'^-?\d+\.\d+$', c_clean) and not c.strip().endswith('.'):
                                        temp_floats.append(float(c_clean))

                            if len(temp_floats) > max_float_count:
                                max_float_count = len(temp_floats)
                                best_floats = temp_floats
                                best_statuses = temp_statuses
                                best_raw_text = raw_text_block

                        if max_float_count == 0 and len(best_statuses) == 0:
                            continue

                        athlete_jumps = []
                        floats_per_round = len(best_floats) // num_rounds if len(best_floats) > 0 else 0
                        for r in range(num_rounds):
                            r_floats = best_floats[
                                r * floats_per_round: (r + 1) * floats_per_round] if floats_per_round > 0 else []
                            dist = r_floats[1] if len(r_floats) >= 2 else None
                            pts = r_floats[-1] if len(r_floats) >= 1 else None
                            status = best_statuses[r] if r < len(best_statuses) else None

                            athlete_jumps.append({
                                "round": round_names[r],
                                "distance": dist,
                                "points": pts,
                                "status": status
                            })

                        if athlete_jumps:
                            athlete_obj = {
                                "name": athlete_name,
                                "jumps": athlete_jumps
                            }
                            if len(chunks) > 1:
                                athlete_obj["raw_text_list"] = all_raw_texts
                            else:
                                athlete_obj["raw_text"] = best_raw_text

                            athletes_results.append(athlete_obj)

                    # 🚀 Jeśli znaleźliśmy zawodników tym silnikiem, nie musimy odpalać drugiego!
                    if athletes_results:
                        used_parser_name = parser_name
                        break

                if athletes_results:
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
                        "hillSize": session.get("hillSize"),
                        "hill_size_unit": session.get("hill_size_unit") or "HS",
                        "hillType": session.get("hillType"),
                        "results_data": {"results": athletes_results}
                    }

                    doc_to_hash = {k: v for k, v in new_session_doc.items() if k != "_id"}
                    new_checksum = hashlib.md5(json.dumps(doc_to_hash, sort_keys=True).encode('utf-8')).hexdigest()
                    existing_session = await db_instance["sessions"].find_one({"_id": new_session_id})

                    if existing_session:
                        old_checksum = existing_session.get("original_checksum")
                        if not old_checksum:
                            old_doc_to_hash = {k: v for k, v in existing_session.items() if
                                               k not in ["_id", "original_checksum"]}
                            old_checksum = hashlib.md5(
                                json.dumps(old_doc_to_hash, sort_keys=True).encode('utf-8')).hexdigest()
                        if old_checksum == new_checksum:
                            return {"success": False, "ignored": True, "reason": "UNCHANGED", "parent_id": parent_id}

                    new_session_doc["original_checksum"] = new_checksum
                    print(
                        f"✅ SUKCES ({used_parser_name}) | {race_id}_{codex}_{base_suffix}  Zawodników: {len(athletes_results)} | {pdf_url}")
                    return {
                        "success": True, "bulk_op": ReplaceOne({"_id": new_session_id}, new_session_doc, upsert=True),
                        "report": {"codex": codex, "type": doc_type, "athletes_parsed": len(athletes_results)},
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
            if doc_type: distinct_doc_types.add(doc_type)
            doc_type_lower = doc_type.lower()
            is_training_or_trial = (
                ("training" in doc_type_lower or "trial" in doc_type_lower)
                and "start list" not in doc_type_lower
            )
            if is_training_or_trial:
                if doc.get("url"):
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
        if isinstance(res, Exception) or not isinstance(res, dict): continue
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

    # Zabezpieczenie przed nieskończoną pętlą: jeśli sesja miała błąd 502/404, zwiększamy licznik prób.
    # Po 2 nieudanych próbach oznaczamy jako przetworzoną z błędem, aby nie blokowała kolejki.
    for pid, err in failed_parents.items():
        parent_ops.append(UpdateOne(
            {"_id": pid},
            {
                "$inc": {"trainings_retry_count": 1},
                "$set": {"last_training_error": str(err)}
            }
        ))
        # Sprawdzamy czy przekroczyło limit (po aktualizacji lub dla obecnej sesji)
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
    debug_log.append("1. Budowanie bazy kotwic (Anchors)...")
    global_known_names = await build_global_anchors(db)
    debug_log.append(f"Załadowano {len(global_known_names)} nazwisk z bazy (tylko z full_name).")
    sorted_anchors = sorted(list(global_known_names), key=len, reverse=True)

    debug_log.append(f"2. Pobieranie pliku: {pdf_url}")
    async with httpx.AsyncClient(timeout=15.0, headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        try:
            response = await client.get(pdf_url)
            response.raise_for_status()

            if b"%PDF" not in response.content[:10]:
                return {"error": "Pobrany plik nie jest dokumentem PDF.", "debug_log": debug_log}

            parsed_results = []
            used_parser_name = ""

            for parser_name, parser_func in [("STANDARDOWY", parse_pdf_content_standard),
                                             ("OPTYCZNY (Awaryjny)", parse_pdf_content_optical)]:
                debug_log.append(f"-> Uruchamiam silnik analizy: {parser_name}")
                full_text = await asyncio.to_thread(parser_func, response.content)
                raw_preview = full_text[:1000]

                lines = [line.strip() for line in full_text.split('\n') if line.strip()]
                start_indices = []
                for i, line in enumerate(lines):
                    line_to_check = re.sub(r'\s+', ' ', line.upper())
                    line_to_check = re.sub(r'^\d+\.?\s*', '', line_to_check).strip()
                    if not line_to_check: continue
                    for name in sorted_anchors:
                        if line_to_check == name or line_to_check.startswith(name + " "):
                            start_indices.append((i, name))
                            break

                start_indices = sorted(start_indices, key=lambda x: x[0])

                athlete_chunks_map = {}
                for idx, (line_idx, athlete_name) in enumerate(start_indices):
                    end_idx = start_indices[idx + 1][0] if idx + 1 < len(start_indices) else len(lines)
                    chunk = lines[line_idx: end_idx]
                    if athlete_name not in athlete_chunks_map:
                        athlete_chunks_map[athlete_name] = []
                    athlete_chunks_map[athlete_name].append(chunk)

                parsed_results = []
                for athlete_name, chunks in athlete_chunks_map.items():
                    best_floats = []
                    best_raw_text = ""
                    all_raw_texts = []
                    max_float_count = -1

                    for chunk in chunks:
                        raw_text_block = " | ".join(chunk)
                        all_raw_texts.append(raw_text_block)
                        temp_floats = []

                        for line in chunk:
                            tokens = line.split()
                            for c in tokens:
                                c_clean = c.replace("=", "").replace("m", "").strip()
                                if re.match(r'^-?\d+\.\d+$', c_clean) and not c.strip().endswith('.'):
                                    temp_floats.append(float(c_clean))

                        if len(temp_floats) > max_float_count:
                            max_float_count = len(temp_floats)
                            best_floats = temp_floats
                            best_raw_text = raw_text_block

                    if max_float_count == 0:
                        continue

                    res_obj = {
                        "name": athlete_name,
                        "floats_found": best_floats,
                        "occurrences": len(chunks)
                    }
                    if len(chunks) > 1:
                        res_obj["raw_text_list"] = all_raw_texts
                    else:
                        res_obj["raw_text"] = best_raw_text

                    parsed_results.append(res_obj)

                if parsed_results:
                    used_parser_name = parser_name
                    debug_log.append(
                        f"✅ SUKCES! Silnik {parser_name} znalazł {len(parsed_results)} zawodników z prawidłowymi liczbami. " )
                    break
                else:
                    debug_log.append(f"❌ Porażka. Silnik {parser_name} znalazł 0 wyników.")

            return {
                "success": True,
                "codex": codex,
                "race_id": race_id,
                "used_engine": used_parser_name,
                "debug_log": debug_log,
                "athletes_found": len(parsed_results),
                "parsed_results": parsed_results,
                "raw_text_preview": raw_preview
            }

        except Exception as e:
            debug_log.append(f"BŁĄD KRYTYCZNY: {str(e)}")
            return {"error": str(e), "traceback": traceback.format_exc(), "debug_log": debug_log}