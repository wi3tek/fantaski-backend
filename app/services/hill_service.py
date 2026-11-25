# app/services/hill_service.py
"""
Serwis zarządzania 3-poziomową hierarchią skoczni narciarskich:
Poziom 1: Miejscowość / Kompleks (place, country_code)
Poziom 2: Rodzaj skoczni (FLYING, LARGE, NORMAL, SMALL)
Poziom 3: Wersje skoczni (z flagą is_current: bool oraz parametrami HS, K, rekord)
"""

from typing import Optional, List, Dict, Any
import re
from pymongo import UpdateOne
from app.infrastructure.cache import clear_cache


TYPE_ORDER = {
    "FLYING": 1,
    "LARGE": 2,
    "NORMAL": 3,
    "SMALL": 4,
}

TYPE_LABELS = {
    "FLYING": "Skocznia mamucia",
    "LARGE": "Duża skocznia",
    "NORMAL": "Normalna skocznia",
    "SMALL": "Mała / średnia skocznia",
}

# ==========================================
# SŁOWNIK ALIASÓW I NORMALIZACJA MIEJSCOWOŚCI
# ==========================================
# Zapobiega tworzeniu sztucznych duplikatów skoczni wynikających ze zmiennego nazewnictwa w FIS
KNOWN_PLACE_ALIASES: Dict[str, str] = {
    # Bad Mitterndorf / Kulm / Tauplitz (AUT)
    "kulm": "Bad Mitterndorf",
    "kulm mitterndorf": "Bad Mitterndorf",
    "kulm bad mitterndorf/tauplitz": "Bad Mitterndorf",
    "kulm bad mitterndorftauplitz": "Bad Mitterndorf",
    "tauplitz/bad mitterndorf": "Bad Mitterndorf",
    "tauplitz/ bad mitterndorf": "Bad Mitterndorf",
    "tauplitz bad mitterndorf": "Bad Mitterndorf",
    "tauplitz": "Bad Mitterndorf",
    "bad mitterndorf": "Bad Mitterndorf",
    "bad mitterndorf/tauplitz": "Bad Mitterndorf",
    "bad mitterndorf / tauplitz": "Bad Mitterndorf",

    # Titisee-Neustadt (GER)
    "titisee neustadt": "Titisee-Neustadt",
    "titisee-neustadt": "Titisee-Neustadt",
    "neustadt": "Titisee-Neustadt",

    # Szczyrk (POL)
    "szczyrk-skaliterk": "Szczyrk",
    "szczyrk skaliterk": "Szczyrk",
    "szczyrk-skalite": "Szczyrk",
    "szczyrk skalite": "Szczyrk",
    "szczyrk": "Szczyrk",

    # Wisła (POL)
    "wisla-malinka": "Wisla",
    "wisla malinka": "Wisla",
    "wisla": "Wisla",

    # Sapporo (JPN)
    "sapporo okuraymama": "Sapporo",
    "sapporo okurayama": "Sapporo",
    "sapporo miyanomori": "Sapporo",
    "sapporo": "Sapporo",

    # Frenstat (CZE)
    "frenstat od radhostem": "Frenstat pod Radhostem",
    "frenstat pod radhostem": "Frenstat pod Radhostem",
    "frenstat": "Frenstat pod Radhostem",

    # Lake Placid (USA)
    "lake placid, ny/orda": "Lake Placid",
    "lake placid/orda": "Lake Placid",
    "lake placid, ny": "Lake Placid",
    "lake placid ny": "Lake Placid",
    "lake placid": "Lake Placid",

    # Park City (USA)
    "park city, ut": "Park City",
    "park city ut": "Park City",
    "utah olympic park, park city": "Park City",
    "park city": "Park City",

    # Eau Claire (USA)
    "eau claire, wi": "Eau Claire",
    "eau claire - silver mine hill": "Eau Claire",
    "eau claire": "Eau Claire",

    # Iron Mountain (USA)
    "iron mountain, mi": "Iron Mountain",
    "iron mountain": "Iron Mountain",

    # Chaikovsky (RUS)
    "chaikovski": "Chaikovsky",
    "chaikovskiy": "Chaikovsky",
    "chaikovsky": "Chaikovsky",

    # Nizhny Tagil (RUS)
    "nizhniy tagil": "Nizhny Tagil",
    "nizhny tagil": "Nizhny Tagil",

    # Ljubno (SLO)
    "ljubno": "Ljubno ob Savinji",
    "ljubno ob savinji": "Ljubno ob Savinji",

    # Pyeongchang (KOR)
    "alpensia resort": "Pyeongchang",
    "alpensia": "Pyeongchang",
    "pyeongchang": "Pyeongchang",

    # Brotterode (GER)
    "brotterode-trusetal": "Brotterode",
    "brotterode": "Brotterode",
    "brotterode/oberhof": "Oberhof",

    # Erzurum (TUR)
    "erzurum - kiremitliktepe": "Erzurum",
    "erzurum": "Erzurum",

    # Zhangjiakou (CHN)
    "zhangjiakou national ski jumping ceter": "Zhangjiakou",
    "zhangjiakou national ski jumping center": "Zhangjiakou",
    "zhangjiakou": "Zhangjiakou",

    # Garmisch-Partenkirchen (GER)
    "garmisch partenkirchen": "Garmisch-Partenkirchen",
    "garmisch-partenkirchen": "Garmisch-Partenkirchen",
}


def normalize_place_name(raw_place: Optional[str], country_code: Optional[str] = None) -> str:
    """
    Zwraca kanoniczną, ujednoliconą nazwę miejscowości/kompleksu skoczni.
    Rozwiązuje problem wielu nazw tej samej skoczni (np. Kulm Bad Mitterndorf/Tauplitz -> Bad Mitterndorf).
    """
    if not raw_place:
        return ""
    clean = raw_place.strip()
    clean_lower = re.sub(r'\s+', ' ', clean.lower())
    clean_no_slash = clean_lower.replace('/', ' ').replace('-', ' ')
    clean_no_slash = re.sub(r'\s+', ' ', clean_no_slash).strip()

    # 1. Dokładne dopasowanie w słowniku aliasów
    if clean_lower in KNOWN_PLACE_ALIASES:
        return KNOWN_PLACE_ALIASES[clean_lower]
    if clean_no_slash in KNOWN_PLACE_ALIASES:
        return KNOWN_PLACE_ALIASES[clean_no_slash]

    # 2. Heurystyka dla skoczni mamuciej w Austrii (Bad Mitterndorf / Kulm / Tauplitz)
    if (not country_code or country_code.upper() == "AUT") and any(w in clean_lower for w in ["kulm", "mitterndorf", "tauplitz"]):
        return "Bad Mitterndorf"

    return clean


def infer_hill_type(size: Optional[int]) -> str:
    """Określa typ skoczni na podstawie rozmiaru HS/K."""
    if not size or size <= 0:
        return "LARGE"
    if size >= 185:
        return "FLYING"
    if size >= 110:
        return "LARGE"
    if size >= 85:
        return "NORMAL"
    return "SMALL"


def get_hill_type_label(htype: Optional[str]) -> str:
    """Zwraca czytelną polską nazwę rodzaju skoczni."""
    if not htype:
        return "Inna skocznia"
    return TYPE_LABELS.get(htype.upper(), htype.capitalize())


async def record_hill_version_from_session(
    db,
    place: str,
    country_code: str,
    hill_size: int,
    hill_unit: str = "HS",
    hill_type: Optional[str] = None,
    session_date: Optional[str] = None,
    season: Optional[str] = None,
    k_point: Optional[float] = None,
    record: Optional[float] = None,
    record_holder: Optional[str] = None,
    meter_value: Optional[float] = None,
) -> str:
    """
    Rejestruje lub aktualizuje wersję skoczni na podstawie danych ze scrapowanej sesji.
    Zapewnia automatyczne utrzymanie flagi is_current (najnowsza wersja dla danej miejscowości i typu).
    """
    if not place or not hill_size:
        return ""

    canon_place = normalize_place_name(place, country_code)
    unit = (hill_unit or "HS").strip().upper()
    effective_type = (hill_type or infer_hill_type(hill_size)).strip().upper()
    hill_id = f"{canon_place.strip().upper()}_{unit}{hill_size}"

    hills_col = db["hills"]
    existing_hill = await hills_col.find_one({"_id": hill_id})

    update_fields: Dict[str, Any] = {
        "place": canon_place,
        "country_code": (country_code or "UNKNOWN").strip().upper(),
        "size": int(hill_size),
        "unit": unit,
        "type": effective_type,
    }

    if k_point is not None:
        update_fields["k_point"] = float(k_point)
    if record is not None:
        update_fields["record"] = float(record)
    if record_holder is not None:
        update_fields["record_holder"] = record_holder.strip()
    if meter_value is not None:
        update_fields["meter_value"] = float(meter_value)

    # Sprawdzamy czy ta sesja jest nowsza
    should_be_current = False
    if existing_hill and existing_hill.get("is_current"):
        should_be_current = True
    else:
        # Szukamy aktualnej wersji dla tej samej miejscowości i typu
        current_counterpart = await hills_col.find_one({
            "place": {"$regex": f"^{re.escape(place.strip())}$", "$options": "i"},
            "type": effective_type,
            "is_current": True,
        })
        if not current_counterpart:
            should_be_current = True
        elif session_date:
            last_date = current_counterpart.get("last_session_date") or ""
            if session_date >= last_date:
                should_be_current = True

    update_fields["is_current"] = should_be_current

    if session_date:
        if not existing_hill or not existing_hill.get("last_session_date") or session_date >= existing_hill.get("last_session_date", ""):
            update_fields["last_session_date"] = session_date
    if season:
        if not existing_hill or not existing_hill.get("last_season") or season >= existing_hill.get("last_season", ""):
            update_fields["last_season"] = season

    await hills_col.update_one(
        {"_id": hill_id},
        {
            "$set": update_fields,
            "$inc": {"total_sessions": 1} if not existing_hill else {}
        },
        upsert=True
    )

    # Jeśli ta wersja została oznaczona jako current, wyłączamy inne wersje w tym kompleksie
    if should_be_current:
        await hills_col.update_many(
            {
                "place": {"$regex": f"^{re.escape(place.strip())}$", "$options": "i"},
                "type": effective_type,
                "_id": {"$ne": hill_id}
            },
            {"$set": {"is_current": False}}
        )

    clear_cache("long")
    return hill_id


async def bulk_record_hill_versions(db, hill_entries: List[Dict[str, Any]]) -> int:
    """
    Masowo aktualizuje wersje skoczni na podstawie listy wpisów ze scrapera,
    a następnie synchronizuje flagę is_current dla zaktualizowanych kompleksów.
    """
    if not hill_entries:
        return 0

    hills_col = db["hills"]
    bulk_ops = []
    affected_complexes = set()

    for entry in hill_entries:
        place = (entry.get("place") or "").strip()
        size = entry.get("size")
        unit = (entry.get("unit") or "HS").strip().upper()
        if not place or not size:
            continue

        htype = (entry.get("type") or infer_hill_type(size)).strip().upper()
        hill_id = f"{place.upper()}_{unit}{size}"
        affected_complexes.add((place, htype))

        update_fields = {
            "place": place,
            "country_code": (entry.get("country_code") or "UNKNOWN").strip().upper(),
            "size": int(size),
            "unit": unit,
            "type": htype,
        }
        if entry.get("k_point") is not None:
            update_fields["k_point"] = float(entry["k_point"])
        if entry.get("record") is not None:
            update_fields["record"] = float(entry["record"])
        if entry.get("record_holder"):
            update_fields["record_holder"] = entry["record_holder"].strip()
        if entry.get("last_session_date"):
            update_fields["last_session_date"] = entry["last_session_date"]
        if entry.get("last_season"):
            update_fields["last_season"] = entry["last_season"]

        bulk_ops.append(
            UpdateOne(
                {"_id": hill_id},
                {
                    "$set": update_fields,
                    "$inc": {"total_sessions": 1}
                },
                upsert=True
            )
        )

    if bulk_ops:
        await hills_col.bulk_write(bulk_ops)

    # Synchronizacja is_current dla dotkniętych kompleksów
    for place, htype in affected_complexes:
        hills_in_group = await hills_col.find({
            "place": {"$regex": f"^{re.escape(place)}$", "$options": "i"},
            "type": htype
        }).to_list(100)

        if not hills_in_group:
            continue

        sorted_group = sorted(
            hills_in_group,
            key=lambda h: (
                h.get("last_session_date") or "",
                1 if h.get("unit") == "HS" else 0,
                h.get("size") or 0,
                1 if h.get("record") else 0
            ),
            reverse=True
        )

        current_id = sorted_group[0]["_id"]
        await hills_col.update_one({"_id": current_id}, {"$set": {"is_current": True}})
        if len(sorted_group) > 1:
            other_ids = [h["_id"] for h in sorted_group[1:]]
            await hills_col.update_many({"_id": {"$in": other_ids}}, {"$set": {"is_current": False}})

    clear_cache("long")
    return len(bulk_ops)


async def set_current_version(db, hill_id: str) -> bool:
    """Ręcznie ustawia wskazaną wersję jako aktualną (is_current=True) w jej kompleksie i typie."""
    hills_col = db["hills"]
    target = await hills_col.find_one({"_id": hill_id})
    if not target:
        return False

    place = target.get("place", "")
    htype = target.get("type", "")

    # Wyłączamy is_current na wszystkich wersjach tego typu w miejscowości
    await hills_col.update_many(
        {
            "place": {"$regex": f"^{re.escape(place.strip())}$", "$options": "i"},
            "type": htype,
        },
        {"$set": {"is_current": False}}
    )

    # Ustawiamy is_current = True na wybranej
    await hills_col.update_one(
        {"_id": hill_id},
        {"$set": {"is_current": True}}
    )

    clear_cache("long")
    return True


async def get_hills_hierarchy(db, query: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Zwraca ustrukturyzowaną 3-poziomową hierarchię skoczni:
    [
      {
        "place": "Zakopane",
        "country_code": "POL",
        "total_types": 2,
        "types": [
          {
            "type": "LARGE",
            "label": "Duża skocznia",
            "current_version": { ... },
            "versions": [ ... ]
          }
        ]
      }
    ]
    """
    hills_col = db["hills"]
    mongo_filter = {}
    if query:
        escaped_q = re.escape(query.strip())
        mongo_filter = {
            "$or": [
                {"place": {"$regex": escaped_q, "$options": "i"}},
                {"country_code": {"$regex": escaped_q, "$options": "i"}},
                {"_id": {"$regex": escaped_q, "$options": "i"}},
            ]
        }

    raw_hills = await hills_col.find(mongo_filter).to_list(length=2000)

    # Formatujemy hill_id
    for h in raw_hills:
        h["hill_id"] = str(h.pop("_id"))
        if "is_current" not in h:
            h["is_current"] = True

    # Grupowanie poziom 1: place + country_code
    grouped_places: Dict[str, Dict[str, Any]] = {}

    for h in raw_hills:
        raw_place = (h.get("place") or "Nieznane").strip()
        country = (h.get("country_code") or "UNK").strip().upper()
        place = normalize_place_name(raw_place, country)
        key = f"{place.lower()}_{country}"

        if key not in grouped_places:
            grouped_places[key] = {
                "place": place,
                "country_code": country,
                "types_map": {}
            }

        htype = (h.get("type") or infer_hill_type(h.get("size"))).strip().upper()
        if htype not in grouped_places[key]["types_map"]:
            grouped_places[key]["types_map"][htype] = []

        grouped_places[key]["types_map"][htype].append(h)

    result = []
    for key, p_data in sorted(grouped_places.items(), key=lambda x: x[1]["place"]):
        types_list = []
        for htype, versions in sorted(
            p_data["types_map"].items(),
            key=lambda item: TYPE_ORDER.get(item[0], 99)
        ):
            # Sortujemy wersje: najpierw is_current == True, potem wg daty / rozmiaru malejąco
            sorted_versions = sorted(
                versions,
                key=lambda v: (
                    1 if v.get("is_current") else 0,
                    v.get("last_session_date") or "",
                    1 if v.get("unit") == "HS" else 0,
                    v.get("size") or 0
                ),
                reverse=True
            )

            current_ver = next((v for v in sorted_versions if v.get("is_current")), sorted_versions[0])

            types_list.append({
                "type": htype,
                "label": get_hill_type_label(htype),
                "current_version": current_ver,
                "versions": sorted_versions,
                "versions_count": len(sorted_versions)
            })

        result.append({
            "place": p_data["place"],
            "country_code": p_data["country_code"],
            "total_types": len(types_list),
            "types": types_list
        })

    return result


async def resolve_event_hill_data(
    db,
    event_sessions: List[Dict[str, Any]],
    place: str,
    country: str
) -> Optional[Dict[str, Any]]:
    """
    Automatycznie odnajduje parametry skoczni dla eventu, gdy strona wyników FIS HTML
    nie zawiera parametru HS w nagłówku:
    1. Sprawdza czy którakolwiek sesja w evencie ma już hillSize.
    2. Sprawdza istniejącą bazę hills dla danej miejscowości i typu (is_current: True).
    3. W razie braku, pobiera pierwszą stronę załącznika PDF (Official Results, Qualification Results, Training)
       i wyciąga HS, punkt K, rekord oraz typ z oficjalnych metadanych FIS.
    """
    clean_place = (place or "").strip()
    if not clean_place:
        return None

    # 1. Sprawdzamy czy któraś sesja już ma hillSize
    for s in event_sessions:
        if s.get("hillSize"):
            return {
                "size": int(s["hillSize"]),
                "unit": (s.get("hill_size_unit") or "HS").strip().upper(),
                "type": (s.get("hillType") or infer_hill_type(s["hillSize"])).strip().upper()
            }

    # 2. Sprawdzamy czy w kolekcji hills mamy aktualną wersję dla tego miejsca
    existing = await db["hills"].find_one({
        "place": {"$regex": f"^{re.escape(clean_place)}$", "$options": "i"},
        "is_current": True
    })
    if existing and existing.get("size"):
        return {
            "size": int(existing["size"]),
            "unit": (existing.get("unit") or "HS").strip().upper(),
            "type": (existing.get("type") or infer_hill_type(existing["size"])).strip().upper(),
            "k_point": existing.get("k_point"),
            "record": existing.get("record"),
            "record_holder": existing.get("record_holder"),
            "meter_value": existing.get("meter_value")
        }

    # 3. Pobieramy pierwszą stronę załącznika PDF z sesji tego eventu
    from app.services.scrapers.http_client import safe_get
    from app.services.scrapers.pdf_training_scraper import parse_hill_data_from_pdf_text
    import fitz

    for s in event_sessions:
        for d in s.get("documents", []):
            url = d.get("url")
            dtype = (d.get("type") or "").lower()
            if url and any(x in dtype for x in ["results", "qualification", "training"]):
                if "/competition-attachment/" in url and not url.lower().endswith(".pdf"):
                    url = f"{url}.pdf"
                try:
                    resp = safe_get(url)
                    if resp and resp.content and b"%PDF" in resp.content[:10]:
                        doc = fitz.open(stream=resp.content, filetype="pdf")
                        txt = doc[0].get_text()
                        hdata = parse_hill_data_from_pdf_text(txt)
                        if hdata and hdata.get("size"):
                            return hdata
                except Exception as e:
                    print(f"⚠️ Nie udało się wyciągnąć danych skoczni z PDF ({url}): {e}")
                    pass

    return None

