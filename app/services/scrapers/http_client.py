# app/services/scrapers/http_client.py
"""
Centralny moduł zapytań HTTP do serwerów FIS z automatyczną odpornością na błędy:
- Globalny Rate Limiter (Token Bucket / Spacing) synchronizujący wszystkie wątki (maks. 1 zapytanie co ~1.3-1.5s)
- Błyskawiczna reakcja na 429: globalny cooldown na 5s, odczekujący opróżnienie wiadra Nginx
- Utrzymywanie sesji HTTP z connection pooling (reusable keep-alive TCP/TLS)
- Realistyczne nagłówki przeglądarki Chrome zapobiegające blokadom WAF
- Bezpieczny fallback bez nieobsłużonych wyjątków wywołujących błędy 500
"""

import time
import random
import threading
import logging
from typing import Optional, Dict, Any
import httpx
from app import config

logger = logging.getLogger("scraper.http")

SCRAPER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}

# --- GLOBALNY LIMITER RATE-LIMIT (SYNCHRONIZACJA WSZYSTKICH WĄTKÓW) ---
_rate_lock = threading.Lock()
_last_request_time = 0.0
MIN_REQUEST_INTERVAL = 1.35  # Bezpieczny odstęp między zapytaniami do serwera FIS w sekundach

# Współdzielona sesja klienta HTTP (connection pooling i keep-alive)
_shared_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()
_thread_local = threading.local()


def clear_last_http_error():
    """Czyści ostatni błąd HTTP zapisany dla bieżącego wątku."""
    _thread_local.last_error = None


def get_last_http_error() -> Optional[str]:
    """Zwraca kod/powód ostatniego niepowodzenia HTTP w bieżącym wątku (np. 'HTTP_429')."""
    return getattr(_thread_local, "last_error", None)


def get_shared_client() -> httpx.Client:
    global _shared_client
    with _client_lock:
        if _shared_client is None or _shared_client.is_closed:
            _shared_client = httpx.Client(
                headers=SCRAPER_HEADERS,
                timeout=httpx.Timeout(25.0, connect=10.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
                follow_redirects=True
            )
        return _shared_client


def _enforce_rate_limit(cooldown_seconds: float = 0.0):
    """
    Wymusza minimalny odstęp czasu między zapytaniami z dowolnego wątku.
    Gdy serwer FIS zwróci 429, ustawia globalny cooldown (np. 5 sekund).
    """
    global _last_request_time
    with _rate_lock:
        now = time.time()
        if cooldown_seconds > 0:
            _last_request_time = now + cooldown_seconds
            time.sleep(cooldown_seconds)
            return

        elapsed = now - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            sleep_needed = MIN_REQUEST_INTERVAL - elapsed + random.uniform(0.05, 0.2)
            time.sleep(sleep_needed)

        _last_request_time = time.time()


def safe_get(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 25.0,
    max_retries: int = 5,
    base_delay: float = 3.0
) -> Optional[httpx.Response]:
    """
    Bezpieczne synchroniczne pobieranie zasobów HTTP z serwerów FIS:
    - Przepuszcza zapytania przez globalny rate limiter
    - Automatycznie ponawia próby po napotkaniu kodu 429 lub 5xx z adaptacyjnym czasem oczekiwania
    - Zwraca None zamiast rzucać nieobsłużony wyjątek w przypadku trwałej niedostępności
    - Zapisuje kod ostatniego błędu w pamięci lokalnej wątku (get_last_http_error())
    """
    clear_last_http_error()
    client = get_shared_client()
    req_headers = {**SCRAPER_HEADERS, **(headers or {})}

    for attempt in range(max_retries + 1):
        # 1. Czekamy na naszą kolej w globalnym limiterze
        _enforce_rate_limit()

        try:
            response = client.get(
                url,
                headers=req_headers,
                params=params,
                timeout=timeout
            )

            # 2. Sukces (kod 200)
            if response.status_code == 200:
                clear_last_http_error()
                return response

            # 3. Rate Limit od Nginx FIS (kod 429)
            if response.status_code == 429:
                _thread_local.last_error = "HTTP_429"
                if attempt < max_retries:
                    retry_after_hdr = response.headers.get("Retry-After")
                    try:
                        retry_after = int(retry_after_hdr) if retry_after_hdr else 0
                    except ValueError:
                        retry_after = 0

                    cooldown = max(retry_after, base_delay * (1.5 ** attempt)) + random.uniform(1.0, 2.0)
                    print(f"⚠️ [FIS Nginx 429] Przekroczono limit zapytań dla {url}. Wstrzymuję wszystkie wątki na {cooldown:.1f}s (próba {attempt + 1}/{max_retries})...")
                    _enforce_rate_limit(cooldown_seconds=cooldown)
                    continue
                else:
                    print(f"❌ [FIS Nginx 429] Wyczerpano limit ponownych prób ({max_retries}) dla {url}.")
                    return None

            # 4. Chwilowe błędy serwera FIS (500, 502, 503, 504)
            if response.status_code in [500, 502, 503, 504]:
                _thread_local.last_error = f"HTTP_{response.status_code}"
                if attempt < max_retries:
                    backoff = base_delay * (1.3 ** attempt) + random.uniform(0.5, 1.5)
                    print(f"⚠️ [FIS {response.status_code}] Chwilowy błąd serwera FIS. Ponawiam za {backoff:.1f}s (próba {attempt + 1}/{max_retries})...")
                    _enforce_rate_limit(cooldown_seconds=backoff)
                    continue
                else:
                    print(f"❌ [FIS {response.status_code}] Serwer FIS nie odpowiedział poprawnie po {max_retries} próbach.")
                    return None

            # 5. Inne kody (np. 404 Not Found)
            _thread_local.last_error = f"HTTP_{response.status_code}"
            print(f"ℹ️ [FIS {response.status_code}] Odpowiedź serwera: {url}")
            return None

        except (httpx.TimeoutException, httpx.NetworkError, httpx.RequestError) as exc:
            _thread_local.last_error = f"NET_{type(exc).__name__}"
            if attempt < max_retries:
                backoff = base_delay * (1.3 ** attempt) + random.uniform(0.5, 1.5)
                print(f"⚠️ Błąd sieci z FIS ({type(exc).__name__}). Ponawiam za {backoff:.1f}s (próba {attempt + 1}/{max_retries})...")
                _enforce_rate_limit(cooldown_seconds=backoff)
                continue
            else:
                print(f"❌ Nie udało się połączyć z FIS po {max_retries} próbach: {exc}")
                return None
        except Exception as exc:
            _thread_local.last_error = f"ERR_{type(exc).__name__}"
            print(f"❌ Nieoczekiwany błąd zapytania HTTP: {exc}")
            return None

    return None
