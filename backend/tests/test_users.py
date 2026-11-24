"""Unit and PostgreSQL integration tests for user profile and settings endpoints."""

import asyncio
import hashlib
import hmac
import json
import urllib.request
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from app.api.deps import get_utc_now
from app.config import Settings
from app.core.auth import TELEGRAM_WEBAPP_HMAC_KEY
from app.db.base import create_async_db_engine, create_session_factory, get_db
from app.db.models import Deck, User
from app.db.users import DEFAULT_DECK_LEVEL, DEFAULT_DECK_TITLE, ensure_user_initialized
from app.main import create_app
from app.schemas.users import (
    UserSettingsUpdate,
    validate_iana_timezone,
    validate_sheets_url,
)

TEST_BOT_TOKEN = "123456789:AAH_users_test_bot_token_secret_XYZ"
TEST_INTERNAL_API_KEY = "users-test-internal-api-key-987654"
FIXED_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
FIXED_NOW_TS = int(FIXED_NOW.timestamp())

USER_A_ID = 5_100_000_001  # > 2^31 - 1 (64-bit BIGINT)
USER_B_ID = 5_100_000_002
USER_CONCURRENT_GET_ID = 5_900_000_101
USER_CONCURRENT_MIX_ID = 5_900_000_102
USER_CONCURRENT_DECK_ID = 5_900_000_103

EXPECTED_PROFILE_KEYS = frozenset(
    {
        "telegram_id",
        "level",
        "exercises_per_set",
        "theme",
        "sheets_url",
        "reminder_hour",
        "timezone",
        "daily_llm_calls",
        "created_at",
    },
)

VALID_SHEETS_URLS = (
    "https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit#gid=0",
    "https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit?usp=sharing",
    "https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/export?format=csv&gid=0",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vTestPublishedSheet123/pub?output=csv",
    "https://docs.google.com/spreadsheets/u/0/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit",
    "https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
)

INVALID_SHEETS_URLS = (
    "http://docs.google.com/spreadsheets/d/12345/edit",
    "javascript:alert(1)",
    "ftp://docs.google.com/spreadsheets/d/12345/edit",
    "file:///etc/passwd",
    "data:text/csv,word,translation",
    "//docs.google.com/spreadsheets/d/12345/edit",
    "not-a-url",
    "https://evil.com/spreadsheets/d/12345/edit",
    "https://docs.google.com.evil.com/spreadsheets/d/12345/edit",
    "https://sub.docs.google.com/spreadsheets/d/12345/edit",
    "https://127.0.0.1/spreadsheets/d/12345/edit",
    "https://localhost/spreadsheets/d/12345/edit",
    "https://evil.com@docs.google.com/spreadsheets/d/12345/edit",
    "https://docs.google.com@evil.com/spreadsheets/d/12345/edit",
    "https://docs.google.com:8443/spreadsheets/d/12345/edit",
    "https://docs.google.com:notaport/spreadsheets/d/12345/edit",
    "https://docs.google.com/document/d/12345/edit",
    "https://docs.google.com/spreadsheets",
    "https://docs.google.com/spreadsheets/",
    "https://docs.google.com/spreadsheets///",
    "https://docs.google.com/spreadsheets/../document/d/12345/edit",
    "https://docs.google.com/spreadsheets/%2e%2e/document/d/12345/edit",
    "https://docs.google.com/spreadsheets/d/12345/./edit",
    "https://docs.google.com/spreadsheets/d/12345%2f..%2fsecret",
    "https://docs.google.com\\@evil.com/spreadsheets/d/12345/edit",
    "https://docs.google.com/spreadsheets/d/12345/edit\nX-Test: 1",
    "https://docs.google.com/spreadsheets/d/123 45/edit",
    f"https://docs.google.com/spreadsheets/d/{'a' * 2100}/edit",
)


def build_tma_headers(
    telegram_id: int,
    *,
    bot_token: str = TEST_BOT_TOKEN,
    auth_date: int = FIXED_NOW_TS,
    hash_override: str | None = None,
) -> dict[str, str]:
    """Build an ``Authorization: tma <initData>`` header signed with HMAC-SHA256."""
    fields: dict[str, str] = {
        "user": json.dumps(
            {"id": telegram_id, "first_name": "Олена", "language_code": "uk"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "auth_date": str(auth_date),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(
        TELEGRAM_WEBAPP_HMAC_KEY,
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    digest = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    pairs: list[tuple[str, str]] = [
        *fields.items(),
        ("hash", hash_override if hash_override is not None else digest),
    ]
    return {"Authorization": f"tma {urlencode(pairs, encoding='utf-8')}"}


def build_bot_headers(
    telegram_id: int | str,
    *,
    api_key: str = TEST_INTERNAL_API_KEY,
) -> dict[str, str]:
    """Build internal bot authentication headers."""
    return {
        "X-Internal-API-Key": api_key,
        "X-Telegram-User-Id": str(telegram_id),
    }


def _assert_profile_shape(data: Mapping[str, Any], *, expected_telegram_id: int) -> None:
    assert set(data.keys()) == EXPECTED_PROFILE_KEYS
    assert data["telegram_id"] == expected_telegram_id
    parsed_created_at = datetime.fromisoformat(data["created_at"])
    assert parsed_created_at.tzinfo is not None


@pytest.fixture
async def users_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """ASGI AsyncClient wired to the isolated rolled-back PostgreSQL test session."""
    test_app = create_app(
        settings=Settings(
            _env_file=None,
            app_env="test",
            database_url=None,
            bot_token=TEST_BOT_TOKEN,
            internal_api_key=TEST_INTERNAL_API_KEY,
        ),
    )

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_utc_now] = lambda: FIXED_NOW

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# Offline unit tests: schemas, validators, routing, and unauthenticated 401
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", VALID_SHEETS_URLS)
def test_validate_sheets_url_accepts_valid_google_sheets_https_urls(url: str) -> None:
    assert validate_sheets_url(url) == url


def test_validate_sheets_url_normalizes_whitespace_and_case_and_clears_empty() -> None:
    assert validate_sheets_url(None) is None
    assert validate_sheets_url("") is None
    assert validate_sheets_url("   ") is None
    raw_url = "  HTTPS://DOCS.GOOGLE.COM:443/spreadsheets/d/abc123/edit?usp=sharing  "
    assert (
        validate_sheets_url(raw_url)
        == "https://docs.google.com/spreadsheets/d/abc123/edit?usp=sharing"
    )


@pytest.mark.parametrize("bad_url", [*INVALID_SHEETS_URLS, 123, True, ["https://docs.google.com"]])
def test_validate_sheets_url_rejects_invalid_urls_and_types(bad_url: Any) -> None:
    with pytest.raises(ValueError):
        validate_sheets_url(bad_url)


@pytest.mark.parametrize(
    "tz_name",
    ["Europe/Kyiv", "Europe/Berlin", "UTC", "Etc/UTC", "America/New_York", "Asia/Tokyo"],
)
def test_validate_iana_timezone_accepts_valid_zones(tz_name: str) -> None:
    assert validate_iana_timezone(tz_name) == tz_name


@pytest.mark.parametrize(
    "bad_tz",
    [
        "Mars/Olympus",
        "",
        "   ",
        " Europe/Kyiv ",
        "UTC+2",
        "Kyiv",
        "../UTC",
        "/etc/passwd",
        "posix/UTC",
        "localtime",
        "A" * 65,
        123,
        None,
    ],
)
def test_validate_iana_timezone_rejects_invalid_zones(bad_tz: Any) -> None:
    with pytest.raises(ValueError):
        validate_iana_timezone(bad_tz)


def test_user_settings_update_tracks_model_fields_set_and_forbids_extras() -> None:
    empty = UserSettingsUpdate.model_validate({})
    assert empty.model_fields_set == set()

    cleared = UserSettingsUpdate.model_validate({"sheets_url": ""})
    assert cleared.model_fields_set == {"sheets_url"}
    assert cleared.sheets_url is None

    with pytest.raises(ValidationError):
        UserSettingsUpdate.model_validate({"telegram_id": 123})

    with pytest.raises(ValidationError):
        UserSettingsUpdate.model_validate({"daily_llm_calls": 0})


async def test_openapi_schema_mounts_users_routes_under_api_prefix(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/users/me" in paths
    assert "get" in paths["/api/users/me"]
    assert "/api/users/settings" in paths
    assert "patch" in paths["/api/users/settings"]
    assert "/users/me" not in paths
    assert "/users/settings" not in paths


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/users/me", None),
        ("PATCH", "/api/users/settings", {}),
        ("PATCH", "/api/users/settings", {"level": "B1"}),
        ("PATCH", "/api/users/settings", {"level": "C1"}),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer some-token"},
        build_tma_headers(USER_A_ID, hash_override="0" * 64),
        build_tma_headers(USER_A_ID, auth_date=FIXED_NOW_TS - 86_401),
        build_bot_headers(USER_A_ID, api_key="wrong-key"),
        {"X-Internal-API-Key": TEST_INTERNAL_API_KEY},
        {"X-Telegram-User-Id": str(USER_A_ID)},
    ],
)
async def test_users_endpoints_return_401_on_missing_or_invalid_auth(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    headers: dict[str, str],
) -> None:
    test_app = create_app(
        settings=Settings(
            _env_file=None,
            app_env="test",
            database_url=None,
            bot_token=TEST_BOT_TOKEN,
            internal_api_key=TEST_INTERNAL_API_KEY,
        ),
    )
    test_app.dependency_overrides[get_utc_now] = lambda: FIXED_NOW

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.request(method, path, headers=headers, json=body)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing authentication credentials"}
    assert TEST_BOT_TOKEN not in response.text
    assert TEST_INTERNAL_API_KEY not in response.text


# ---------------------------------------------------------------------------
# PostgreSQL integration tests (isolated TEST_DATABASE_URL)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("auth_mode", ["tma", "bot"])
async def test_get_users_me_auto_initializes_profile_and_default_deck_idempotently(
    users_client: AsyncClient,
    db_session: AsyncSession,
    auth_mode: str,
) -> None:
    telegram_id = USER_A_ID if auth_mode == "tma" else USER_B_ID
    headers = (
        build_tma_headers(telegram_id)
        if auth_mode == "tma"
        else build_bot_headers(telegram_id)
    )

    first_response = await users_client.get("/api/users/me", headers=headers)
    assert first_response.status_code == 200
    first_data = first_response.json()
    _assert_profile_shape(first_data, expected_telegram_id=telegram_id)
    assert first_data["level"] == "A1"
    assert first_data["exercises_per_set"] == 5
    assert first_data["theme"] == "system"
    assert first_data["sheets_url"] is None
    assert first_data["reminder_hour"] == 19
    assert first_data["timezone"] == "Europe/Kyiv"
    assert first_data["daily_llm_calls"] == 0

    second_response = await users_client.get("/api/users/me", headers=headers)
    assert second_response.status_code == 200
    assert second_response.json() == first_data

    db_users = (
        await db_session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalars().all()
    assert len(db_users) == 1

    db_decks = (
        await db_session.execute(select(Deck).where(Deck.owner_id == telegram_id))
    ).scalars().all()
    assert len(db_decks) == 1
    assert db_decks[0].title == DEFAULT_DECK_TITLE
    assert db_decks[0].level == DEFAULT_DECK_LEVEL


async def test_get_users_me_creates_personal_deck_even_when_shared_deck_has_same_title(
    users_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    db_session.add(Deck(owner_id=None, title=DEFAULT_DECK_TITLE, level="A1"))
    await db_session.commit()

    response = await users_client.get("/api/users/me", headers=build_bot_headers(USER_A_ID))
    assert response.status_code == 200

    personal_decks = (
        await db_session.execute(select(Deck).where(Deck.owner_id == USER_A_ID))
    ).scalars().all()
    assert len(personal_decks) == 1
    assert personal_decks[0].title == DEFAULT_DECK_TITLE
    assert personal_decks[0].level == DEFAULT_DECK_LEVEL


@pytest.mark.parametrize("auth_mode", ["tma", "bot"])
async def test_patch_settings_before_first_get_initializes_user_and_default_deck(
    users_client: AsyncClient,
    db_session: AsyncSession,
    auth_mode: str,
) -> None:
    telegram_id = USER_A_ID if auth_mode == "tma" else USER_B_ID
    headers = (
        build_tma_headers(telegram_id)
        if auth_mode == "tma"
        else build_bot_headers(telegram_id)
    )
    sheet_url = VALID_SHEETS_URLS[0]

    patch_response = await users_client.patch(
        "/api/users/settings",
        headers=headers,
        json={
            "level": "B1",
            "exercises_per_set": 10,
            "theme": "dark",
            "sheets_url": sheet_url,
            "reminder_hour": 8,
            "timezone": "Europe/Berlin",
        },
    )
    assert patch_response.status_code == 200
    patched = patch_response.json()
    _assert_profile_shape(patched, expected_telegram_id=telegram_id)
    assert patched["level"] == "B1"
    assert patched["exercises_per_set"] == 10
    assert patched["theme"] == "dark"
    assert patched["sheets_url"] == sheet_url
    assert patched["reminder_hour"] == 8
    assert patched["timezone"] == "Europe/Berlin"
    assert patched["daily_llm_calls"] == 0

    me_response = await users_client.get("/api/users/me", headers=headers)
    assert me_response.status_code == 200
    assert me_response.json() == patched

    db_decks = (
        await db_session.execute(select(Deck).where(Deck.owner_id == telegram_id))
    ).scalars().all()
    assert len(db_decks) == 1
    assert db_decks[0].title == DEFAULT_DECK_TITLE
    assert db_decks[0].level == DEFAULT_DECK_LEVEL


async def test_patch_settings_empty_body_on_new_user_initializes_defaults(
    users_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    response = await users_client.patch(
        "/api/users/settings",
        headers=build_bot_headers(USER_A_ID),
        json={},
    )
    assert response.status_code == 200
    data = response.json()
    _assert_profile_shape(data, expected_telegram_id=USER_A_ID)
    assert data["level"] == "A1"
    assert data["exercises_per_set"] == 5
    assert data["theme"] == "system"
    assert data["sheets_url"] is None
    assert data["reminder_hour"] == 19
    assert data["timezone"] == "Europe/Kyiv"

    db_decks = (
        await db_session.execute(select(Deck).where(Deck.owner_id == USER_A_ID))
    ).scalars().all()
    assert len(db_decks) == 1
    assert db_decks[0].title == DEFAULT_DECK_TITLE


async def test_patch_settings_partial_updates_and_empty_noop_preserve_other_fields(
    users_client: AsyncClient,
) -> None:
    headers = build_tma_headers(USER_A_ID)
    sheet_url = VALID_SHEETS_URLS[1]

    # Set initial custom values across all fields
    setup_response = await users_client.patch(
        "/api/users/settings",
        headers=headers,
        json={
            "level": "A2",
            "exercises_per_set": 12,
            "theme": "light",
            "sheets_url": sheet_url,
            "reminder_hour": 21,
            "timezone": "UTC",
        },
    )
    assert setup_response.status_code == 200
    baseline = setup_response.json()

    # Empty JSON body {} is a strict no-op
    noop_response = await users_client.patch("/api/users/settings", headers=headers, json={})
    assert noop_response.status_code == 200
    assert noop_response.json() == baseline

    # Updating only `theme` leaves every other field (including `sheets_url`) untouched
    theme_response = await users_client.patch(
        "/api/users/settings",
        headers=headers,
        json={"theme": "dark"},
    )
    assert theme_response.status_code == 200
    after_theme = theme_response.json()
    assert after_theme == {**baseline, "theme": "dark"}

    # Explicitly resetting a field back to schema default (`A1`) still applies
    reset_level_response = await users_client.patch(
        "/api/users/settings",
        headers=headers,
        json={"level": "A1"},
    )
    assert reset_level_response.status_code == 200
    after_reset = reset_level_response.json()
    assert after_reset == {**after_theme, "level": "A1"}

    # Persisted state matches via GET /api/users/me
    me_response = await users_client.get("/api/users/me", headers=headers)
    assert me_response.status_code == 200
    assert me_response.json() == after_reset


@pytest.mark.parametrize(
    "boundary_payload",
    [
        {"level": "A1", "exercises_per_set": 1, "theme": "system", "reminder_hour": 0},
        {"level": "A2", "exercises_per_set": 15, "theme": "dark", "reminder_hour": 12},
        {"level": "B1", "exercises_per_set": 30, "theme": "light", "reminder_hour": 23},
        {"timezone": "Europe/Kyiv"},
        {"timezone": "Europe/Berlin"},
        {"timezone": "UTC"},
    ],
)
async def test_patch_settings_accepts_valid_boundary_values(
    users_client: AsyncClient,
    boundary_payload: dict[str, Any],
) -> None:
    headers = build_bot_headers(USER_A_ID)

    response = await users_client.patch(
        "/api/users/settings",
        headers=headers,
        json=boundary_payload,
    )

    assert response.status_code == 200
    body = response.json()
    for key, value in boundary_payload.items():
        assert body[key] == value


async def test_patch_settings_sheets_url_set_preserve_clear_and_no_external_http(
    users_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External network requests must not be executed")

    async def fail_async_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External async HTTP requests must not be executed")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fail_async_network)
    monkeypatch.setattr(urllib.request, "urlopen", fail_network)

    headers = build_tma_headers(USER_A_ID)
    url_1 = VALID_SHEETS_URLS[0]
    url_2 = VALID_SHEETS_URLS[3]

    # Set valid Google Sheets URL
    set_resp = await users_client.patch(
        "/api/users/settings",
        headers=headers,
        json={"sheets_url": url_1},
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["sheets_url"] == url_1

    # Omitting `sheets_url` preserves existing value
    preserve_resp = await users_client.patch(
        "/api/users/settings",
        headers=headers,
        json={"reminder_hour": 18},
    )
    assert preserve_resp.status_code == 200
    assert preserve_resp.json()["sheets_url"] == url_1
    assert preserve_resp.json()["reminder_hour"] == 18

    # Clearing via explicit null sets DB value to NULL
    clear_null_resp = await users_client.patch(
        "/api/users/settings",
        headers=headers,
        json={"sheets_url": None},
    )
    assert clear_null_resp.status_code == 200
    assert clear_null_resp.json()["sheets_url"] is None
    user_after_null = await db_session.get(User, USER_A_ID)
    assert user_after_null is not None
    assert user_after_null.sheets_url is None

    # Set another valid URL, then clear via empty string ""
    set_resp_2 = await users_client.patch(
        "/api/users/settings",
        headers=headers,
        json={"sheets_url": url_2},
    )
    assert set_resp_2.status_code == 200
    assert set_resp_2.json()["sheets_url"] == url_2

    clear_empty_resp = await users_client.patch(
        "/api/users/settings",
        headers=headers,
        json={"sheets_url": ""},
    )
    assert clear_empty_resp.status_code == 200
    assert clear_empty_resp.json()["sheets_url"] is None
    user_after_empty = await db_session.get(User, USER_A_ID)
    assert user_after_empty is not None
    assert user_after_empty.sheets_url is None


@pytest.mark.parametrize("bad_url", INVALID_SHEETS_URLS)
async def test_patch_settings_rejects_invalid_sheets_url_with_422_without_network(
    users_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    bad_url: str,
) -> None:
    def fail_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External network requests must not be executed")

    async def fail_async_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External async HTTP requests must not be executed")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fail_async_network)
    monkeypatch.setattr(urllib.request, "urlopen", fail_network)

    response = await users_client.patch(
        "/api/users/settings",
        headers=build_bot_headers(USER_A_ID),
        json={"sheets_url": bad_url},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "invalid_payload",
    [
        # Invalid field values
        {"level": "C1"},
        {"level": "a1"},
        {"level": ""},
        {"exercises_per_set": 0},
        {"exercises_per_set": -5},
        {"exercises_per_set": 31},
        {"exercises_per_set": True},
        {"exercises_per_set": "5"},
        {"exercises_per_set": 5.0},
        {"theme": "auto"},
        {"theme": "DARK"},
        {"theme": ""},
        {"reminder_hour": -1},
        {"reminder_hour": 24},
        {"reminder_hour": False},
        {"reminder_hour": "19"},
        {"reminder_hour": 19.0},
        {"timezone": "Mars/Olympus"},
        {"timezone": ""},
        {"timezone": "   "},
        {"timezone": "UTC+2"},
        {"timezone": " Europe/Kyiv "},
        {"timezone": "../UTC"},
        {"timezone": 123},
        # Explicit null for non-nullable fields
        {"level": None},
        {"exercises_per_set": None},
        {"theme": None},
        {"reminder_hour": None},
        {"timezone": None},
        # Protected / service / unknown fields (extra="forbid")
        {"telegram_id": USER_B_ID},
        {"daily_llm_calls": 0},
        {"daily_llm_calls": 999},
        {"llm_calls_reset_date": "2026-05-01"},
        {"created_at": "2026-05-01T00:00:00Z"},
        {"unknown_field": "value"},
        # Mix of valid + forbidden field must reject entire payload
        {"level": "B1", "daily_llm_calls": 0},
        {"level": "B1", "telegram_id": USER_B_ID},
    ],
)
async def test_patch_settings_rejects_invalid_null_and_protected_fields_with_422(
    users_client: AsyncClient,
    invalid_payload: dict[str, Any],
) -> None:
    headers = build_bot_headers(USER_A_ID)

    # Ensure user exists with default values first
    before = (await users_client.get("/api/users/me", headers=headers)).json()

    response = await users_client.patch(
        "/api/users/settings",
        headers=headers,
        json=invalid_payload,
    )
    assert response.status_code == 422

    # Profile must remain completely unchanged
    after = (await users_client.get("/api/users/me", headers=headers)).json()
    assert after == before


async def test_strict_user_id_isolation_between_two_users(
    users_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    headers_a = build_tma_headers(USER_A_ID)
    headers_b = build_bot_headers(USER_B_ID)

    # Initialize both users
    profile_a_initial = (await users_client.get("/api/users/me", headers=headers_a)).json()
    profile_b_initial = (await users_client.get("/api/users/me", headers=headers_b)).json()
    assert profile_a_initial["telegram_id"] == USER_A_ID
    assert profile_b_initial["telegram_id"] == USER_B_ID

    # Update every mutable setting for user A
    updated_a = (
        await users_client.patch(
            "/api/users/settings",
            headers=headers_a,
            json={
                "level": "B1",
                "exercises_per_set": 20,
                "theme": "dark",
                "sheets_url": VALID_SHEETS_URLS[0],
                "reminder_hour": 7,
                "timezone": "Europe/Berlin",
            },
        )
    ).json()

    # User B profile must remain completely unaffected
    profile_b_after = (await users_client.get("/api/users/me", headers=headers_b)).json()
    assert profile_b_after == profile_b_initial

    # User A cannot target user B via telegram_id in PATCH payload
    forged_patch = await users_client.patch(
        "/api/users/settings",
        headers=headers_a,
        json={"telegram_id": USER_B_ID, "level": "A1"},
    )
    assert forged_patch.status_code == 422

    # Re-verify both profiles and separate personal decks in PostgreSQL
    assert (await users_client.get("/api/users/me", headers=headers_a)).json() == updated_a
    assert (await users_client.get("/api/users/me", headers=headers_b)).json() == profile_b_initial

    decks_a = (
        await db_session.execute(select(Deck).where(Deck.owner_id == USER_A_ID))
    ).scalars().all()
    decks_b = (
        await db_session.execute(select(Deck).where(Deck.owner_id == USER_B_ID))
    ).scalars().all()
    assert len(decks_a) == 1
    assert len(decks_b) == 1
    assert decks_a[0].id != decks_b[0].id
    assert decks_a[0].title == DEFAULT_DECK_TITLE
    assert decks_b[0].title == DEFAULT_DECK_TITLE


async def test_concurrent_first_access_requests_create_single_user_and_single_deck(
    migrated_test_db_url: str,
) -> None:
    """Verify real multi-connection PostgreSQL concurrency does not duplicate users or decks."""
    engine = create_async_db_engine(migrated_test_db_url, poolclass=NullPool)
    session_factory = create_session_factory(engine)
    test_ids = (USER_CONCURRENT_GET_ID, USER_CONCURRENT_MIX_ID, USER_CONCURRENT_DECK_ID)

    test_app = create_app(
        settings=Settings(
            _env_file=None,
            app_env="test",
            database_url=migrated_test_db_url,
            bot_token=TEST_BOT_TOKEN,
            internal_api_key=TEST_INTERNAL_API_KEY,
        ),
    )

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_utc_now] = lambda: FIXED_NOW

    try:
        # Clean slate for dedicated concurrency test IDs
        async with session_factory() as cleanup_session:
            await cleanup_session.execute(delete(User).where(User.telegram_id.in_(test_ids)))
            await cleanup_session.commit()

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
            # 1. Six concurrent GET /api/users/me for the same brand-new user
            get_responses = await asyncio.gather(
                *[
                    async_client.get(
                        "/api/users/me",
                        headers=build_bot_headers(USER_CONCURRENT_GET_ID),
                    )
                    for _ in range(6)
                ],
            )
            for resp in get_responses:
                assert resp.status_code == 200
                _assert_profile_shape(resp.json(), expected_telegram_id=USER_CONCURRENT_GET_ID)

            # 2. Concurrent GET + PATCH for the same brand-new user
            mix_responses = await asyncio.gather(
                async_client.get(
                    "/api/users/me",
                    headers=build_tma_headers(USER_CONCURRENT_MIX_ID),
                ),
                async_client.patch(
                    "/api/users/settings",
                    headers=build_bot_headers(USER_CONCURRENT_MIX_ID),
                    json={"level": "B1", "theme": "dark"},
                ),
                async_client.get(
                    "/api/users/me",
                    headers=build_bot_headers(USER_CONCURRENT_MIX_ID),
                ),
            )
            for resp in mix_responses:
                assert resp.status_code == 200

            # 3. Pre-existing user row without deck hit by concurrent GET requests
            async with session_factory() as seed_session:
                seed_session.add(User(telegram_id=USER_CONCURRENT_DECK_ID))
                await seed_session.commit()

            deck_responses = await asyncio.gather(
                *[
                    async_client.get(
                        "/api/users/me",
                        headers=build_bot_headers(USER_CONCURRENT_DECK_ID),
                    )
                    for _ in range(5)
                ],
            )
            for resp in deck_responses:
                assert resp.status_code == 200

        async with session_factory() as verify_session:
            for tid in test_ids:
                users_rows = (
                    await verify_session.execute(select(User).where(User.telegram_id == tid))
                ).scalars().all()
                decks_rows = (
                    await verify_session.execute(select(Deck).where(Deck.owner_id == tid))
                ).scalars().all()
                assert len(users_rows) == 1
                assert len(decks_rows) == 1
                assert decks_rows[0].title == DEFAULT_DECK_TITLE
                assert decks_rows[0].level == DEFAULT_DECK_LEVEL

            mix_user = await verify_session.get(User, USER_CONCURRENT_MIX_ID)
            assert mix_user is not None
            assert mix_user.level == "B1"
            assert mix_user.theme == "dark"
    finally:
        async with session_factory() as cleanup_session:
            await cleanup_session.execute(delete(User).where(User.telegram_id.in_(test_ids)))
            await cleanup_session.commit()
        await engine.dispose()


async def test_patch_settings_preserves_service_fields_and_rejects_non_object_json(
    users_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    reset_date = date(2026, 5, 1)
    db_session.add(
        User(
            telegram_id=USER_A_ID,
            daily_llm_calls=7,
            llm_calls_reset_date=reset_date,
        ),
    )
    db_session.add(
        Deck(owner_id=USER_A_ID, title=DEFAULT_DECK_TITLE, level=DEFAULT_DECK_LEVEL),
    )
    await db_session.commit()

    headers = build_bot_headers(USER_A_ID)
    patch_response = await users_client.patch(
        "/api/users/settings",
        headers=headers,
        json={"level": "B1"},
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["daily_llm_calls"] == 7
    assert "llm_calls_reset_date" not in patch_response.json()

    db_user = await db_session.get(User, USER_A_ID)
    assert db_user is not None
    assert db_user.daily_llm_calls == 7
    assert db_user.llm_calls_reset_date == reset_date

    for raw_body in (b"[]", b"null", b'"string"'):
        bad_resp = await users_client.patch(
            "/api/users/settings",
            headers={**headers, "Content-Type": "application/json"},
            content=raw_body,
        )
        assert bad_resp.status_code == 422


async def test_users_route_methods_and_prefix_guards(client: AsyncClient) -> None:
    assert (await client.get("/users/me")).status_code == 404
    assert (await client.patch("/users/settings", json={})).status_code == 404
    assert (await client.post("/api/users/me")).status_code == 405
    assert (await client.get("/api/users/settings")).status_code == 405
    assert (await client.put("/api/users/settings", json={})).status_code == 405


async def test_auto_initialization_rolls_back_atomically_if_deck_flush_fails(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If deck persistence fails mid-transaction, no orphaned user row is left behind."""

    async def failing_flush(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Simulated deck flush failure")

    monkeypatch.setattr(db_session, "flush", failing_flush)

    with pytest.raises(RuntimeError, match="Simulated deck flush failure"):
        await ensure_user_initialized(db_session, USER_A_ID)

    await db_session.rollback()

    user_row = (
        await db_session.execute(select(User).where(User.telegram_id == USER_A_ID))
    ).scalar_one_or_none()
    deck_row = (
        await db_session.execute(select(Deck).where(Deck.owner_id == USER_A_ID))
    ).scalar_one_or_none()
    assert user_row is None
    assert deck_row is None
