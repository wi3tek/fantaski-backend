import requests
from bs4 import BeautifulSoup
from app import config
import re


def fetch_race_results(race_url, session_type="Competition"):
    """
    Pobiera wyniki dla zawodów indywidualnych (IND).
    """
    try:
        print(f"Pobieranie wyników IND z: {race_url}")
        response = requests.get(race_url, headers=config.DEFAULT_HEADERS)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')


        # --- 1. WYCIĄGANIE DANYCH O SKOCZNI ---
        hill_size = None
        hill_size_unit = None
        hill_type = None

        kind_div = soup.find('div', class_='event-header__kind')
        if kind_div:
            kind_text = kind_div.text.strip()
            if "Flying" in kind_text:
                hill_type = "FLYING"
            elif "Large" in kind_text:
                hill_type = "LARGE"
            elif "Normal" in kind_text:
                hill_type = "NORMAL"

            match = re.search(r'(HS|K)\s*(\d+)', kind_text, re.IGNORECASE)
            if match:
                hill_size_unit = match.group(1).upper()
                hill_size = int(match.group(2))


        athletes_results = []
        is_cancelled = True

        tables = soup.find_all('div', class_='table__body')

        for table_body in tables:
            current_status = "Classified"
            prev_head = table_body.find_previous_sibling('div', class_='table__head')
            if prev_head:
                header_text = prev_head.text.strip().lower()
                if "disqualified" in header_text:
                    current_status = "DSQ"
                elif "did not start" in header_text:
                    current_status = "DNS"
                elif "not permitted to start" in header_text:
                    current_status = "NPS"

            rows = table_body.find_all('a', class_='table-row')

            for row in rows:
                cols = row.find('div', class_='justify-sb')
                if not cols:
                    continue

                col_divs = cols.find_all('div', recursive=False)
                # Zmieniono z 8 na 7, aby nie pomijać węższych tabel,
                # gdzie brakuje np. kolumny ze stratą do lidera
                if len(col_divs) < 7:
                    continue

                rank_str = col_divs[0].text.strip()
                rank = int(rank_str) if rank_str.isdigit() else None

                # 1. Dynamiczne szukanie kontenera ze skokami (split-row)
                jumps_idx = 6 # Domyślny indeks (oryginalne działanie)
                jumps_container = None
                for idx, div in enumerate(col_divs):
                    if div.find('div', class_='split-row'):
                        jumps_container = div.find('div', class_='split-row')
                        jumps_idx = idx
                        break

                # 2. Przypisanie danych na podstawie układu kolumn
                if jumps_idx == 5:
                    # Układ dla 3/4 skoków (brak kolumny z "bib")
                    bib = ""
                    fis_code = col_divs[1].text.strip()
                    name = col_divs[2].text.strip()
                    year = col_divs[3].text.strip()
                    country_span = col_divs[4].find('span', class_='country__name-short')
                    country = country_span.text.strip() if country_span else None
                else:
                    # Standardowy układ (oryginalne zachowanie skryptu)
                    bib = col_divs[1].text.strip()
                    fis_code = col_divs[2].text.strip()
                    name = col_divs[3].text.strip()
                    year = col_divs[4].text.strip()
                    country_span = col_divs[5].find('span', class_='country__name-short')
                    country = country_span.text.strip() if country_span else None

                jumps = []
                if jumps_container:
                    jump_items = jumps_container.find_all('div', class_='split-row__item')
                    for j_idx, jump_html in enumerate(jump_items):
                        j_stats = jump_html.find('div', class_='g-row').find_all('div', recursive=False)
                        if len(j_stats) >= 4:
                            distance = j_stats[1].text.strip()
                            points = j_stats[2].text.strip()
                            jump_rank = j_stats[3].text.strip()

                            if distance and points:
                                jumps.append({
                                    "round": j_idx + 1,
                                    "distance": float(distance) if distance else None,
                                    "points": float(points) if points else None,
                                    "rank": int(jump_rank) if jump_rank.isdigit() else None
                                })
                                is_cancelled = False

                total_points = None
                # 3. Suma punktów zawsze znajduje się w kolumnie bezpośrednio po kontenerze ze skokami
                if jumps_idx + 1 < len(col_divs):
                    total_points_div = col_divs[jumps_idx + 1]
                    if total_points_div.text.strip():
                        try:
                            total_points = float(total_points_div.text.strip())
                            is_cancelled = False
                        except ValueError:
                            pass

                competition_points = 0
                if current_status == "Classified" and session_type == "Competition" and rank:
                    competition_points = config.COMPETITION_POINTS.get(rank, 0)

                athletes_results.append({
                    "rank": rank,
                    "bib": bib,
                    "fis_code": fis_code,
                    "name": name,
                    "country": country,
                    "year": year,
                    "status": current_status,
                    "jumps": jumps,
                    "total_points": total_points,
                    "competition_points": competition_points
                })

        return {
            "hill_size": hill_size,
            "hill_size_unit": hill_size_unit,
            "hill_type": hill_type,
            "is_cancelled": is_cancelled,
            "results": athletes_results
        }

    except Exception as e:
        print(f"Błąd podczas pobierania wyników IND: {e}")
        return None