import time

import fitz
import requests
import io
from app import config
from typing import List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types


# ---------------------------------------------------------
# 1. TWOJE MODELE PYDANTIC (pozostają bez zmian)
# ---------------------------------------------------------
class JumpDataLite(BaseModel):
    round: int
    distance_m: Optional[float]
    points: Optional[float]


class AthleteDataLite(BaseModel):
    rank: Optional[int]
    name: str
    country: str
    jumps: List[JumpDataLite]


class EventDataLite(BaseModel):
    event_name: str
    athletes: List[AthleteDataLite]


# ---------------------------------------------------------
# 2. KONFIGURACJA KLIENTA (BEZ WYMUSZANIA JSON!)
# ---------------------------------------------------------
client = genai.Client(api_key=config.GEMINI_API_KEY)

# Używamy standardowej konfiguracji tekstu (nie JSON)
konfiguracja = types.GenerateContentConfig(
    temperature=0.0,
)


# ---------------------------------------------------------
# 3. GŁÓWNA LOGIKA
# ---------------------------------------------------------
def pdf_z_url_do_tekstu(url: str) -> Optional[str]:
    naglowki = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        response = requests.get(url, headers=naglowki, timeout=15)
        response.raise_for_status()
        with fitz.open(stream=io.BytesIO(response.content), filetype="pdf") as doc:
            return "".join(strona.get_text("text") for strona in doc)
    except Exception as e:
        print(f"❌ Błąd pobierania PDF: {e}")
        return None


def przetworz_wyniki_fis(url: str) -> Optional[EventDataLite]:
    tekst = pdf_z_url_do_tekstu(url)
    if not tekst:
        return None

    # Twój prompt z zabezpieczeniem (pamiętaj o zakazie kodu!)
    prompt = f"""
    ABSOLUTNY ZAKAZ PISANIA KODU PYTHON LUB JAKICHKOLWIEK SKRYPTÓW. JESTEŚ EKSTRAKTOREM DANYCH. MASZ SAMODZIELNIE WYCIĄGNĄĆ DANE I ZWRÓCIĆ TYLKO I WYŁĄCZNIE SUROWY TEKST.
    ZAKAZ UŻYWANIA BLOKÓW MARKDOWN (nie używaj ```).

    Zignoruj sponsorów, sędziów i tabele techniczne z poniższego dokumentu.
    Wyciągnij wyniki WSZYSTKICH zawodników (w tym DSQ, DNS) i sformatuj je DOKŁADNIE według poniższego wzoru, linijka po linijce:
    MIEJSCE | NAZWISKO Imię | KRAJ | SKOK1_ODL | SKOK1_PKT | SKOK2_ODL | SKOK2_PKT | SKOK3_ODL | SKOK3_PKT

    Zasady:
    - Rozdzielaj kolumny wyłącznie znakiem '|' (pipe).
    - Jeśli w dokumencie występują noty sędziowskie, to SKOK_PKT to łączna nota za skok ze wszystkimi zmiennymi, a nie punkty za odleglość
    - Jeśli zawodnik nie miał 2 lub 3 skoku (bo np. odpadł), w odpowiednich kolumnach wstaw słowo 'BRAK'.
    - Jeśli zawodnik ma DSQ, w miejsce wpisz 'DSQ'.
    - Jeśli zawodnik ma DNS, w miejsce wpisz 'DNS'.
    - Nie dodawaj żadnych nagłówków, wstępów, komentarzy ani kodu. TYLKO SUROWE DANE.

    DOKUMENT:
    {tekst}
    """

    # MECHANIZM PONOWIEŃ (RETRY)
    max_prob = 3
    response = None

    for proba in range(1, max_prob + 1):
        try:
            print(f"🧠 Próba {proba}/{max_prob}: Błyskawiczna analiza przez AI...")
            response = client.models.generate_content(
                model=config.AI_MODEL,
                contents=prompt,
                config=konfiguracja
            )
            # Jeśli się udało, przerywamy pętlę i idziemy dalej
            break

        except Exception as e:
            error_msg = str(e)
            if "503" in error_msg or "429" in error_msg:
                if proba < max_prob:
                    czas_oczekiwania = proba * 3  # Czeka 3s, potem 6s
                    print(f"⚠️ Serwer Google zajęty (Błąd 503). Czekam {czas_oczekiwania} sekund przed ponowieniem...")
                    time.sleep(czas_oczekiwania)
                    continue
                else:
                    print(f"❌ Wyczerpano limit prób. Serwer Google jest permanentnie przeciążony: {e}")
                    return None
            else:
                # Jeśli to inny błąd niż przeciążenie, przerywamy od razu
                print(f"❌ Nieoczekiwany błąd API: {error_msg}")
                return None

    # Jeśli pętla skończyła się bez response (np. wszystkie 3 próby padły)
    if not response:
        return None

    try:
        dane_tekstowe = response.text.strip()

        # Zabezpieczenie przed Markdownem
        if dane_tekstowe.startswith("```"):
            linie_czyste = dane_tekstowe.split('\n')
            if len(linie_czyste) > 2:
                dane_tekstowe = "\n".join(linie_czyste[1:-1])

        print("✅ AI wygenerowało dane! Parsowanie w Pythonie...")

        athletes_list = []
        linie = dane_tekstowe.split('\n')

        for linia in linie:
            if not linia.strip() or '|' not in linia:
                continue

            elementy = [el.strip() for el in linia.split('|')]
            # Mamy teraz 9 kolumn (Miejsce, Nazwa, Kraj, S1odl, S1pkt, S2odl, S2pkt, S3odl, S3pkt)
            if len(elementy) < 9:
                continue

            miejsce_str, nazwa, kraj, s1_odl, s1_pkt, s2_odl, s2_pkt, s3_odl, s3_pkt = elementy[:9]

            rank = None if 'DSQ' in miejsce_str.upper() or 'DNS' in miejsce_str.upper() else int(
                miejsce_str.replace('.', ''))

            skoki = []

            # Grupowanie odległości i punktów w pary dla pętli
            dane_skokow = [(s1_odl, s1_pkt), (s2_odl, s2_pkt), (s3_odl, s3_pkt)]

            for runda, (odl_str, pkt_str) in enumerate(dane_skokow, start=1):
                odl_czysta = odl_str.strip()
                pkt_czyste = pkt_str.strip()

                if odl_czysta.upper() != 'BRAK' and odl_czysta.replace('.', '', 1).isdigit():
                    # Próba sparsowania punktów za skok (odporność na brakujące / niepoprawne dane)
                    punkty_za_skok = float(pkt_czyste) if pkt_czyste.replace('.', '', 1).isdigit() else None

                    skoki.append(JumpDataLite(
                        round=runda,
                        distance_m=float(odl_czysta),
                        points=punkty_za_skok
                    ))

            zawodnik = AthleteDataLite(
                rank=rank, name=nazwa.strip(), country=kraj.strip(), jumps=skoki
            )
            athletes_list.append(zawodnik)

        zawody = EventDataLite(event_name="Skoki Narciarskie - Odczyt z PDF", athletes=athletes_list)
        print("✅ Mapowanie zakończone sukcesem!")
        return zawody

    except Exception as e:
        print(f"❌ Błąd podczas parsowania tekstu w Pythonie: {e}")
        return None