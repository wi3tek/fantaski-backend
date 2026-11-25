import asyncio
import datetime
import logging
import random
from typing import Dict, Any, List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateOne

from app.services.scrapers.generate_results import run_scraper_pipeline
from app.services.weather.weather_service import get_weather_service
from app.services.hill_service import resolve_event_hill_data, bulk_record_hill_versions
import hashlib
import json

logger = logging.getLogger(__name__)


def generate_full_checksum(data: Any) -> str:
    """Generuje hash MD5 ze zrzutu JSON słownika z pominięciem pól systemowych."""
    if isinstance(data, dict):
        clean_data = {
            k: v for k, v in data.items()
            if k not in [
                "checksum", "last_checked", "_id", "review_reason",
                "original_checksum", "weather", "scrape_status",
                "scrape_error", "scrape_error_at", "event_id"
            ]
        }
    else:
        clean_data = data
    data_str = json.dumps(clean_data, sort_keys=True, default=str)
    return hashlib.md5(data_str.encode("utf-8")).hexdigest()


class NightlyBatchScraper:
    """
    Silnik nocnego, powolnego przetwarzania i synchronizacji bazy skoków FIS.
    Działa w tle z kontrolowanym opóźnieniem (throttle delay) między zapytaniami,
    co zapobiega błędom 429 Too Many Requests i pozwala bezpiecznie pobrać całą bazę przez noc.
    Automatycznie wzbogaca każdą nową/zaktualizowaną sesję o historyczne dane pogodowe.
    """
    def __init__(self):
        self.is_running: bool = False
        self.stop_requested: bool = False
        self.started_at: Optional[datetime.datetime] = None
        self.finished_at: Optional[datetime.datetime] = None
        self.current_season: str = ""
        self.current_category: str = ""
        self.current_item: str = ""
        self.current_combination_idx: int = 0
        self.total_combinations: int = 0
        self.inserted_sessions: int = 0
        self.updated_sessions: int = 0
        self.unchanged_sessions: int = 0
        self.weather_attached_count: int = 0
        self.failed_combinations: int = 0
        self.last_error: Optional[str] = None
        self.last_message: Optional[str] = None
        self.delay_seconds: float = 1.8

    def _on_pipeline_progress(self, payload: Dict[str, Any]):
        evt = payload.get("type")
        if evt == "calendar_loaded":
            self.current_item = f"Kalendarz: {payload.get('total_events')} eventów"
        elif evt == "event_started":
            self.current_item = f"Event: {payload.get('place', '')}"
        elif evt == "event_skipped":
            self.current_item = f"Pominięto kompletny: {payload.get('place', '')}"
        elif evt == "race_fetched":
            self.current_item = f"{payload.get('place', '')} - {payload.get('title', '')}"

    def get_status(self) -> Dict[str, Any]:
        pct = 0.0
        if self.total_combinations > 0:
            pct = round((self.current_combination_idx / self.total_combinations) * 100, 1)

        elapsed_sec = 0
        if self.started_at:
            end_t = self.finished_at or datetime.datetime.now()
            elapsed_sec = int((end_t - self.started_at).total_seconds())

        return {
            "is_running": self.is_running,
            "stop_requested": self.stop_requested,
            "current_season": self.current_season,
            "current_category": self.current_category,
            "current_item": self.current_item,
            "progress_index": self.current_combination_idx,
            "total_combinations": self.total_combinations,
            "percent_complete": pct,
            "inserted_sessions": self.inserted_sessions,
            "updated_sessions": self.updated_sessions,
            "unchanged_sessions": self.unchanged_sessions,
            "weather_attached_count": self.weather_attached_count,
            "failed_combinations": self.failed_combinations,
            "delay_seconds": self.delay_seconds,
            "elapsed_seconds": elapsed_sec,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "last_error": self.last_error,
            "last_message": self.last_message,
        }

    def stop(self):
        if self.is_running:
            self.stop_requested = True
            self.last_message = "Trwa bezpieczne zatrzymywanie nocnego przetwarzania..."
            logger.info("Zażądano zatrzymania nocnego przetwarzania.")

    async def run_nightly_scrape(
        self,
        db: AsyncIOMotorDatabase,
        seasons: List[str],
        categories: List[str],
        delay_seconds: float = 1.8,
        mode: str = "IND"
    ) -> Dict[str, Any]:
        if self.is_running:
            return {"status": "error", "message": "Nocne przetwarzanie jest już w toku."}

        self.is_running = True
        self.stop_requested = False
        self.started_at = datetime.datetime.now()
        self.finished_at = None
        self.last_error = None
        self.delay_seconds = max(0.5, delay_seconds)

        self.current_combination_idx = 0
        self.inserted_sessions = 0
        self.updated_sessions = 0
        self.unchanged_sessions = 0
        self.weather_attached_count = 0
        self.failed_combinations = 0

        sessions_col = db["sessions"]
        athletes_col = db["athletes"]
        weather_service = get_weather_service()

        combinations = []
        for s in seasons:
            for c in categories:
                combinations.append((str(s), str(c)))

        self.total_combinations = len(combinations)
        logger.info(f"Rozpoczynanie nocnego przetwarzania: {self.total_combinations} kombinacji (Sezony={seasons}, Kategorie={categories})...")

        try:
            for season, category in combinations:
                if self.stop_requested:
                    logger.info("Nocne przetwarzanie przerwane na żądanie użytkownika.")
                    break

                self.current_season = season
                self.current_category = category
                self.current_combination_idx += 1

                try:
                    # Wywołanie potoku scrapowania w osobnym wątku
                    scraped_events = await asyncio.to_thread(
                        run_scraper_pipeline,
                        seasoncode=season,
                        categorycode=category,
                        gendercode="M",
                        limit_events=None,
                        mode=mode,
                        progress_callback=self._on_pipeline_progress,
                        stop_check=lambda: self.stop_requested,
                    )
                except Exception as e:
                    logger.warning(f"Błąd scrapowania ({season}, {category}): {e}")
                    self.failed_combinations += 1
                    # Krótki odpoczynek przy błędzie sieci
                    await asyncio.sleep(self.delay_seconds * 2)
                    continue

                if not scraped_events:
                    # Throttling
                    await asyncio.sleep(self.delay_seconds)
                    continue

                hill_ops_dict = {}
                athlete_ops_dict = {}

                for event in scraped_events:
                    if self.stop_requested:
                        break

                    event_id = str(event.get("id") or "")
                    place = event.get("place", "UNKNOWN")
                    country = event.get("country", "UNKNOWN")
                    event_sessions = event.get("sessions", [])

                    # Rozwiązanie brakujących parametrów skoczni
                    resolved_hill = None
                    if any(not s.get("hillSize") and not s.get("_scrape_error") for s in event_sessions):
                        resolved_hill = await resolve_event_hill_data(db, event_sessions, place, country)

                    for session in event_sessions:
                        scrape_error = session.pop("_scrape_error", None)

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
                            hill_ops_dict[hill_id] = entry

                        codex = session.get("codex")
                        race_id = session.get("raceId")
                        if not codex or not race_id:
                            continue

                        session_composite_id = f"{race_id}_{codex}"

                        # Skoczkowie startujący w sesji
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
                                        "last_active_season": season
                                    }
                                except ValueError:
                                    pass

                        session["event_place"] = place
                        session["event_country"] = country
                        session["season"] = season
                        session["category_code"] = category
                        if event_id:
                            session["event_id"] = event_id

                        now_utc = datetime.datetime.utcnow()
                        if scrape_error:
                            session["scrape_status"] = "FAILED_429" if scrape_error == "HTTP_429" else "FAILED_ERROR"
                            session["scrape_error"] = scrape_error
                            session["scrape_error_at"] = now_utc
                        else:
                            session["scrape_status"] = "OK"

                        # WZBOGACENIE O WARUNKI POGODOWE JEŚLI BRAK
                        if not scrape_error and not session.get("weather") and session.get("date") and place != "UNKNOWN":
                            try:
                                w_data = await asyncio.to_thread(
                                    weather_service.get_weather,
                                    place=place,
                                    date_str=session.get("date"),
                                    time_str=session.get("time", "16:00")
                                )
                                if w_data:
                                    session["weather"] = w_data
                                    self.weather_attached_count += 1
                            except Exception as e:
                                logger.debug(f"Pominięto pogodę dla sesji {session_composite_id}: {e}")

                        current_hash = generate_full_checksum(session)
                        session["checksum"] = current_hash
                        session["last_checked"] = now_utc

                        existing = await sessions_col.find_one({"_id": session_composite_id})
                        if not existing:
                            session["_id"] = session_composite_id
                            await sessions_col.insert_one(session)
                            self.inserted_sessions += 1
                        else:
                            existing_has_res = bool(
                                ((existing.get("results_data") or {}).get("results"))
                                or existing.get("is_cancelled") is True
                                or (existing.get("results_data") or {}).get("is_cancelled") is True
                            )
                            # Nie nadpisuj kompletnych wyników błędem 429/sieci!
                            if scrape_error and existing_has_res:
                                await sessions_col.update_one(
                                    {"_id": session_composite_id},
                                    {"$set": {"last_checked": now_utc}}
                                )
                                self.unchanged_sessions += 1
                                continue

                            existing_hash = generate_full_checksum(existing)
                            if existing_hash == current_hash and not scrape_error:
                                # Uzupełnij pogodę i event_id jeśli nie było w istniejącej
                                update_fields: Dict[str, Any] = {"last_checked": now_utc, "scrape_status": "OK"}
                                if not existing.get("weather") and session.get("weather"):
                                    update_fields["weather"] = session["weather"]
                                if event_id and not existing.get("event_id"):
                                    update_fields["event_id"] = event_id
                                await sessions_col.update_one({"_id": session_composite_id}, {"$set": update_fields})
                                self.unchanged_sessions += 1
                            else:
                                session["_id"] = session_composite_id
                                await sessions_col.replace_one({"_id": session_composite_id}, session)
                                self.updated_sessions += 1

                # Zapis wersji skoczni
                if hill_ops_dict:
                    await bulk_record_hill_versions(db, list(hill_ops_dict.values()))

                # Rejestracja zawodników
                if athlete_ops_dict:
                    ath_writes = [
                        UpdateOne(
                            {"fis_code": f_code},
                            {
                                "$setOnInsert": {k: v for k, v in ath_data.items() if k not in ("last_active_season", "status")},
                                "$set": {"last_active_season": ath_data.get("last_active_season"), "status": "Active"},
                            },
                            upsert=True
                        )
                        for f_code, ath_data in athlete_ops_dict.items()
                    ]
                    await athletes_col.bulk_write(ath_writes)

                # Nocny Throttling: losowe opóźnienie 1.5 - 2.5s między kombinacjami
                jitter = random.uniform(0.8, 1.3)
                await asyncio.sleep(self.delay_seconds * jitter)

            self.finished_at = datetime.datetime.now()
            if self.stop_requested:
                self.last_message = "Nocne przetwarzanie przerwane na żądanie użytkownika."
            else:
                self.last_message = (
                    f"Nocne przetwarzanie zakończone! Wstawiono: {self.inserted_sessions}, "
                    f"zaktualizowano: {self.updated_sessions}, dołączono pogodę do {self.weather_attached_count} sesji."
                )
            logger.info(self.last_message)
            return self.get_status()

        except Exception as e:
            self.last_error = str(e)
            logger.error(f"Krytyczny błąd nocnego przetwarzania: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}
        finally:
            self.is_running = False
            self.stop_requested = False


_nightly_scraper_instance: Optional[NightlyBatchScraper] = None

def get_nightly_batch_scraper() -> NightlyBatchScraper:
    global _nightly_scraper_instance
    if _nightly_scraper_instance is None:
        _nightly_scraper_instance = NightlyBatchScraper()
    return _nightly_scraper_instance
