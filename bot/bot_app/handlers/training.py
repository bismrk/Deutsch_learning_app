"""Training session FSM flow: Smart Mix, Topic selection, hints, answers, and abandon."""

from __future__ import annotations

from typing import Final, cast

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InaccessibleMessage, LinkPreviewOptions, Message

from bot_app.api_client import (
    APIUnavailableError,
    BackendAPIClient,
    BackendAPIError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    RateLimitError,
)
from bot_app.config import BotSettings
from bot_app.formatters import (
    format_evaluation_message,
    format_exercise_message,
    format_hint_message,
    format_summary_message,
)
from bot_app.keyboards import (
    MENU_BTN_TRAINING,
    TRAIN_ABANDON_CALLBACK_PREFIX,
    TRAIN_HINT_CALLBACK_PREFIX,
    TRAIN_MODE_MIX_CALLBACK,
    TRAIN_MODE_TOPIC_CALLBACK,
    TRAIN_TOPIC_CALLBACK_PREFIX,
    TRAIN_TOPIC_CANCEL_CALLBACK,
    TRAIN_TOPIC_LEVEL_CALLBACK_PREFIX,
    build_exercise_keyboard,
    build_main_menu_keyboard,
    build_topic_keyboard,
    build_training_mode_keyboard,
    resolve_grammar_topic,
)
from bot_app.schemas import VALID_USER_LEVELS, ExercisePublic, SessionMode, UserLevel

router = Router(name="training")

MAX_TARGET_TOPIC_LENGTH: Final = 200
MAX_USER_ANSWER_LENGTH: Final = 2000

TRAINING_MODE_PROMPT_UA: Final = (
    "Обери режим тренування:\n\n"
    "• <b>Розумний мікс</b> — підбираю слова за графіком FSRS і чергую граматичні теми "
    "твого рівня.\n"
    "• <b>Обрати граматичну тему</b> — зосередимося на конкретному правилі."
)
EMPTY_DICTIONARY_MESSAGE_UA: Final = (
    "Твій словник поки що порожній 📚\n\n"
    "Додай щонайменше 3–5 слів через розділ «📚 Словник», щоб я міг скласти тренувальні вправи."
)
TOPIC_CANCELLED_MESSAGE_UA: Final = (
    "Вибір теми скасовано. Обирай дію в головному меню нижче."
)
INVALID_TOPIC_MESSAGE_UA: Final = (
    "Не вдалося визначити граматичну тему. Обери тему зі списку або введи її текстом."
)
INVALID_SESSION_CALLBACK_MESSAGE_UA: Final = (
    "Некоректний ідентифікатор тренувальної сесії."
)
STALE_SESSION_MESSAGE_UA: Final = (
    "Ця тренувальна сесія вже завершена або неактивна. "
    "Можеш почати нове тренування в головному меню."
)
NON_TEXT_ANSWER_MESSAGE_UA: Final = (
    "Будь ласка, надішли відповідь текстом ✍️\n\n"
    "Стікери, фото та голосові повідомлення поки що не перевіряються."
)
EMPTY_ANSWER_MESSAGE_UA: Final = (
    "Відповідь не може бути порожньою. Напиши свій варіант текстом ✍️"
)
ANSWER_TOO_LONG_MESSAGE_UA: Final = (
    f"Відповідь занадто довга (максимум {MAX_USER_ANSWER_LENGTH} символів). "
    "Спробуй скоротити текст і надіслати ще раз."
)
NON_TEXT_TOPIC_MESSAGE_UA: Final = (
    "Будь ласка, обери граматичну тему кнопкою вище або напиши її назву текстом."
)
EMPTY_TOPIC_MESSAGE_UA: Final = (
    "Назва теми не може бути порожньою. Обери тему зі списку або введи її текстом."
)
TOPIC_TOO_LONG_MESSAGE_UA: Final = (
    f"Назва теми занадто довга (максимум {MAX_TARGET_TOPIC_LENGTH} символів). "
    "Спробуй сформулювати її коротше."
)
TEMPORARY_RETRY_HINT_UA: Final = (
    "Твоя сесія збережена — можеш надіслати відповідь ще раз."
)
COMPLETED_FALLBACK_MESSAGE_UA: Final = (
    "🏁 <b>Тренувальний сет завершено!</b>\n\nЧудова робота — повертайся до практики згодом!"
)

_DISABLED_LINK_PREVIEW: Final = LinkPreviewOptions(is_disabled=True)


class TrainingStates(StatesGroup):
    """Finite state machine for interactive training sessions."""

    choosing_topic = State()
    waiting_answer = State()


def build_topic_prompt_text(level: UserLevel) -> str:
    """Format the Ukrainian prompt shown when choosing a grammar topic."""
    return (
        f"Обери граматичну тему зі списку для рівня <b>{level}</b> "
        "(можеш перемкнути рівень нижче) або просто напиши власну тему повідомленням:"
    )


def build_abandon_confirmation_text(completed_exercises: int, total_exercises: int) -> str:
    """Format the Ukrainian confirmation message after abandoning a session early."""
    return (
        f"Тренування завершено достроково (виконано {completed_exercises} із {total_exercises} "
        "вправ).\n\n"
        "Можеш повернутися до занять у будь-який момент!"
    )


def _get_accessible_message(callback: CallbackQuery) -> Message | None:
    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        return None
    return callback.message


def _parse_positive_session_id(raw_value: str) -> int | None:
    try:
        session_id = int(raw_value)
    except ValueError:
        return None
    if session_id <= 0:
        return None
    return session_id


async def _send_exercise(
    target_message: Message,
    *,
    session_id: int,
    exercise: ExercisePublic,
    current_number: int,
    total_exercises: int,
    resumed: bool = False,
    hint_ua: str | None = None,
) -> None:
    text = format_exercise_message(
        exercise,
        current_number=current_number,
        total_exercises=total_exercises,
        resumed=resumed,
        hint_ua=hint_ua,
    )
    await target_message.answer(
        text,
        parse_mode="HTML",
        reply_markup=build_exercise_keyboard(session_id),
    )


async def _start_or_resume_session(
    target_message: Message,
    *,
    user_id: int,
    mode: SessionMode,
    target_topic: str | None,
    state: FSMContext,
    api_client: BackendAPIClient,
    settings: BotSettings,
) -> None:
    """Start a training session, handling empty dictionary (400) and active recovery (409)."""
    try:
        started = await api_client.start_session(
            user_id,
            mode=mode,
            target_topic=target_topic,
        )
    except BadRequestError:
        await state.clear()
        await target_message.answer(
            EMPTY_DICTIONARY_MESSAGE_UA,
            reply_markup=build_main_menu_keyboard(settings.miniapp_url),
        )
        return
    except ConflictError:
        await _recover_active_session(
            target_message,
            user_id=user_id,
            state=state,
            api_client=api_client,
            settings=settings,
        )
        return
    except BackendAPIError as exc:
        await target_message.answer(exc.user_message)
        return

    await state.set_state(TrainingStates.waiting_answer)
    await state.update_data(
        session_id=started.session_id,
        current_exercise_number=started.current_exercise_number,
        total_exercises=started.total_exercises,
        attempt_id=started.attempt_id,
    )
    await _send_exercise(
        target_message,
        session_id=started.session_id,
        exercise=started.exercise,
        current_number=started.current_exercise_number,
        total_exercises=started.total_exercises,
    )


async def _recover_active_session(
    target_message: Message,
    *,
    user_id: int,
    state: FSMContext,
    api_client: BackendAPIClient,
    settings: BotSettings,
) -> None:
    """Recover an already-active backend session via ``GET /api/sessions/active``."""
    try:
        active = await api_client.get_active_session(user_id)
    except NotFoundError:
        await state.clear()
        await target_message.answer(
            STALE_SESSION_MESSAGE_UA,
            reply_markup=build_main_menu_keyboard(settings.miniapp_url),
        )
        return
    except BackendAPIError as exc:
        await target_message.answer(exc.user_message)
        return

    await state.set_state(TrainingStates.waiting_answer)
    await state.update_data(
        session_id=active.session_id,
        current_exercise_number=active.current_exercise_number,
        total_exercises=active.total_exercises,
        attempt_id=active.attempt_id,
    )
    await _send_exercise(
        target_message,
        session_id=active.session_id,
        exercise=active.exercise,
        current_number=active.current_exercise_number,
        total_exercises=active.total_exercises,
        resumed=True,
        hint_ua=active.hint_ua if active.used_hint else None,
    )


@router.message(F.text == MENU_BTN_TRAINING)
@router.message(Command("train"))
async def handle_training_menu(message: Message, state: FSMContext) -> None:
    """Display the training mode chooser (Smart Mix or Grammar Topic)."""
    if message.from_user is None:
        return

    current_state = await state.get_state()
    if current_state == TrainingStates.choosing_topic.state:
        await state.clear()

    await message.answer(
        TRAINING_MODE_PROMPT_UA,
        parse_mode="HTML",
        reply_markup=build_training_mode_keyboard(),
    )


@router.callback_query(F.data == TRAIN_MODE_MIX_CALLBACK)
async def handle_start_mix_mode(
    callback: CallbackQuery,
    state: FSMContext,
    api_client: BackendAPIClient,
    settings: BotSettings,
) -> None:
    """Start (or resume) a Smart Mix training session."""
    await callback.answer()
    if callback.from_user is None:
        return

    target = _get_accessible_message(callback)
    if target is None:
        return

    await _start_or_resume_session(
        target,
        user_id=callback.from_user.id,
        mode="mix",
        target_topic=None,
        state=state,
        api_client=api_client,
        settings=settings,
    )


@router.callback_query(F.data == TRAIN_MODE_TOPIC_CALLBACK)
async def handle_choose_topic_mode(
    callback: CallbackQuery,
    state: FSMContext,
    api_client: BackendAPIClient,
) -> None:
    """Enter ``TrainingStates.choosing_topic`` and show level-appropriate grammar topics."""
    await callback.answer()
    if callback.from_user is None:
        return

    target = _get_accessible_message(callback)
    if target is None:
        return

    try:
        profile = await api_client.get_me(callback.from_user.id)
    except BackendAPIError as exc:
        await target.answer(exc.user_message)
        return

    await state.set_state(TrainingStates.choosing_topic)
    await target.answer(
        build_topic_prompt_text(profile.level),
        parse_mode="HTML",
        reply_markup=build_topic_keyboard(profile.level),
    )


@router.callback_query(F.data.startswith(TRAIN_TOPIC_LEVEL_CALLBACK_PREFIX))
async def handle_topic_level_switch(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Switch the inline grammar topic list between A1, A2, and B1 levels."""
    if callback.data is None:
        await callback.answer()
        return

    raw_level = callback.data.removeprefix(TRAIN_TOPIC_LEVEL_CALLBACK_PREFIX)
    if raw_level not in VALID_USER_LEVELS:
        await callback.answer(INVALID_TOPIC_MESSAGE_UA)
        return

    selected_level = cast(UserLevel, raw_level)
    await state.set_state(TrainingStates.choosing_topic)
    await callback.answer()

    target = _get_accessible_message(callback)
    if target is None:
        return

    prompt_text = build_topic_prompt_text(selected_level)
    keyboard = build_topic_keyboard(selected_level)
    if hasattr(target, "edit_text"):
        try:
            await target.edit_text(
                prompt_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest:
            return
    await target.answer(prompt_text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == TRAIN_TOPIC_CANCEL_CALLBACK)
async def handle_topic_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    """Cancel grammar topic selection, clear FSM state, and restore the main menu."""
    await state.clear()
    await callback.answer("Вибір теми скасовано.")

    target = _get_accessible_message(callback)
    if target is not None:
        await target.answer(
            TOPIC_CANCELLED_MESSAGE_UA,
            reply_markup=build_main_menu_keyboard(settings.miniapp_url),
        )


@router.callback_query(F.data.startswith(TRAIN_TOPIC_CALLBACK_PREFIX))
async def handle_topic_selected(
    callback: CallbackQuery,
    state: FSMContext,
    api_client: BackendAPIClient,
    settings: BotSettings,
) -> None:
    """Start (or resume) a topic-focused session for an inline-selected grammar topic."""
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return

    payload = callback.data.removeprefix(TRAIN_TOPIC_CALLBACK_PREFIX)
    parts = payload.split(":", 1)
    topic = resolve_grammar_topic(parts[0], parts[1]) if len(parts) == 2 else None
    if topic is None:
        await callback.answer(INVALID_TOPIC_MESSAGE_UA)
        return

    await callback.answer()
    target = _get_accessible_message(callback)
    if target is None:
        return

    await _start_or_resume_session(
        target,
        user_id=callback.from_user.id,
        mode="topic",
        target_topic=topic,
        state=state,
        api_client=api_client,
        settings=settings,
    )


@router.callback_query(F.data.startswith(TRAIN_HINT_CALLBACK_PREFIX))
async def handle_training_hint(
    callback: CallbackQuery,
    state: FSMContext,
    api_client: BackendAPIClient,
) -> None:
    """Fetch and display the exercise hint while keeping the user in ``waiting_answer``."""
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return

    raw_session_id = callback.data.removeprefix(TRAIN_HINT_CALLBACK_PREFIX)
    session_id = _parse_positive_session_id(raw_session_id)
    if session_id is None:
        await callback.answer(INVALID_SESSION_CALLBACK_MESSAGE_UA)
        return

    target = _get_accessible_message(callback)
    try:
        hint_response = await api_client.request_hint(callback.from_user.id, session_id)
    except (NotFoundError, ConflictError):
        data = await state.get_data()
        if data.get("session_id") == session_id:
            await state.clear()
        await callback.answer("Сесія вже завершена.")
        if target is not None:
            await target.answer(STALE_SESSION_MESSAGE_UA)
        return
    except BackendAPIError as exc:
        await callback.answer()
        if target is not None:
            await target.answer(exc.user_message)
        return

    await callback.answer()
    current_state = await state.get_state()
    data = await state.get_data()
    if current_state != TrainingStates.waiting_answer.state or data.get("session_id") != session_id:
        await state.set_state(TrainingStates.waiting_answer)
        await state.update_data(session_id=session_id)

    if target is not None:
        await target.answer(
            format_hint_message(hint_response.hint_ua),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith(TRAIN_ABANDON_CALLBACK_PREFIX))
async def handle_training_abandon(
    callback: CallbackQuery,
    state: FSMContext,
    api_client: BackendAPIClient,
    settings: BotSettings,
) -> None:
    """Abandon the active training session early, clear FSM, and restore the main menu."""
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return

    raw_session_id = callback.data.removeprefix(TRAIN_ABANDON_CALLBACK_PREFIX)
    session_id = _parse_positive_session_id(raw_session_id)
    if session_id is None:
        await callback.answer(INVALID_SESSION_CALLBACK_MESSAGE_UA)
        return

    target = _get_accessible_message(callback)
    try:
        abandoned = await api_client.abandon_session(callback.from_user.id, session_id)
    except (NotFoundError, ConflictError):
        data = await state.get_data()
        if data.get("session_id") in (None, session_id):
            await state.clear()
        await callback.answer("Сесія вже завершена.")
        if target is not None:
            await target.answer(
                STALE_SESSION_MESSAGE_UA,
                reply_markup=build_main_menu_keyboard(settings.miniapp_url),
            )
        return
    except BackendAPIError as exc:
        await callback.answer()
        if target is not None:
            await target.answer(exc.user_message)
        return

    await state.clear()
    await callback.answer("Тренування завершено.")
    if target is not None:
        await target.answer(
            build_abandon_confirmation_text(
                abandoned.completed_exercises,
                abandoned.total_exercises,
            ),
            reply_markup=build_main_menu_keyboard(settings.miniapp_url),
        )


@router.message(TrainingStates.choosing_topic, F.text, ~F.text.startswith("/"))
async def handle_topic_text_input(
    message: Message,
    state: FSMContext,
    api_client: BackendAPIClient,
    settings: BotSettings,
) -> None:
    """Start a topic-focused training session from custom user-typed topic text."""
    if message.from_user is None:
        return

    cleaned_topic = (message.text or "").strip()
    if not cleaned_topic:
        await message.answer(EMPTY_TOPIC_MESSAGE_UA)
        return
    if cleaned_topic.startswith("/"):
        return
    if len(cleaned_topic) > MAX_TARGET_TOPIC_LENGTH:
        await message.answer(TOPIC_TOO_LONG_MESSAGE_UA)
        return

    await _start_or_resume_session(
        message,
        user_id=message.from_user.id,
        mode="topic",
        target_topic=cleaned_topic,
        state=state,
        api_client=api_client,
        settings=settings,
    )


@router.message(TrainingStates.choosing_topic, ~F.text)
async def handle_topic_non_text(message: Message) -> None:
    """Prompt the user to choose or type a grammar topic instead of sending media."""
    await message.answer(NON_TEXT_TOPIC_MESSAGE_UA)


@router.message(TrainingStates.waiting_answer, F.text, ~F.text.startswith("/"))
async def handle_exercise_answer(
    message: Message,
    state: FSMContext,
    api_client: BackendAPIClient,
    settings: BotSettings,
) -> None:
    """Submit a text exercise answer, show structured evaluation, and advance or finish."""
    if message.from_user is None:
        return

    cleaned_answer = (message.text or "").strip()
    if not cleaned_answer:
        await message.answer(EMPTY_ANSWER_MESSAGE_UA)
        return
    if cleaned_answer.startswith("/"):
        return
    if len(cleaned_answer) > MAX_USER_ANSWER_LENGTH:
        await message.answer(ANSWER_TOO_LONG_MESSAGE_UA)
        return

    data = await state.get_data()
    raw_session_id = data.get("session_id")
    session_id = (
        raw_session_id
        if isinstance(raw_session_id, int)
        and not isinstance(raw_session_id, bool)
        and raw_session_id > 0
        else None
    )
    if session_id is None:
        try:
            active = await api_client.get_active_session(message.from_user.id)
        except NotFoundError:
            await state.clear()
            await message.answer(
                STALE_SESSION_MESSAGE_UA,
                reply_markup=build_main_menu_keyboard(settings.miniapp_url),
            )
            return
        except BackendAPIError as exc:
            await message.answer(exc.user_message)
            return
        session_id = active.session_id
        await state.update_data(session_id=session_id)

    try:
        answer_response = await api_client.submit_answer(
            message.from_user.id,
            session_id,
            cleaned_answer,
        )
    except (NotFoundError, ConflictError):
        await state.clear()
        await message.answer(
            STALE_SESSION_MESSAGE_UA,
            reply_markup=build_main_menu_keyboard(settings.miniapp_url),
        )
        return
    except RateLimitError as exc:
        await message.answer(exc.user_message)
        return
    except APIUnavailableError as exc:
        await message.answer(f"{exc.user_message}\n\n{TEMPORARY_RETRY_HINT_UA}")
        return
    except BackendAPIError as exc:
        await message.answer(exc.user_message)
        return

    await message.answer(
        format_evaluation_message(
            answer_response.evaluation,
            used_hint=answer_response.used_hint,
        ),
        parse_mode="HTML",
        link_preview_options=_DISABLED_LINK_PREVIEW,
    )

    if answer_response.status == "active" and answer_response.next_exercise is not None:
        next_number = answer_response.completed_exercises + 1
        await state.set_state(TrainingStates.waiting_answer)
        await state.update_data(
            session_id=answer_response.session_id,
            current_exercise_number=next_number,
            total_exercises=answer_response.total_exercises,
            attempt_id=answer_response.next_attempt_id,
        )
        await _send_exercise(
            message,
            session_id=answer_response.session_id,
            exercise=answer_response.next_exercise,
            current_number=next_number,
            total_exercises=answer_response.total_exercises,
        )
        return

    if answer_response.status == "completed":
        await state.clear()
        summary_text = (
            format_summary_message(answer_response.summary)
            if answer_response.summary is not None
            else COMPLETED_FALLBACK_MESSAGE_UA
        )
        await message.answer(
            summary_text,
            parse_mode="HTML",
            link_preview_options=_DISABLED_LINK_PREVIEW,
            reply_markup=build_main_menu_keyboard(settings.miniapp_url),
        )
        return

    await state.clear()
    await message.answer(
        STALE_SESSION_MESSAGE_UA,
        reply_markup=build_main_menu_keyboard(settings.miniapp_url),
    )


@router.message(TrainingStates.waiting_answer, ~F.text)
async def handle_non_text_answer(message: Message) -> None:
    """Reject stickers, photos, voice notes, and other non-text answers without API calls."""
    await message.answer(NON_TEXT_ANSWER_MESSAGE_UA)


__all__ = [
    "ANSWER_TOO_LONG_MESSAGE_UA",
    "COMPLETED_FALLBACK_MESSAGE_UA",
    "EMPTY_ANSWER_MESSAGE_UA",
    "EMPTY_DICTIONARY_MESSAGE_UA",
    "EMPTY_TOPIC_MESSAGE_UA",
    "INVALID_SESSION_CALLBACK_MESSAGE_UA",
    "INVALID_TOPIC_MESSAGE_UA",
    "MAX_TARGET_TOPIC_LENGTH",
    "MAX_USER_ANSWER_LENGTH",
    "NON_TEXT_ANSWER_MESSAGE_UA",
    "NON_TEXT_TOPIC_MESSAGE_UA",
    "STALE_SESSION_MESSAGE_UA",
    "TEMPORARY_RETRY_HINT_UA",
    "TOPIC_CANCELLED_MESSAGE_UA",
    "TOPIC_TOO_LONG_MESSAGE_UA",
    "TRAINING_MODE_PROMPT_UA",
    "TrainingStates",
    "build_abandon_confirmation_text",
    "build_topic_prompt_text",
    "handle_choose_topic_mode",
    "handle_exercise_answer",
    "handle_non_text_answer",
    "handle_start_mix_mode",
    "handle_topic_cancel",
    "handle_topic_level_switch",
    "handle_topic_non_text",
    "handle_topic_selected",
    "handle_topic_text_input",
    "handle_training_abandon",
    "handle_training_hint",
    "handle_training_menu",
    "router",
]
