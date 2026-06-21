from enum import Enum

from pydantic import BaseModel, HttpUrl

from pydantic import BaseModel, HttpUrl

class DocumentType(str, Enum):
    RESULTS = "RESULTS"
    QUA = "QUA"
    TRIAL = "TRIAL"
    TRAINING = "TRAINING"

class ScraperRequest(BaseModel):
    pdf_link: HttpUrl

class ScraperResponse(BaseModel):
    message: str
    task_id: str

from pydantic import BaseModel, Field
from typing import List, Optional

class JumpRecord(BaseModel):
    speed: Optional[float] = None
    distance: Optional[float] = None
    distance_points: Optional[float] = None
    judges_points: Optional[float] = None
    gate_wind_comp: Optional[float] = None
    round_points: Optional[float] = None

class AthleteResult(BaseModel):
    rank: Optional[str] = None
    bib: Optional[str] = None
    name: str
    nation: Optional[str] = None
    total_points: Optional[float] = None
    jumps: List[JumpRecord] = Field(default_factory=list)