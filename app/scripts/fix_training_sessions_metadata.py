# app/scripts/fix_training_sessions_metadata.py
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings

async def main():
    client = AsyncIOMotorClient(settings.MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client[settings.MONGO_DB_NAME]
    cursor = db["sessions"].find({"$or": [{"event_country": None}, {"season": None}]}, {"results_data": 0})
    sessions = await cursor.to_list(length=200)
    print(f"Znaleziono {len(sessions)} sesji do uzupełnienia metadanych.")

    updated_count = 0
    for s in sessions:
        race_id = s.get("raceId")
        codex = s.get("codex")
        parent = await db["sessions"].find_one({
            "raceId": race_id,
            "codex": codex,
            "_id": {"$ne": s["_id"]},
            "event_country": {"$ne": None}
        })

        updates = {}
        if parent:
            if not s.get("event_country") and parent.get("event_country"):
                updates["event_country"] = parent["event_country"]
            if not s.get("season") and parent.get("season"):
                updates["season"] = parent["season"]
            if not s.get("title"):
                updates["title"] = f"Official Training - {s.get('event_place', parent.get('event_place', ''))}"

        if updates:
            await db["sessions"].update_one({"_id": s["_id"]}, {"$set": updates})
            print(f"  Zaktualizowano {s['_id']}: {updates}")
            updated_count += 1
        else:
            print(f"  Brak danych nadrzędnych dla {s['_id']}")

    print(f"Zakończono. Zaktualizowano {updated_count}/{len(sessions)} sesji.")

if __name__ == "__main__":
    asyncio.run(main())
