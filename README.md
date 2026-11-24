# FantaSKI - backend

FastAPI backend dla platformy **FantaSKI** — bazy wyników FIS, klasyfikacji, narzędzi analitycznych Fantasy oraz predykcji sztucznej inteligencji (PyTorch).

## Funkcjonalności
- Autoryzacja Google OAuth2 + JWT
- Role & Uprawnienia: Gość (wyniki), Użytkownik zalogowany (klasyfikacje), FantaSKI PRO (porównywarka, predykcje, wyceny Fantasy), Administrator (zarządzanie i scrapowanie)
- Pamięć podręczna (in-memory multi-tier TTLCache)
- Ochrona zasobów (slowapi rate limiting, CPU PyTorch)
- Integracja z MongoDB Atlas

## Uruchomienie lokalne
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```
