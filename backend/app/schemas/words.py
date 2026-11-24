"""Pydantic request and response schemas for vocabulary words endpoints."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictStr

WordSource = Literal["text", "file", "sheets", "system"]


class WordItemResponse(BaseModel):
    """Serialized vocabulary word with its per-user FSRS card state."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    word_de: str
    translation_ua: str
    part_of_speech: str | None
    source: WordSource
    state: int
    stability: float
    difficulty: float
    due_at: datetime | None
    reps: int
    lapses: int


class WordListResponse(BaseModel):
    """Paginated vocabulary listing returned by ``GET /api/words``."""

    items: list[WordItemResponse]
    total: int


class WordImportJsonRequest(BaseModel):
    """JSON request payload for ``POST /api/words/import``."""

    model_config = ConfigDict(extra="forbid")

    text: StrictStr


class WordImportResponse(BaseModel):
    """Import summary returned by ``POST /api/words/import`` and ``POST /api/words/sync-sheets``."""

    imported: int
    duplicates: int
    skipped: int


class SheetsSyncRequest(BaseModel):
    """Optional JSON body for ``POST /api/words/sync-sheets``."""

    model_config = ConfigDict(extra="forbid")

    sheets_url: StrictStr | None = None


__all__ = [
    "SheetsSyncRequest",
    "WordImportJsonRequest",
    "WordImportResponse",
    "WordItemResponse",
    "WordListResponse",
    "WordSource",
]
