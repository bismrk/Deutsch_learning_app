"""Database helpers for idempotent user profile and default deck initialization."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Deck, User

DEFAULT_DECK_TITLE = "Мій словник"
DEFAULT_DECK_LEVEL = "mixed"


async def _load_user_or_none(
    session: AsyncSession,
    telegram_id: int,
    *,
    for_update: bool = False,
) -> User | None:
    stmt = (
        select(User)
        .where(User.telegram_id == telegram_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _require_user(
    session: AsyncSession,
    telegram_id: int,
    *,
    for_update: bool = False,
) -> User:
    stmt = (
        select(User)
        .where(User.telegram_id == telegram_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    return result.scalar_one()


async def _has_default_deck(session: AsyncSession, telegram_id: int) -> bool:
    stmt = (
        select(Deck.id)
        .where(Deck.owner_id == telegram_id, Deck.title == DEFAULT_DECK_TITLE)
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def ensure_user_initialized(
    session: AsyncSession,
    telegram_id: int,
    *,
    for_update: bool = False,
) -> tuple[User, bool]:
    """Ensure the user row and default personal deck exist within the current transaction.

    Uses PostgreSQL ``INSERT ... ON CONFLICT DO NOTHING`` plus row-level locking on
    ``users`` when creating a missing default deck, preventing duplicate user or deck
    rows under concurrent first-access requests.

    Returns ``(user, changed)`` where ``changed`` indicates whether a user or deck row
    was created during this call. The caller owns committing or rolling back.
    """
    user = await _load_user_or_none(session, telegram_id, for_update=for_update)
    changed = False

    if user is None:
        insert_stmt = (
            pg_insert(User)
            .values(telegram_id=telegram_id)
            .on_conflict_do_nothing(index_elements=[User.telegram_id])
            .returning(User.telegram_id)
        )
        inserted_id = (await session.execute(insert_stmt)).scalar_one_or_none()
        if inserted_id is not None:
            session.add(
                Deck(
                    owner_id=telegram_id,
                    title=DEFAULT_DECK_TITLE,
                    level=DEFAULT_DECK_LEVEL,
                ),
            )
            await session.flush()
            created_user = await _require_user(session, telegram_id, for_update=for_update)
            return created_user, True

        user = await _require_user(session, telegram_id, for_update=for_update)

    if not await _has_default_deck(session, telegram_id):
        if not for_update:
            user = await _require_user(session, telegram_id, for_update=True)
        if not await _has_default_deck(session, telegram_id):
            session.add(
                Deck(
                    owner_id=telegram_id,
                    title=DEFAULT_DECK_TITLE,
                    level=DEFAULT_DECK_LEVEL,
                ),
            )
            await session.flush()
            changed = True

    return user, changed


__all__ = [
    "DEFAULT_DECK_LEVEL",
    "DEFAULT_DECK_TITLE",
    "ensure_user_initialized",
]
