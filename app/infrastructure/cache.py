import time
from typing import Any, Optional, Dict
from cachetools import TTLCache

# Globalne cache w pamięci RAM z czasem życia (TTL)
# Działa bez żadnych zewnętrznych serwisów (0 zł, 0 dodatkowych zasobów na Render/Railway)
_caches: Dict[str, TTLCache] = {
    "short": TTLCache(maxsize=200, ttl=60),       # 1 minuta (np. ceny skoczków)
    "medium": TTLCache(maxsize=100, ttl=300),     # 5 minut (np. klasyfikacje, słowniki)
    "long": TTLCache(maxsize=50, ttl=3600),       # 1 godzina (np. lista skoczni)
}

def get_cached_value(cache_tier: str, key: str) -> Optional[Any]:
    cache = _caches.get(cache_tier)
    if cache is not None:
        return cache.get(key)
    return None

def set_cached_value(cache_tier: str, key: str, value: Any):
    cache = _caches.get(cache_tier)
    if cache is not None:
        cache[key] = value

def clear_cache(cache_tier: Optional[str] = None):
    if cache_tier and cache_tier in _caches:
        _caches[cache_tier].clear()
    else:
        for c in _caches.values():
            c.clear()
