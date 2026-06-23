from pydantic import BaseModel
from typing import Optional, List

class SearchRequest(BaseModel):
    names: List[str] = None
    place: Optional[str] = None
    hill_type: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    category: List[str] = None  # <-- Zmiana na listę (tablicę) stringów
    limit: Optional[int] = None
    session_type: Optional[str] = None



