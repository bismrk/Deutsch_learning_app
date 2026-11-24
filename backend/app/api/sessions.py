"""FastAPI endpoints for training sessions, hints with anti-cheat, answer checking, and abandon."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id, get_utc_now
from app.config import Settings, get_settings
from app.core.fsrs_engine import apply_word_ratings
from app.core.llm_service import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMInvalidResponseError,
    LLMRequestError,
    LLMService,
    LLMUnavailableError,
)
from app.core.rate_limit import (
    DailyLLMLimitExceededError,
    check_and_increment_llm_quota,
)
from app.core.sessions_service import (
    build_session_summary,
    choose_grammar_topic,
    choose_next_exercise_type,
    collect_used_word_ids,
    evaluate_attempt_answer,
    generate_exercise_for_session,
    load_attempt_target_words,
    select_words_for_exercise,
)
from app.db.base import get_db
from app.db.models import Attempt, TrainingSession, User
from app.db.users import ensure_user_initialized
from app.schemas.exercises import GeneratedExercise
from app.schemas.sessions import (
    ActiveSessionResponse,
    SessionAbandonResponse,
    SessionAnswerRequest,
    SessionAnswerResponse,
    SessionHintResponse,
    SessionStartRequest,
    SessionStartResponse,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])

_PG_INT32_MAX = 2_147_483_647


def _resolve_active_settings(request: Request, injected_settings: Settings) -> Settings:
    """Resolve active Settings from overrides or ``create_app(settings=...)``."""
    if get_settings in request.app.dependency_overrides:
        return injected_settings
    app_settings = getattr(request.app.state, "settings", None)
    if isinstance(app_settings, Settings):
        return app_settings
    return injected_settings


async def get_llm_service(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[LLMService]:
    """Provide a request-scoped ``LLMService`` dependency (overridable in tests)."""
    resolved_settings = _resolve_active_settings(request, settings)
    async with LLMService(settings=resolved_settings) as service:
        yield service


def _validate_session_id_or_404(session_id: int) -> None:
    if session_id < 1 or session_id > _PG_INT32_MAX:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Тренувальну сесію не знайдено.",
        )


def _raise_http_for_llm_error(exc: LLMError) -> NoReturn:
    if isinstance(exc, LLMUnavailableError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI-сервіс тимчасово недоступний. Спробуйте ще раз трохи пізніше.",
        ) from exc
    if isinstance(exc, LLMConfigurationError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI-сервіс тимчасово не налаштований.",
        ) from exc
    if isinstance(exc, LLMInvalidResponseError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI-сервіс повернув некоректну відповідь. Спробуйте ще раз.",
        ) from exc
    if isinstance(exc, (LLMAuthenticationError, LLMRequestError)):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Помилка звернення до AI-сервісу. Спробуйте ще раз.",
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Помилка звернення до AI-сервісу. Спробуйте ще раз.",
    ) from exc


async def _load_user_session(
    db: AsyncSession,
    *,
    session_id: int,
    user_id: int,
    for_update: bool = False,
) -> TrainingSession:
    _validate_session_id_or_404(session_id)
    stmt = (
        select(TrainingSession)
        .where(TrainingSession.id == session_id, TrainingSession.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    training_session = (await db.execute(stmt)).scalar_one_or_none()
    if training_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Тренувальну сесію не знайдено.",
        )
    return training_session


async def _load_session_attempts(
    db: AsyncSession,
    *,
    session_id: int,
    user_id: int,
    for_update: bool = False,
) -> list[Attempt]:
    stmt = (
        select(Attempt)
        .where(Attempt.session_id == session_id, Attempt.user_id == user_id)
        .order_by(Attempt.id.asc())
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list((await db.execute(stmt)).scalars().all())


def _find_current_unfinished_attempt(attempts: Sequence[Attempt]) -> Attempt | None:
    for attempt in reversed(attempts):
        if attempt.user_answer is None:
            return attempt
    return None


@router.post(
    "/start",
    status_code=status.HTTP_201_CREATED,
    response_model=SessionStartResponse,
)
async def start_session(
    request: Request,
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
    llm_service: Annotated[LLMService, Depends(get_llm_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    now: Annotated[datetime, Depends(get_utc_now)],
    payload: Annotated[SessionStartRequest | None, Body()] = None,
) -> SessionStartResponse:
    """Start a new training session and generate its first exercise."""
    resolved_payload = payload if payload is not None else SessionStartRequest()
    resolved_settings = _resolve_active_settings(request, settings)

    user, _ = await ensure_user_initialized(db, user_id, for_update=True)

    existing_active_id = (
        await db.execute(
            select(TrainingSession.id)
            .where(
                TrainingSession.user_id == user_id,
                TrainingSession.status == "active",
            )
            .limit(1),
        )
    ).scalar_one_or_none()
    if existing_active_id is not None:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "У вас уже є активна тренувальна сесія. "
                "Завершіть або скасуйте її перед початком нової."
            ),
        )

    selected_words = await select_words_for_exercise(
        db,
        user_id,
        exclude_word_ids=None,
        now=now,
    )
    if not selected_words:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ваш словник порожній. Додайте хоча б одне слово перед початком тренування.",
        )

    total_exercises = (
        resolved_payload.total_exercises
        if resolved_payload.total_exercises is not None
        else user.exercises_per_set
    )
    exercise_type = choose_next_exercise_type([])
    grammar_topic = choose_grammar_topic(
        level=user.level,  # type: ignore[arg-type]
        mode=resolved_payload.mode,
        target_topic=resolved_payload.target_topic,
        previous_topics=[],
    )

    try:
        await check_and_increment_llm_quota(
            db,
            user_id,
            settings=resolved_settings,
            today=now,
        )
        generated_exercise, word_ids = await generate_exercise_for_session(
            llm_service,
            exercise_type=exercise_type,
            level=user.level,  # type: ignore[arg-type]
            grammar_topic=grammar_topic,
            selected_words=selected_words,
        )

        training_session = TrainingSession(
            user_id=user_id,
            mode=resolved_payload.mode,
            target_topic=resolved_payload.target_topic,
            total_exercises=total_exercises,
            status="active",
            created_at=now,
        )
        db.add(training_session)
        await db.flush()

        first_attempt = Attempt(
            session_id=training_session.id,
            user_id=user_id,
            word_ids=word_ids,
            exercise_type=generated_exercise.exercise_type,
            level=generated_exercise.level,
            grammar_topic=generated_exercise.grammar_topic,
            question_payload=generated_exercise.model_dump(),
            user_answer=None,
            used_hint=False,
            is_correct=None,
            grammar_score=None,
            fsrs_ratings=None,
            youtube_topic=None,
            created_at=now,
        )
        db.add(first_attempt)
        await db.flush()
        await db.commit()
    except DailyLLMLimitExceededError:
        await db.rollback()
        raise
    except LLMError as exc:
        await db.rollback()
        _raise_http_for_llm_error(exc)
    except Exception:
        await db.rollback()
        raise

    return SessionStartResponse(
        session_id=training_session.id,
        mode=training_session.mode,  # type: ignore[arg-type]
        target_topic=training_session.target_topic,
        total_exercises=training_session.total_exercises,
        completed_exercises=0,
        current_exercise_number=1,
        status=training_session.status,  # type: ignore[arg-type]
        created_at=training_session.created_at,
        attempt_id=first_attempt.id,
        used_hint=False,
        exercise=generated_exercise.to_public(),
    )


@router.get(
    "/active",
    response_model=ActiveSessionResponse,
    response_model_exclude_unset=True,
)
async def get_active_session(
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ActiveSessionResponse:
    """Return the authenticated user's currently active training session and unfinished exercise."""
    stmt = (
        select(TrainingSession)
        .where(TrainingSession.user_id == user_id, TrainingSession.status == "active")
        .order_by(TrainingSession.id.desc())
        .limit(1)
    )
    training_session = (await db.execute(stmt)).scalar_one_or_none()
    if training_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Активну тренувальну сесію не знайдено.",
        )

    attempts = await _load_session_attempts(
        db,
        session_id=training_session.id,
        user_id=user_id,
    )
    current_attempt = _find_current_unfinished_attempt(attempts)
    if current_attempt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Активну тренувальну сесію не знайдено.",
        )

    completed_count = sum(1 for attempt in attempts if attempt.user_answer is not None)
    generated_exercise = GeneratedExercise.model_validate(current_attempt.question_payload)

    response_kwargs: dict[str, Any] = {
        "session_id": training_session.id,
        "mode": training_session.mode,
        "target_topic": training_session.target_topic,
        "total_exercises": training_session.total_exercises,
        "completed_exercises": completed_count,
        "current_exercise_number": completed_count + 1,
        "status": training_session.status,
        "created_at": training_session.created_at,
        "attempt_id": current_attempt.id,
        "used_hint": current_attempt.used_hint,
        "exercise": generated_exercise.to_public(),
    }
    if current_attempt.used_hint:
        response_kwargs["hint_ua"] = generated_exercise.hint_ua

    return ActiveSessionResponse(**response_kwargs)


@router.post("/{id}/hint", response_model=SessionHintResponse)
async def request_session_hint(
    session_id: Annotated[int, Path(alias="id")],
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionHintResponse:
    """Reveal the hint for the current unfinished exercise and record ``used_hint=True``."""
    training_session = await _load_user_session(
        db,
        session_id=session_id,
        user_id=user_id,
        for_update=True,
    )
    if training_session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Підказка недоступна для завершеної або скасованої сесії.",
        )

    attempts = await _load_session_attempts(
        db,
        session_id=training_session.id,
        user_id=user_id,
        for_update=True,
    )
    current_attempt = _find_current_unfinished_attempt(attempts)
    if current_attempt is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="У сесії відсутня поточна незавершена вправа.",
        )

    generated_exercise = GeneratedExercise.model_validate(current_attempt.question_payload)
    if not current_attempt.used_hint:
        current_attempt.used_hint = True
        await db.commit()

    return SessionHintResponse(hint_ua=generated_exercise.hint_ua, used_hint=True)


@router.post("/{id}/answer", response_model=SessionAnswerResponse)
async def submit_session_answer(
    session_id: Annotated[int, Path(alias="id")],
    payload: SessionAnswerRequest,
    request: Request,
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
    llm_service: Annotated[LLMService, Depends(get_llm_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    now: Annotated[datetime, Depends(get_utc_now)],
) -> SessionAnswerResponse:
    """Evaluate the user's answer, update FSRS cards with hint anti-cheat, and advance the set."""
    resolved_settings = _resolve_active_settings(request, settings)

    training_session = await _load_user_session(
        db,
        session_id=session_id,
        user_id=user_id,
        for_update=True,
    )
    if training_session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Неможливо надіслати відповідь для завершеної або скасованої сесії.",
        )

    attempts = await _load_session_attempts(
        db,
        session_id=training_session.id,
        user_id=user_id,
        for_update=True,
    )
    current_attempt = _find_current_unfinished_attempt(attempts)
    if current_attempt is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="У сесії відсутня поточна незавершена вправа.",
        )

    completed_before = [attempt for attempt in attempts if attempt.user_answer is not None]
    completed_after_count = len(completed_before) + 1
    is_last_exercise = completed_after_count >= training_session.total_exercises

    user = (
        await db.execute(
            select(User)
            .where(User.telegram_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True),
        )
    ).scalar_one()

    current_exercise = GeneratedExercise.model_validate(current_attempt.question_payload)
    target_words = await load_attempt_target_words(
        db,
        user_id=user_id,
        word_ids=current_attempt.word_ids,
    )

    try:
        # Reserve quota for answer evaluation (and next exercise generation if not the last step).
        await check_and_increment_llm_quota(
            db,
            user_id,
            settings=resolved_settings,
            today=now,
        )
        if not is_last_exercise:
            await check_and_increment_llm_quota(
                db,
                user_id,
                settings=resolved_settings,
                today=now,
            )

        effective_evaluation, raw_rating_inputs = await evaluate_attempt_answer(
            llm_service,
            exercise=current_exercise,
            user_answer=payload.user_answer,
            target_words=target_words,
            attempt_word_ids=current_attempt.word_ids,
            used_hint=current_attempt.used_hint,
        )

        if raw_rating_inputs:
            await apply_word_ratings(
                db,
                user_id,
                raw_rating_inputs,
                used_hint=current_attempt.used_hint,
                now=now,
            )

        current_attempt.user_answer = payload.user_answer
        current_attempt.is_correct = effective_evaluation.is_correct
        current_attempt.grammar_score = effective_evaluation.grammar_score
        current_attempt.fsrs_ratings = [
            item.model_dump() for item in effective_evaluation.word_ratings
        ]
        current_attempt.youtube_topic = effective_evaluation.youtube_topic
        await db.flush()

        all_completed = [*completed_before, current_attempt]

        if is_last_exercise:
            training_session.status = "completed"
            summary = build_session_summary(all_completed)
            await db.commit()
            return SessionAnswerResponse(
                session_id=training_session.id,
                attempt_id=current_attempt.id,
                status="completed",
                completed_exercises=len(all_completed),
                total_exercises=training_session.total_exercises,
                used_hint=current_attempt.used_hint,
                evaluation=effective_evaluation,
                next_attempt_id=None,
                next_exercise=None,
                summary=summary,
            )

        used_word_ids = collect_used_word_ids(all_completed)
        next_words = await select_words_for_exercise(
            db,
            user_id,
            exclude_word_ids=used_word_ids,
            now=now,
        )
        next_exercise_type = choose_next_exercise_type(
            [attempt.exercise_type for attempt in all_completed],
        )
        next_grammar_topic = choose_grammar_topic(
            level=user.level,  # type: ignore[arg-type]
            mode=training_session.mode,  # type: ignore[arg-type]
            target_topic=training_session.target_topic,
            previous_topics=[attempt.grammar_topic for attempt in all_completed],
        )
        next_generated, next_word_ids = await generate_exercise_for_session(
            llm_service,
            exercise_type=next_exercise_type,
            level=user.level,  # type: ignore[arg-type]
            grammar_topic=next_grammar_topic,
            selected_words=next_words,
        )

        next_attempt = Attempt(
            session_id=training_session.id,
            user_id=user_id,
            word_ids=next_word_ids,
            exercise_type=next_generated.exercise_type,
            level=next_generated.level,
            grammar_topic=next_generated.grammar_topic,
            question_payload=next_generated.model_dump(),
            user_answer=None,
            used_hint=False,
            is_correct=None,
            grammar_score=None,
            fsrs_ratings=None,
            youtube_topic=None,
            created_at=now,
        )
        db.add(next_attempt)
        await db.flush()
        await db.commit()
    except DailyLLMLimitExceededError:
        await db.rollback()
        raise
    except LLMError as exc:
        await db.rollback()
        _raise_http_for_llm_error(exc)
    except Exception:
        await db.rollback()
        raise

    return SessionAnswerResponse(
        session_id=training_session.id,
        attempt_id=current_attempt.id,
        status="active",
        completed_exercises=completed_after_count,
        total_exercises=training_session.total_exercises,
        used_hint=current_attempt.used_hint,
        evaluation=effective_evaluation,
        next_attempt_id=next_attempt.id,
        next_exercise=next_generated.to_public(),
        summary=None,
    )


@router.post("/{id}/abandon", response_model=SessionAbandonResponse)
async def abandon_session(
    session_id: Annotated[int, Path(alias="id")],
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionAbandonResponse:
    """Abandon an active training session (idempotent on already abandoned, 409 on completed)."""
    training_session = await _load_user_session(
        db,
        session_id=session_id,
        user_id=user_id,
        for_update=True,
    )
    if training_session.status == "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Неможливо скасувати вже завершену сесію.",
        )

    attempts = await _load_session_attempts(
        db,
        session_id=training_session.id,
        user_id=user_id,
    )
    completed_count = sum(1 for attempt in attempts if attempt.user_answer is not None)

    if training_session.status == "active":
        training_session.status = "abandoned"
        await db.commit()

    return SessionAbandonResponse(
        session_id=training_session.id,
        status="abandoned",
        completed_exercises=completed_count,
        total_exercises=training_session.total_exercises,
    )


__all__ = [
    "get_llm_service",
    "router",
]
