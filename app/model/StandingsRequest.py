from pydantic import BaseModel

class StandingsRequest(BaseModel):
    season: str
    category: str = "WC"