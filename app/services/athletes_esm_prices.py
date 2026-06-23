import requests
import urllib.parse
from bs4 import BeautifulSoup
import asyncio
from pymongo import UpdateOne
import config

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
}

def get_esm_token() -> str:
    print("[DEBUG] Pobieranie tokena ESM...")
    payload = {"email": config.CONFIG_EMAIL, "password": config.CONFIG_PASS, "from": "/joinTeam"}
    response = requests.post(config.ESM_AUTH_URL, json=payload, timeout=15)
    response.raise_for_status()
    return response.text.strip()

def get_players_data(token: str) -> list:
    print("[DEBUG] Pobieranie listy zawodników z ESM...")
    response = requests.get(config.ESM_DATA_URL, headers={"x-auth-token": token}, timeout=15)
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

    response = requests.get(search_url, headers=DEFAULT_HEADERS, timeout=10)
    if response.status_code != 200:
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

async def process_single_player(player: dict, db, countries_dict: dict, semaphore: asyncio.Semaphore):
    async with semaphore:
        name = player.get("name", "")
        surname = player.get("surname", "")
        team_eng = player.get("team", "")
        full_name = f"{name} {surname}".strip()

        ioc_code = get_ioc_code(team_eng, countries_dict)
        if not ioc_code:
            return {"status": "no_ioc_code", "name": full_name}

        try:
            fis_code_str = await asyncio.to_thread(scrape_fis_code, name, surname, ioc_code)
        except ValueError as e:
            if str(e) == "DUPLICATE_IN_FIS":
                return {"status": "duplicate_in_fis", "name": full_name}
            return {"status": "not_scraped", "name": f"{full_name} ({e})"}
        except Exception as e:
            return {"status": "not_scraped", "name": f"{full_name} ({e})"}

        if not fis_code_str:
            return {"status": "not_scraped", "name": full_name}

        fis_code = int(fis_code_str)

        athletes_col = db["athletes"]
        db_athlete = await athletes_col.find_one({"fis_code": fis_code})

        if db_athlete:
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
            # DODANE: Zwracamy konkretne dane do weryfikacji poza bazą (dry_run)
            return {
                "status": "success",
                "name": full_name,
                "fis_code": fis_code,
                "esm_data": {
                    "price": price,
                    "isDisabled": is_disabled,
                    "isInjured": is_injured
                },
                "operation": operation
            }
        else:
            return {"status": "not_found_in_db", "name": f"{full_name} (Odnaleziony FIS: {fis_code})"}

# DODANE: Flaga dry_run, która pomija zapis bezpośredni
async def update_esm_prices(db, countries_dict: dict, dry_run: bool = False):
    try:
        token = await asyncio.to_thread(get_esm_token)
        all_players = await asyncio.to_thread(get_players_data, token)
    except Exception as e:
        raise Exception(f"Błąd podczas łączenia z API ESM: {e}")

    active_players = [p for p in all_players if p.get("isDisabled") is False]

    semaphore = asyncio.Semaphore(15)
    tasks = [process_single_player(player, db, countries_dict, semaphore) for player in active_players]
    results = await asyncio.gather(*tasks)

    bulk_operations = []
    esm_updates = {} # Zbiera wyciągnięte obiekty
    updated_athletes = []
    not_found_in_db_athletes = []
    not_scraped_athletes = []
    no_ioc_code_athletes = []
    duplicate_in_fis_athletes = []

    for result in results:
        status = result["status"]
        athlete_name = result["name"]

        if status == "success":
            if not dry_run:
                bulk_operations.append(result["operation"])
            esm_updates[result["fis_code"]] = result["esm_data"]
            updated_athletes.append(athlete_name)
        elif status == "not_found_in_db":
            not_found_in_db_athletes.append(athlete_name)
        elif status == "not_scraped":
            not_scraped_athletes.append(athlete_name)
        elif status == "no_ioc_code":
            no_ioc_code_athletes.append(athlete_name)
        elif status == "duplicate_in_fis":
            duplicate_in_fis_athletes.append(athlete_name)

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
            "esm_updates": esm_updates, # To leci do controllera
            "not_found_in_db": not_found_in_db_athletes,
            "duplicate_in_fis_error": duplicate_in_fis_athletes,
            "not_scraped_error": not_scraped_athletes,
            "no_ioc_code_error": no_ioc_code_athletes
        }
    }