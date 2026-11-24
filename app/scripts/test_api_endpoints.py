import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from httpx import AsyncClient, ASGITransport
from app.main import app

async def run_tests():
    print("=" * 60)
    print("🧪 TEST NOWYCH FUNKCJONALNOŚCI PREDYKCJI")
    print("=" * 60)
    
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            
            # 1. Test available-athletes
            print("\n[1/5] Test endpointu /prediction/available-athletes...")
            res_ath = await ac.get("/prediction/available-athletes")
            print(f"Status: {res_ath.status_code}")
            assert res_ath.status_code == 200
            available = res_ath.json()
            wc_athletes = [a for a in available if a["has_wc"]]
            priced_athletes = [a for a in available if a["has_price"]]
            print(f"Wszyscy aktywni: {len(available)}, aktywni z PŚ: {len(wc_athletes)}, z wyceną (ESM fantasy): {len(priced_athletes)}")
            assert len(priced_athletes) == 100
            print("Przykłady aktywnych z ceną:", [(a["full_name"], a["price"]) for a in priced_athletes[:5]])
            print("✅ Test /prediction/available-athletes OK!")

            # 2. Test domyślnej predykcji (tylko skoczkowie z ceną)
            print("\n[2/5] Test domyślnej predykcji (100 skoczków z ceną, Zakopane HS140)...")
            payload_default = {
                "hill_place": "Zakopane",
                "hill_size": 140.0,
                "hill_type": "LARGE",
                "date": "2026-11-25",
                "time": "16:00",
                "weather": {
                    "temperature": -2.0,
                    "wind_speed": 1.5,
                    "wind_direction": 180.0,
                    "wind_gusts": 2.5,
                    "surface_pressure": 960.0,
                    "relative_humidity": 75.0,
                    "precipitation": 0.0,
                    "headwind_component": 0.8
                },
                "num_simulations": 100
            }
            res_pred = await ac.post("/prediction/predict", json=payload_default)
            assert res_pred.status_code == 200
            data = res_pred.json()
            predictions = data["predictions"]
            print(f"Liczba skoczków w domyślnej predykcji: {len(predictions)}")
            assert len(predictions) == 100
            
            top1 = predictions[0]
            print("\nZwycięzca:")
            print(f"  {top1['predicted_rank']}. {top1['name']} ({top1['country']}) - Cena: {top1.get('price')}m")
            print(f"  Odległość: {top1['predicted_distance']} m")
            print(f"  Punkty: {top1['predicted_points']} pkt")
            print(f"  Szansa Top 30: {top1['top30_probability']}%")
            print(f"  Szansa Podium: {top1['podium_probability']}%")
            print(f"  Forma bieżącego sezonu: {top1['form_score']}%")
            print(f"  Afinitet skoczni: {top1['hill_affinity_score']}%")
            print(f"  Profile psychologiczne/stylu: {[p['label'] for p in top1.get('profiles', [])]}")
            print(f"  Powtarzalność: {top1.get('consistency_score')}%")
            
            # Weryfikacja wymaganych pól
            assert "top30_probability" in top1
            assert "predicted_points" in top1
            assert "profiles" in top1
            assert "consistency_score" in top1
            assert "conversion_delta" in top1
            print("✅ Test pól top30_probability, predicted_points, profiles i consistency OK!")

            # 3. Test ręcznego wyboru listy startowej (np. Stoch, Zyla, Kubacki, Kraft)
            print("\n[3/5] Test ręcznej listy startowej (własny wybór skoczków)...")
            selected_test_athletes = [a["fis_code"] for a in available if any(name in a["full_name"] for name in ["STOCH", "ZYLA", "KUBACKI", "KRAFT"])]
            print(f"Wybrane kody FIS: {selected_test_athletes}")
            
            payload_custom = dict(payload_default)
            payload_custom["fis_codes"] = selected_test_athletes
            res_custom = await ac.post("/prediction/predict", json=payload_custom)
            assert res_custom.status_code == 200
            custom_data = res_custom.json()
            assert len(custom_data["predictions"]) == len(selected_test_athletes)
            print(f"Przetestowano {len(custom_data['predictions'])} wybranych zawodników:")
            for r in custom_data["predictions"]:
                profs = ", ".join(p["label"] for p in r.get("profiles", []))
                print(f"  {r['predicted_rank']}. {r['name']} ({r['country']}): {r['predicted_distance']} m | {r['predicted_points']} pkt | Top 30: {r['top30_probability']}% | Profile: [{profs}]")
            print("✅ Test własnej listy startowej OK!")

            # 4. Test mamuciej skoczni i afinitetu typu skoczni (Planica HS240)
            print("\n[4/5] Test mamuciej skoczni Planica HS240...")
            payload_flying = dict(payload_default)
            payload_flying["hill_place"] = "Planica"
            payload_flying["hill_size"] = 240.0
            payload_flying["hill_type"] = "FLYING"
            payload_flying["weather"]["headwind_component"] = 2.0
            payload_flying["fis_codes"] = selected_test_athletes
            
            res_flying = await ac.post("/prediction/predict", json=payload_flying)
            assert res_flying.status_code == 200
            flying_data = res_flying.json()
            for r in flying_data["predictions"]:
                print(f"  {r['predicted_rank']}. {r['name']} ({r['country']}): {r['predicted_distance']} m | {r['predicted_points']} pkt")
            print("✅ Test mamuciej skoczni OK!")

            # 5. Test automatycznego wykrywania sesji z bieżącego weekendu
            print("\n[5/5] Test automatycznego wykrywania sesji z bieżącego weekendu (Planica 2024-03-24)...")
            payload_weekend = {
                "hill_place": "Planica",
                "hill_size": 240.0,
                "hill_type": "FLYING",
                "date": "2024-03-24",
                "time": "10:00",
                "fis_codes": selected_test_athletes,
                "num_simulations": 50
            }
            res_weekend = await ac.post("/prediction/predict", json=payload_weekend)
            assert res_weekend.status_code == 200
            weekend_data = res_weekend.json()
            detected_sessions = weekend_data.get("weekend_sessions_detected", [])
            print(f"Wykryte sesje z tego samego weekendu: {len(detected_sessions)}")
            for s in detected_sessions:
                print(f"  - {s.get('date')} ({s.get('type')}): {s.get('title')}")
            assert len(detected_sessions) > 0
            print("✅ Test wykrywania sesji weekendowych OK!")

if __name__ == "__main__":
    asyncio.run(run_tests())
