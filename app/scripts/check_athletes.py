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
    
    statuses = await db['athletes'].distinct('status')
    print('Athlete statuses:', statuses)
    
    active_count = await db['athletes'].count_documents({'status': 'Active'})
    print('Active athletes count:', active_count)
    
    # Check sample active athlete
    sample = await db['athletes'].find_one({'status': 'Active'})
    print('Sample active athlete:', sample)

if __name__ == '__main__':
    asyncio.run(main())
