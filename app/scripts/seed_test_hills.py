# app/scripts/seed_test_hills.py
"""
Skrypt kopiujący katalog skoczni (kolekcję 'hills') z bazy FIS_DATA_DB_DEV do FIS_DATA_DB_TEST.
Przydatny po wyczyszczeniu bazy testowej, jeśli chcemy zachować pełny słownik 303 skoczni
przed scrapowaniem kolejnych sezonów.

Uruchomienie:
    python app/scripts/seed_test_hills.py
"""

import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings


async def seed_hills():
    client = AsyncIOMotorClient(settings.MONGO_URL, serverSelectionTimeoutMS=5000)
    dev_db = client["FIS_DATA_DB_DEV"]
    test_db = client["FIS_DATA_DB_TEST"]

    dev_hills = await dev_db["hills"].find({}).to_list(1000)
    if not dev_hills:
        print("❌ Brak skoczni w bazie FIS_DATA_DB_DEV.")
        return

    print(f"Pobrano {len(dev_hills)} skoczni z FIS_DATA_DB_DEV.")

    # Wstawiamy do bazy TEST metodą upsert (zastępując lub dodając)
    from pymongo import ReplaceOne
    ops = [ReplaceOne({"_id": h["_id"]}, h, upsert=True) for h in dev_hills]
    res = await test_db["hills"].bulk_write(ops)

    test_count = await test_db["hills"].count_documents({})
    print(f"✅ Baza FIS_DATA_DB_TEST posiada teraz {test_count} skoczni!")


if __name__ == "__main__":
    asyncio.run(seed_hills())
