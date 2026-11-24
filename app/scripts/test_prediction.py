import asyncio
import sys
import pprint
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings
from app.services.weather.weather_service import get_weather_service
from app.services.prediction.neural_network import get_prediction_manager

async def test():
    print("=" * 60)
    print("🔮 TEST SYSTEMU PREDYKCJI SKOKÓW I POGODY")
    print("=" * 60)
    
    # 1. Test pogody
    ws = get_weather_service()
    weather = ws.get_weather("Zakopane", "2026-11-25", "16:00")
    print("\nPobrana prognoza pogody dla Zakopanego (Open-Meteo):")
    pprint.pprint(weather)
    
    # 2. Test predykcji
    client = AsyncIOMotorClient(settings.MONGO_URL)
    db = client[settings.MONGO_DB_NAME]
    
    # Pobierz 15 znanych aktywnych skoczków
    athletes = await db["athletes"].find({"status": "Active"}).to_list(100)
    print(f"\nZnaleziono {len(athletes)} aktywnych skoczków do testu.")
    
    manager = get_prediction_manager()
    print("Wykonuję predykcję na Wielkiej Krokwi (Zakopane HS140)...")
    results = manager.predict(
        athletes=athletes,
        hill_size=140.0,
        hill_type="LARGE",
        weather=weather,
        num_simulations=100
    )
    
    print("\nTOP 10 PRZEWIDYWANYCH WYNIKÓW:")
    print(f"{'Msc':<4} | {'Skoczek':<26} | {'Kraj':<4} | {'Dystans':<8} | {'Punkty':<8} | {'Podium %':<9} | {'Forma':<6}")
    print("-" * 75)
    for r in results[:10]:
        print(f"{r['predicted_rank']:<4} | {r['name']:<26} | {r['country']:<4} | {r['predicted_distance']:>6.1f} m | {r['predicted_points']:>6.1f} p | {r['podium_probability']:>7.1f}% | {r['form_score']:>5.1f}")
        
    print("\nTest mamuciej skoczni w Planicy (HS240, mocny wiatr pod narty 2.5 m/s):")
    planica_weather = dict(weather)
    planica_weather["headwind_component"] = 2.5
    planica_weather["wind_speed"] = 3.0
    results_planica = manager.predict(
        athletes=athletes,
        hill_size=240.0,
        hill_type="FLYING",
        weather=planica_weather,
        num_simulations=100
    )
    print(f"{'Msc':<4} | {'Skoczek':<26} | {'Kraj':<4} | {'Dystans':<8} | {'Punkty':<8} | {'Podium %':<9} | {'Afinitet':<8}")
    print("-" * 75)
    for r in results_planica[:10]:
        print(f"{r['predicted_rank']:<4} | {r['name']:<26} | {r['country']:<4} | {r['predicted_distance']:>6.1f} m | {r['predicted_points']:>6.1f} p | {r['podium_probability']:>7.1f}% | {r['hill_affinity_score']:>6.1f}%")

if __name__ == "__main__":
    asyncio.run(test())
