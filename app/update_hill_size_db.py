from pymongo import MongoClient
import requests
from bs4 import BeautifulSoup
import re
import time


def update_missing_hill_sizes():
    # 1. Połączenie z bazą danych (Dostosuj adres URI i nazwę bazy!)
    client = MongoClient("mongodb://localhost:27017")
    db = client["FIS_DATA_DB"]  # ZMIEŃ NAZWĘ BAZY DANYCH
    sessions_col = db["sessions"]

    # Szukamy sesji, które nie mają hillSize, ale mają zapisany raceUrl
    query = {"hillSize": None, "raceUrl": {"$ne": None}}
    sessions_to_update = list(sessions_col.find(query))

    print(f"Znaleziono {len(sessions_to_update)} sesji do zaktualizowania.")

    updated_count = 0

    for session in sessions_to_update:
        race_url = session.get("raceUrl")
        session_id = session.get("_id")

        try:
            print(f"Pobieranie: {race_url}")
            response = requests.get(race_url, timeout=10)

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')

                # Szukamy div'a o odpowiedniej klasie
                kind_div = soup.find('div', class_='event-header__kind')

                if kind_div:
                    # Regex szukający "HS" i łapiący cyfry po nim (odporny na spacje)
                    match = re.search(r'HS\s*(\d+)', kind_div.text, re.IGNORECASE)

                    if match:
                        hill_size = int(match.group(1))

                        # Aktualizacja w MongoDB
                        sessions_col.update_one(
                            {"_id": session_id},
                            {"$set": {"hillSize": hill_size}}
                        )
                        print(f" [SUKCES] Zaktualizowano ID: {session_id} -> HS {hill_size}")
                        updated_count += 1
                    else:
                        print(f" [POMINIĘTO] Brak frazy 'HS' w nagłówku dla ID: {session_id}")
                else:
                    print(f" [POMINIĘTO] Brak klasy 'event-header__kind' dla ID: {session_id}")

            # Lekkie opóźnienie, aby nie zablokowali nam IP na serwerach FIS
            # time.sleep(0.5)

        except Exception as e:
            print(f" [BŁĄD] Wystąpił problem przy ID {session_id}: {e}")

    print(f"\nGotowe! Pomyślnie zaktualizowano {updated_count} z {len(sessions_to_update)} sesji.")


if __name__ == "__main__":
    update_missing_hill_sizes()