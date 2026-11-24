import httpx
import re
from bs4 import BeautifulSoup
from app import config


#
#  TODO - Mechanizm dla statystyk drużynowych nie został do końca dopracowany
#
def fetch_race_results(race_url, session_type="Competition", team_type="TEAM"):
    """
    Pobiera wyniki dla zawodów drużynowych (TEAM / SUPER_TEAM).
    Na ten moment struktura HTML jest zbieżna, ale punkty liczone są z klasyfikacji drużynowej.
    """
    try:
        print(f"Pobieranie wyników {team_type} z: {race_url}")
        response = httpx.get(race_url, headers=config.DEFAULT_HEADERS, timeout=25.0, follow_redirects=True)
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

        team_results = []
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

            rows = table_body.find_all('a', class_='table-row')

            for row in rows:
                cols = row.find('div', class_='justify-sb')
                if not cols:
                    continue

                col_divs = cols.find_all('div', recursive=False)
                if len(col_divs) < 8:
                    continue

                rank_str = col_divs[0].text.strip()
                rank = int(rank_str) if rank_str.isdigit() else None

                bib = col_divs[1].text.strip()
                # W zawodach drużynowych kodem może być kod kraju lub kod specyficzny, wyciągamy tak samo
                fis_code = col_divs[2].text.strip()
                name = col_divs[3].text.strip()  # Nazwa drużyny / kraju lub zawodnika
                year = col_divs[4].text.strip()

                country_span = col_divs[5].find('span', class_='country__name-short')
                country = country_span.text.strip() if country_span else None

                jumps = []
                jumps_container = col_divs[6].find('div', class_='split-row')

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
                total_points_div = col_divs[7]
                if total_points_div.text.strip():
                    try:
                        total_points = float(total_points_div.text.strip())
                        is_cancelled = False
                    except ValueError:
                        pass

                # --- NALICZANIE PUNKTÓW DRUŻYNOWYCH / SUPER TEAM ---
                wc_points = 0
                if current_status == "Classified" and session_type == "Competition" and rank:
                    if team_type == "SUPER_TEAM":
                        wc_points = config.SUPER_TEAM_POINTS.get(rank, 0)
                    else:
                        wc_points = config.TEAM_POINTS.get(rank, 0)

                team_results.append({
                    "rank": rank,
                    "bib": bib,
                    "fis_code": fis_code,
                    "name": name,
                    "country": country,
                    "year": year,
                    "status": current_status,
                    "jumps": jumps,
                    "total_points": total_points,
                    "world_cup_points": wc_points
                })

        return {
            "is_cancelled": is_cancelled,
            "hill_size": hill_size,
            "hill_size_unit": hill_size_unit,
            "hill_type": hill_type,
            "results": team_results
        }

    except Exception as e:
        print(f"Błąd podczas pobierania wyników drużynowych: {e}")
        return None