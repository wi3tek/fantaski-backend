# app/scripts/migrate_hills_hierarchy.py
"""
Skrypt migracyjny bazy skoczni w MongoDB do 3-poziomowej hierarchii:
Poziom 1: Miejscowość / Kompleks (place, country_code)
Poziom 2: Rodzaj skoczni (FLYING, LARGE, NORMAL, SMALL)
Poziom 3: Wersje skoczni (z flagą is_current: bool oraz statystykami sesji)

Uruchomienie:
    python app/scripts/migrate_hills_hierarchy.py [--apply] [--db NAZWA_BAZY]
"""

import asyncio
import sys
import argparse
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne
from app.config import settings


async def run_hierarchy_migration(apply: bool = False, target_db: str = None):
    db_name = target_db or settings.MONGO_DB_NAME
    print("=" * 70)
    print("🏔️  MIGRACJA HIERARCHII SKOCZNI (3-POZIOMOWY PODZIAŁ I WERSJONOWANIE)")
    print("=" * 70)
    print(f"Baza danych : {db_name}")
    print(f"Tryb        : {'APPLY (Zapis zmian w bazie)' if apply else 'DRY-RUN (Tylko symulacja)'}\n")

    client = AsyncIOMotorClient(settings.MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client[db_name]
    hills_col = db["hills"]
    sessions_col = db["sessions"]

    # 1. Agregacja sesji: liczymy ostatnią datę, sezon i liczbę sesji dla każdej konfiguracji (place, size)
    print("1. Analiza rozegranych sesji w bazie danych...")
    pipeline = [
        {"$match": {"event_place": {"$ne": None}, "hillSize": {"$gt": 0}}},
        {"$group": {
            "_id": {
                "place": {"$toLower": {"$trim": {"input": "$event_place"}}},
                "size": "$hillSize"
            },
            "last_date": {"$max": "$date"},
            "last_season": {"$max": "$season"},
            "count": {"$sum": 1}
        }}
    ]
    sessions_stats = await sessions_col.aggregate(pipeline).to_list(None)
    stats_map = {}
    for s in sessions_stats:
        p = s["_id"]["place"]
        sz = s["_id"]["size"]
        stats_map[(p, sz)] = {
            "last_date": s.get("last_date") or "",
            "last_season": s.get("last_season") or "",
            "count": s.get("count") or 0
        }
    print(f"   Znaleziono statystyki dla {len(stats_map)} konfiguracji (miejscowość, rozmiar).")

    # 2. Pobieramy wszystkie skocznie
    all_hills = await hills_col.find({}).to_list(None)
    print(f"2. Liczba wszystkich obiektów w kolekcji 'hills': {len(all_hills)}")

    if not all_hills:
        print("Brak skoczni w bazie. Zakończono.")
        return

    # Grupujemy po (place_lower, type)
    groups = defaultdict(list)
    for h in all_hills:
        place = (h.get("place") or "").strip()
        htype = (h.get("type") or "OTHER").strip().upper()
        groups[(place.lower(), htype)].append(h)

    print(f"   Liczba unikalnych kompleksów i rodzajów: {len(groups)}")

    bulk_ops = []
    multi_version_info = []
    single_version_count = 0

    for (p_lower, htype), hill_list in groups.items():
        # Punktacja wersji:
        # 1. Najnowsza data sesji
        # 2. Jednostka HS wyżej niż K
        # 3. Rozmiar (większy HS oznacza nowszą modernizację)
        # 4. Czy posiada odnotowany rekord
        # 5. Czy posiada punkt K
        def score_hill(h):
            sz = h.get("size") or 0
            st = stats_map.get((p_lower, sz), {"last_date": "", "last_season": "", "count": 0})
            last_date = st["last_date"]
            unit_score = 1 if (h.get("unit") or "HS") == "HS" else 0
            has_record = 1 if h.get("record") else 0
            has_k = 1 if h.get("k_point") else 0
            return (last_date, unit_score, sz, has_record, has_k)

        sorted_hills = sorted(hill_list, key=score_hill, reverse=True)
        best_hill = sorted_hills[0]

        if len(sorted_hills) > 1:
            multi_version_info.append({
                "place": best_hill.get("place"),
                "type": htype,
                "current": best_hill["_id"],
                "versions": [h["_id"] for h in sorted_hills]
            })
        else:
            single_version_count += 1

        for idx, h in enumerate(sorted_hills):
            hid = h["_id"]
            is_current = (idx == 0)
            sz = h.get("size") or 0
            st = stats_map.get((p_lower, sz), {"last_date": None, "last_season": None, "count": 0})

            update_fields = {
                "is_current": is_current,
                "total_sessions": st["count"],
            }
            if st["last_date"]:
                update_fields["last_session_date"] = st["last_date"]
            if st["last_season"]:
                update_fields["last_season"] = st["last_season"]

            bulk_ops.append(
                UpdateOne({"_id": hid}, {"$set": update_fields})
            )

    print(f"\n3. Wynik klasyfikacji wersji:")
    print(f"   - Kompleksy jednoobiektowe: {single_version_count}")
    print(f"   - Kompleksy z wieloma wersjami historycznymi: {len(multi_version_info)}")
    print(f"   - Łączna liczba operacji aktualizacji: {len(bulk_ops)}")

    print("\n   Przykłady wykrytych wersji historycznych:")
    for item in multi_version_info[:8]:
        print(f"     📍 {item['place']:<20} [{item['type']:<7}] -> AKTUALNA: {item['current']:<20} WSZYSTKIE: {item['versions']}")

    if apply:
        if bulk_ops:
            result = await hills_col.bulk_write(bulk_ops)
            print(f"\n✅ Pomyślnie zaktualizowano {result.modified_count} rekordów w kolekcji 'hills'.")

        # Tworzenie indeksów pod hierarchię
        print("\n4. Tworzenie indeksów w MongoDB...")
        await hills_col.create_index([("place", 1), ("type", 1)])
        await hills_col.create_index([("is_current", 1)])
        print("   ✅ Utworzono indeksy: (place, type), is_current")

        print("\n🎉 Migracja hierarchii skoczni zakończona sukcesem!")
    else:
        print("\nℹ️  Aby zapisać te zmiany w bazie danych, uruchom skrypt z flagą --apply:")
        print(f"    python app/scripts/migrate_hills_hierarchy.py --apply --db {db_name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migracja 3-poziomowej hierarchii skoczni w MongoDB")
    parser.add_argument("--apply", action="store_true", help="Wykonaj zapis zmian w bazie")
    parser.add_argument("--db", type=str, default=None, help="Nazwa docelowej bazy (domyślnie z configu)")
    args = parser.parse_args()

    asyncio.run(run_hierarchy_migration(apply=args.apply, target_db=args.db))
