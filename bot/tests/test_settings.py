"""Unit and dispatcher tests for user settings handlers, keyboards, and error handling."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText
from aiogram.types import (
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from bot_app.api_client import (
    RATE_LIMIT_MESSAGE_UA,
    TIMEOUT_ERROR_MESSAGE_UA,
    UNAVAILABLE_ERROR_MESSAGE_UA,
    APIUnavailableError,
    BackendAPIClient,
    RateLimitError,
    ValidationAPIError,
)
from bot_app.config import BotSettings
from bot_app.handlers.settings import (
    SETTINGS_CANCELLED_UA,
    SETTINGS_CUSTOM_EXERCISES_PROMPT_UA,
    SETTINGS_CUSTOM_REMINDER_PROMPT_UA,
    SETTINGS_CUSTOM_TIMEZONE_PROMPT_UA,
    SETTINGS_INVALID_EXERCISES_UA,
    SETTINGS_INVALID_LEVEL_UA,
    SETTINGS_INVALID_REMINDER_UA,
    SETTINGS_INVALID_TIMEZONE_UA,
    SETTINGS_NON_TEXT_INPUT_UA,
    SettingsStates,
    handle_settings_back_to_menu,
    handle_settings_cancel,
    handle_settings_exercises_custom_start,
    handle_settings_exercises_select,
    handle_settings_exercises_text_input,
    handle_settings_level_select,
    handle_settings_menu,
    handle_settings_non_text_input,
    handle_settings_open_exercises,
    handle_settings_open_level,
    handle_settings_open_reminder,
    handle_settings_open_timezone,
    handle_settings_reminder_custom_start,
    handle_settings_reminder_select,
    handle_settings_reminder_text_input,
    handle_settings_timezone_custom_start,
    handle_settings_timezone_select,
    handle_settings_timezone_text_input,
)
from bot_app.handlers.training import TrainingStates
from bot_app.keyboards import (
    MENU_BTN_SETTINGS,
    SETTINGS_CANCEL_CALLBACK,
    SETTINGS_EXERCISES_CALLBACK_PREFIX,
    SETTINGS_EXERCISES_CUSTOM_CALLBACK,
    SETTINGS_LEVEL_CALLBACK_PREFIX,
    SETTINGS_MENU_CALLBACK,
    SETTINGS_OPEN_EXERCISES_CALLBACK,
    SETTINGS_OPEN_LEVEL_CALLBACK,
    SETTINGS_OPEN_REMINDER_CALLBACK,
    SETTINGS_OPEN_TIMEZONE_CALLBACK,
    SETTINGS_REMINDER_CALLBACK_PREFIX,
    SETTINGS_REMINDER_CUSTOM_CALLBACK,
    SETTINGS_TZ_CALLBACK_PREFIX,
    SETTINGS_TZ_CUSTOM_CALLBACK,
    build_settings_exercises_keyboard,
    build_settings_level_keyboard,
    build_settings_reminder_keyboard,
    build_settings_timezone_keyboard,
)
from bot_app.main import create_dispatcher
from bot_app.schemas import (
    ExerciseEvaluation,
    SessionAnswerResponse,
    SessionSummary,
    UserProfile,
)

BASE_URL = "http://backend.test:8000"
SECRET_API_KEY = "test-internal-api-key-15"
BOT_ID = 777015
USER_ID = 555222333
CHAT_ID = 555222333


def _make_settings(miniapp_url: str | None = "https://deutsch.example.com") -> BotSettings:
    return BotSettings(
        _env_file=None,
        bot_token="123456:TEST-TOKEN",
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
        miniapp_url=miniapp_url,
    )


def _make_fsm() -> tuple[MemoryStorage, FSMContext]:
    storage = MemoryStorage()
    key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
    return storage, FSMContext(storage=storage, key=key)


def _make_api_mock() -> AsyncMock:
    return AsyncMock(spec=BackendAPIClient)


def _make_message(text: str | None = None, user_id: int = USER_ID) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )


def _make_callback(
    data: str,
    *,
    user_id: int = USER_ID,
    edit_side_effect: Exception | None = None,
) -> SimpleNamespace:
    edit_mock = AsyncMock(side_effect=edit_side_effect)
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        data=data,
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock(), edit_text=edit_mock),
    )


def _make_profile(
    *,
    level: str = "A2",
    exercises_per_set: int = 5,
    reminder_hour: int = 19,
    timezone: str = "Europe/Kyiv",
    sheets_url: str | None = None,
) -> UserProfile:
    return UserProfile(
        telegram_id=USER_ID,
        level=level,  # type: ignore[arg-type]
        exercises_per_set=exercises_per_set,
        theme="system",
        sheets_url=sheets_url,
        reminder_hour=reminder_hour,
        timezone=timezone,
        daily_llm_calls=0,
        created_at=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
    )


def _not_modified_error() -> TelegramBadRequest:
    return TelegramBadRequest(
        method=EditMessageText(chat_id=CHAT_ID, message_id=1, text="same"),
        message="Bad Request: message is not modified: specified new message content "
        "and reply markup are exactly the same",
    )


def _flatten_callbacks(markup: InlineKeyboardMarkup) -> list[str]:
    return [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data is not None
    ]


def _flatten_texts(markup: InlineKeyboardMarkup) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]


def test_settings_keyboards_include_expected_options_and_active_markers() -> None:
    lvl_kb = build_settings_level_keyboard("B1")
    assert _flatten_callbacks(lvl_kb) == [
        "settings:level:A1",
        "settings:level:A2",
        "settings:level:B1",
        SETTINGS_MENU_CALLBACK,
    ]
    assert "✓ B1" in _flatten_texts(lvl_kb)

    ex_kb = build_settings_exercises_keyboard(10)
    assert "✓ 10" in _flatten_texts(ex_kb)
    assert SETTINGS_EXERCISES_CUSTOM_CALLBACK in _flatten_callbacks(ex_kb)
    assert SETTINGS_MENU_CALLBACK in _flatten_callbacks(ex_kb)

    rem_kb = build_settings_reminder_keyboard(7)
    rem_callbacks = _flatten_callbacks(rem_kb)
    for hour in range(24):
        assert f"{SETTINGS_REMINDER_CALLBACK_PREFIX}{hour}" in rem_callbacks
    assert "✓ 07:00" in _flatten_texts(rem_kb)
    assert SETTINGS_REMINDER_CUSTOM_CALLBACK in rem_callbacks

    tz_kb = build_settings_timezone_keyboard("Europe/Berlin")
    tz_callbacks = _flatten_callbacks(tz_kb)
    for expected_tz in ("Europe/Kyiv", "Europe/Berlin", "Europe/Warsaw", "UTC"):
        assert f"{SETTINGS_TZ_CALLBACK_PREFIX}{expected_tz}" in tz_callbacks
    assert "✓ Europe/Berlin" in _flatten_texts(tz_kb)
    assert SETTINGS_TZ_CUSTOM_CALLBACK in tz_callbacks


async def test_settings_menu_fetches_profile_and_displays_overview() -> None:
    _, state = _make_fsm()
    await state.set_state(SettingsStates.waiting_timezone)
    api_client = _make_api_mock()
    api_client.get_me.return_value = _make_profile(
        level="B1",
        exercises_per_set=10,
        reminder_hour=8,
        timezone="Europe/Berlin",
        sheets_url="https://docs.google.com/spreadsheets/d/abc/edit",
    )
    message = _make_message(MENU_BTN_SETTINGS)

    await handle_settings_menu(message=message, state=state, api_client=api_client)

    assert await state.get_state() is None
    api_client.get_me.assert_awaited_once_with(USER_ID)
    reply = message.answer.await_args.args[0]
    assert "B1" in reply
    assert "10" in reply
    assert "08:00" in reply
    assert "Europe/Berlin" in reply
    assert "підключено" in reply
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_callbacks(markup) == [
        SETTINGS_OPEN_LEVEL_CALLBACK,
        SETTINGS_OPEN_EXERCISES_CALLBACK,
        SETTINGS_OPEN_REMINDER_CALLBACK,
        SETTINGS_OPEN_TIMEZONE_CALLBACK,
    ]


@pytest.mark.parametrize(
    ("exc", "expected_fragment"),
    [
        (RateLimitError(RATE_LIMIT_MESSAGE_UA, status_code=429), "ліміт"),
        (APIUnavailableError(UNAVAILABLE_ERROR_MESSAGE_UA, status_code=503), "недоступний"),
        (APIUnavailableError(TIMEOUT_ERROR_MESSAGE_UA, status_code=None), "довго"),
    ],
)
async def test_settings_menu_handles_rate_limit_and_unavailable(
    exc: Exception,
    expected_fragment: str,
) -> None:
    _, state = _make_fsm()
    api_client = _make_api_mock()
    api_client.get_me.side_effect = exc
    message = _make_message(MENU_BTN_SETTINGS)

    await handle_settings_menu(message=message, state=state, api_client=api_client)

    message.answer.assert_awaited_once()
    assert expected_fragment in message.answer.await_args.args[0].lower()


async def test_settings_open_submenus_and_back_to_menu() -> None:
    _, state = _make_fsm()
    api_client = _make_api_mock()
    api_client.get_me.return_value = _make_profile()

    for handler, cb_data in (
        (handle_settings_open_level, SETTINGS_OPEN_LEVEL_CALLBACK),
        (handle_settings_open_exercises, SETTINGS_OPEN_EXERCISES_CALLBACK),
        (handle_settings_open_reminder, SETTINGS_OPEN_REMINDER_CALLBACK),
        (handle_settings_open_timezone, SETTINGS_OPEN_TIMEZONE_CALLBACK),
    ):
        cb = _make_callback(cb_data)
        await handler(callback=cb, api_client=api_client)
        cb.answer.assert_awaited_once()
        cb.message.edit_text.assert_awaited_once()

    back_cb = _make_callback(SETTINGS_MENU_CALLBACK)
    await handle_settings_back_to_menu(callback=back_cb, state=state, api_client=api_client)
    back_cb.answer.assert_awaited_once()
    back_cb.message.edit_text.assert_awaited_once()
    assert "Налаштування" in back_cb.message.edit_text.await_args.args[0]


@pytest.mark.parametrize("selected_level", ["A1", "A2", "B1"])
async def test_settings_level_update_patches_and_edits_message(selected_level: str) -> None:
    api_client = _make_api_mock()
    api_client.update_settings.return_value = _make_profile(level=selected_level)
    cb = _make_callback(f"{SETTINGS_LEVEL_CALLBACK_PREFIX}{selected_level}")

    await handle_settings_level_select(callback=cb, api_client=api_client)

    api_client.update_settings.assert_awaited_once_with(USER_ID, level=selected_level)
    cb.answer.assert_awaited_once()
    assert selected_level in cb.answer.await_args.args[0]
    cb.message.edit_text.assert_awaited_once()


async def test_settings_clicking_already_active_level_ignores_message_not_modified() -> None:
    api_client = _make_api_mock()
    api_client.update_settings.return_value = _make_profile(level="A2")
    cb = _make_callback(
        f"{SETTINGS_LEVEL_CALLBACK_PREFIX}A2",
        edit_side_effect=_not_modified_error(),
    )

    await handle_settings_level_select(callback=cb, api_client=api_client)

    cb.answer.assert_awaited_once()
    cb.message.edit_text.assert_awaited_once()
    cb.message.answer.assert_not_awaited()


async def test_settings_invalid_level_callback_rejected_without_api_call() -> None:
    api_client = _make_api_mock()
    cb = _make_callback(f"{SETTINGS_LEVEL_CALLBACK_PREFIX}C1")

    await handle_settings_level_select(callback=cb, api_client=api_client)

    api_client.update_settings.assert_not_awaited()
    cb.answer.assert_awaited_once_with(SETTINGS_INVALID_LEVEL_UA)


async def test_settings_exercises_preset_update_and_not_modified_guard() -> None:
    api_client = _make_api_mock()
    api_client.update_settings.return_value = _make_profile(exercises_per_set=10)
    cb = _make_callback(
        f"{SETTINGS_EXERCISES_CALLBACK_PREFIX}10",
        edit_side_effect=_not_modified_error(),
    )

    await handle_settings_exercises_select(callback=cb, api_client=api_client)

    api_client.update_settings.assert_awaited_once_with(USER_ID, exercises_per_set=10)
    cb.answer.assert_awaited_once()
    assert "10" in cb.answer.await_args.args[0]
    cb.message.answer.assert_not_awaited()


@pytest.mark.parametrize("bad_value", ["0", "31", "-5", "abc"])
async def test_settings_exercises_invalid_callback_rejected_without_api_call(
    bad_value: str,
) -> None:
    api_client = _make_api_mock()
    cb = _make_callback(f"{SETTINGS_EXERCISES_CALLBACK_PREFIX}{bad_value}")

    await handle_settings_exercises_select(callback=cb, api_client=api_client)

    api_client.update_settings.assert_not_awaited()
    cb.answer.assert_awaited_once()


async def test_settings_exercises_custom_text_flow_validation_and_success() -> None:
    _, state = _make_fsm()
    start_cb = _make_callback(SETTINGS_EXERCISES_CUSTOM_CALLBACK)
    await handle_settings_exercises_custom_start(callback=start_cb, state=state)

    start_cb.answer.assert_awaited_once()
    assert await state.get_state() == SettingsStates.waiting_exercises_per_set.state
    assert start_cb.message.answer.await_args.args[0] == SETTINGS_CUSTOM_EXERCISES_PROMPT_UA
    assert _flatten_callbacks(start_cb.message.answer.await_args.kwargs["reply_markup"]) == [
        SETTINGS_CANCEL_CALLBACK,
    ]

    api_client = _make_api_mock()
    bad_msg = _make_message("35")
    await handle_settings_exercises_text_input(
        message=bad_msg,
        state=state,
        api_client=api_client,
    )
    api_client.update_settings.assert_not_awaited()
    assert await state.get_state() == SettingsStates.waiting_exercises_per_set.state
    assert bad_msg.answer.await_args.args[0] == SETTINGS_INVALID_EXERCISES_UA

    api_client.update_settings.return_value = _make_profile(exercises_per_set=12)
    good_msg = _make_message("12")
    await handle_settings_exercises_text_input(
        message=good_msg,
        state=state,
        api_client=api_client,
    )
    api_client.update_settings.assert_awaited_once_with(USER_ID, exercises_per_set=12)
    assert await state.get_state() is None
    assert "12" in good_msg.answer.await_args.args[0]


@pytest.mark.parametrize("hour", [0, 9, 19, 23])
async def test_settings_reminder_hour_preset_updates_successfully(hour: int) -> None:
    api_client = _make_api_mock()
    api_client.update_settings.return_value = _make_profile(reminder_hour=hour)
    cb = _make_callback(f"{SETTINGS_REMINDER_CALLBACK_PREFIX}{hour}")

    await handle_settings_reminder_select(callback=cb, api_client=api_client)

    api_client.update_settings.assert_awaited_once_with(USER_ID, reminder_hour=hour)
    cb.answer.assert_awaited_once()
    assert f"{hour:02d}:00" in cb.answer.await_args.args[0]
    cb.message.edit_text.assert_awaited_once()


async def test_settings_reminder_custom_text_flow_validation_and_success() -> None:
    _, state = _make_fsm()
    start_cb = _make_callback(SETTINGS_REMINDER_CUSTOM_CALLBACK)
    await handle_settings_reminder_custom_start(callback=start_cb, state=state)

    start_cb.answer.assert_awaited_once()
    assert await state.get_state() == SettingsStates.waiting_reminder_hour.state
    assert start_cb.message.answer.await_args.args[0] == SETTINGS_CUSTOM_REMINDER_PROMPT_UA

    api_client = _make_api_mock()
    bad_msg = _make_message("24")
    await handle_settings_reminder_text_input(
        message=bad_msg,
        state=state,
        api_client=api_client,
    )
    api_client.update_settings.assert_not_awaited()
    assert bad_msg.answer.await_args.args[0] == SETTINGS_INVALID_REMINDER_UA
    assert await state.get_state() == SettingsStates.waiting_reminder_hour.state

    api_client.update_settings.return_value = _make_profile(reminder_hour=7)
    good_msg = _make_message("07")
    await handle_settings_reminder_text_input(
        message=good_msg,
        state=state,
        api_client=api_client,
    )
    api_client.update_settings.assert_awaited_once_with(USER_ID, reminder_hour=7)
    assert await state.get_state() is None
    assert "07:00" in good_msg.answer.await_args.args[0]


@pytest.mark.parametrize("zone", ["Europe/Kyiv", "Europe/Berlin", "Europe/Warsaw", "UTC"])
async def test_settings_timezone_preset_updates_and_guards_not_modified(zone: str) -> None:
    api_client = _make_api_mock()
    api_client.update_settings.return_value = _make_profile(timezone=zone)
    cb = _make_callback(
        f"{SETTINGS_TZ_CALLBACK_PREFIX}{zone}",
        edit_side_effect=_not_modified_error(),
    )

    await handle_settings_timezone_select(callback=cb, api_client=api_client)

    api_client.update_settings.assert_awaited_once_with(USER_ID, timezone=zone)
    cb.answer.assert_awaited_once()
    assert zone in cb.answer.await_args.args[0]
    cb.message.answer.assert_not_awaited()


async def test_settings_timezone_custom_422_and_successful_retry() -> None:
    _, state = _make_fsm()
    start_cb = _make_callback(SETTINGS_TZ_CUSTOM_CALLBACK)
    await handle_settings_timezone_custom_start(callback=start_cb, state=state)

    start_cb.answer.assert_awaited_once()
    assert await state.get_state() == SettingsStates.waiting_timezone.state
    assert start_cb.message.answer.await_args.args[0] == SETTINGS_CUSTOM_TIMEZONE_PROMPT_UA
    assert _flatten_callbacks(start_cb.message.answer.await_args.kwargs["reply_markup"]) == [
        SETTINGS_CANCEL_CALLBACK,
    ]

    api_client = _make_api_mock()
    api_client.update_settings.side_effect = ValidationAPIError(
        "Value error, timezone must be a valid IANA timezone name",
        status_code=422,
        detail="Value error, timezone must be a valid IANA timezone name",
    )
    bad_msg = _make_message("Mars/Olympus")
    await handle_settings_timezone_text_input(
        message=bad_msg,
        state=state,
        api_client=api_client,
    )

    api_client.update_settings.assert_awaited_once_with(USER_ID, timezone="Mars/Olympus")
    assert await state.get_state() == SettingsStates.waiting_timezone.state
    assert bad_msg.answer.await_args.args[0] == SETTINGS_INVALID_TIMEZONE_UA
    assert "Europe/Kyiv" in bad_msg.answer.await_args.args[0]
    assert "Value error" not in bad_msg.answer.await_args.args[0]
    assert _flatten_callbacks(bad_msg.answer.await_args.kwargs["reply_markup"]) == [
        SETTINGS_CANCEL_CALLBACK,
    ]

    api_client.update_settings.side_effect = None
    api_client.update_settings.return_value = _make_profile(timezone="America/New_York")
    good_msg = _make_message("America/New_York")
    await handle_settings_timezone_text_input(
        message=good_msg,
        state=state,
        api_client=api_client,
    )

    assert await state.get_state() is None
    assert "America/New_York" in good_msg.answer.await_args.args[0]


@pytest.mark.parametrize(
    ("exc", "expected_fragment"),
    [
        (RateLimitError(RATE_LIMIT_MESSAGE_UA, status_code=429), "ліміт"),
        (APIUnavailableError(UNAVAILABLE_ERROR_MESSAGE_UA, status_code=503), "недоступний"),
        (APIUnavailableError(TIMEOUT_ERROR_MESSAGE_UA, status_code=None), "довго"),
    ],
)
async def test_settings_callbacks_and_text_inputs_handle_rate_limit_and_unavailable(
    exc: Exception,
    expected_fragment: str,
) -> None:
    api_client = _make_api_mock()
    api_client.update_settings.side_effect = exc

    cb = _make_callback(f"{SETTINGS_TZ_CALLBACK_PREFIX}Europe/Berlin")
    await handle_settings_timezone_select(callback=cb, api_client=api_client)
    cb.answer.assert_awaited_once()
    assert expected_fragment in cb.message.answer.await_args.args[0].lower()

    _, state = _make_fsm()
    await state.set_state(SettingsStates.waiting_timezone)
    msg = _make_message("Europe/Vienna")
    await handle_settings_timezone_text_input(message=msg, state=state, api_client=api_client)
    assert await state.get_state() == SettingsStates.waiting_timezone.state
    assert expected_fragment in msg.answer.await_args.args[0].lower()


async def test_settings_non_text_input_shows_hint_with_cancel() -> None:
    msg = _make_message(text=None)
    await handle_settings_non_text_input(message=msg)

    assert msg.answer.await_args.args[0] == SETTINGS_NON_TEXT_INPUT_UA
    assert _flatten_callbacks(msg.answer.await_args.kwargs["reply_markup"]) == [
        SETTINGS_CANCEL_CALLBACK,
    ]


@pytest.mark.parametrize(
    "active_state",
    [
        SettingsStates.waiting_exercises_per_set,
        SettingsStates.waiting_reminder_hour,
        SettingsStates.waiting_timezone,
    ],
)
async def test_settings_cancel_clears_fsm_and_restores_main_menu(active_state: object) -> None:
    _, state = _make_fsm()
    await state.set_state(active_state)  # type: ignore[arg-type]
    cb = _make_callback(SETTINGS_CANCEL_CALLBACK)

    await handle_settings_cancel(callback=cb, state=state, settings=_make_settings())

    assert await state.get_state() is None
    cb.answer.assert_awaited_once()
    assert cb.message.answer.await_args.args[0] == SETTINGS_CANCELLED_UA
    assert isinstance(cb.message.answer.await_args.kwargs["reply_markup"], ReplyKeyboardMarkup)


async def test_dispatcher_training_waiting_answer_is_not_hijacked_by_settings_router() -> None:
    api_client = _make_api_mock()
    api_client.submit_answer.return_value = SessionAnswerResponse(
        session_id=42,
        attempt_id=101,
        status="completed",
        completed_exercises=1,
        total_exercises=1,
        used_hint=False,
        evaluation=ExerciseEvaluation(
            is_correct=True,
            grammar_score=95,
            corrected_answer="Europe/Berlin",
            word_ratings=[],
            errors=[],
            youtube_topic="Geografie A1",
            summary_ua="Добре!",
        ),
        summary=SessionSummary(
            total_exercises=1,
            correct_count=1,
            avg_grammar_score=95.0,
            weak_topics=[],
        ),
    )
    dispatcher = create_dispatcher(api_client=api_client, settings=_make_settings())
    key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
    await dispatcher.storage.set_state(key, TrainingStates.waiting_answer)
    await dispatcher.storage.set_data(key, {"session_id": 42})

    bot = MagicMock()
    bot.id = BOT_ID
    bot.side_effect = AsyncMock()

    msg = Message(
        message_id=30,
        date=datetime(2026, 11, 24, 12, 10, tzinfo=UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="Tester"),
        text=MENU_BTN_SETTINGS,
    )
    await dispatcher.feed_update(bot, Update(update_id=10, message=msg))

    api_client.submit_answer.assert_awaited_once_with(USER_ID, 42, MENU_BTN_SETTINGS)
    api_client.get_me.assert_not_awaited()


@respx.mock
async def test_dispatcher_start_command_clears_settings_fsm_state() -> None:
    respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "telegram_id": USER_ID,
                "level": "A2",
                "exercises_per_set": 5,
                "theme": "system",
                "sheets_url": None,
                "reminder_hour": 19,
                "timezone": "Europe/Kyiv",
                "daily_llm_calls": 0,
                "created_at": "2026-11-24T12:00:00Z",
            },
        ),
    )
    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        dispatcher = create_dispatcher(api_client=client, settings=_make_settings())
        key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
        await dispatcher.storage.set_state(key, SettingsStates.waiting_timezone)

        bot = MagicMock()
        bot.id = BOT_ID
        sent_payloads: list[object] = []

        async def _capture(method: object, request_timeout: int | None = None) -> None:
            sent_payloads.append(method)

        bot.side_effect = _capture

        start_msg = Message(
            message_id=31,
            date=datetime(2026, 11, 24, 12, 11, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=USER_ID, is_bot=False, first_name="Tester"),
            text="/start",
        )
        await dispatcher.feed_update(bot, Update(update_id=11, message=start_msg))

        assert await dispatcher.storage.get_state(key) is None
        assert any("Вітаю!" in getattr(p, "text", "") for p in sent_payloads)


async def test_dispatcher_registers_all_four_routers_in_safe_order() -> None:
    api_client = _make_api_mock()
    dispatcher = create_dispatcher(api_client=api_client, settings=_make_settings())
    router_names = [r.name for r in dispatcher.sub_routers]
    assert router_names == ["start", "training", "vocabulary", "settings"]


async def test_settings_edit_fallback_to_answer_on_other_telegram_bad_request() -> None:
    api_client = _make_api_mock()
    api_client.update_settings.return_value = _make_profile(level="B1")
    cb = _make_callback(
        f"{SETTINGS_LEVEL_CALLBACK_PREFIX}B1",
        edit_side_effect=TelegramBadRequest(
            method=EditMessageText(chat_id=CHAT_ID, message_id=1, text="x"),
            message="Bad Request: message to edit not found",
        ),
    )

    await handle_settings_level_select(callback=cb, api_client=api_client)

    cb.answer.assert_awaited_once()
    cb.message.edit_text.assert_awaited_once()
    cb.message.answer.assert_awaited_once()
    assert "B1" in cb.message.answer.await_args.args[0]


@respx.mock
async def test_settings_custom_timezone_422_end_to_end_with_api_client() -> None:
    route = respx.patch(f"{BASE_URL}/api/users/settings").mock(
        return_value=httpx.Response(
            422,
            json={"detail": [{"msg": "Value error, timezone must be a valid IANA timezone name"}]},
        ),
    )
    _, state = _make_fsm()
    await state.set_state(SettingsStates.waiting_timezone)
    msg = _make_message("Not/A_Timezone")

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        await handle_settings_timezone_text_input(message=msg, state=state, api_client=client)

    assert route.called
    assert await state.get_state() == SettingsStates.waiting_timezone.state
    assert msg.answer.await_args.args[0] == SETTINGS_INVALID_TIMEZONE_UA


@pytest.mark.parametrize("unicode_digit", ["²", "³", "¹", "⁰", "₂", "9" * 5000])
async def test_settings_unicode_superscript_and_huge_digits_rejected_safely(
    unicode_digit: str,
) -> None:
    api_client = _make_api_mock()

    # 1. Custom exercises_per_set text input
    _, state_ex = _make_fsm()
    await state_ex.set_state(SettingsStates.waiting_exercises_per_set)
    ex_msg = _make_message(unicode_digit)
    await handle_settings_exercises_text_input(
        message=ex_msg,
        state=state_ex,
        api_client=api_client,
    )
    assert await state_ex.get_state() == SettingsStates.waiting_exercises_per_set.state
    assert ex_msg.answer.await_args.args[0] == SETTINGS_INVALID_EXERCISES_UA

    # 2. Custom reminder_hour text input
    _, state_rem = _make_fsm()
    await state_rem.set_state(SettingsStates.waiting_reminder_hour)
    rem_msg = _make_message(unicode_digit)
    await handle_settings_reminder_text_input(
        message=rem_msg,
        state=state_rem,
        api_client=api_client,
    )
    assert await state_rem.get_state() == SettingsStates.waiting_reminder_hour.state
    assert rem_msg.answer.await_args.args[0] == SETTINGS_INVALID_REMINDER_UA

    # 3. Malformed callback data must never raise and must still call callback.answer()
    ex_cb = _make_callback(f"{SETTINGS_EXERCISES_CALLBACK_PREFIX}{unicode_digit}")
    await handle_settings_exercises_select(callback=ex_cb, api_client=api_client)
    ex_cb.answer.assert_awaited_once()

    rem_cb = _make_callback(f"{SETTINGS_REMINDER_CALLBACK_PREFIX}{unicode_digit}")
    await handle_settings_reminder_select(callback=rem_cb, api_client=api_client)
    rem_cb.answer.assert_awaited_once()

    api_client.update_settings.assert_not_awaited()


async def test_dispatcher_unicode_superscript_in_waiting_exercises_is_rejected() -> None:
    api_client = _make_api_mock()
    dispatcher = create_dispatcher(api_client=api_client, settings=_make_settings())
    key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
    await dispatcher.storage.set_state(key, SettingsStates.waiting_exercises_per_set)

    bot = MagicMock()
    bot.id = BOT_ID
    sent_payloads: list[object] = []

    async def _capture(method: object, request_timeout: int | None = None) -> None:
        sent_payloads.append(method)

    bot.side_effect = _capture

    msg = Message(
        message_id=40,
        date=datetime(2026, 11, 24, 12, 20, tzinfo=UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="Tester"),
        text="²",
    )
    await dispatcher.feed_update(bot, Update(update_id=20, message=msg))

    api_client.update_settings.assert_not_awaited()
    assert await dispatcher.storage.get_state(key) == SettingsStates.waiting_exercises_per_set.state
    assert len(sent_payloads) == 1
    assert getattr(sent_payloads[0], "text", "") == SETTINGS_INVALID_EXERCISES_UA
