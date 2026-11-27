import asyncio
import datetime
import hashlib
import json
import logging
import threading
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateOne

from app.services.draft_service import notify_season_started_for_session
from app.services.hill_service import bulk_record_hill_versions, resolve_event_hill_data
from app.services.scrapers.generate_results import process_single_race, run_scraper_pipeline
from app.services.weather.weather_service import get_weather_service

logger = logging.getLogger(__name__)

CHECKSUM_IGNORED_KEYS = {
    "_id",
    "checksum",
    "original_checksum",
    "last_checked",
    "review_reason",
    "weather",
    "scrape_status",
    "scrape_error",
    "scrape_error_at",
    "event_id",
}


def _json_serial(obj):
    """Zabezpieczenie parsowania dat dla json.dumps."""
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def generate_full_checksum(data: dict) -> str:
    """Tworzy deterministyczny hash ignorując klucze systemowe i techniczne."""
    clean_data = {k: v for k, v in data.items() if k not in CHECKSUM_IGNORED_KEYS}
    json_string = json.dumps(clean_data, sort_keys=True, ensure_ascii=False, default=_json_serial)
    return hashlib.md5(json_string.encode("utf-8")).hexdigest()


def _has_complete_results(doc: Optional[Dict[str, Any]]) -> bool:
    if not doc:
        return False
    res = (doc.get("results_data") or {}).get("results")
    if isinstance(res, list) and len(res) > 0:
        return True
    if doc.get("is_cancelled") is True or (doc.get("results_data") or {}).get("is_cancelled") is True:
        return True
    return False


async def save_scraped_events_to_db(
    db: AsyncIOMotorDatabase,
    scraped_events: List[Dict[str, Any]],
    season: str,
    category: str,
    only_missing: bool = True,
    existing_event_ids_by_session: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, int]:
    """
    Wspólna funkcja zapisująca zescrapowane eventy do bazy MongoDB (sessions / sessions_review),
    rejestrująca wersje skoczni, aktywnych zawodników oraz oznaczająca błędy HTTP 429 do wznowienia.
    """
    sessions_col = db["sessions"]
    review_col = db["sessions_review"]
    ath_col = db["athletes"]

    stats = {
        "inserted": 0,
        "sent_to_review": 0,
        "unchanged": 0,
        "skipped_complete": 0,
        "failed_429": 0,
        "failed_other": 0,
        "recovered_missing": 0,
    }
    hill_ops_dict: Dict[str, Dict[str, Any]] = {}
    athlete_ops_dict: Dict[int, Dict[str, Any]] = {}
    event_id_backfill_ops: List[UpdateOne] = []

    for event in scraped_events:
        if event.get("_skipped_stopped"):
            continue

        event_id = str(event.get("id") or "")
        place = event.get("place", "UNKNOWN")
        country = event.get("country", "UNKNOWN")
        event_sessions = event.get("sessions", [])

        if event.get("_skipped_event_complete"):
            # Event pominięty na poziomie kalendarza — nie pobrano żadnych event-details
            stats["skipped_complete"] += 1
            continue

        unskipped_sessions = [s for s in event_sessions if not s.get("_skipped_already_complete")]

        # Backfill event_id dla pominiętych sesji, żeby kolejne uruchomienia pomijały cały event
        if existing_event_ids_by_session is not None and event_id:
            for s in event_sessions:
                if s.get("_skipped_already_complete"):
                    s_cid = f"{s.get('raceId')}_{s.get('codex')}"
                    if s.get("raceId") and s.get("codex") and not existing_event_ids_by_session.get(s_cid):
                        event_id_backfill_ops.append(
                            UpdateOne({"_id": s_cid}, {"$set": {"event_id": event_id}})
                        )

        if not unskipped_sessions and only_missing:
            stats["unchanged"] += len(event_sessions)
            stats["skipped_complete"] += len(event_sessions)
            continue

        # Uzupełnienie parametrów skoczni z bazy / PDF tylko dla faktycznie pobranych sesji
        resolved_hill = None
        if any(not s.get("hillSize") and not s.get("_scrape_error") for s in unskipped_sessions):
            resolved_hill = await resolve_event_hill_data(db, unskipped_sessions, place, country)

        for session in event_sessions:
            if session.get("_skipped_already_complete"):
                stats["unchanged"] += 1
                stats["skipped_complete"] += 1
                continue

            scrape_error = session.pop("_scrape_error", None)

            if scrape_error == "HTTP_429":
                stats["failed_429"] += 1
            elif scrape_error:
                stats["failed_other"] += 1

            if resolved_hill and not session.get("hillSize"):
                session["hillSize"] = resolved_hill.get("size")
                session["hill_size_unit"] = resolved_hill.get("unit") or "HS"
                if not session.get("hillType") and resolved_hill.get("type"):
                    session["hillType"] = resolved_hill.get("type")

            hill_size = session.get("hillSize", 0)
            hill_unit = session.get("hill_size_unit") or "HS"

            if hill_size:
                hill_id = f"{place.upper()}_{hill_unit}{hill_size}"
                s_date = session.get("date")
                prev = hill_ops_dict.get(hill_id)
                best_date = s_date if not prev else max(s_date or "", prev.get("last_session_date") or "")
                entry = {
                    "place": place,
                    "country_code": country,
                    "size": hill_size,
                    "unit": hill_unit,
                    "type": session.get("hillType") or (resolved_hill.get("type") if resolved_hill else None),
                    "last_session_date": best_date,
                    "last_season": season,
                }
                if resolved_hill:
                    for fld in ("k_point", "record", "record_holder", "meter_value"):
                        if resolved_hill.get(fld) is not None:
                            entry[fld] = resolved_hill[fld]
                hill_ops_dict[hill_id] = entry

            codex = session.get("codex")
            race_id = session.get("raceId")
            if not codex or not race_id:
                continue

            session_composite_id = f"{race_id}_{codex}"

            session_results = session.get("results_data", {}).get("results", []) if session.get("results_data") else []
            for ath in session_results:
                f_code = ath.get("fis_code")
                if f_code:
                    try:
                        f_int = int(f_code)
                        athlete_ops_dict[f_int] = {
                            "fis_code": f_int,
                            "full_name": ath.get("name"),
                            "country_code": ath.get("country"),
                            "status": "Active",
                            "birth_year": ath.get("year"),
                            "last_active_season": season,
                        }
                    except ValueError:
                        pass

            session["event_place"] = place
            session["event_country"] = country
            session["season"] = season
            session["category_code"] = category
            if event_id:
                session["event_id"] = event_id

            has_athletes = bool(session.get("results_data") and session["results_data"].get("results"))
            is_canc = bool(
                (session.get("is_cancelled") or (session.get("results_data") or {}).get("is_cancelled"))
                and not has_athletes
            )
            session["is_cancelled"] = is_canc

            # Oznaczenie statusu 429 / błędu w bazie dla późniejszego wznowienia
            now_utc = datetime.datetime.utcnow()
            if scrape_error:
                session["scrape_status"] = "FAILED_429" if scrape_error == "HTTP_429" else "FAILED_ERROR"
                session["scrape_error"] = scrape_error
                session["scrape_error_at"] = now_utc
            else:
                session["scrape_status"] = "OK"

            # Automatyczne dołączenie pogody tylko jeśli sesja nie zwróciła błędu HTTP
            if not scrape_error and not session.get("weather") and session.get("date") and place != "UNKNOWN":
                try:
                    ws = get_weather_service()
                    w_data = ws.get_weather(place, session.get("date"), session.get("time", "16:00"))
                    if w_data:
                        session["weather"] = w_data
                except Exception:
                    pass

            current_hash = generate_full_checksum(session)
            session["checksum"] = current_hash
            session["last_checked"] = now_utc

            existing_session = await sessions_col.find_one({"_id": session_composite_id})

            if not existing_session:
                session["_id"] = session_composite_id
                await sessions_col.insert_one(session)
                stats["inserted"] += 1
                await notify_season_started_for_session(sessions_col.database, session)
            else:
                existing_complete = _has_complete_results(existing_session)

                # Nigdy nie nadpisuj kompletnych wyników błędem 429/sieci!
                if scrape_error and existing_complete:
                    await sessions_col.update_one(
                        {"_id": session_composite_id},
                        {"$set": {"last_checked": now_utc}},
                    )
                    stats["unchanged"] += 1
                    continue

                # Jeśli sesja była niekompletna / 429 w bazie, uzupełnij ją bezpośrednio
                if not existing_complete:
                    session["_id"] = session_composite_id
                    if scrape_error:
                        await sessions_col.update_one(
                            {"_id": session_composite_id},
                            {
                                "$set": {
                                    "scrape_status": session["scrape_status"],
                                    "scrape_error": session["scrape_error"],
                                    "scrape_error_at": now_utc,
                                    "last_checked": now_utc,
                                    **({"event_id": event_id} if event_id else {}),
                                }
                            },
                        )
                    else:
                        await sessions_col.replace_one({"_id": session_composite_id}, session, upsert=True)
                        await sessions_col.update_one(
                            {"_id": session_composite_id},
                            {"$unset": {"scrape_error": "", "scrape_error_at": ""}},
                        )
                        await review_col.delete_one({"_id": session_composite_id})
                        stats["recovered_missing"] += 1
                        await notify_season_started_for_session(sessions_col.database, session)
                    continue

                existing_hash = generate_full_checksum(existing_session)
                if existing_hash == current_hash:
                    update_set: Dict[str, Any] = {"last_checked": now_utc, "scrape_status": "OK"}
                    if event_id and not existing_session.get("event_id"):
                        update_set["event_id"] = event_id
                    await sessions_col.update_one({"_id": session_composite_id}, {"$set": update_set})
                    stats["unchanged"] += 1
                else:
                    session["_id"] = session_composite_id
                    session["original_checksum"] = existing_hash
                    await review_col.update_one({"_id": session_composite_id}, {"$set": session}, upsert=True)
                    stats["sent_to_review"] += 1

    if event_id_backfill_ops:
        await sessions_col.bulk_write(event_id_backfill_ops, ordered=False)

    if hill_ops_dict:
        await bulk_record_hill_versions(db, list(hill_ops_dict.values()))

    if athlete_ops_dict:
        ath_ops = [
            UpdateOne(
                {"fis_code": fc},
                {
                    "$setOnInsert": {k: v for k, v in doc.items() if k not in ("last_active_season", "status")},
                    "$set": {"last_active_season": doc.get("last_active_season"), "status": "Active"},
                },
                upsert=True,
            )
            for fc, doc in athlete_ops_dict.items()
        ]
        await ath_col.bulk_write(ath_ops, ordered=False)

    return stats


class ResultsScraperJobManager:
    """
    Zarządza zadaniami scrapowania wyników FIS w tle (zarówno pełnego / tylko brakujących
    z formularza, jak i chirurgicznego wznawiania brakujących sesji / błędów 429 z bazy),
    dostarczając na żywo dane dla paska postępu w panelu administratora.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.is_running: bool = False
        self.stop_requested: bool = False
        self.job_type: str = "idle"  # "scrape" | "retry_missing"
        self.started_at: Optional[datetime.datetime] = None
        self.finished_at: Optional[datetime.datetime] = None
        self.current_season: str = ""
        self.current_category: str = ""
        self.current_item: str = ""
        self.combination_index: int = 0
        self.total_combinations: int = 0
        self.events_done: int = 0
        self.events_total: int = 0
        self.races_done: int = 0
        self.races_total: int = 0
        self.inserted: int = 0
        self.sent_to_review: int = 0
        self.unchanged: int = 0
        self.skipped_complete: int = 0
        self.recovered_missing: int = 0
        self.failed_429: int = 0
        self.failed_other: int = 0
        self.last_error: Optional[str] = None
        self.last_message: Optional[str] = None

    def _reset_stats(self, job_type: str):
        with self._lock:
            self.is_running = True
            self.stop_requested = False
            self.job_type = job_type
            self.started_at = datetime.datetime.now()
            self.finished_at = None
            self.current_season = ""
            self.current_category = ""
            self.current_item = ""
            self.combination_index = 0
            self.total_combinations = 0
            self.events_done = 0
            self.events_total = 0
            self.races_done = 0
            self.races_total = 0
            self.inserted = 0
            self.sent_to_review = 0
            self.unchanged = 0
            self.skipped_complete = 0
            self.recovered_missing = 0
            self.failed_429 = 0
            self.failed_other = 0
            self.last_error = None
            self.last_message = None

    def mark_started(self, job_type: str, total_combinations: int = 0):
        self._reset_stats(job_type)
        with self._lock:
            self.total_combinations = total_combinations

    def stop(self):
        with self._lock:
            if self.is_running:
                self.stop_requested = True
                self.last_message = "Trwa bezpieczne zatrzymywanie procesu..."
                logger.info("Zażądano zatrzymania zadania scrapowania wyników.")

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            pct = 0.0
            if self.job_type == "retry_missing":
                if self.races_total > 0:
                    pct = round((self.races_done / self.races_total) * 100, 1)
                elif not self.is_running and self.finished_at:
                    pct = 100.0
            elif self.total_combinations > 0:
                completed_combos = max(0, self.combination_index - 1)
                event_frac = (self.events_done / self.events_total) if self.events_total > 0 else 0.0
                if not self.is_running and self.finished_at and not self.stop_requested:
                    pct = 100.0
                else:
                    pct = round(((completed_combos + event_frac) / self.total_combinations) * 100, 1)
                    pct = min(100.0, max(0.0, pct))

            elapsed_sec = 0
            if self.started_at:
                end_t = self.finished_at or datetime.datetime.now()
                elapsed_sec = int((end_t - self.started_at).total_seconds())

            return {
                "is_running": self.is_running,
                "stop_requested": self.stop_requested,
                "job_type": self.job_type,
                "current_season": self.current_season,
                "current_category": self.current_category,
                "current_item": self.current_item,
                "combination_index": self.combination_index,
                "total_combinations": self.total_combinations,
                "events_done": self.events_done,
                "events_total": self.events_total,
                "races_done": self.races_done,
                "races_total": self.races_total,
                "percent_complete": pct,
                "inserted": self.inserted,
                "sent_to_review": self.sent_to_review,
                "unchanged": self.unchanged,
                "skipped_complete": self.skipped_complete,
                "recovered_missing": self.recovered_missing,
                "failed_429": self.failed_429,
                "failed_other": self.failed_other,
                "elapsed_seconds": elapsed_sec,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "last_error": self.last_error,
                "last_message": self.last_message,
                # Zgodność ze strukturą stats w Frontendzie
                "stats": {
                    "combinations_processed": self.combination_index,
                    "inserted": self.inserted,
                    "unchanged": self.unchanged,
                    "sent_to_review": self.sent_to_review,
                    "skipped_complete": self.skipped_complete,
                    "recovered_missing": self.recovered_missing,
                    "failed_429": self.failed_429,
                },
            }

    def _on_pipeline_progress(self, payload: Dict[str, Any]):
        evt = payload.get("type")
        with self._lock:
            if evt == "calendar_loaded":
                self.events_total = int(payload.get("total_events") or 0)
                self.events_done = 0
                self.current_item = f"Załadowano kalendarz ({self.events_total} eventów)"
            elif evt == "event_started":
                self.current_item = f"Event: {payload.get('place', '')}"
            elif evt == "event_skipped":
                self.events_done += 1
                self.skipped_complete += 1
                self.current_item = f"Pominięto kompletny event: {payload.get('place', '')}"
            elif evt == "event_done":
                self.events_done += 1
            elif evt == "race_skipped":
                self.skipped_complete += 1
            elif evt == "race_fetched":
                self.races_done += 1
            elif evt == "race_429":
                self.races_done += 1
                self.failed_429 += 1
                self.current_item = f"⚠️ Limit FIS 429: {payload.get('place', '')} — {payload.get('title', '')}"

    async def _build_complete_sets(
        self,
        db: AsyncIOMotorDatabase,
        seasons: List[str],
        categories: List[str],
    ):
        sessions_col = db["sessions"]
        existing_complete_ids = set()
        existing_event_ids_by_session: Dict[str, Optional[str]] = {}
        complete_event_ids = set()

        # 1. Wszystkie kompletne sesje (niezależnie od kategorii, bo _id = raceId_codex jest unikalne)
        complete_cursor = sessions_col.find(
            {
                "season": {"$in": seasons},
                "$or": [
                    {"results_data.results": {"$exists": True, "$type": "array", "$ne": []}},
                    {"is_cancelled": True},
                    {"results_data.is_cancelled": True},
                ],
            },
            {"_id": 1, "event_id": 1},
        )
        async for doc in complete_cursor:
            sid = doc["_id"]
            existing_complete_ids.add(sid)
            existing_event_ids_by_session[sid] = doc.get("event_id")

        # 2. Eventy zakończone (>10 dni temu), w których wszystkie sesje w bazie są kompletne
        cutoff_date = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        event_stats: Dict[str, Dict[str, Any]] = {}
        ev_cursor = sessions_col.find(
            {
                "season": {"$in": seasons},
                "category_code": {"$in": categories},
                "event_id": {"$exists": True, "$nin": [None, ""]},
            },
            {"event_id": 1, "date": 1, "results_data.results": 1, "is_cancelled": 1, "results_data.is_cancelled": 1, "scrape_status": 1},
        )
        async for doc in ev_cursor:
            eid = str(doc["event_id"])
            st = event_stats.setdefault(eid, {"complete": 0, "incomplete": 0, "max_date": ""})
            d_str = str(doc.get("date") or "")[:10]
            if d_str and d_str > st["max_date"]:
                st["max_date"] = d_str
            if doc.get("scrape_status") in ("FAILED_429", "FAILED_ERROR") or not _has_complete_results(doc):
                st["incomplete"] += 1
            else:
                st["complete"] += 1

        for eid, st in event_stats.items():
            if st["complete"] > 0 and st["incomplete"] == 0 and st["max_date"] and st["max_date"] < cutoff_date:
                complete_event_ids.add(eid)

        return existing_complete_ids, existing_event_ids_by_session, complete_event_ids

    async def run_results_scrape(
        self,
        db: AsyncIOMotorDatabase,
        seasons: List[str],
        categories: List[str],
        mode: str = "IND",
        only_missing: bool = True,
    ):
        try:
            combinations = [(s, c) for s in seasons for c in categories]
            with self._lock:
                self.total_combinations = len(combinations)

            existing_complete_ids = set()
            existing_event_ids_by_session: Dict[str, Optional[str]] = {}
            complete_event_ids = set()

            if only_missing:
                existing_complete_ids, existing_event_ids_by_session, complete_event_ids = await self._build_complete_sets(
                    db, seasons, categories
                )
                print(
                    f"⚡ [Tryb Tylko Brakujące] Kompletnych sesji: {len(existing_complete_ids)}, "
                    f"kompletnych eventów do pełnego pominięcia: {len(complete_event_ids)}"
                )

            for idx, (season, category) in enumerate(combinations, start=1):
                if self.stop_requested:
                    break

                with self._lock:
                    self.combination_index = idx
                    self.current_season = season
                    self.current_category = category
                    self.events_done = 0
                    self.events_total = 0
                    self.current_item = f"Pobieranie kalendarza {season} ({category})..."

                try:
                    scraped_events = await asyncio.to_thread(
                        run_scraper_pipeline,
                        seasoncode=season,
                        categorycode=category,
                        gendercode="M",
                        limit_events=None,
                        mode=mode,
                        existing_complete_ids=existing_complete_ids if only_missing else None,
                        existing_complete_event_ids=complete_event_ids if only_missing else None,
                        progress_callback=self._on_pipeline_progress,
                        stop_check=lambda: self.stop_requested,
                    )
                except Exception as e:
                    logger.warning(f"Błąd pobierania ({season}, {category}): {e}")
                    with self._lock:
                        self.last_error = str(e)
                    continue

                if not scraped_events:
                    continue

                # Zapis do bazy (uwaga: skipped_complete z race_skipped zliczamy już z DB stats lub z eventów)
                batch_stats = await save_scraped_events_to_db(
                    db=db,
                    scraped_events=scraped_events,
                    season=season,
                    category=category,
                    only_missing=only_missing,
                    existing_event_ids_by_session=existing_event_ids_by_session if only_missing else None,
                )

                with self._lock:
                    self.inserted += batch_stats["inserted"]
                    self.sent_to_review += batch_stats["sent_to_review"]
                    self.unchanged += batch_stats["unchanged"]
                    self.recovered_missing += batch_stats["recovered_missing"]
                    self.failed_429 += batch_stats["failed_429"]
                    self.failed_other += batch_stats["failed_other"]
                    # Uzupełniamy ewentualne pominięte kompletne eventy z batch_stats, jeśli nie były zliczone live
                    if self.skipped_complete == 0 and batch_stats.get("skipped_complete", 0) > 0:
                        self.skipped_complete = batch_stats["skipped_complete"]

            with self._lock:
                self.finished_at = datetime.datetime.now()
                self.combination_index = self.total_combinations
                if self.stop_requested:
                    self.current_item = "Zatrzymano przez użytkownika"
                    self.last_message = "Scrapowanie zostało bezpiecznie zatrzymane przez administratora."
                else:
                    self.current_item = "Zakończono pomyślnie"
                    tot_comp = self.skipped_complete + self.unchanged
                    tot_new = self.inserted + self.recovered_missing
                    self.last_message = (
                        f"Zakończono scrapowanie! Nowych/uzupełnionych: {tot_new}, pominiętych kompletnych: {tot_comp}."
                    )
        except Exception as e:
            logger.error(f"Krytyczny błąd zadania scrapowania: {e}", exc_info=True)
            with self._lock:
                self.last_error = str(e)
                self.finished_at = datetime.datetime.now()
        finally:
            with self._lock:
                self.is_running = False
                self.stop_requested = False

    async def run_retry_missing(
        self,
        db: AsyncIOMotorDatabase,
        season: Optional[str] = None,
        only_429: bool = False,
    ):
        """
        Chirurgiczne dociąganie WYŁĄCZNIE brakujących sesji lub sesji z błędem 429,
        które już istnieją w bazie (bez pobierania kalendarzy i kompletnych eventów z FIS!).
        """
        try:
            sessions_col = db["sessions"]
            review_col = db["sessions_review"]
            query: Dict[str, Any] = {
                "raceUrl": {"$exists": True, "$nin": [None, ""]},
                "is_cancelled": {"$ne": True},
                "results_data.is_cancelled": {"$ne": True},
            }
            if season:
                query["season"] = season
            if only_429:
                query["scrape_status"] = "FAILED_429"
            else:
                query["$or"] = [
                    {"results_data": None},
                    {"results_data.results": []},
                    {"results_data.results": {"$exists": False}},
                    {"scrape_status": {"$in": ["FAILED_429", "FAILED_ERROR"]}},
                ]

            missing_docs = await sessions_col.find(query).sort([("season", -1), ("date", 1)]).to_list(1000)

            with self._lock:
                self.races_total = len(missing_docs)
                self.total_combinations = 1
                self.combination_index = 1
                self.current_season = season or "Wszystkie sezony"
                self.current_category = "429" if only_429 else "Brakujące"

            if not missing_docs:
                with self._lock:
                    self.finished_at = datetime.datetime.now()
                    self.last_message = "Brak brakujących sesji do pobrania dla wybranych kryteriów!"
                return

            ws = get_weather_service()
            hill_ops_dict: Dict[str, Dict[str, Any]] = {}
            athlete_ops_dict: Dict[int, Dict[str, Any]] = {}

            for doc in missing_docs:
                if self.stop_requested:
                    break

                s_id = doc["_id"]
                s_place = doc.get("event_place") or "UNKNOWN"
                s_country = doc.get("event_country") or "UNKNOWN"
                s_season = str(doc.get("season") or "")
                s_title = doc.get("title") or "Konkurs"

                with self._lock:
                    self.current_season = s_season
                    self.current_category = doc.get("category_code") or ""
                    self.current_item = f"{s_place}: {s_title}"

                race_payload = {
                    "raceUrl": doc.get("raceUrl"),
                    "title": s_title,
                    "raceId": doc.get("raceId"),
                    "codex": doc.get("codex"),
                    "date": doc.get("date"),
                    "time": doc.get("time"),
                }

                # Odpytujemy pojedynczy wyścig w wątku (z globalnym rate limiterem w http_client)
                completed = await asyncio.to_thread(process_single_race, race_payload, lambda: self.stop_requested)
                scrape_error = completed.get("_scrape_error")
                now_utc = datetime.datetime.utcnow()

                if scrape_error:
                    status_code = "FAILED_429" if scrape_error == "HTTP_429" else "FAILED_ERROR"
                    await sessions_col.update_one(
                        {"_id": s_id},
                        {
                            "$set": {
                                "scrape_status": status_code,
                                "scrape_error": scrape_error,
                                "scrape_error_at": now_utc,
                                "last_checked": now_utc,
                            }
                        },
                    )
                    with self._lock:
                        if status_code == "FAILED_429":
                            self.failed_429 += 1
                        else:
                            self.failed_other += 1
                        self.races_done += 1
                    continue

                # Sukces lub formalne odwołanie przez jury FIS
                results_data = completed.get("results_data")
                is_canc = bool(completed.get("is_cancelled"))
                update_fields: Dict[str, Any] = {
                    "results_data": results_data,
                    "is_cancelled": is_canc,
                    "scrape_status": "OK",
                    "last_checked": now_utc,
                }
                if completed.get("hillSize"):
                    update_fields["hillSize"] = completed["hillSize"]
                    update_fields["hill_size_unit"] = completed.get("hill_size_unit") or "HS"
                    update_fields["hillType"] = completed.get("hillType")
                elif not doc.get("hillSize"):
                    resolved_hill = await resolve_event_hill_data(db, [doc], s_place, s_country)
                    if resolved_hill and resolved_hill.get("size"):
                        update_fields["hillSize"] = resolved_hill["size"]
                        update_fields["hill_size_unit"] = resolved_hill.get("unit") or "HS"
                        if resolved_hill.get("type"):
                            update_fields["hillType"] = resolved_hill["type"]

                eff_hs = update_fields.get("hillSize") or doc.get("hillSize")
                if eff_hs and s_place != "UNKNOWN":
                    h_unit = update_fields.get("hill_size_unit") or doc.get("hill_size_unit") or "HS"
                    hill_ops_dict[f"{s_place.upper()}_{h_unit}{eff_hs}"] = {
                        "place": s_place,
                        "country_code": s_country,
                        "size": eff_hs,
                        "unit": h_unit,
                        "type": update_fields.get("hillType") or doc.get("hillType"),
                        "last_session_date": doc.get("date"),
                        "last_season": s_season,
                    }

                if not doc.get("weather") and doc.get("date") and s_place != "UNKNOWN":
                    try:
                        w_data = await asyncio.to_thread(
                            ws.get_weather, s_place, doc.get("date"), doc.get("time", "16:00")
                        )
                        if w_data:
                            update_fields["weather"] = w_data
                    except Exception:
                        pass

                for ath in (results_data or {}).get("results", []):
                    fc = ath.get("fis_code")
                    if fc:
                        try:
                            fc_int = int(fc)
                            athlete_ops_dict[fc_int] = {
                                "fis_code": fc_int,
                                "full_name": ath.get("name"),
                                "country_code": ath.get("country"),
                                "status": "Active",
                                "birth_year": ath.get("year"),
                                "last_active_season": s_season,
                            }
                        except ValueError:
                            pass

                merged_doc = {**doc, **update_fields}
                update_fields["checksum"] = generate_full_checksum(merged_doc)

                await sessions_col.update_one(
                    {"_id": s_id},
                    {
                        "$set": update_fields,
                        "$unset": {"scrape_error": "", "scrape_error_at": ""},
                    },
                )
                await review_col.delete_one({"_id": s_id})

                with self._lock:
                    self.recovered_missing += 1
                    self.inserted += 1
                    self.races_done += 1

            if hill_ops_dict:
                await bulk_record_hill_versions(db, list(hill_ops_dict.values()))
            if athlete_ops_dict:
                ath_ops = [
                    UpdateOne(
                        {"fis_code": fc},
                        {
                            "$setOnInsert": {k: v for k, v in d.items() if k not in ("last_active_season", "status")},
                            "$set": {"last_active_season": d.get("last_active_season"), "status": "Active"},
                        },
                        upsert=True,
                    )
                    for fc, d in athlete_ops_dict.items()
                ]
                await db["athletes"].bulk_write(ath_ops, ordered=False)

            with self._lock:
                self.finished_at = datetime.datetime.now()
                if self.stop_requested:
                    self.current_item = "Zatrzymano przez użytkownika"
                    self.last_message = f"Dociąganie przerwane na żądanie. Uzupełniono: {self.recovered_missing}."
                else:
                    self.current_item = "Zakończono pomyślnie"
                    self.last_message = (
                        f"Dociąganie brakujących zakończone! Uzupełniono: {self.recovered_missing}, "
                        f"błędy 429: {self.failed_429}."
                    )
        except Exception as e:
            logger.error(f"Błąd wznawiania brakujących sesji: {e}", exc_info=True)
            with self._lock:
                self.last_error = str(e)
                self.finished_at = datetime.datetime.now()
        finally:
            with self._lock:
                self.is_running = False
                self.stop_requested = False


_results_job_manager: Optional[ResultsScraperJobManager] = None


def get_results_job_manager() -> ResultsScraperJobManager:
    global _results_job_manager
    if _results_job_manager is None:
        _results_job_manager = ResultsScraperJobManager()
    return _results_job_manager
