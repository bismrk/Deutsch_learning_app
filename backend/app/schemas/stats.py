"""Pydantic v2 response schemas for 5-axis radar statistics and weak grammar topics."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RadarAxisKey = Literal["vocab", "grammar", "consistency", "writing", "listening"]
RadarAxisStatus = Literal["active", "coming_soon"]

RADAR_AXIS_ORDER: tuple[RadarAxisKey, ...] = (
    "vocab",
    "grammar",
    "consistency",
    "writing",
    "listening",
)

RADAR_AXIS_LABELS_UA: dict[RadarAxisKey, str] = {
    "vocab": "Словник",
    "grammar": "Граматика",
    "consistency": "Стабільність",
    "writing": "Шрайбен",
    "listening": "Хьорен",
}


class RadarAxis(BaseModel):
    """Single axis entry on the 0–100 B1-calibrated radar chart."""

    model_config = ConfigDict(extra="forbid")

    axis: RadarAxisKey
    label_ua: str
    score: int = Field(ge=0, le=100)
    status: RadarAxisStatus


class WeakTopicItem(BaseModel):
    """Aggregated weak grammar topic over the last 14 days."""

    model_config = ConfigDict(extra="forbid")

    grammar_topic: str
    avg_grammar_score: float = Field(ge=0.0, lt=100.0)
    attempts_count: int = Field(ge=1)
    youtube_topic: str | None = None


class RadarStatsResponse(BaseModel):
    """Response payload for ``GET /api/stats/radar`` (also reusable by weekly scheduler)."""

    model_config = ConfigDict(extra="forbid")

    axes: list[RadarAxis] = Field(min_length=5, max_length=5)
    weak_topics: list[WeakTopicItem] = Field(default_factory=list, max_length=3)

    def scores_by_axis(self) -> dict[RadarAxisKey, int]:
        """Return a mapping of axis key -> integer score (0–100) for snapshot persistence."""
        return {item.axis: item.score for item in self.axes}


__all__ = [
    "RADAR_AXIS_LABELS_UA",
    "RADAR_AXIS_ORDER",
    "RadarAxis",
    "RadarAxisKey",
    "RadarAxisStatus",
    "RadarStatsResponse",
    "WeakTopicItem",
]
