"""Unit tests for BotSettings, keyboards, /start onboarding, and architectural isolation."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from bot_app.api_client import BackendAPIClient
from bot_app.config import BotSettings, normalize_https_miniapp_url
from bot_app.handlers.start import (
    LEVEL_PROMPT_TEXT_UA,
    UNKNOWN_LEVEL_MESSAGE_UA,
    handle_level_selection,
    handle_start,
)
from bot_app.keyboards import (
    MENU_BTN_MINIAPP,
    MENU_BTN_SETTINGS,
    MENU_BTN_TRAINING,
    MENU_BTN_VOCABULARY,
    build_level_keyboard,
    build_main_menu_keyboard,
)
from bot_app.main import create_dispatcher

BASE_URL = "http://backend.test:8000"
SECRET_API_KEY = "test-internal-api-key-42"
USER_ID = 987654321


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


def test_main_menu_includes_webapp_only_for_https_url() -> None:
    with_https = build_main_menu_keyboard("https://deutsch.example.com/app")
    https_buttons = _flatten_reply_buttons(with_https)
    assert [btn.text for btn in https_buttons] == [
        MENU_BTN_TRAINING,
        MENU_BTN_VOCABULARY,
        MENU_BTN_SETTINGS,
        MENU_BTN_MINIAPP,
    ]
    miniapp_btn = https_buttons[-1]
    assert miniapp_btn.web_app is not None
    assert miniapp_btn.web_app.url == "https://deutsch.example.com/app"

    for unsafe_url in (None, "", "   ", "http://127.0.0.1:8080", "http://localhost:5173"):
        without_https = build_main_menu_keyboard(unsafe_url)
        buttons = _flatten_reply_buttons(without_https)
        assert [btn.text for btn in buttons] == [
            MENU_BTN_TRAINING,
            MENU_BTN_VOCABULARY,
            MENU_BTN_SETTINGS,
        ]
        assert all(btn.web_app is None for btn in buttons)


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
    miniapp_buttons = [b for b in _flatten_reply_buttons(main_menu) if b.web_app is not None]
    assert len(miniapp_buttons) == 1
    assert miniapp_buttons[0].web_app.url == "https://deutsch.example.com"

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
        assert all(b.web_app is None for b in _flatten_reply_buttons(main_menu))


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
