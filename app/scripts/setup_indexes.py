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


async def create_all_indexes():
    print("=" * 60)
    print("⚡ TWORZENIE OPTYMALNYCH INDEKSÓW W MONGODB")
    print("=" * 60)

    client = AsyncIOMotorClient(settings.MONGO_URL, serverSelectionTimeoutMS=3000)
    db = client[settings.MONGO_DB_NAME]

    # 1. Kolekcja: athletes
    print("\n[1/4] Indeksowanie kolekcji 'athletes'...")
    try:
        # Unikalny indeks na fis_code (kluczowy dla find_by_fis_code i kwarantanny)
        idx_fis = await db["athletes"].create_index(
            [("fis_code", pymongo.ASCENDING)],
            unique=True,
            name="idx_athletes_fis_code_unique"
        )
        print(f"  ✅ {idx_fis}")
    except Exception as e:
        print(f"  ⚠️ Indeks fis_code: {e}")

    idx_name = await db["athletes"].create_index([("full_name", pymongo.ASCENDING)], name="idx_athletes_full_name")
    idx_country = await db["athletes"].create_index([("country_code", pymongo.ASCENDING)], name="idx_athletes_country")
    idx_status = await db["athletes"].create_index([("status", pymongo.ASCENDING)], name="idx_athletes_status")
    idx_price = await db["athletes"].create_index([("price", pymongo.DESCENDING)], name="idx_athletes_price")
    print(f"  ✅ {idx_name}\n  ✅ {idx_country}\n  ✅ {idx_status}\n  ✅ {idx_price}")

    # 2. Kolekcja: sessions
    print("\n[2/4] Indeksowanie kolekcji 'sessions'...")
    # Indeks złożony dla filtrowania po sezonie, kategorii i dacie
    idx_season = await db["sessions"].create_index(
        [("season", pymongo.ASCENDING), ("category_code", pymongo.ASCENDING), ("date", pymongo.DESCENDING)],
        name="idx_sessions_season_cat_date"
    )
    # Indeks złożony dla filtrowania po typie sesji i dacie
    idx_session_type = await db["sessions"].create_index(
        [("session_type", pymongo.ASCENDING), ("date", pymongo.DESCENDING)],
        name="idx_sessions_type_date"
    )
    # Indeks dla scrapera PDF treningów
    idx_trainings_proc = await db["sessions"].create_index(
        [("trainings_processed", pymongo.ASCENDING)],
        name="idx_sessions_trainings_processed"
    )
    idx_place = await db["sessions"].create_index([("event_place", pymongo.ASCENDING)], name="idx_sessions_place")
    # Indeks dla głównej listy sesji (sortowanie chronologiczne)
    idx_date_time = await db["sessions"].create_index(
        [("date", pymongo.DESCENDING), ("time", pymongo.DESCENDING)],
        name="idx_sessions_date_time"
    )
    print(f"  ✅ {idx_season}\n  ✅ {idx_session_type}\n  ✅ {idx_trainings_proc}\n  ✅ {idx_place}\n  ✅ {idx_date_time}")

    # 3. Kolekcja: sessions_review
    print("\n[3/4] Indeksowanie kolekcji 'sessions_review'...")
    idx_review_date = await db["sessions_review"].create_index(
        [("date", pymongo.DESCENDING)],
        name="idx_sessions_review_date"
    )
    print(f"  ✅ {idx_review_date}")

    # 4. Kolekcja: hills
    print("\n[4/4] Indeksowanie kolekcji 'hills'...")
    idx_hills_place = await db["hills"].create_index([("place", pymongo.ASCENDING)], name="idx_hills_place")
    idx_hills_country = await db["hills"].create_index([("country_code", pymongo.ASCENDING)], name="idx_hills_country")
    print(f"  ✅ {idx_hills_place}\n  ✅ {idx_hills_country}")

    print("\n🎉 Wszystkie indeksy zostały pomyślnie utworzone!")


if __name__ == "__main__":
    asyncio.run(create_all_indexes())
