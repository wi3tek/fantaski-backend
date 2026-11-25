# fis_athletes_scraper.py
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
import time
import json
import re
from typing import List, Dict
import os
from datetime import datetime
from app.services.scrapers.http_client import safe_get

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
    athlete_status = status_div.text.strip() if status_div else None
    data["status"] = "Active" if  athlete_status == "Active" else "Inactive"

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

    birthdate_div = a_tag.select_one(".hidden-sm-down .g-lg-2, .g-lg-2.justify-right")
    if birthdate_div:
        birthdate_str = birthdate_div.text.strip()
        parsed_dt = None
        if birthdate_str:
            for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d"):
                try:
                    parsed_dt = datetime.strptime(birthdate_str, fmt)
                    break
                except ValueError:
                    pass
            if not parsed_dt and len(birthdate_str) == 4 and birthdate_str.isdigit():
                try:
                    parsed_dt = datetime(int(birthdate_str), 1, 1)
                except ValueError:
                    pass
        data["birthdate"] = parsed_dt
        if parsed_dt:
            data["birth_year"] = str(parsed_dt.year)
        else:
            data["birth_year"] = birthdate_str if len(birthdate_str) == 4 else None
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
def fetch_chunk(birthyear: str, limit: int = DEFAULT_LIMIT, offset: int = 0, headers: dict = DEFAULT_HEADERS) -> List[Dict]:
    params = {
        "lastname": "",
        "firstname": "",
        "sectorcode": "JP",
        "gendercode": "M",
        "birthyear": birthyear,
        "skiclub": "",
        "skis": "",
        "nationcode": "",
        "fiscode": "",
        "status": "",
        "search": "true",
        "limit": str(limit),
        "offset": str(offset),
    }
    resp = safe_get(AJAX_BASE, headers=headers, params=params, timeout=20.0)
    if not resp or not resp.text:
        return []

    return parse_fragment_html(resp.text)


# --- Główna funkcja iterująca po paczkach aż do wyczerpania ---
def fetch_all_athletes_by_chunks(birthyear:str,limit: int = DEFAULT_LIMIT) -> List[Dict]:
    all_results: List[Dict] = []
    seen_ids = set()
    offset = 0

    while True:
        print(f"Fetching chunk offset={offset} limit={limit}, year= {birthyear}")
        chunk = fetch_chunk(birthyear=birthyear,limit=limit, offset=offset)
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
                if r.get("birthdate") is None or r.get("status") is None:
                    continue

                # 3. Jeśli wszystko jest ok, dodajemy do ostatecznej listy
                all_results.append(r)
                added_to_results += 1

        print(
            f"Chunk returned {len(chunk)} rows. Found {new_seen_count} new unique IDs. Saved {added_to_results} valid athletes: {all_results}")

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