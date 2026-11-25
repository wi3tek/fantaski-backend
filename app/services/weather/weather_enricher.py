import asyncio
import datetime
import logging
from typing import Dict, Any, Optional, List
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.services.weather.weather_service import get_weather_service

logger = logging.getLogger(__name__)


class WeatherEnricher:
    """
    Serwis asynchronicznego uzupełniania danych pogodowych dla sesji w bazie MongoDB.
    Wykorzystuje Open-Meteo Archive API oraz inteligentny cache po (miejscowość, data),
    co pozwala obsłużyć cały weekend zawodów jednym zapytaniem.
    """
    def __init__(self):
        self.is_running: bool = False
        self.stop_requested: bool = False
        self.current_db_name: str = ""
        self.total_sessions: int = 0
        self.processed_sessions: int = 0
        self.updated_sessions: int = 0
        self.failed_sessions: int = 0
        self.current_item: str = ""
        self.started_at: Optional[datetime.datetime] = None
        self.finished_at: Optional[datetime.datetime] = None
        self.last_error: Optional[str] = None

    def get_status(self) -> Dict[str, Any]:
        pct = 0.0
        if self.total_sessions > 0:
            pct = round((self.processed_sessions / self.total_sessions) * 100, 1)

        return {
            "is_running": self.is_running,
            "stop_requested": self.stop_requested,
            "db_name": self.current_db_name,
            "total_sessions": self.total_sessions,
            "processed_sessions": self.processed_sessions,
            "updated_sessions": self.updated_sessions,
            "failed_sessions": self.failed_sessions,
            "percent_complete": pct,
            "current_item": self.current_item,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "last_error": self.last_error
        }

    def stop(self):
        if self.is_running:
            self.stop_requested = True
            logger.info("Zażądano zatrzymania wzbogacania bazy pogodą.")

    async def get_db_stats(self, db: AsyncIOMotorDatabase) -> Dict[str, Any]:
        """Zwraca statystyki pokrycia pogodą dla danej bazy."""
        total = await db["sessions"].count_documents({"date": {"$ne": None}})
        with_weather = await db["sessions"].count_documents({"weather": {"$exists": True, "$ne": None}})
        without_weather = total - with_weather
        pct = round((with_weather / total * 100), 1) if total > 0 else 0.0

        return {
            "db_name": db.name,
            "total_sessions": total,
            "with_weather": with_weather,
            "without_weather": without_weather,
            "coverage_pct": pct
        }

    async def enrich_database(
        self,
        db: AsyncIOMotorDatabase,
        force: bool = False,
        delay_between_calls: float = 0.06
    ) -> Dict[str, Any]:
        """
        Główna pętla wzbogacająca sesje o historyczne warunki pogodowe z Open-Meteo Archive API.
        """
        if self.is_running:
            return {"status": "error", "message": "Zadanie wzbogacania bazy jest już uruchomione."}

        self.is_running = True
        self.stop_requested = False
        self.current_db_name = db.name
        self.started_at = datetime.datetime.now()
        self.finished_at = None
        self.last_error = None
        self.processed_sessions = 0
        self.updated_sessions = 0
        self.failed_sessions = 0

        weather_service = get_weather_service()
        # Lokalny cache w pamięci RAM per (place, date, hour)
        weather_cache: Dict[str, Dict[str, Any]] = {}

        try:
            query: Dict[str, Any] = {"date": {"$ne": None}, "event_place": {"$ne": None}}
            if not force:
                query["$or"] = [{"weather": {"$exists": False}}, {"weather": None}]

            cursor = db["sessions"].find(
                query,
                {"_id": 1, "event_place": 1, "date": 1, "time": 1, "hillSize": 1, "title": 1}
            ).sort([("date", -1), ("time", -1)])

            sessions_to_process = await cursor.to_list(length=10000)
            self.total_sessions = len(sessions_to_process)

            logger.info(f"Rozpoczynanie wzbogacania {self.total_sessions} sesji w bazie {db.name}...")

            for session in sessions_to_process:
                if self.stop_requested:
                    logger.info("Wzbogacanie bazy zostało zatrzymane przez użytkownika.")
                    break

                s_id = session.get("_id")
                place = str(session.get("event_place") or "").strip()
                date_str = str(session.get("date") or "").strip()[:10]
                time_str = str(session.get("time") or "16:00").strip()

                if not place or place.upper() == "UNKNOWN" or not date_str:
                    self.processed_sessions += 1
                    continue

                self.current_item = f"{place} ({date_str} {time_str})"
                cache_key = f"{place.lower()}_{date_str}_{time_str[:2]}"

                weather_data = None
                if cache_key in weather_cache:
                    weather_data = weather_cache[cache_key]
                else:
                    try:
                        # Pobranie synchroniczne z Open-Meteo Archive API
                        weather_data = await asyncio.to_thread(
                            weather_service.get_weather,
                            place=place,
                            date_str=date_str,
                            time_str=time_str
                        )
                        if weather_data:
                            weather_cache[cache_key] = weather_data
                            # Krótkie opóźnienie, aby nie przekroczyć limitów API
                            await asyncio.sleep(delay_between_calls)
                    except Exception as e:
                        logger.warning(f"Błąd pobierania pogody dla {place} ({date_str}): {e}")
                        self.failed_sessions += 1

                if weather_data:
                    try:
                        await db["sessions"].update_one(
                            {"_id": s_id},
                            {"$set": {"weather": weather_data}}
                        )
                        self.updated_sessions += 1
                    except Exception as e:
                        logger.error(f"Błąd zapisu pogody do sesji {s_id}: {e}")
                        self.failed_sessions += 1

                self.processed_sessions += 1

            self.finished_at = datetime.datetime.now()
            logger.info(
                f"Ukończono wzbogacanie bazy {db.name}: zaktualizowano {self.updated_sessions}/{self.total_sessions} sesji."
            )
            return self.get_status()

        except Exception as e:
            self.last_error = str(e)
            logger.error(f"Nieoczekiwany błąd enrichera: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}
        finally:
            self.is_running = False
            self.stop_requested = False


_enricher_instance: Optional[WeatherEnricher] = None

def get_weather_enricher() -> WeatherEnricher:
    global _enricher_instance
    if _enricher_instance is None:
        _enricher_instance = WeatherEnricher()
    return _enricher_instance
