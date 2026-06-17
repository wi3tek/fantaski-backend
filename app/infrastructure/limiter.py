from slowapi import Limiter
from slowapi.util import get_remote_address

# Przenosimy inicjalizację tutaj, aby uniknąć błędów pętli importów
limiter = Limiter(key_func=get_remote_address)