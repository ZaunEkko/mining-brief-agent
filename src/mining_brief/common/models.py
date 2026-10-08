"""Models shared by every server and the agent."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class DataMode(StrEnum):
    """Where a piece of upstream data actually came from."""

    LIVE = "live"
    CACHE = "cache"
    STALE_CACHE = "stale_cache"
    FIXTURE = "fixture"


class Provenance(BaseModel):
    source: str = Field(description="Human-readable name of the upstream source")
    source_url: str
    retrieved_at: datetime
    data_mode: DataMode
