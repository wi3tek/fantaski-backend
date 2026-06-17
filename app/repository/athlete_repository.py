from typing import List, Mapping, Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from fastapi import Request

class AthleteRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        # Inicjalizujemy repozytorium wskazując konkretną kolekcję
        self.collection = db["athletes"]

    async def find_by_fis_code(self, fis_code: int) -> dict | None:
        """
        Szuka zawodnika po fis_code.
        Zwraca słownik z danymi lub None, jeśli nie znajdzie.
        """
        # {"_id": 0} ukrywa domyślne ObjectId z MongoDB, którego FastAPI nie umie domyślnie zserializować do JSON
        return await self.collection.find_one({"fis_code": fis_code}, {"_id": 0})

    async def find_all(self, limit: int = 1000) -> List[Mapping[str, Any]]:
        """
        Pobiera wszystkich zawodników z bazy.
        Zwraca listę słowników.
        """
        # Pusty słownik {} oznacza "pobierz wszystko" (brak filtrów).
        # {"_id": 0} ukrywa domyślne ObjectId, żeby FastAPI nie miało problemów z konwersją do JSON.
        cursor = self.collection.find({}, {"_id": 0})

        # Kursor asynchroniczny trzeba zamienić na listę.
        # Motor wymaga podania argumentu 'length' jako zabezpieczenia przed
        # zapchaniem pamięci RAM, gdyby w bazie były miliony rekordów.
        return await cursor.to_list(length=limit)

    async def get_all_names(self) -> list[str]:
        # {"_id": 0, "full_name": 1} oznacza: zignoruj ID, zwróć TYLKO full_name
        cursor = self.collection.find({}, {"_id": 0, "full_name": 1})
        athletes = await cursor.to_list(length=None)
        # Zwracamy płaską listę stringów: ["STOCH Kamil", "KRAFT Stefan", ...]
        return [athlete["full_name"] for athlete in athletes if "full_name" in athlete]

# --- Funkcja pomocnicza do wstrzykiwania zależności (Dependency Injection) ---
def get_athlete_repository(request: Request) -> AthleteRepository:
    """
    Wyciąga połączenie z bazą z requestu i tworzy instancję repozytorium.
    To jest nasz odpowiednik mechanizmów ze Springa.
    """
    return AthleteRepository(request.app.database)