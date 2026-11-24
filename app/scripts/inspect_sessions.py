import asyncio
import sys
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings

async def main():
    client = AsyncIOMotorClient(settings.MONGO_URL)
    db = client[settings.MONGO_DB_NAME]
    
    recent_events = set()
    total_recent = 0
    async for s in db['sessions'].find({'date': {'$gte': '2022-01-01'}}, {'event_place': 1, 'date': 1, 'results_data': 1}):
        if s.get('results_data', {}).get('results'):
            total_recent += 1
            p, d = s.get('event_place'), s.get('date')
            if p and d:
                recent_events.add((p.strip().title(), d.strip()))
    print(f'Recent sessions (>= 2022): {total_recent}, unique events: {len(recent_events)}')

if __name__ == '__main__':
    asyncio.run(main())
