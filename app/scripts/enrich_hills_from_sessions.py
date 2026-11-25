# app/scripts/enrich_hills_from_sessions.py
"""
Skrypt wzbogacający istniejące rekordy w kolekcji 'hills' w MongoDB
o punkt konstrukcyjny K (k_point), rekord skoczni (record) i rekordzistę (record_holder).

Pobiera dane z załączników PDF (Official Results, Qualification Results, Results Training)
przypisanych do sesji w kolekcji 'sessions'.
Zgodnie z zasadą: jeśli skocznia już istnieje w bazie, aktualizuje ją bez dodawania duplikatów.

Uruchomienie:
    python -m app.scripts.enrich_hills_from_sessions [--apply]
"""

import asyncio
import sys
import argparse
import io
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

import httpx
import fitz
from motor.motor_asyncio import AsyncIOMotorClient
from app.config import settings, DEFAULT_HEADERS
from app.services.scrapers.pdf_training_scraper import parse_hill_data_from_pdf_text


async def enrich_hills(apply: bool = False):
    print("=" * 60)
    print("🏔️  WZBOGACANIE BAZY SKOCZNI (HILLS) O PUNKT K I REKORDY Z PDF")
    print("=" * 60)
    print(f"Tryb: {'APPLY (Zapis zmian w bazie)' if apply else 'DRY-RUN (Tylko symulacja)'}\n")

    client = AsyncIOMotorClient(settings.MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client[settings.MONGO_DB_NAME]
    hills_col = db["hills"]
    sessions_col = db["sessions"]

    all_hills = await hills_col.find({}).to_list(1000)
    print(f"Liczba skoczni w bazie: {len(all_hills)}")

    # Indeksujemy sesje po (place_lower, hillSize)
    all_sessions = await sessions_col.find(
        {"documents.0": {"$exists": True}},
        {"event_place": 1, "hillSize": 1, "hill_size_unit": 1, "documents": 1}
    ).to_list(None)

    place_size_to_docs = {}
    for s in all_sessions:
        p = (s.get("event_place") or "").strip().lower()
        sz = s.get("hillSize")
        docs = s.get("documents") or []
        if p and sz and docs:
            key = (p, sz)
            if key not in place_size_to_docs:
                place_size_to_docs[key] = []
            for d in docs:
                url = d.get("url")
                dtype = d.get("type", "")
                if url:
                    if "/competition-attachment/" in url and not url.lower().endswith(".pdf"):
                        url = f"{url}.pdf"
                    # Priorytet: Official Results > Qualification > Training
                    priority = 1 if "official" in dtype.lower() else (2 if "qualification" in dtype.lower() else 3)
                    place_size_to_docs[key].append((priority, dtype, url))

    # Sortujemy dokumenty wg priorytetu
    for k in place_size_to_docs:
        place_size_to_docs[k].sort(key=lambda x: x[0])

    semaphore = asyncio.Semaphore(2)
    enriched_count = 0
    k_fallback_count = 0
    not_found_count = 0

    async with httpx.AsyncClient(timeout=20.0, headers=DEFAULT_HEADERS, follow_redirects=True) as http_client:
        async def process_hill(hill):
            nonlocal enriched_count, k_fallback_count, not_found_count
            async with semaphore:
                hid = str(hill["_id"])
                place = (hill.get("place") or "").strip()
                p_lower = place.lower()
                size = hill.get("size")
                unit = hill.get("unit") or "HS"

                # 1. Sprawdzamy czy mamy sesje z PDF
                doc_entries = place_size_to_docs.get((p_lower, size), [])
                hill_data = {}

                for _, dtype, url in doc_entries[:3]:
                    try:
                        await asyncio.sleep(0.5)
                        r = await http_client.get(url)
                        if r.status_code == 429:
                            await asyncio.sleep(4.0)
                            r = await http_client.get(url)

                        if b"%PDF" in r.content[:10]:
                            doc = fitz.open(stream=r.content, filetype="pdf")
                            txt = doc[0].get_text("text")
                            extracted = parse_hill_data_from_pdf_text(txt)
                            if extracted.get("k_point") or extracted.get("size"):
                                hill_data = extracted
                                break
                    except Exception:
                        pass

                updates = {}
                if hill_data.get("k_point"):
                    updates["k_point"] = hill_data["k_point"]
                elif unit == "K" and size:
                    # Stara skocznia przed erą HS - rozmiar był punktem K
                    updates["k_point"] = float(size)
                    k_fallback_count += 1

                if hill_data.get("record"):
                    updates["record"] = hill_data["record"]
                if hill_data.get("record_holder"):
                    updates["record_holder"] = hill_data["record_holder"]
                if hill_data.get("meter_value"):
                    updates["meter_value"] = hill_data["meter_value"]
                if hill_data.get("type") and not hill.get("type"):
                    updates["type"] = hill_data["type"]

                if updates:
                    enriched_count += 1
                    if apply:
                        await hills_col.update_one({"_id": hid}, {"$set": updates})
                    return (hid, place, size, unit, updates)
                else:
                    not_found_count += 1
                    return None

        tasks = [process_hill(h) for h in all_hills]
        results = await asyncio.gather(*tasks)

    valid_results = [r for r in results if r]
    print(f"\nWyniki analizy:")
    print(f"  ✅ Wzbogacono skoczni (k_point / rekord): {len(valid_results)} / {len(all_hills)}")
    print(f"  ℹ️  W tym starych skoczni K przypisanych bezpośrednio: {k_fallback_count}")
    print(f"  ⚠️ Nie znaleziono danych w PDF dla: {not_found_count}")

    print("\nPrzykładowe zaktualizowane skocznie:")
    for hid, place, size, unit, up in valid_results[:12]:
        k_str = f"K-{up.get('k_point')}" if up.get('k_point') else "-"
        rec_str = f"{up.get('record')}m ({up.get('record_holder', '')})" if up.get('record') else "-"
        print(f"  {hid:<25} | {place:<18} | {unit} {size:<4} | {k_str:<8} | Rekord: {rec_str}")

    if apply:
        print("\n🎉 Zmiany zostały pomyślnie zapisane w bazie MongoDB!")
    else:
        print("\nℹ️  Aby zapisać te zmiany w bazie danych, uruchom:")
        print("    python -m app.scripts.enrich_hills_from_sessions --apply")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Zapisz zmiany w bazie danych")
    args = parser.parse_args()

    asyncio.run(enrich_hills(apply=args.apply))
