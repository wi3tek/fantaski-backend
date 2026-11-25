import asyncio
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings
from app.services.prediction.neural_network import get_prediction_manager

async def main():
    print("=" * 60)
    print("🧠 TRENOWANIE SIECI NEURONOWEJ DO PREDYKCJI SKOKÓW FIS (v3 — 14 cech)")
    print("=" * 60)
    
    start_time = time.time()
    db_name = sys.argv[1] if len(sys.argv) > 1 else (settings.MONGO_DB_NAME if settings.MONGO_DB_NAME != "FIS_DATA_DB_DEV" else "FIS_DATA_DB_TEST")
    print(f"Używana baza danych: {db_name}")
    client = AsyncIOMotorClient(settings.MONGO_URL)
    db = client[db_name]
    
    print("Pobieranie sesji z MongoDB...")
    cursor = db["sessions"].find(
        {"results_data.results": {"$exists": True, "$ne": []}},
        {"date": 1, "time": 1, "hillSize": 1, "hillType": 1, "event_place": 1, "session_type": 1, "race_category": 1, "category_code": 1, "results_data": 1, "weather": 1}
    )
    
    sessions = await cursor.to_list(length=3500)
    print(f"Załadowano {len(sessions)} sesji z wynikami w {time.time() - start_time:.2f}s")
    
    manager = get_prediction_manager()
    print("Uruchamianie treningu sieci neuronowej...")
    train_start = time.time()
    meta = manager.train(sessions)
    print(f"Trening ukończony w {time.time() - train_start:.2f}s!")
    print("Metadane modelu:")
    for k, v in meta.items():
        print(f"  - {k}: {v}")

if __name__ == "__main__":
    asyncio.run(main())
