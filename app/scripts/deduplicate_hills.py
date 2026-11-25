# app/scripts/deduplicate_hills.py
"""
Skrypt deduplikujący i ujednolicający skocznie w MongoDB:
1. Normalizuje nazwy miejscowości za pomocą KNOWN_PLACE_ALIASES (np. Kulm Bad Mitterndorf/Tauplitz -> Bad Mitterndorf).
2. Łączy powielone rekordy w kolekcji 'hills' (sumuje sesje, zachowuje rekordy i flagę is_current).
3. Aktualizuje kolekcję 'sessions' (pole 'event_place' -> kanoniczna nazwa), aby historia skoków nie była rozbita.
4. Usuwa nadmiarowe duplikaty z kolekcji 'hills'.

Użycie:
    python app/scripts/deduplicate_hills.py [--apply] [--db NAZWA_BAZY]
"""

import asyncio
import sys
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings
from app.services.hill_service import normalize_place_name, infer_hill_type


async def deduplicate_hills(apply: bool = False, target_db: str = None):
    db_name = target_db or settings.MONGO_DB_NAME
    print("=" * 80)
    print(f"🏔️  DEDUPLIKACJA I UNIFIKACJA SKOCZNI DLA BAZY: {db_name}")
    print(f"    Tryb: {'APPLY (Zapis zmian w bazie)' if apply else 'DRY-RUN (Tylko symulacja)'}")
    print("=" * 80)

    client = AsyncIOMotorClient(settings.MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client[db_name]
    hills_col = db["hills"]
    sessions_col = db["sessions"]

    all_hills = await hills_col.find({}).to_list(None)
    print(f"1. Liczba wszystkich obiektów w kolekcji 'hills': {len(all_hills)}")

    # 1. Mapowanie do kanonicznych kluczy
    canonical_groups = {}
    for h in all_hills:
        raw_place = (h.get("place") or "").strip()
        country = (h.get("country_code") or "UNK").strip().upper()
        size = int(h.get("size") or 0)
        unit = (h.get("unit") or "HS").strip().upper()
        htype = (h.get("type") or infer_hill_type(size)).strip().upper()

        canon_place = normalize_place_name(raw_place, country)
        canon_id = f"{canon_place.strip().upper()}_{unit}{size}"

        key = (canon_id, canon_place, country, size, unit, htype)
        if key not in canonical_groups:
            canonical_groups[key] = []
        canonical_groups[key].append(h)

    print(f"2. Liczba unikalnych skoczni po normalizacji: {len(canonical_groups)}")

    merged_count = 0
    deleted_ids = []
    hills_to_save = []
    place_renames = {}

    for (canon_id, canon_place, country, size, unit, htype), members in canonical_groups.items():
        # Zapisz zmianę nazwy dla sesji
        for m in members:
            orig_p = m.get("place", "")
            if orig_p and orig_p != canon_place:
                place_renames[orig_p] = canon_place

        if len(members) == 1:
            m = members[0]
            # Upewnij się, że pojedyncza skocznia ma prawidłowy _id i place
            if m["_id"] != canon_id or m.get("place") != canon_place:
                doc = dict(m)
                old_id = doc.pop("_id")
                doc["_id"] = canon_id
                doc["place"] = canon_place
                deleted_ids.append(old_id)
                hills_to_save.append(doc)
            continue

        # Wiele skoczni do połączenia!
        merged_count += 1
        print(f"\n🔗 Łączenie {len(members)} obiektów w kanoniczny: {canon_id} ({canon_place}, {country} {unit}{size})")

        # Scalanie parametrów:
        total_sessions = sum(int(m.get("total_sessions") or 0) for m in members)
        latest_date = max((str(m.get("last_session_date") or "") for m in members), default="")
        latest_season = max((str(m.get("last_season") or "") for m in members), default="")

        # Wybierz najlepszy rekord
        best_record = None
        best_record_holder = None
        for m in members:
            rec = m.get("record")
            if rec and (best_record is None or float(rec) >= best_record):
                best_record = float(rec)
                best_record_holder = m.get("record_holder") or best_record_holder

        # Wybierz najlepszy punkt K i meter_value
        best_k = next((float(m["k_point"]) for m in members if m.get("k_point")), None)
        best_mv = next((float(m["meter_value"]) for m in members if m.get("meter_value")), None)
        has_current = any(bool(m.get("is_current")) for m in members)

        canonical_doc = {
            "_id": canon_id,
            "place": canon_place,
            "country_code": country,
            "size": size,
            "unit": unit,
            "type": htype,
            "total_sessions": total_sessions,
            "last_session_date": latest_date if latest_date else None,
            "last_season": latest_season if latest_season else None,
            "is_current": has_current,
            "record": best_record,
            "record_holder": best_record_holder,
            "k_point": best_k,
            "meter_value": best_mv
        }

        for m in members:
            print(f"   - Stary ID: {m['_id']} (place={m.get('place')!r}, sessions={m.get('total_sessions')}, rec={m.get('record')})")
            deleted_ids.append(m["_id"])

        hills_to_save.append(canonical_doc)

    print(f"\nPodsumowanie analizy:")
    print(f"  - Grupy wymagające połączenia: {merged_count}")
    print(f"  - Nadmiarowe rekordy do usunięcia: {len(deleted_ids)}")
    print(f"  - Nowe / zaktualizowane rekordy kanoniczne: {len(hills_to_save)}")
    print(f"  - Reguły ujednolicenia event_place w sesjach: {len(place_renames)}")
    for old_p, new_p in place_renames.items():
        print(f"     '{old_p}' -> '{new_p}'")

    if apply:
        print("\n⏳ Wprowadzanie zmian do bazy danych...")

        # 1. Zapisz rekordy kanoniczne
        for h in hills_to_save:
            await hills_col.replace_one({"_id": h["_id"]}, h, upsert=True)
        print(f"✅ Zapisano {len(hills_to_save)} rekordów kanonicznych w 'hills'.")

        # 2. Usuń stare ID, które nie są kanonicznymi
        ids_to_del = [hid for hid in deleted_ids if hid not in [h["_id"] for h in hills_to_save]]
        if ids_to_del:
            del_res = await hills_col.delete_many({"_id": {"$in": ids_to_del}})
            print(f"✅ Usunięto {del_res.deleted_count} starych duplikatów z 'hills'.")

        # 3. Ujednolicenie event_place w sessions
        updated_sessions_count = 0
        for old_p, new_p in place_renames.items():
            res = await sessions_col.update_many(
                {"event_place": old_p},
                {"$set": {"event_place": new_p}}
            )
            updated_sessions_count += res.modified_count
        print(f"✅ Zaktualizowano pole event_place w {updated_sessions_count} sesjach.")

        final_hills_count = await hills_col.count_documents({})
        print(f"\n🎉 ZAKOŃCZONO! Nowa liczba skoczni w bazie {db_name}: {final_hills_count}")
    else:
        print("\n💡 Uruchomiono w trybie DRY-RUN. Aby zapisać zmiany, dodaj flagę --apply.")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deduplikacja skoczni w bazie danych")
    parser.add_argument("--apply", action="store_true", help="Zastosuj zmiany w bazie danych")
    parser.add_argument("--db", type=str, default=None, help="Nazwa docelowej bazy danych")
    args = parser.parse_args()

    asyncio.run(deduplicate_hills(apply=args.apply, target_db=args.db))
