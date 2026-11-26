import httpx
import urllib.parse
import unicodedata
import re
from bs4 import BeautifulSoup
import asyncio
from pymongo import UpdateOne
from app import config
from app.services.scrapers.http_client import safe_get

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
}

POLISH_MAP = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")

def normalize_text(text: str) -> str:
    text = text.translate(POLISH_MAP)
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = nfkd.encode("ASCII", "ignore").decode("utf-8")
    clean = re.sub(r"[^a-zA-Z0-9\s-]", " ", ascii_text)
    return clean.strip().lower()

def extract_significant_tokens(text: str) -> list[str]:
    tokens = normalize_text(text).split()
    return [t for t in tokens if len(t) > 1]

def get_esm_token() -> str:
    if not config.ESM_AUTH_URL or not config.CONFIG_EMAIL or not config.CONFIG_PASS:
        raise ValueError("Brak konfiguracji ESM_AUTH_URL / CONFIG_EMAIL / CONFIG_PASS w zmiennych środowiskowych.")
    print("[DEBUG] Pobieranie tokena ESM...")
    payload = {"email": config.CONFIG_EMAIL, "password": config.CONFIG_PASS, "from": "/joinTeam"}
    response = httpx.post(config.ESM_AUTH_URL, json=payload, timeout=15)
    response.raise_for_status()
    return response.text.strip()

def get_players_data(token: str) -> list:
    if not config.ESM_DATA_URL:
        raise ValueError("Brak konfiguracji ESM_DATA_URL w zmiennych środowiskowych.")
    print("[DEBUG] Pobieranie listy zawodników z ESM...")
    response = httpx.get(config.ESM_DATA_URL, headers={"x-auth-token": token}, timeout=15)
    response.raise_for_status()
    return response.json()

def get_ioc_code(team_name_eng: str, countries_dict: dict) -> str:
    for code, names in countries_dict.items():
        if names.get("eng", "").lower() == team_name_eng.lower():
            return code
    return ""

def scrape_fis_code(name: str, surname: str, ioc_code: str) -> str:
    safe_name = urllib.parse.quote(name.strip())
    safe_surname = urllib.parse.quote(surname.strip())
    safe_ioc = ioc_code.strip()

    search_url = (
        f"https://www.fis-ski.com/DB/ski-jumping/biographies.html"
        f"?lastname={safe_surname}&firstname={safe_name}&sectorcode=JP"
        f"&gendercode=M&birthyear=&skiclub=&skis=&nationcode={safe_ioc}&fiscode=&status=O&search=true"
    )

    response = safe_get(search_url, timeout=15.0)
    if not response or response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    table_body = soup.find('div', id='athletes-search')
    if not table_body:
        return None

    rows = table_body.find_all('a', class_='table-row')

    if len(rows) > 1:
        raise ValueError("DUPLICATE_IN_FIS")

    if not rows:
        return None

    first_row = rows[0]
    g_row = first_row.find('div', class_='g-row')
    col_divs = g_row.find_all('div', recursive=False)
    inner_divs = col_divs[0].find_all('div', recursive=False)
    fis_code = inner_divs[1].text.strip()

    if fis_code.isdigit():
        return fis_code

    return None

def find_athlete_in_pool(player: dict, ioc_code: str, athletes_by_ioc: dict) -> dict:
    """Dopasowuje zawodnika z ESM do skoczka w bazie danych bez odpytywania zewnętrznego serwisu FIS."""
    name = player.get("name", "").strip()
    surname = player.get("surname", "").strip()

    surname_tokens = extract_significant_tokens(surname)
    name_tokens = extract_significant_tokens(name)

    pool = athletes_by_ioc.get(ioc_code, [])
    candidates = []

    for cand in pool:
        if all(st in cand["norm"] for st in surname_tokens):
            if any(nt in cand["norm"] for nt in name_tokens):
                candidates.append(cand)

    if len(candidates) > 1:
        active_cands = [c for c in candidates if c.get("status") == "Active"]
        if len(active_cands) == 1:
            candidates = active_cands

    if len(candidates) == 1:
        return candidates[0]["raw"]
    elif len(candidates) > 1:
        return {"_error": "DUPLICATE_IN_DB", "candidates": candidates}
    return None

# DODANE: Flaga dry_run, która pomija zapis bezpośredni
async def update_esm_prices(db, countries_dict: dict, dry_run: bool = False):
    try:
        token = await asyncio.to_thread(get_esm_token)
        all_players = await asyncio.to_thread(get_players_data, token)
    except Exception as e:
        raise Exception(f"Błąd podczas łączenia z API ESM: {e}")

    active_players = [p for p in all_players if p.get("isDisabled") is False]

    # Pobieramy wszystkich skoczków z bazy do szybkiego matchowania lokalnego (brak zapytań HTTP do FIS)
    athletes_cursor = db["athletes"].find(
        {},
        {"fis_code": 1, "full_name": 1, "country_code": 1, "status": 1}
    )
    db_athletes = await athletes_cursor.to_list(length=None)

    athletes_by_ioc = {}
    for a in db_athletes:
        ioc = a.get("country_code", "")
        if ioc not in athletes_by_ioc:
            athletes_by_ioc[ioc] = []

        fn_norm = normalize_text(a.get("full_name", ""))
        athletes_by_ioc[ioc].append({
            "raw": a,
            "norm": fn_norm,
            "status": a.get("status")
        })

    bulk_operations = []
    esm_updates = {}
    updated_athletes = []
    not_found_in_db_athletes = []
    not_scraped_athletes = []
    no_ioc_code_athletes = []
    duplicate_in_fis_athletes = []

    for player in active_players:
        name = player.get("name", "")
        surname = player.get("surname", "")
        team_eng = player.get("team", "")
        full_name = f"{name} {surname}".strip()

        ioc_code = get_ioc_code(team_eng, countries_dict)
        if not ioc_code:
            no_ioc_code_athletes.append(full_name)
            continue

        matched_db_athlete = find_athlete_in_pool(player, ioc_code, athletes_by_ioc)

        if isinstance(matched_db_athlete, dict) and matched_db_athlete.get("_error") == "DUPLICATE_IN_DB":
            duplicate_in_fis_athletes.append(full_name)
            continue

        if not matched_db_athlete:
            not_found_in_db_athletes.append(full_name)
            continue

        fis_code = int(matched_db_athlete["fis_code"])
        price = player.get("price", 0.0)
        is_disabled = player.get("isDisabled", False)
        is_injured = player.get("isInjured", False)

        operation = UpdateOne(
            {"fis_code": fis_code},
            {"$set": {
                "price": price,
                "isDisabled": is_disabled,
                "isInjured": is_injured
            }}
        )

        if not dry_run:
            bulk_operations.append(operation)

        esm_updates[fis_code] = {
            "price": price,
            "isDisabled": is_disabled,
            "isInjured": is_injured
        }
        updated_athletes.append(full_name)

    db_saved_count = 0
    if bulk_operations and not dry_run:
        try:
            write_result = await db["athletes"].bulk_write(bulk_operations)
            db_saved_count = write_result.matched_count
            print(f"[DEBUG] Pomyślnie wykonano bulk_write. Zmatchowano: {write_result.matched_count}")
        except Exception as e:
            print(f"[DEBUG] KRYTYCZNY BŁĄD PODCZAS ZAPISU DO BAZY (bulk_write): {e}")
            raise Exception(f"Błąd zapisu danych zawodników do bazy: {e}")

    return {
        "status": "success",
        "summary": {
            "total_esm_players": len(all_players),
            "filtered_active_players": len(active_players),
            "successfully_saved_to_db": db_saved_count,
            "not_found_in_db_count": len(not_found_in_db_athletes),
            "duplicate_in_fis_count": len(duplicate_in_fis_athletes),
            "not_scraped_count": len(not_scraped_athletes),
            "no_ioc_code_count": len(no_ioc_code_athletes)
        },
        "details": {
            "updated_and_saved": updated_athletes,
            "esm_updates": esm_updates,
            "not_found_in_db": not_found_in_db_athletes,
            "duplicate_in_fis_error": duplicate_in_fis_athletes,
            "not_scraped_error": not_scraped_athletes,
            "no_ioc_code_error": no_ioc_code_athletes
        }
    }