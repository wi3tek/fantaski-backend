import httpx
from bs4 import BeautifulSoup
from app import config
from app.services.scrapers.http_client import safe_get


def fetch_fis_calendar(seasoncode="2026", categorycode="WC", gendercode="M", seasonmonth="X-2026", **kwargs):
    # 1. Czysty adres bazowy (bez podwójnych ukośników)
    endpoint = "ski-jumping/calendar-results.html"
    base_url = config.FIS_BASE_URL.rstrip("/") + "/" + endpoint
    # 2. Słownik domyślnych parametrów (odzwierciedla Twój oryginalny link)
    params = {
        "eventselection": "results",
        "place": "",
        "sectorcode": "JP",  # JP = Ski Jumping
        "seasoncode": seasoncode,
        "categorycode": categorycode,  # WC = World Cup, CO = Continental Cup itd.
        "disciplinecode": "",
        "gendercode": gendercode,  # M = Men, W = Women
        "racedate": "",
        "racecodex": "",
        "nationcode": "",
        "seasonmonth": seasonmonth,
        "saveselection": "-1",
        "seasonselection": ""
    }

    # 3. Nadpisanie pustych wartości, jeśli przekazano je w wywołaniu (np. place="Planica")
    params.update(kwargs)

    headers = config.DEFAULT_HEADERS

    try:
        print(f"Pobieranie danych (Sezon: {params['seasoncode']}, Kategoria: {params['categorycode']}, Płeć: {params['gendercode']})...")

        # 4. Bezpieczne pobieranie z automatycznym retry i obsługą limitu 429
        response = safe_get(base_url, params=params, timeout=25.0)
        if not response:
            print(f"⚠️ Nie udało się pobrać kalendarza FIS dla sezonu {seasoncode} ({categorycode})")
            return []

        # 5. Parsowanie pobranego HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.find_all('div', class_='table-row')

        events = []
        for row in rows:
            event_id = row.get('id')
            if not event_id:
                continue

            link_tag = row.find('a', href=True)
            event_url = link_tag['href'] if link_tag else None

            a_tags = row.find_all('a')
            date = a_tags[1].text.strip() if len(a_tags) > 1 else "Brak daty"

            place_div = row.find('div', class_='font_lg_large')
            place = place_div.text.strip() if place_div else "Brak miejsca"

            country_span = row.find('span', class_='country__name-short')
            country = country_span.text.strip() if country_span else "Brak kraju"

            status_items = row.select('.status__item')
            status_titles = [str(el.get('title') or '').strip().lower() for el in status_items]
            is_cancelled_on_fis = any(t == 'cancelled' for t in status_titles)
            has_results_on_fis = any('data available' in t for t in status_titles)

            events.append({
                "id": event_id,
                "date": date,
                "place": place,
                "country": country,
                "url": event_url,
                "categorycode": categorycode,
                "is_cancelled": is_cancelled_on_fis,
                "has_results_on_fis": has_results_on_fis,
            })

            # Wypisanie wyniku w konsoli
            print(f"[{country}] {place} ({date}) -> ID: {event_id}")
            print(f"Link: {event_url}\n")

        return events

    except Exception as e:
        print(f"Błąd podczas pobierania strony kalendarza FIS: {e}")
        return []


# Przykłady użycia
if __name__ == "__main__":
    print("--- TEST 1: Domyślne wywołanie (Mężczyźni, Puchar Świata 2026) ---")
    lista_zawodow = fetch_fis_calendar()
    print(f"Znaleziono {len(lista_zawodow)} wydarzeń.\n")

    print("--- TEST 2: Kobiety, sezon 2025 ---")
    lista_kobiety = fetch_fis_calendar(seasoncode="2025", seasonmonth="X-2025")
    print(f"Znaleziono {len(lista_kobiety)} wydarzeń.\n")

    print("--- TEST 3: Szukanie konkretnego miejsca (np. Zakopane) ---")
    # Przekazujemy parametr 'place', którego nie było w głównych argumentach funkcji,
    # ale zostanie przechwycony przez **kwargs i dodany do zapytania.
    lista_zakopane = fetch_fis_calendar(place="Zakopane")
    print(f"Znaleziono {len(lista_zakopane)} wydarzeń.\n")