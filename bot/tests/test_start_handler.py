"""Unit tests for BotSettings, keyboards, /start onboarding, and architectural isolation."""

from __future__ import annotations

import ast
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SetChatMenuButton
from aiogram.types import (
    Chat,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
    WebAppInfo,
)

import bot_app.keyboards as keyboards_module
import bot_app.main as main_module
from bot_app.api_client import BackendAPIClient
from bot_app.config import BotSettings, normalize_https_miniapp_url
from bot_app.handlers.settings import SettingsStates
from bot_app.handlers.start import (
    LEVEL_PROMPT_TEXT_UA,
    MINIAPP_OPEN_TEXT_UA,
    MINIAPP_UNAVAILABLE_TEXT_UA,
    UNKNOWN_LEVEL_MESSAGE_UA,
    handle_level_selection,
    handle_miniapp_menu,
    handle_start,
)
from bot_app.handlers.training import TrainingStates
from bot_app.handlers.vocabulary import VocabStates
from bot_app.keyboards import (
    MENU_BTN_MINIAPP,
    MENU_BTN_SETTINGS,
    MENU_BTN_TRAINING,
    MENU_BTN_VOCABULARY,
    MINIAPP_OPEN_BTN,
    build_level_keyboard,
    build_main_menu_keyboard,
    build_miniapp_inline_keyboard,
)
from bot_app.main import configure_menu_button, create_dispatcher, run_polling

BASE_URL = "http://backend.test:8000"
SECRET_API_KEY = "test-internal-api-key-42"
BOT_TOKEN_SECRET = "123456:TEST-BOT-TOKEN-SECRET"
BOT_ID = 777023
USER_ID = 987654321
CHAT_ID = 987654321


def _profile_payload(level: str = "A1") -> dict[str, object]:
    return {
        "telegram_id": USER_ID,
        "level": level,
        "exercises_per_set": 5,
        "theme": "system",
        "sheets_url": None,
        "reminder_hour": 19,
        "timezone": "Europe/Kyiv",
        "daily_llm_calls": 0,
        "created_at": "2026-11-24T12:00:00Z",
    }


def _make_message(user_id: int = USER_ID) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
    )


def _make_callback(data: str, user_id: int = USER_ID) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        data=data,
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock()),
    )


def _flatten_reply_buttons(markup: ReplyKeyboardMarkup) -> list[object]:
    return [button for row in markup.keyboard for button in row]


def test_bot_settings_loads_without_env_file_and_ignores_extra_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POSTGRES_PASSWORD", "ignored-db-password")
    monkeypatch.setenv("LLM_API_KEY", "ignored-llm-key")
    monkeypatch.setenv("BACKEND_API_URL", "http://localhost:8000/")
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST-TOKEN")
    monkeypatch.setenv("INTERNAL_API_KEY", SECRET_API_KEY)
    monkeypatch.setenv("MINIAPP_URL", "https://example.com/app")

    settings = BotSettings()

    assert settings.backend_api_url == "http://localhost:8000"
    assert settings.backend_api_timeout_seconds >= 30.0
    assert settings.https_miniapp_url == "https://example.com/app"
    # Ensure secrets are never exposed in repr()
    rendered = repr(settings)
    assert "123456:TEST-TOKEN" not in rendered
    assert SECRET_API_KEY not in rendered


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("http://localhost:8080", None),
        ("http://example.com", None),
        ("ftp://example.com", None),
        ("https://", None),
        ("https://example.com/my app", None),
        ("https://example.com", "https://example.com"),
        ("HTTPS://app.example.com/miniapp?v=1", "https://app.example.com/miniapp?v=1"),
    ],
)
def test_normalize_https_miniapp_url(raw_url: str | None, expected: str | None) -> None:
    assert normalize_https_miniapp_url(raw_url) == expected


def test_main_menu_never_sets_webapp_and_includes_plain_stats_only_for_https_url() -> None:
    for valid_url in (
        "https://deutsch.example.com/app",
        "  HTTPS://deutsch.example.com/app?v=1  ",
    ):
        with_https = build_main_menu_keyboard(valid_url)
        https_buttons = _flatten_reply_buttons(with_https)
        assert [btn.text for btn in https_buttons] == [
            MENU_BTN_TRAINING,
            MENU_BTN_VOCABULARY,
            MENU_BTN_SETTINGS,
            MENU_BTN_MINIAPP,
        ]
        assert all(btn.web_app is None for btn in https_buttons)

    for unsafe_url in (
        None,
        "",
        "   ",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "ftp://example.com",
        "https://",
        "https://example.com/my app",
    ):
        without_https = build_main_menu_keyboard(unsafe_url)
        buttons = _flatten_reply_buttons(without_https)
        assert [btn.text for btn in buttons] == [
            MENU_BTN_TRAINING,
            MENU_BTN_VOCABULARY,
            MENU_BTN_SETTINGS,
        ]
        assert all(btn.web_app is None for btn in buttons)


def test_build_miniapp_inline_keyboard_returns_inline_webapp_or_none() -> None:
    assert "build_miniapp_inline_keyboard" in keyboards_module.__all__

    valid = build_miniapp_inline_keyboard("  HTTPS://deutsch.example.com/app?v=1  ")
    assert isinstance(valid, InlineKeyboardMarkup)
    assert len(valid.inline_keyboard) == 1
    assert len(valid.inline_keyboard[0]) == 1
    button = valid.inline_keyboard[0][0]
    assert button.text == MINIAPP_OPEN_BTN == "📊 Відкрити статистику"
    assert button.web_app == WebAppInfo(url="https://deutsch.example.com/app?v=1")

    for invalid_url in (
        None,
        "",
        "   ",
        "http://localhost:8080",
        "ftp://example.com/app",
        "https://",
        "https://example.com/with space",
    ):
        assert build_miniapp_inline_keyboard(invalid_url) is None


def test_level_keyboard_contains_a1_a2_b1() -> None:
    keyboard = build_level_keyboard(current_level="A2")
    assert isinstance(keyboard, InlineKeyboardMarkup)
    buttons = keyboard.inline_keyboard[0]
    assert [btn.callback_data for btn in buttons] == ["level:A1", "level:A2", "level:B1"]
    assert buttons[0].text == "A1"
    assert buttons[1].text == "✓ A2"
    assert buttons[2].text == "B1"


@respx.mock
async def test_start_handler_initializes_user_and_sends_menu_with_https_miniapp() -> None:
    route = respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(200, json=_profile_payload("A1")),
    )
    settings = BotSettings(
        _env_file=None,
        bot_token="123:token",
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
        miniapp_url="https://deutsch.example.com",
    )
    message = _make_message()

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        await handle_start(message=message, api_client=client, settings=settings)

    assert route.called
    assert route.calls.last.request.headers["X-Internal-API-Key"] == SECRET_API_KEY
    assert route.calls.last.request.headers["X-Telegram-User-Id"] == str(USER_ID)
    assert message.answer.await_count == 2

    welcome_call = message.answer.await_args_list[0]
    assert "Вітаю!" in welcome_call.args[0]
    assert "A1" in welcome_call.args[0]
    main_menu = welcome_call.kwargs["reply_markup"]
    assert isinstance(main_menu, ReplyKeyboardMarkup)
    buttons = _flatten_reply_buttons(main_menu)
    assert [b.text for b in buttons] == [
        MENU_BTN_TRAINING,
        MENU_BTN_VOCABULARY,
        MENU_BTN_SETTINGS,
        MENU_BTN_MINIAPP,
    ]
    assert all(b.web_app is None for b in buttons)

    level_call = message.answer.await_args_list[1]
    assert level_call.args[0] == LEVEL_PROMPT_TEXT_UA
    level_markup = level_call.kwargs["reply_markup"]
    assert isinstance(level_markup, InlineKeyboardMarkup)
    assert [b.callback_data for b in level_markup.inline_keyboard[0]] == [
        "level:A1",
        "level:A2",
        "level:B1",
    ]


@respx.mock
async def test_start_handler_omits_webapp_when_miniapp_url_is_empty_or_http() -> None:
    respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(200, json=_profile_payload("A2")),
    )
    for url in (None, "", "http://localhost:8080"):
        settings = BotSettings(
            _env_file=None,
            bot_token="123:token",
            internal_api_key=SECRET_API_KEY,
            backend_api_url=BASE_URL,
            miniapp_url=url,
        )
        message = _make_message()
        async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
            await handle_start(message=message, api_client=client, settings=settings)

        main_menu = message.answer.await_args_list[0].kwargs["reply_markup"]
        assert isinstance(main_menu, ReplyKeyboardMarkup)
        buttons = _flatten_reply_buttons(main_menu)
        assert MENU_BTN_MINIAPP not in [b.text for b in buttons]
        assert all(b.web_app is None for b in buttons)


@pytest.mark.parametrize("selected_level", ["A1", "A2", "B1"])
@respx.mock
async def test_level_selection_patches_settings_and_answers_callback(
    selected_level: str,
) -> None:
    route = respx.patch(f"{BASE_URL}/api/users/settings").mock(
        return_value=httpx.Response(200, json=_profile_payload(selected_level)),
    )
    settings = BotSettings(
        _env_file=None,
        bot_token="123:token",
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
        miniapp_url="https://deutsch.example.com",
    )
    callback = _make_callback(f"level:{selected_level}")

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        await handle_level_selection(callback=callback, api_client=client, settings=settings)

    assert route.called
    req = route.calls.last.request
    assert req.headers["X-Internal-API-Key"] == SECRET_API_KEY
    assert req.headers["X-Telegram-User-Id"] == str(USER_ID)
    assert json.loads(req.content) == {"level": selected_level}

    callback.answer.assert_awaited_once()
    assert selected_level in callback.answer.await_args.args[0]
    callback.message.answer.assert_awaited_once()
    assert selected_level in callback.message.answer.await_args.args[0]
    reply_markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert isinstance(reply_markup, ReplyKeyboardMarkup)


@pytest.mark.parametrize(
    "bad_callback",
    ["level:C1", "level:C2", "level:", "level:a1", "level:xyz"],
)
@respx.mock
async def test_unknown_level_callback_rejected_without_api_call(bad_callback: str) -> None:
    patch_route = respx.patch(f"{BASE_URL}/api/users/settings").mock(
        return_value=httpx.Response(200, json=_profile_payload("A1")),
    )
    settings = BotSettings(
        _env_file=None,
        bot_token="123:token",
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
    )
    callback = _make_callback(bad_callback)

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        await handle_level_selection(callback=callback, api_client=client, settings=settings)

    assert not patch_route.called
    callback.answer.assert_awaited_once_with(UNKNOWN_LEVEL_MESSAGE_UA)
    callback.message.answer.assert_not_awaited()


@pytest.mark.parametrize(
    ("mock_response", "expected_fragment"),
    [
        (httpx.Response(429, json={"detail": "Daily limit reached"}), "ліміт"),
        (httpx.Response(500, json={"detail": "Boom"}), "недоступний"),
        (httpx.ReadTimeout("Timed out"), "довго"),
    ],
)
@respx.mock
async def test_start_handler_handles_api_errors_gracefully(
    mock_response: httpx.Response | Exception,
    expected_fragment: str,
) -> None:
    route = respx.get(f"{BASE_URL}/api/users/me")
    if isinstance(mock_response, Exception):
        route.mock(side_effect=mock_response)
    else:
        route.mock(return_value=mock_response)

    settings = BotSettings(
        _env_file=None,
        bot_token="123:token",
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
    )
    message = _make_message()

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        await handle_start(message=message, api_client=client, settings=settings)

    message.answer.assert_awaited_once()
    reply_text = message.answer.await_args.args[0]
    assert expected_fragment in reply_text.lower()
    assert SECRET_API_KEY not in reply_text


@pytest.mark.parametrize(
    ("mock_response", "expected_fragment"),
    [
        (httpx.Response(429, json={"detail": "Daily limit reached"}), "ліміт"),
        (httpx.Response(503, json={"detail": "Service down"}), "недоступний"),
        (httpx.ReadTimeout("Timed out"), "довго"),
    ],
)
@respx.mock
async def test_level_callback_handles_api_errors_and_still_answers_callback(
    mock_response: httpx.Response | Exception,
    expected_fragment: str,
) -> None:
    route = respx.patch(f"{BASE_URL}/api/users/settings")
    if isinstance(mock_response, Exception):
        route.mock(side_effect=mock_response)
    else:
        route.mock(return_value=mock_response)

    settings = BotSettings(
        _env_file=None,
        bot_token="123:token",
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
    )
    callback = _make_callback("level:B1")

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        await handle_level_selection(callback=callback, api_client=client, settings=settings)

    callback.answer.assert_awaited_once()
    callback.message.answer.assert_awaited_once()
    reply_text = callback.message.answer.await_args.args[0]
    assert expected_fragment in reply_text.lower()
    assert SECRET_API_KEY not in reply_text


async def test_dispatcher_wires_dependencies_and_start_router() -> None:
    settings = BotSettings(
        _env_file=None,
        bot_token="123:token",
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
    )
    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        dispatcher = create_dispatcher(api_client=client, settings=settings)
        assert dispatcher["api_client"] is client
        assert dispatcher["settings"] is settings
        assert any(r.name == "start" for r in dispatcher.sub_routers)


async def test_handle_miniapp_menu_sends_inline_webapp_for_valid_https_url() -> None:
    settings = BotSettings(
        _env_file=None,
        bot_token=BOT_TOKEN_SECRET,
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
        miniapp_url="https://deutsch.example.com/app",
    )
    message = _make_message()

    await handle_miniapp_menu(message=message, settings=settings)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == MINIAPP_OPEN_TEXT_UA
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert len(markup.inline_keyboard) == 1
    assert len(markup.inline_keyboard[0]) == 1
    btn = markup.inline_keyboard[0][0]
    assert btn.text == MINIAPP_OPEN_BTN
    assert btn.web_app == WebAppInfo(url="https://deutsch.example.com/app")


async def test_handle_miniapp_menu_ignores_message_without_from_user() -> None:
    storage = MemoryStorage()
    key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(VocabStates.waiting_import)

    settings = BotSettings(
        _env_file=None,
        bot_token=BOT_TOKEN_SECRET,
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
        miniapp_url="https://deutsch.example.com/app",
    )
    message = SimpleNamespace(from_user=None, answer=AsyncMock())

    await handle_miniapp_menu(message=message, settings=settings, state=state)

    message.answer.assert_not_awaited()
    assert await state.get_state() == VocabStates.waiting_import.state


@pytest.mark.parametrize(
    "invalid_url",
    [None, "", "   ", "http://localhost:8080", "ftp://example.com", "https://"],
)
async def test_handle_miniapp_menu_sends_fallback_when_url_missing_or_invalid(
    invalid_url: str | None,
) -> None:
    settings = BotSettings(
        _env_file=None,
        bot_token=BOT_TOKEN_SECRET,
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
        miniapp_url=invalid_url,
    )
    message = _make_message()

    await handle_miniapp_menu(message=message, settings=settings)

    message.answer.assert_awaited_once_with(MINIAPP_UNAVAILABLE_TEXT_UA)
    assert "reply_markup" not in message.answer.await_args.kwargs


@respx.mock
async def test_dispatcher_miniapp_button_preserves_waiting_answer_and_does_not_submit_answer() -> (
    None
):
    answer_route = respx.post(f"{BASE_URL}/api/sessions/42/answer").mock(
        return_value=httpx.Response(500, json={"detail": "Must not be called"}),
    )
    settings = BotSettings(
        _env_file=None,
        bot_token=BOT_TOKEN_SECRET,
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
        miniapp_url="https://deutsch.example.com/app",
    )
    session_data = {
        "session_id": 42,
        "current_exercise_number": 2,
        "total_exercises": 5,
        "attempt_id": 99,
    }

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        dispatcher = create_dispatcher(api_client=client, settings=settings)
        key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
        await dispatcher.storage.set_state(key, TrainingStates.waiting_answer)
        await dispatcher.storage.set_data(key, session_data)

        bot = MagicMock()
        bot.id = BOT_ID
        sent_payloads: list[object] = []

        async def _capture(method: object, request_timeout: int | None = None) -> None:
            sent_payloads.append(method)

        bot.side_effect = _capture

        stats_msg = Message(
            message_id=101,
            date=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=USER_ID, is_bot=False, first_name="Tester"),
            text=MENU_BTN_MINIAPP,
        )
        await dispatcher.feed_update(bot, Update(update_id=101, message=stats_msg))

        assert not answer_route.called
        assert await dispatcher.storage.get_state(key) == TrainingStates.waiting_answer.state
        assert await dispatcher.storage.get_data(key) == session_data
        assert len(sent_payloads) == 1
        sent = sent_payloads[0]
        assert getattr(sent, "text", None) == MINIAPP_OPEN_TEXT_UA
        markup = getattr(sent, "reply_markup", None)
        assert isinstance(markup, InlineKeyboardMarkup)
        assert markup.inline_keyboard[0][0].text == MINIAPP_OPEN_BTN
        assert markup.inline_keyboard[0][0].web_app == WebAppInfo(
            url="https://deutsch.example.com/app",
        )


@respx.mock
async def test_dispatcher_miniapp_fallback_also_preserves_waiting_answer() -> None:
    answer_route = respx.post(f"{BASE_URL}/api/sessions/42/answer").mock(
        return_value=httpx.Response(500, json={"detail": "Must not be called"}),
    )
    settings = BotSettings(
        _env_file=None,
        bot_token=BOT_TOKEN_SECRET,
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
        miniapp_url="http://localhost:8080",
    )

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        dispatcher = create_dispatcher(api_client=client, settings=settings)
        key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
        await dispatcher.storage.set_state(key, TrainingStates.waiting_answer)
        await dispatcher.storage.set_data(key, {"session_id": 42})

        bot = MagicMock()
        bot.id = BOT_ID
        sent_payloads: list[object] = []

        async def _capture(method: object, request_timeout: int | None = None) -> None:
            sent_payloads.append(method)

        bot.side_effect = _capture

        stats_msg = Message(
            message_id=102,
            date=datetime(2026, 11, 24, 12, 1, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=USER_ID, is_bot=False, first_name="Tester"),
            text=MENU_BTN_MINIAPP,
        )
        await dispatcher.feed_update(bot, Update(update_id=102, message=stats_msg))

        assert not answer_route.called
        assert await dispatcher.storage.get_state(key) == TrainingStates.waiting_answer.state
        assert await dispatcher.storage.get_data(key) == {"session_id": 42}
        assert len(sent_payloads) == 1
        assert getattr(sent_payloads[0], "text", None) == MINIAPP_UNAVAILABLE_TEXT_UA
        assert getattr(sent_payloads[0], "reply_markup", None) is None


@pytest.mark.parametrize(
    "initial_state",
    [
        VocabStates.waiting_import,
        VocabStates.waiting_sheets_url,
        SettingsStates.waiting_exercises_per_set,
        SettingsStates.waiting_reminder_hour,
        SettingsStates.waiting_timezone,
        TrainingStates.choosing_topic,
    ],
)
async def test_dispatcher_miniapp_button_clears_other_fsm_states_without_api_calls(
    initial_state: object,
) -> None:
    api_client = AsyncMock(spec=BackendAPIClient)
    settings = BotSettings(
        _env_file=None,
        bot_token=BOT_TOKEN_SECRET,
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
        miniapp_url="https://deutsch.example.com/app",
    )
    dispatcher = create_dispatcher(api_client=api_client, settings=settings)
    key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
    await dispatcher.storage.set_state(key, initial_state)
    await dispatcher.storage.set_data(key, {"draft": "pending"})

    bot = MagicMock()
    bot.id = BOT_ID
    sent_payloads: list[object] = []

    async def _capture(method: object, request_timeout: int | None = None) -> None:
        sent_payloads.append(method)

    bot.side_effect = _capture

    stats_msg = Message(
        message_id=103,
        date=datetime(2026, 11, 24, 12, 2, tzinfo=UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="Tester"),
        text=MENU_BTN_MINIAPP,
    )
    await dispatcher.feed_update(bot, Update(update_id=103, message=stats_msg))

    assert await dispatcher.storage.get_state(key) is None
    assert await dispatcher.storage.get_data(key) == {}
    api_client.import_words_text.assert_not_awaited()
    api_client.update_settings.assert_not_awaited()
    api_client.start_session.assert_not_awaited()
    api_client.submit_answer.assert_not_awaited()
    assert len(sent_payloads) == 1
    sent = sent_payloads[0]
    assert getattr(sent, "text", None) == MINIAPP_OPEN_TEXT_UA
    markup = getattr(sent, "reply_markup", None)
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[0][0].text == MINIAPP_OPEN_BTN
    assert markup.inline_keyboard[0][0].web_app == WebAppInfo(
        url="https://deutsch.example.com/app",
    )


async def test_configure_menu_button_sets_webapp_for_valid_https_url() -> None:
    bot = AsyncMock()

    await configure_menu_button(bot, "  HTTPS://deutsch.example.com/app  ")

    bot.set_chat_menu_button.assert_awaited_once_with(
        menu_button=MenuButtonWebApp(
            text=MENU_BTN_MINIAPP,
            web_app=WebAppInfo(url="https://deutsch.example.com/app"),
        ),
    )


@pytest.mark.parametrize(
    "invalid_url",
    [None, "", "   ", "http://localhost:8080", "ftp://example.com", "https://"],
)
async def test_configure_menu_button_resets_to_default_for_missing_or_invalid_url(
    invalid_url: str | None,
) -> None:
    bot = AsyncMock()

    await configure_menu_button(bot, invalid_url)

    bot.set_chat_menu_button.assert_awaited_once_with(
        menu_button=MenuButtonDefault(),
    )


async def test_configure_menu_button_handles_telegram_api_error_without_leaking_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = AsyncMock()
    bot.token = BOT_TOKEN_SECRET
    bot.set_chat_menu_button.side_effect = TelegramAPIError(
        method=SetChatMenuButton(),
        message=f"Bad Request: failed for token {BOT_TOKEN_SECRET}",
    )

    with caplog.at_level(logging.WARNING, logger=main_module.__name__):
        await configure_menu_button(bot, "https://deutsch.example.com/app")

    bot.set_chat_menu_button.assert_awaited_once()
    warning_messages = [
        rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING
    ]
    assert len(warning_messages) == 1
    assert BOT_TOKEN_SECRET not in warning_messages[0]
    assert SECRET_API_KEY not in warning_messages[0]


async def test_run_polling_configures_menu_button_before_polling_and_survives_api_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []

    fake_bot = MagicMock()
    fake_bot.token = BOT_TOKEN_SECRET
    fake_bot.session = MagicMock()
    fake_bot.session.close = AsyncMock()

    async def _set_menu_button(*, menu_button: object) -> None:
        events.append("set_chat_menu_button")
        assert menu_button == MenuButtonWebApp(
            text=MENU_BTN_MINIAPP,
            web_app=WebAppInfo(url="https://deutsch.example.com/app"),
        )
        raise TelegramAPIError(
            method=SetChatMenuButton(),
            message="Bad Request: temporary Telegram error",
        )

    fake_bot.set_chat_menu_button = AsyncMock(side_effect=_set_menu_button)

    fake_dispatcher = MagicMock()

    async def _start_polling(bot: object) -> None:
        assert bot is fake_bot
        events.append("start_polling")

    fake_dispatcher.start_polling = AsyncMock(side_effect=_start_polling)

    monkeypatch.setattr(main_module, "create_bot", lambda _settings: fake_bot)
    monkeypatch.setattr(
        main_module,
        "create_dispatcher",
        lambda *, api_client, settings: fake_dispatcher,
    )

    settings = BotSettings(
        _env_file=None,
        bot_token=BOT_TOKEN_SECRET,
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
        miniapp_url="https://deutsch.example.com/app",
    )

    with caplog.at_level(logging.WARNING, logger=main_module.__name__):
        await run_polling(settings=settings)

    assert events == ["set_chat_menu_button", "start_polling"]
    fake_dispatcher.start_polling.assert_awaited_once_with(fake_bot)
    fake_bot.session.close.assert_awaited_once()
    combined_logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "temporary Telegram error" in combined_logs
    assert BOT_TOKEN_SECRET not in combined_logs
    assert SECRET_API_KEY not in combined_logs


def test_bot_has_zero_python_imports_from_backend() -> None:
    """Enforce strict architectural decoupling: zero imports from backend/ or app.*."""
    bot_root = Path(__file__).resolve().parents[1]
    forbidden_roots = {"backend", "app"}
    violations: list[str] = []

    for py_file in sorted(bot_root.rglob("*.py")):
        if ".venv" in py_file.parts:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    if top in forbidden_roots:
                        violations.append(f"{py_file}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".", 1)[0]
                if top in forbidden_roots:
                    violations.append(f"{py_file}: from {node.module} import ...")

    assert violations == []
