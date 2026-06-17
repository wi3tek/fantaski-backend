# fis_athletes_scraper.py
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
import time
import json
import re
from typing import List, Dict
import os
from datetime import datetime

# --- Konfiguracja ---
BASE_URL = "https://www.fis-ski.com"
AJAX_BASE = "https://data.fis-ski.com/fis_athletes/ajax/athletessearchfunctions/load_athleteslist.html"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.fis-ski.com"
}
DEFAULT_LIMIT = 1000

# --- Parsowanie pojedynczego wiersza (a.table-row) ---
def parse_athlete_row(a_tag) -> Dict:
    data = {}
    href = a_tag.get("href")
    data["profile_url"] = urljoin(BASE_URL, href) if href else None

    # status (np. Active)
    status_div = a_tag.select_one(".clip-xs")
    data["status"] = status_div.text.strip() if status_div else None

    # competitor id z pierwszej kolumny (liczba)
    comp_id_text = ""
    first_col = a_tag.select_one(".g-lg-3")
    if first_col:
        texts = [t.strip() for t in first_col.stripped_strings if t.strip()]
        for token in texts:
            if re.fullmatch(r"\d+", token):
                comp_id_text = token
                break
    data["fis_code"] = int(comp_id_text)

    # pełne imię i nazwisko
    name_div = None
    for sel in [
        ".container.g-row > div:nth-of-type(2)",
        ".g-lg + .g-lg",
        ".g-lg .justify-left",
        ".container .g-row > div:nth-of-type(2)",
    ]:
        name_div = a_tag.select_one(sel)
        if name_div and name_div.text.strip():
            break
    data["full_name"] = name_div.text.strip() if name_div else None

    # kraj (kod) i flaga
    country_short = a_tag.select_one(".country__name-short")
    data["country_code"] = country_short.text.strip() if country_short else None

    # wiek i data urodzenia / rok
    age_div = a_tag.select_one(".hidden-sm-down .q-sm-2, .g-lg-1.q-sm-2")
    age = age_div.text.strip() if age_div else ""

    if age and len(age) > 0:
        data["age"] = int(age)
    else:
        data["age"] = None

    birthdate_div = a_tag.select_one(".hidden-sm-down .g-lg-2, .g-lg-2.justify-right")
    if birthdate_div:
        birthdate = birthdate_div.text.strip()
        birthdate = "01-01-" + birthdate if len(birthdate) == 4 else birthdate

        try:
            if birthdate and len(birthdate) > 0:
                data["birthdate"] = datetime.strptime(birthdate, "%d-%m-%Y")
            else:
                data["birthdate"] = None
        except ValueError as e:
            # Wypisujemy dokładną informację, co spowodowało błąd
            print(f"\n[KRYTYCZNY BŁĄD DATY] Przerwano działanie scrapera!")
            print(f"Zawartość zmiennej 'birthdate': '{birthdate}'")

            # Rzucamy wyjątek dalej. Spowoduje to przerwanie skryptu
            # i wyświetlenie pełnego tracebacku (drzewa wywołań)
            raise e
    else:
        mobile_year = a_tag.select_one(".hidden-md-up .g-sm-3, .hidden-md-up .g-xs-6")
        data["birth_year"] = mobile_year.text.strip() if mobile_year else None


    # płeć
    gender_item = a_tag.select_one(".gender__item")
    data["gender"] = gender_item.text.strip() if gender_item else None

    # klub
    club_div = a_tag.select_one(".clip-sm")
    data["club"] = club_div.text.strip() if club_div else None

    # competitor id z URL (competitorid)
    if href:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        cid = qs.get("competitorid") or qs.get("competitorId") or qs.get("competitorID")
        comp_id = cid[0] if cid else 0
        data["competitor_id_from_url"] = int(comp_id)
    else:
        data["competitor_id_from_url"] = None

    return data

# --- Parsowanie HTML fragmentu zwróconego przez AJAX (lista <a>) ---
def parse_fragment_html(html_text: str) -> List[Dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    # fragment zawiera <a class="table-row"> ... </a>
    rows = soup.select("a.table-row")
    results = [parse_athlete_row(a) for a in rows]
    return results

# --- Pobranie jednej paczki (limit/offset) z data.fis-ski.com ---
def fetch_chunk(limit: int = DEFAULT_LIMIT, offset: int = 0, headers: dict = DEFAULT_HEADERS) -> List[Dict]:
    params = {
        "lastname": "",
        "firstname": "",
        "sectorcode": "JP",
        "gendercode": "M",
        "birthyear": "",
        "skiclub": "",
        "skis": "",
        "nationcode": "",
        "fiscode": "",
        "status": "O",
        "search": "true",
        "limit": str(limit),
        "offset": str(offset),
    }
    try:
        resp = requests.get(AJAX_BASE, headers=headers, params=params, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Request error for offset={offset}: {e}")
        return []

    return parse_fragment_html(resp.text)


# --- Główna funkcja iterująca po paczkach aż do wyczerpania ---
def fetch_all_athletes_by_chunks(limit: int = DEFAULT_LIMIT) -> List[Dict]:
    all_results: List[Dict] = []
    seen_ids = set()
    offset = 0

    while True:
        print(f"Fetching chunk offset={offset} limit={limit}")
        chunk = fetch_chunk(limit=limit, offset=offset)
        if not chunk:
            print("Empty chunk or request failed, stopping.")
            break

        # deduplikacja i agregacja
        new_seen_count = 0  # Liczy unikalne ID (żeby pętla wiedziała, że idzie do przodu)
        added_to_results = 0  # Liczy faktycznie zapisanych skoczków z poprawnymi danymi

        for r in chunk:
            cid = r.get("competitor_id_from_url") or r.get("competitor_id")

            if cid and cid not in seen_ids:
                # 1. Zaznaczamy, że widzieliśmy to ID (bezpieczeństwo pętli)
                seen_ids.add(cid)
                new_seen_count += 1

                # 2. FILTROWANIE: Jeśli brakuje wieku LUB daty urodzenia, pomijamy zapis do wyników
                if r.get("age") is None or r.get("birthdate") or r.get("status") is None:
                    continue

                # 3. Jeśli wszystko jest ok, dodajemy do ostatecznej listy
                all_results.append(r)
                added_to_results += 1

        print(
            f"Chunk returned {len(chunk)} rows. Found {new_seen_count} new unique IDs. Saved {added_to_results} valid athletes.")

        # jeśli zwrócono mniej niż limit, to koniec
        if len(chunk) < limit:
            print("Chunk size < limit -> last page reached.")
            break

        # Tę weryfikację opieramy na przeprocesowanych ID, a nie zapisanych!
        if new_seen_count == 0:
            print("No new unique records in this chunk -> stopping to avoid duplicates.")
            break

        offset += limit

    return all_results

# --- CLI / zapis do pliku ---
def main():
    athletes = fetch_all_athletes_by_chunks(limit=1000)
    print(f"Found {len(athletes)} unique athletes.")
    out_path = os.path.join("app", "scripts", "athletes_parsed.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(athletes, f, ensure_ascii=False, indent=2)
    print(f"Saved to {out_path}")

# if __name__ == "__main__":
#     main()
