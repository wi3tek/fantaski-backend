# app/scripts/setup_indexes.py
"""
Skrypt automatycznego tworzenia optymalnych indeksów w MongoDB.
Zapewnia błyskawiczne zapytania dla kontrolerów:
- athletes (wyszukiwanie po fis_code, full_name, filtracja po statusie i kraju)
- sessions (filtrowanie po sezonie, typie sesji, dacie)
- hills (wyszukiwanie po miejscowości i kraju)

Uruchomienie:
    python app/scripts/setup_indexes.py
"""

import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings
import pymongo


async def _safe_index(col, keys, **kwargs):
    try:
        await col.create_index(keys, **kwargs)
    except Exception as exc:
        # Kod 85/86 = ten sam indeks istnieje już w bazie pod inną nazwą
        if getattr(exc, "code", None) in (85, 86) or "already exists" in str(exc).lower():
            return
        raise


async def ensure_all_indexes(db):
    """Tworzy wszystkie kluczowe indeksy w MongoDB w sposób idempotentny i bezpieczny."""
    # 1. Kolekcja: users & powiadomienia & draft
    await _safe_index(db["users"], "email", unique=True)
    await _safe_index(db["users"], "created_at")
    await _safe_index(
        db["users"],
        [("esm_integration.connected", pymongo.ASCENDING), ("name", pymongo.ASCENDING)],
        name="idx_users_esm_name",
    )

    await _safe_index(
        db["draft_leagues"],
        [("participants.user_id", pymongo.ASCENDING), ("updated_at", pymongo.DESCENDING)],
        name="idx_draft_leagues_participant_updated",
    )
    await _safe_index(
        db["draft_leagues"],
        [("invite_code", pymongo.ASCENDING)],
        unique=True,
        sparse=True,
        name="idx_draft_leagues_invite_code",
    )
    await _safe_index(
        db["notifications"],
        [("user_id", pymongo.ASCENDING), ("is_read", pymongo.ASCENDING), ("created_at", pymongo.DESCENDING)],
        name="idx_notifications_user_read_created",
    )
    await _safe_index(
        db["notifications"],
        [("league_id", pymongo.ASCENDING)],
        name="idx_notifications_league_id",
    )

    # 2. Kolekcja: athletes
    await _safe_index(
        db["athletes"],
        [("fis_code", pymongo.ASCENDING)],
        unique=True,
        name="idx_athletes_fis_code_unique",
    )
    await _safe_index(db["athletes"], [("full_name", pymongo.ASCENDING)], name="idx_athletes_full_name")
    await _safe_index(db["athletes"], [("country_code", pymongo.ASCENDING)], name="idx_athletes_country")
    await _safe_index(db["athletes"], [("status", pymongo.ASCENDING)], name="idx_athletes_status")
    await _safe_index(db["athletes"], [("price", pymongo.DESCENDING)], name="idx_athletes_price")

    # 3. Kolekcja: sessions
    await _safe_index(
        db["sessions"],
        [("season", pymongo.ASCENDING), ("category_code", pymongo.ASCENDING), ("date", pymongo.DESCENDING)],
        name="idx_sessions_season_cat_date",
    )
    await _safe_index(
        db["sessions"],
        [("session_type", pymongo.ASCENDING), ("date", pymongo.DESCENDING)],
        name="idx_sessions_type_date",
    )
    await _safe_index(
        db["sessions"],
        [("trainings_processed", pymongo.ASCENDING)],
        name="idx_sessions_trainings_processed",
    )
    await _safe_index(db["sessions"], [("event_place", pymongo.ASCENDING)], name="idx_sessions_place")
    await _safe_index(
        db["sessions"],
        [("date", pymongo.DESCENDING), ("time", pymongo.DESCENDING)],
        name="idx_sessions_date_time",
    )
    await _safe_index(
        db["sessions"],
        [("results_data.results.fis_code", pymongo.ASCENDING)],
        name="idx_sessions_results_fis_code",
    )

    # 4. Kolekcja: sessions_review
    await _safe_index(
        db["sessions_review"],
        [("date", pymongo.DESCENDING)],
        name="idx_sessions_review_date",
    )

    # 5. Kolekcja: hills
    await _safe_index(db["hills"], [("place", pymongo.ASCENDING)], name="idx_hills_place")
    await _safe_index(db["hills"], [("country_code", pymongo.ASCENDING)], name="idx_hills_country")
    await _safe_index(db["hills"], [("is_current", pymongo.ASCENDING)], name="idx_hills_is_current")


async def create_all_indexes():
    print("=" * 60)
    print("⚡ TWORZENIE OPTYMALNYCH INDEKSÓW W MONGODB")
    print("=" * 60)

    client = AsyncIOMotorClient(settings.MONGO_URL, serverSelectionTimeoutMS=3000)
    db = client[settings.MONGO_DB_NAME]
    await ensure_all_indexes(db)
    print("\n🎉 Wszystkie indeksy zostały pomyślnie utworzone!")


if __name__ == "__main__":
    asyncio.run(create_all_indexes())
