"""
Moduł konfiguracji wag i parametrów sieci neuronowej do przewidywania skoków narciarskich (FantaSKI).

Zawiera wagi rang zawodów (prestiż ligi), wagi typów sesji, parametry rozpadu formy w czasie
oraz funkcje pomocnicze do poprawnej interpretacji kategorii FIS.
"""

from typing import Dict, Any

# ==============================================================================
# 1. HIERARCHIA I WAGI RANG ZAWODÓW (LIGI FIS)
# ==============================================================================
# Waga ligowa określa poziom trudności stawki i prestiż zawodów.
# Puchar Świata i zawody mistrzowskie seniorów są najważniejsze.
# Zawody niższych lig (COC, FC) są mocno zredukowane, aby nie zaburzać formy w PŚ.
RACE_CATEGORY_WEIGHTS: Dict[str, float] = {
    # TIER 1: Super-Elita Seniorów (Najwyższa ranga światowa, ścisła czołówka globu)
    "WC": 1.00,    # Puchar Świata (World Cup) - główny cykl zimowy seniorów
    "OWG": 1.00,   # Igrzyska Olimpijskie (Olympic Winter Games)
    "WSC": 1.00,   # Mistrzostwa Świata w narciarstwie klasycznym (World Ski Championships)
    "SFWC": 1.00,  # Mistrzostwa Świata w lotach narciarskich (Ski Flying World Championships)

    # TIER 2: Elita Letnia (Igelit)
    # Daje obraz potencjału skoczka, ale zima na śniegu weryfikuje stawkę
    "GP": 0.60,    # Letnie Grand Prix (Grand Prix)

    # TIER 3: Zaplecze Pucharu Świata / Druga Liga
    # Puchar Kontynentalny - podium w COC nie przekłada się automatycznie na punkty PŚ
    "COC": 0.25,   # Puchar Kontynentalny (Continental Cup)

    # TIER 4: Trzecia Liga
    # Puchar FIS - startują głównie utalentowani juniorzy i zaplecze krajowe
    "FC": 0.08,    # FIS Cup

    # TIER 5: Rozgrywki Młodzieżowe i Regionalne
    "WJC": 0.05,   # Mistrzostwa Świata Juniorów (World Junior Championships)
    "FESA": 0.05,  # Puchar FESA / Alpen Cup / OPA Cup
}

# ==============================================================================
# 2. WAGI TYPÓW SESJI W RAMACH WEEKENDU
# ==============================================================================
# W skokach narciarskich konkurs główny to pełna stawka punktowa, presja i decydujący sygnał.
# Kwalifikacje to skoki zachowawcze (awans do 50-tki).
# Treningi służą testom sprzętu, belek i mają znikome znaczenie (częste DNS lub brak telemarku).
SESSION_TYPE_WEIGHTS: Dict[str, float] = {
    "COMPETITION": 1.00,       # Konkurs główny - pełna stawka punktowa i maksymalna presja
    "QUA": 0.35,               # Kwalifikacje - skoki zachowawcze, czołówka często odpuszcza
    "QUALIFICATION": 0.35,
    "QUALIFICATIONS": 0.35,
    "TRIAL": 0.05,             # Seria próbna bezpośrednio przed konkursem (wyczucie progu)
    "TRAINING": 0.00,          # Oficjalne treningi (testy sprzętu, belek — pomijane w ocenie formy konkursowej)
}

# ==============================================================================
# 3. DYNAMIKA CZASOWA FORMY (TIME-DECAY & RECENCY)
# ==============================================================================
# HALF_LIFE_DAYS: Czas połowicznego rozpadu wagi wyniku w dniach (~5 tygodni).
# Ostatni miesiąc / początek sezonu ma decydujące znaczenie.
HALF_LIFE_DAYS: float = 35.0

# RECENT_FORM_WINDOW_DAYS: Okno bieżącej dyspozycji (ostatnie 4 tygodnie).
# Wykorzystywane do wyliczenia cechy `recent_form_ratio`, która błyskawicznie
# reaguje na skok formy (np. Sundal, Zimmermann po Lillehammer) lub załamanie formy.
RECENT_FORM_WINDOW_DAYS: int = 28

# Domyślna ranga analizowanego konkursu
DEFAULT_TARGET_CATEGORY: str = "WC"

# Liczba cech wejściowych sieci neuronowej (czysty, bezszumowy, niezależny od kolejności wektor 14 cech)
INPUT_FEATURES_DIM: int = 14


# ==============================================================================
# 4. FUNKCJE POMOCNICZE RESOLVINGU KATEGORII I TYPU SESJI
# ==============================================================================
def resolve_session_category(session: Dict[str, Any]) -> str:
    """
    Zwraca rzeczywistą rangę zawodów (WC, GP, COC, FC itd.).
    
    Rozwiązuje krytyczny błąd w bazie FIS, w którym sesja kwalifikacji PŚ ma:
    - race_category: "QUA"
    - category_code: "WC"
    Dzięki temu kwalifikacje Pucharu Świata nie tracą wagi i są traktowane
    jako sesja o randze WC (1.00) z wagą sesji QUA (0.65).
    """
    cat_code = str(session.get("category_code") or "").upper().strip()
    race_cat = str(session.get("race_category") or "").upper().strip()

    # Jeśli category_code wskazuje na znaną rangę (WC, GP, COC, FC, etc.)
    if cat_code in RACE_CATEGORY_WEIGHTS:
        return cat_code
    # Jeśli race_category wskazuje na znaną rangę i nie jest to oznaczenie serii ("QUA")
    if race_cat in RACE_CATEGORY_WEIGHTS and race_cat != "QUA":
        return race_cat
    if cat_code:
        return cat_code
    return "WC"


def resolve_session_type(session: Dict[str, Any]) -> str:
    """
    Określa typ serii (COMPETITION, QUA, TRIAL, TRAINING).
    Prawidłowo klasyfikuje kwalifikacje niezależnie od tego, czy wpisano je w
    race_category czy session_type.
    """
    stype = str(session.get("session_type") or "").upper().strip()
    rcat = str(session.get("race_category") or "").upper().strip()

    if rcat in ("QUA", "QUALIFICATION", "QUALIFICATIONS") or "QUA" in stype:
        return "QUA"
    if "TRAIN" in stype:
        return "TRAINING"
    if "TRIAL" in stype:
        return "TRIAL"
    if stype in SESSION_TYPE_WEIGHTS:
        return stype
    return "COMPETITION"
