import requests
import pdfplumber
import io
import json  # <-- DODAJ TEN IMPORT NA GÓRZE
import pandas as pd
from pathlib import Path
from .strategies import get_strategy, structure_results


def download_and_parse_pdf(url: str, document_type: str):
    print(f"Rozpoczynam pobieranie: {url} (Zdeklarowany typ: {document_type})")

    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        pdf_bytes = io.BytesIO(response.content)

        with pdfplumber.open(pdf_bytes) as pdf:
            strategy = get_strategy(document_type)
            df = strategy.parse(pdf)

            print(f"Zakończono parsowanie. Wyciągnięto wierszy (płaskich): {len(df)}")

            # Przetwarzanie na listę obiektów Pydantic
            structured_results = structure_results(df)
            print(f"Ustrukturyzowano na {len(structured_results)} zawodników.")

            # ---> NOWY KOD DO WYPRINTOWANIA CAŁEGO JSONA W TERMINALU <---
            # 1. Zamieniamy obiekty Pydantic na zwykłe słowniki (dict)
            json_ready_data = [athlete.model_dump() for athlete in structured_results]

            # 2. Printujemy sformatowanego JSON-a z ładnymi wcięciami (indent=4)
            print("\n" + "=" * 50)
            print("WYNIKOWY JSON:")
            print("=" * 50)
            print(json.dumps(json_ready_data, ensure_ascii=False, indent=4))
            print("=" * 50 + "\n")
            # -------------------------------------------------------------

            return True

    except Exception as e:
        print(f"Błąd przetwarzania PDF: {e}")
        return False