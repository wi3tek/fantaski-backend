import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs
import re
from app import config


def parse_hill_info(title):
    """
    Na podstawie tytułu wyciąga rozmiar skoczni (liczba po HS) oraz jej typ.
    """
    if not title:
        return None, None

    hill_size = None
    hill_type = None

    # Wyciąganie hillSize (szukamy 'HS' i łapiemy ciąg cyfr po nim)
    hs_match = re.search(r'HS(\d+)', title)
    if hs_match:
        hill_size = int(hs_match.group(1))

    # Wyciąganie hillType
    if "Flying Hill" in title:
        hill_type = "FLYING"
    elif "Large Hill" in title:
        hill_type = "LARGE"
    elif "Normal Hill" in title:
        hill_type = "NORMAL"

    return hill_size, hill_type


def fetch_hillsize_from_race_url(race_url):
    """
    Dodatkowa funkcja. Wchodzi bezpośrednio w raceUrl i pobiera
    hillSize z precyzyjnej klasy 'event-header__kind' w HTML.
    """
    if not race_url:
        return None

    try:
        response = requests.get(race_url, headers=config.DEFAULT_HEADERS, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            kind_div = soup.find('div', class_='event-header__kind')

            if kind_div:
                match = re.search(r'HS\s*(\d+)', kind_div.text, re.IGNORECASE)
                if match:
                    return int(match.group(1))
    except Exception as e:
        print(f" [!] Błąd pobierania rozmiaru skoczni z {race_url}: {e}")

    return None


def fetch_event_details(sectorcode="JP", eventid="", seasoncode="", filter_gender=None, mode="IND", categorycode=None,
                        **kwargs):
    endpoint = "general/event-details.html"
    url = config.FIS_BASE_URL + endpoint

    params = {
        "sectorcode": sectorcode,
        "eventid": eventid,
        "seasoncode": seasoncode
    }
    params.update(kwargs)

    try:
        print(f"Pobieranie szczegółów eventu (ID: {eventid})...")
        response = requests.get(url, headers=config.DEFAULT_HEADERS, params=params)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.find_all('div', class_='table-row')

        results = []

        for row in rows:
            race_data = {}

            # --- PŁEĆ WYŚCIGU ---
            gender_div = row.find('div', class_='gender__item')
            race_gender = gender_div.text.strip() if gender_div else None

            if filter_gender in ['M', 'W']:
                if race_gender != filter_gender:
                    continue
            race_data['gender'] = race_gender

            # --- KATEGORIA WYŚCIGU ---
            race_category = None
            category_tag_desktop = row.find('a', class_='g-lg-2 g-xs-2 justify-center hidden-sm-down')
            if category_tag_desktop:
                race_category = category_tag_desktop.text.strip()
            else:
                category_tag_mobile = row.find('div', class_='g-xs-12 justify-left')
                if category_tag_mobile:
                    race_category = category_tag_mobile.text.strip()
            race_data['session_type'] = 'QUA' if race_category == 'QUA' else 'COMPETITION'
            race_data['race_category'] = race_category

            if race_category != 'QUA' and race_category != categorycode:
                continue

            race_data['type'] = mode

            # --- NAZWA WYDARZENIA I FILTROWANIE ---
            title_div = row.find('div', class_='clip')
            title = title_div.text.strip() if title_div else ""

            # 1. Globalne wykluczenia (np. Overall Standings)
            if any(title.startswith(prefix) for prefix in config.EXCLUDED_TITLES_GLOBAL):
                continue

            # 2. Sprawdzamy, czy są to zawody drużynowe
            is_team_comp = any(title.startswith(prefix) for prefix in config.TEAM_COMPETITION_PREFIXES)

            # 3. Filtrujemy na podstawie wybranego trybu
            if mode == "IND" and is_team_comp:
                continue
            if mode == "TEAM" and not is_team_comp:
                continue

            race_data['title'] = title

            # --- WYCIĄGANIE RACE ID (Przesunięte wyżej, by url był dostępny do sprawdzenia HS) ---
            first_link = row.find('a', href=True)
            if first_link:
                parsed_url = urlparse(first_link['href'])
                query_params = parse_qs(parsed_url.query)
                race_id = query_params.get('raceid', [None])[0]
                race_data['raceId'] = race_id
                race_data[
                    'raceUrl'] = f"{config.FIS_BASE_URL}general/results.html?sectorcode={sectorcode}&raceid={race_id}"
            else:
                race_data['raceId'] = None
                race_data['raceUrl'] = None

            # --- INFORMACJE O SKOCZNI ---
            hill_size, hill_type = parse_hill_info(title)

            # WYZWALACZ: Jeśli nie ma hill_size w tytule, to idziemy do źródła (raceUrl)
            if not hill_size and race_data['raceUrl']:
                hill_size = fetch_hillsize_from_race_url(race_data['raceUrl'])

            race_data['hillSize'] = hill_size
            race_data['hillType'] = hill_type

            # --- DATA I CZAS ---
            date_div = row.find('div', class_='timezone-date')
            time_div = row.find('div', class_='timezone-time')
            race_data['date'] = date_div['data-date'] if date_div and date_div.has_attr('data-date') else None
            race_data['time'] = time_div['data-time'] if time_div and time_div.has_attr('data-time') else None

            # --- DOKUMENTY (PDFy i ich statystyki) ---
            documents = []
            documents_section = row.find_all('div', class_='drop-btn__item')

            main_codex = None
            for doc_item in documents_section:
                doc_link = doc_item.find('a', href=True)

                if doc_link:
                    doc_gender = doc_link.get('data-ga-gender')
                    if filter_gender in ['M', 'W']:
                        if doc_gender and doc_gender != filter_gender:
                            continue

                    doc_span = doc_item.find('span')
                    doc_name_full = doc_span.text.strip() if doc_span else "Brak nazwy"
                    doc_codex = doc_link.get('data-ga-codex')

                    if not main_codex and doc_codex:
                        main_codex = doc_codex

                    documents.append({
                        "filename_and_size": doc_name_full,
                        "url": doc_link['href'],
                        "codex": doc_codex,
                        "type": doc_link.get('data-ga-download'),
                        "gender": doc_gender
                    })

            race_data['codex'] = main_codex
            race_data['documents'] = documents

            if race_data.get('raceId') or race_data.get('title'):
                results.append(race_data)

        return results

    except requests.exceptions.RequestException as e:
        print(f"Błąd sieci: {e}")
        return []


# --- TESTOWANIE ---
if __name__ == "__main__":
    print("--- TEST: TRYB TEAM ---")
    data_team = fetch_event_details(sectorcode="JP", eventid="55900", seasoncode="2025", filter_gender="M", mode="TEAM")
    for d in data_team:
        print(f"[TEAM] Znaleziono: {d.get('title')} - HS: {d.get('hillSize')}")

    print("\n--- TEST: TRYB IND ---")
    data_ind = fetch_event_details(sectorcode="JP", eventid="55900", seasoncode="2025", filter_gender="M", mode="IND")
    for d in data_ind:
        print(f"[IND] Znaleziono: {d.get('title')} - HS: {d.get('hillSize')}")