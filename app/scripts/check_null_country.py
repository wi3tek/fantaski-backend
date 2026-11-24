import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings

async def main():
    client = AsyncIOMotorClient(settings.MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client[settings.MONGO_DB_NAME]
    cursor = db["sessions"].find({"event_country": None}, {"results_data": 0})
    sessions = await cursor.to_list(length=100)
    print(f"Total sessions with null event_country: {len(sessions)}")
    for s in sessions:
        print(f"ID: {s.get('_id')} | place: {s.get('event_place')} | date: {s.get('date')} | title: {s.get('title')}")

if __name__ == "__main__":
    asyncio.run(main())
