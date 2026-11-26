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
# 2. WAGI TYPÓW SESJI I DYNAMIKA CZASOWA (MULTI-SCALE TIME DECAY)
# ==============================================================================
# Konkurs główny ma długi rozpad formy (half-life = 35 dni) i najwyższą wagę.
# Kwalifikacje (10 dni), serie próbne (4 dni) i treningi (3 dni) wygasają bardzo szybko,
# przez co skoki sprzed roku praktycznie nie ważą w dzisiejszej ocenie formy.
SESSION_TYPE_WEIGHTS: Dict[str, float] = {
    "COMPETITION": 1.00,       # Konkurs główny - pełna stawka punktowa i maksymalna presja
    "QUA": 0.45,               # Kwalifikacje - silny sygnał w krótkim oknie
    "QUALIFICATION": 0.45,
    "QUALIFICATIONS": 0.45,
    "TRIAL": 0.25,             # Seria próbna - ważna w bieżącym weekendzie
    "TRAINING": 0.15,          # Oficjalne treningi - liczone tylko krótkoterminowo
}

SESSION_HALF_LIFE_DAYS: Dict[str, float] = {
    "COMPETITION": 35.0,
    "QUA": 10.0,
    "TRIAL": 4.0,
    "TRAINING": 3.0,
}

# Maksymalny wiek (w dniach), po którym sesje niekonkursowe są całkowicie pomijane w formie
SESSION_MAX_AGE_DAYS: Dict[str, int] = {
    "COMPETITION": 730,
    "QUA": 90,
    "TRIAL": 21,
    "TRAINING": 14,
}

# Okno bieżącego weekendu na tej samej skoczni (dni przed konkursem) i wagi hierarchii serii
WEEKEND_LOOKBACK_DAYS: int = 4
WEEKEND_SESSION_WEIGHTS: Dict[str, float] = {
    "COMPETITION": 1.00,       # Np. sobotni konkurs przed niedzielnym
    "QUA": 0.75,               # Kwalifikacje (w tym piątkowe na sobotę i niedzielę)
    "TRIAL": 0.45,             # Seria próbna
    "TRAINING": 0.30,          # Oficjalne serie treningowe
}

# Kolejność etapów w obrębie tego samego dnia / konkursu
SESSION_STAGE_ORDER: Dict[str, int] = {
    "TRAINING": 0,
    "TRIAL": 1,
    "QUA": 2,
    "COMPETITION": 3,
}

# Ligowe wagi próbek podczas treningu sieci neuronowej (tylko COMPETITION; FC/WJC/FESA tylko jako tło historii)
TRAINING_COMPETITION_CATEGORY_WEIGHTS: Dict[str, float] = {
    "WC": 1.00,
    "OWG": 1.00,
    "WSC": 1.00,
    "SFWC": 1.00,
    "GP": 0.60,
    "COC": 0.35,
}

# ==============================================================================
# 3. DYNAMIKA CZASOWA FORMY (TIME-DECAY & RECENCY)
# ==============================================================================
HALF_LIFE_DAYS: float = 35.0
RECENT_FORM_WINDOW_DAYS: int = 28
LOWER_TIER_DOMINANCE_WINDOW_DAYS: int = 120

# Ruchome okno treningowe: ostatnie 8 sezonów (7 pełnych + bieżący, np. 2020-2027, za rok 2021-2028)
TRAINING_ROLLING_WINDOW_SEASONS: int = 8

# Domyślna ranga analizowanego konkursu
DEFAULT_TARGET_CATEGORY: str = "WC"

# Liczba cech wejściowych sieci neuronowej (18 czystych cech bez wycieku z przyszłości)
INPUT_FEATURES_DIM: int = 18


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
