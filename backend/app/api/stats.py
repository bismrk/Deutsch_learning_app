"""FastAPI endpoints for radar chart progress statistics and weak grammar topics."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id, get_utc_now
from app.core.scoring import compute_user_radar
from app.db.base import get_db
from app.schemas.stats import RadarStatsResponse

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/radar", response_model=RadarStatsResponse)
async def get_radar_stats(
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
    now: Annotated[datetime, Depends(get_utc_now)],
) -> RadarStatsResponse:
    """Return 5-axis B1-calibrated radar scores (0–100) and top-3 weak topics (last 14 days)."""
    return await compute_user_radar(db, user_id, now=now)


__all__ = ["router"]
