# app/scripts/migrate_hills.py
"""
Skrypt migracyjny bazy skoczni w MongoDB.
Usuwa reliktowe duplikaty (wpisy bez jednostki HS/K, np. PLANICA_240 vs PLANICA_HS240)
oraz standaryzuje nazewnictwo miejscowości.

Uruchomienie:
    python app/scripts/migrate_hills.py [--apply]
"""

import asyncio
import sys
import argparse
import re
from pathlib import Path

# Dodanie ścieżki głównej projektu do sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings


async def run_migration(apply: bool = False):
    print("=" * 60)
    print("🏔️  MIGRACJA I OCZYSZCZANIE BAZY SKOCZNI (HILLS)")
    print("=" * 60)
    print(f"Tryb: {'APPLY (Zapis zmian w bazie)' if apply else 'DRY-RUN (Tylko symulacja, brak zmian)'}\n")

    client = AsyncIOMotorClient(settings.MONGO_URL, serverSelectionTimeoutMS=3000)
    db = client[settings.MONGO_DB_NAME]
    hills_col = db["hills"]

    all_hills = await hills_col.find({}).to_list(2000)
    print(f"Liczba wszystkich rekordów w kolekcji 'hills': {len(all_hills)}")

    all_ids = set(h["_id"] for h in all_hills)
    to_delete = []
    to_rename = []

    for hill in all_hills:
        hid = str(hill["_id"])
        # Szukamy starych ID postaci: NAZWA_LICZBA (bez HS ani K)
        m = re.match(r"^(.+)_(\d+)$", hid)
        if m:
            prefix, size = m.group(1), m.group(2)
            hs_id = f"{prefix}_HS{size}"
            k_id = f"{prefix}_K{size}"

            if hs_id in all_ids:
                to_delete.append((hid, hs_id, "Identyczny odpowiednik HS już istnieje"))
            elif k_id in all_ids:
                to_delete.append((hid, k_id, "Identyczny odpowiednik K już istnieje"))
            else:
                to_rename.append((hid, hs_id, hill))

    print(f"\nZnaleziono duplikatów do usunięcia: {len(to_delete)}")
    print(f"Znaleziono wpisów do zaktualizowania ID (brak odpowiednika): {len(to_rename)}")

    if to_delete:
        print("\nPrzykłady wykrytych starych duplikatów do usunięcia:")
        for old_id, counterpart, reason in to_delete[:10]:
            print(f"  ❌ Usuń: {old_id:<25} -> Pozostaje: {counterpart}")

    if apply:
        if to_delete:
            delete_ids = [x[0] for x in to_delete]
            del_result = await hills_col.delete_many({"_id": {"$in": delete_ids}})
            print(f"\n✅ Pomyślnie usunięto {del_result.deleted_count} zduplikowanych rekordów z 'hills'.")

        if to_rename:
            for old_id, new_id, doc in to_rename:
                doc["_id"] = new_id
                doc["unit"] = doc.get("unit") or "HS"
                await hills_col.insert_one(doc)
                await hills_col.delete_one({"_id": old_id})
            print(f"✅ Pomyślnie zmigrowano {len(to_rename)} rekordów do nowego formatu ID.")

        final_count = await hills_col.count_documents({})
        print(f"\n🎉 Gotowe! Liczba unikalnych skoczni w bazie po migracji: {final_count}")
    else:
        print("\nℹ️  Aby zapisać te zmiany w bazie danych, uruchom skrypt z flagą --apply:")
        print("    python app/scripts/migrate_hills.py --apply")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Wykonaj zmiany w bazie danych")
    args = parser.parse_args()

    asyncio.run(run_migration(apply=args.apply))
