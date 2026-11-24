"""FastAPI endpoints for user profile initialization and settings management."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id
from app.db.base import get_db
from app.db.users import ensure_user_initialized
from app.schemas.users import UserProfileResponse, UserSettingsUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserProfileResponse)
async def get_my_profile(
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserProfileResponse:
    """Return the authenticated user's profile, auto-initializing on first access."""
    user, changed = await ensure_user_initialized(db, user_id)
    if changed:
        await db.commit()
    return UserProfileResponse.model_validate(user)


@router.patch("/settings", response_model=UserProfileResponse)
async def update_my_settings(
    payload: UserSettingsUpdate,
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserProfileResponse:
    """Partially update the authenticated user's settings, auto-initializing if needed."""
    fields_to_update = payload.model_fields_set
    user, changed = await ensure_user_initialized(
        db,
        user_id,
        for_update=bool(fields_to_update),
    )

    if fields_to_update:
        for field_name in fields_to_update:
            setattr(user, field_name, getattr(payload, field_name))
        changed = True

    if changed:
        await db.commit()

    return UserProfileResponse.model_validate(user)


__all__ = ["router"]
