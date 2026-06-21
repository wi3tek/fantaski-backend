import pdfplumber
from abc import ABC, abstractmethod
import pandas as pd

from model.pdf_models import AthleteResult, JumpRecord


# Interfejs Strategii
class PdfParsingStrategy(ABC):
    @abstractmethod
    def parse(self, pdf: pdfplumber.PDF) -> pd.DataFrame:
        pass


# Konkretne Strategie
import pdfplumber
import pandas as pd
import numpy as np
from abc import ABC, abstractmethod


class OfficialResultsStrategy(PdfParsingStrategy):
    # Słownik ujednolicający nazwy kolumn z różnych lat
    # Klucz to ustandaryzowana nazwa bazy danych, wartości to możliwe warianty z PDF
    COLUMN_MAPPING = {
        'rank': ['Rank', 'Rk', 'Pozycja'],
        'bib': ['Bib', 'Nr'],
        'name': ['Name', 'Imię i Nazwisko', 'Name Club'],
        'nation': ['Nat', 'NSA', 'Kraj'],
        'speed': ['Speed', 'Speed [km/h]'],
        'distance': ['Distance', 'Dist.', 'Distance [m]'],
        'distance_points': ['Distance Points', 'Dist. Points'],
        'judges_points': ['Judges Points', 'Judges Marks'],
        'gate_wind_comp': ['Gate/Wind Compensation', 'Gate/Wind Comp.', 'G/W Comp.'],
        'round_points': ['Round Points', 'Points'],
        'total_points': ['Total Points', 'Total']
    }

    def parse(self, pdf: pdfplumber.PDF) -> pd.DataFrame:
        print("Używam strategii: Official Results")
        all_tables = []

        for page in pdf.pages:
            # Używamy pionowych i poziomych linii jako strategii, co dobrze działa na tabele FIS
            table = page.extract_table(table_settings={
                "vertical_strategy": "text",
                "horizontal_strategy": "text",
                "intersection_tolerance": 15
            })

            if table:
                all_tables.extend(table)

        if not all_tables:
            raise ValueError("Nie znaleziono żadnych tabel w pliku Official Results.")

        # Przekształcamy surowe dane w DataFrame
        df = pd.DataFrame(all_tables)

        # Uruchamiamy potok czyszczący
        return self._clean_dataframe(df)

    def _clean_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        # 1. Usuwanie całkowicie pustych wierszy i kolumn
        df = df.dropna(how='all').dropna(axis=1, how='all')

        # 2. Identyfikacja nagłówka (szukamy głębiej, do 40 wierszy)
        header_idx = None
        for i in range(min(40, len(df))):
            row_text = ' '.join(str(val).lower() for val in df.iloc[i].values if val is not None)
            if 'rank' in row_text and ('bib' in row_text or 'name' in row_text):
                header_idx = i
                break

        if header_idx is not None:
            # Ustawienie nagłówka
            headers = df.iloc[header_idx].fillna('').astype(str).str.replace('\n', ' ').str.strip()
            df.columns = headers
            # Odcięcie wierszy powyżej nagłówka
            df = df.iloc[header_idx + 1:].reset_index(drop=True)
        else:
            raise ValueError("Nie udało się zlokalizować nagłówka tabeli w pliku PDF.")

        # 3. Usuwanie powielonych nagłówków z kolejnych stron
        rank_col = df.columns[0]
        df = df[~df[rank_col].astype(str).str.contains('Rank|Pozycja', case=False, na=False)]

        # 4. NOWE, INTELIGENTNE MAPOWANIE KOLUMN
        new_columns = []
        points_counter = 0
        for col in df.columns:
            col_str = str(col).lower()

            if 'speed' in col_str or 'km/h' in col_str:
                mapped_name = 'speed'
            elif 'dist' in col_str and 'points' in col_str:
                mapped_name = 'distance_points'
            elif 'dist' in col_str or '[m]' in col_str:
                mapped_name = 'distance'
            elif 'judges' in col_str and 'marks' in col_str:
                mapped_name = 'judges_marks'
            elif 'judges' in col_str and 'points' in col_str:
                mapped_name = 'judges_points'
            elif 'total' in col_str:
                mapped_name = 'total_points'
            elif 'round' in col_str and 'rank' in col_str:
                mapped_name = 'round_rank'  # Ignorujemy drugorzędny rank
            elif 'rank' in col_str or 'pozycja' in col_str or 'rk' in col_str:
                mapped_name = 'rank'
            elif 'bib' in col_str or 'nr' in col_str:
                mapped_name = 'bib'
            elif 'name' in col_str or 'imię' in col_str:
                mapped_name = 'name'
            elif 'nat' in col_str or 'nsa' in col_str or 'kraj' in col_str:
                # Omijamy błąd z dopasowaniem "nsa" w środku słowa "compensation"
                if 'comp' not in col_str:
                    mapped_name = 'nation'
                else:
                    mapped_name = 'gate_wind_comp'
            elif 'comp' in col_str or 'wind' in col_str:
                mapped_name = 'gate_wind_comp'
            elif 'gate' in col_str:
                mapped_name = 'gate'
            elif 'points' in col_str:
                # W nowych plikach FIS kolumny to po prostu wielokrotne "POINTS"
                # Rozróżniamy je na podstawie tego, które to z kolei wystąpienie
                points_counter += 1
                if points_counter == 1:
                    mapped_name = 'distance_points'
                elif points_counter == 2:
                    mapped_name = 'judges_points'
                elif points_counter == 3:
                    mapped_name = 'wind_points'
                else:
                    mapped_name = 'round_points'
            else:
                mapped_name = col_str

            new_columns.append(mapped_name)

        df.columns = new_columns

        # --- KLUCZOWA ŁATKA NA BŁĘDY "Series" ---
        # Usuwa zduplikowane kolumny. Pandas wreszcie będzie zwracał wartości, a nie listy!
        df = df.loc[:, ~df.columns.duplicated()]

        # 5. Obsługa wielowierszowych rekordów (Skok 1 i Skok 2)
        if 'rank' in df.columns and 'name' in df.columns:
            df['rank'] = df['rank'].replace(r'^\s*$', np.nan, regex=True)
            df['name'] = df['name'].replace(r'^\s*$', np.nan, regex=True)
            df['rank'] = df['rank'].ffill()
            df['name'] = df['name'].ffill()

        # Uzupełniamy narodowość i numer przy drugich skokach
        if 'nation' in df.columns:
            df['nation'] = df['nation'].replace(r'^\s*$', np.nan, regex=True).ffill()
        if 'bib' in df.columns:
            df['bib'] = df['bib'].replace(r'^\s*$', np.nan, regex=True).ffill()

        # 6. Finalne czyszczenie i odrzucenie "śmieciowych" wierszy
        if 'distance' in df.columns:
            df = df.dropna(subset=['distance'])
            df = df[df['distance'].astype(str).str.strip() != '']

        return df.reset_index(drop=True)


class QualificationStrategy(PdfParsingStrategy):
    def parse(self, pdf: pdfplumber.PDF) -> pd.DataFrame:
        print("Używam strategii: Qualifications")
        all_data = []
        for page in pdf.pages:
            table = page.extract_table()
            if table:
                # Kwalifikacje mają inną strukturę (np. status "Pre-qualified")
                all_data.extend(table)
        return pd.DataFrame(all_data)


class TrainingStrategy(PdfParsingStrategy):
    def parse(self, pdf: pdfplumber.PDF) -> pd.DataFrame:
        print("Używam strategii: Training/Trial")
        all_data = []
        for page in pdf.pages:
            table = page.extract_table()
            if table:
                # Treningi mają tabele podzielone na rundy w poziomie (1st Round, 2nd Round)
                all_data.extend(table)
        return pd.DataFrame(all_data)


# (Na górze pliku zostawiasz klasy OfficialResultsStrategy, QualificationStrategy, itd.)

def get_strategy(doc_type: str) -> PdfParsingStrategy:
    print(f"Wybieram strategię na podstawie parametru od użytkownika: {doc_type}")

    if doc_type == "RESULTS":
        return OfficialResultsStrategy()
    elif doc_type == "QUA":
        return QualificationStrategy()
    elif doc_type in ["TRIAL", "TRAINING"]:
        return TrainingStrategy()
    else:
        # Ten błąd w zasadzie się nie wydarzy, bo FastAPI zablokuje złe wartości na etapie walidacji Enuma, ale zostawiamy dla porządku
        raise ValueError(f"Nieobsługiwany typ dokumentu: {doc_type}")


import pandas as pd
from typing import List


# Zakładam, że zaimportowałeś tu modele z pdf_models.py
# from app.model.pdf_models import AthleteResult, JumpRecord

def safe_float(value) -> float | None:
    """Pomocnicza funkcja do bezpiecznej konwersji ze stringa do float"""
    try:
        # Usuwamy ewentualne białe znaki i przecinki
        cleaned = str(value).strip().replace(',', '.')
        if cleaned == '' or cleaned.lower() == 'nan':
            return None
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def structure_results(df: pd.DataFrame) -> List[AthleteResult]:
    structured_data = []

    # Zabezpieczenie przed brakiem zmapowanych kolumn
    if 'name' not in df.columns:
        raise ValueError(
            f"Krytyczny błąd mapowania! Kolumna 'name' nie istnieje. Znalezione kolumny to: {df.columns.tolist()}")

    # 1. Zabezpieczenie: usuwamy wiersze bez nazwiska (błędy OCR)
    df = df.dropna(subset=['name'])

    # 2. Grupujemy wiersze po Nazwisku i Numerze Startowym (Bib)
    # sort=False zachowuje oryginalną kolejność (czyli runda 1 będzie przed rundą 2)
    grouped = df.groupby(['name', 'bib'], dropna=False, sort=False)

    for (name, bib), group in grouped:
        jumps = []

        # Iterujemy po wierszach w danej grupie (czyli po skokach danego zawodnika)
        for _, row in group.iterrows():
            jump = JumpRecord(
                speed=safe_float(row.get('speed')),
                distance=safe_float(row.get('distance')),
                distance_points=safe_float(row.get('distance_points')),
                judges_points=safe_float(row.get('judges_points')),
                gate_wind_comp=safe_float(row.get('gate_wind_comp')),
                round_points=safe_float(row.get('round_points'))
            )
            jumps.append(jump)

        # Ostatni wiersz zawodnika często zawiera ostateczną pozycję (Rank) i sumę punktów (Total Points)
        last_row = group.iloc[-1]

        athlete = AthleteResult(
            rank=str(last_row.get('rank', '')).strip(),
            bib=str(bib).strip() if pd.notna(bib) else None,
            name=str(name).strip(),
            nation=str(last_row.get('nation', '')).strip(),
            total_points=safe_float(last_row.get('total_points')),
            jumps=jumps
        )

        structured_data.append(athlete)

    return structured_data