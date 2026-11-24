"""Tests for Telegram Mini App initData and internal bot authentication."""

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any
from urllib.parse import quote, urlencode

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.deps import CurrentUserId, get_current_user_id, get_utc_now
from app.api.health import get_db_health_checker
from app.config import Settings, get_settings
from app.core.auth import (
    DEFAULT_INIT_DATA_MAX_AGE_SECONDS,
    DEFAULT_INIT_DATA_MAX_FUTURE_SKEW_SECONDS,
    MAX_TELEGRAM_USER_ID,
    TELEGRAM_WEBAPP_HMAC_KEY,
    AuthError,
    extract_tma_init_data,
    parse_telegram_user_id,
    verify_internal_bot_auth,
    verify_telegram_init_data,
)
from app.main import app as production_app
from app.main import create_app

TEST_BOT_TOKEN = "123456789:AAH_test_bot_token_secret_value_XYZ"
TEST_INTERNAL_API_KEY = "super-secret-internal-bot-api-key-987654"
FIXED_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
FIXED_NOW_TS = int(FIXED_NOW.timestamp())
LARGE_64BIT_USER_ID = 5_000_000_001


def _compute_init_data_hash(fields: Mapping[str, str], bot_token: str) -> str:
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items()) if key != "hash"
    )
    secret_key = hmac.new(
        TELEGRAM_WEBAPP_HMAC_KEY,
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_signed_init_data(
    *,
    bot_token: str = TEST_BOT_TOKEN,
    user: dict[str, Any] | str | None = None,
    auth_date: int | str | None = FIXED_NOW_TS,
    extra_fields: Mapping[str, str] | None = None,
    include_user: bool = True,
    include_auth_date: bool = True,
    include_hash: bool = True,
    hash_override: str | None = None,
) -> tuple[str, str]:
    """Build a signed Telegram ``initData`` query string and return ``(data, expected_hash)``."""
    fields: dict[str, str] = {}
    if extra_fields:
        fields.update(extra_fields)

    if include_user:
        if user is None:
            user_value = json.dumps(
                {"id": 123456789, "first_name": "Max", "language_code": "uk"},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        elif isinstance(user, str):
            user_value = user
        else:
            user_value = json.dumps(user, ensure_ascii=False, separators=(",", ":"))
        fields["user"] = user_value

    if include_auth_date and auth_date is not None:
        fields["auth_date"] = str(auth_date)

    expected_hash = _compute_init_data_hash(fields, bot_token)

    ordered_pairs: list[tuple[str, str]] = list(fields.items())
    if include_hash:
        hash_value = hash_override if hash_override is not None else expected_hash
        ordered_pairs.append(("hash", hash_value))

    return urlencode(ordered_pairs, encoding="utf-8"), expected_hash


def create_protected_test_app(
    *,
    bot_token: str | None = TEST_BOT_TOKEN,
    internal_api_key: str | None = TEST_INTERNAL_API_KEY,
    init_data_max_age_seconds: int = DEFAULT_INIT_DATA_MAX_AGE_SECONDS,
    now: datetime = FIXED_NOW,
    use_settings_override: bool = False,
) -> FastAPI:
    """Create an isolated FastAPI app with a test-only route guarded by ``get_current_user_id``."""
    configured_settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=None,
        bot_token=bot_token,
        internal_api_key=internal_api_key,
        init_data_max_age_seconds=init_data_max_age_seconds,
    )

    if use_settings_override:
        test_app = create_app(settings=Settings(_env_file=None, app_env="test", database_url=None))
        test_app.dependency_overrides[get_settings] = lambda: configured_settings
    else:
        test_app = create_app(settings=configured_settings)

    test_app.dependency_overrides[get_utc_now] = lambda: now

    @test_app.get("/api/_test/me")
    async def protected_me(user_id: CurrentUserId) -> dict[str, int]:
        return {"telegram_id": user_id}

    return test_app


# ---------------------------------------------------------------------------
# Unit tests: backend/app/core/auth.py
# ---------------------------------------------------------------------------


def test_verify_telegram_init_data_accepts_valid_payload_with_cyrillic_and_64bit_id() -> None:
    init_data, _ = build_signed_init_data(
        user={
            "id": LARGE_64BIT_USER_ID,
            "first_name": "Олександр",
            "last_name": "Шевченко-Мюллер äöüß",
            "username": "oleksandr_de",
            "language_code": "uk",
        },
        auth_date=FIXED_NOW_TS - 120,
        extra_fields={
            "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
            "start_param": "ref=lesson_1+intro",
            "chat_type": "private",
            "empty_optional": "",
        },
    )

    user_id = verify_telegram_init_data(init_data, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW)

    assert user_id == LARGE_64BIT_USER_ID


@pytest.mark.parametrize("boundary_id", [1, LARGE_64BIT_USER_ID, MAX_TELEGRAM_USER_ID])
def test_verify_telegram_init_data_accepts_valid_64bit_bigint_boundaries(
    boundary_id: int,
) -> None:
    init_data, _ = build_signed_init_data(user={"id": boundary_id}, auth_date=FIXED_NOW_TS)

    assert verify_telegram_init_data(init_data, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW) == (
        boundary_id
    )


def test_verify_telegram_init_data_is_independent_of_query_parameter_order() -> None:
    user_json = json.dumps({"id": 777, "first_name": "Ірина"}, ensure_ascii=False)
    fields = {
        "user": user_json,
        "auth_date": str(FIXED_NOW_TS),
        "query_id": "Q-99",
        "chat_instance": "42",
    }
    digest = _compute_init_data_hash(fields, TEST_BOT_TOKEN)
    # Intentionally unsorted order with hash in the middle
    raw_init_data = urlencode(
        [
            ("user", user_json),
            ("hash", digest),
            ("query_id", "Q-99"),
            ("auth_date", str(FIXED_NOW_TS)),
            ("chat_instance", "42"),
        ],
        encoding="utf-8",
    )

    assert verify_telegram_init_data(raw_init_data, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW) == 777


def test_verify_telegram_init_data_accepts_percent20_url_encoded_cyrillic_and_spaces() -> None:
    user_json = json.dumps(
        {"id": LARGE_64BIT_USER_ID, "first_name": "Марія Іваненко", "last_name": "Müller + Co"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    fields = {"user": user_json, "auth_date": str(FIXED_NOW_TS)}
    digest = _compute_init_data_hash(fields, TEST_BOT_TOKEN)
    # Percent-encode spaces as %20 and '+' as %2B (as done by JS encodeURIComponent)
    raw_init_data = (
        f"user={quote(user_json, safe='')}&auth_date={FIXED_NOW_TS}&hash={digest}"
    )

    assert (
        verify_telegram_init_data(raw_init_data, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW)
        == LARGE_64BIT_USER_ID
    )


def test_verify_telegram_init_data_accepts_non_utc_timezone_aware_now() -> None:
    kyiv_tz = timezone(timedelta(hours=3))
    fixed_kyiv = FIXED_NOW.astimezone(kyiv_tz)
    init_data, _ = build_signed_init_data(user={"id": 42}, auth_date=FIXED_NOW_TS - 86_400)

    assert verify_telegram_init_data(init_data, bot_token=TEST_BOT_TOKEN, now=fixed_kyiv) == 42


def test_verify_telegram_init_data_rejects_naive_now() -> None:
    init_data, _ = build_signed_init_data(user={"id": 42}, auth_date=FIXED_NOW_TS)
    naive_now = datetime(2026, 5, 1, 12, 0, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        verify_telegram_init_data(init_data, bot_token=TEST_BOT_TOKEN, now=naive_now)


def test_verify_telegram_init_data_accepts_exact_24h_boundary_and_rejects_expired() -> None:
    exact_24h, _ = build_signed_init_data(
        user={"id": 100},
        auth_date=FIXED_NOW_TS - DEFAULT_INIT_DATA_MAX_AGE_SECONDS,
    )
    expired_by_1s, _ = build_signed_init_data(
        user={"id": 100},
        auth_date=FIXED_NOW_TS - DEFAULT_INIT_DATA_MAX_AGE_SECONDS - 1,
    )

    assert verify_telegram_init_data(exact_24h, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW) == 100

    with pytest.raises(AuthError):
        verify_telegram_init_data(expired_by_1s, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW)


def test_verify_telegram_init_data_handles_future_auth_date_skew_boundary() -> None:
    within_skew, _ = build_signed_init_data(
        user={"id": 101},
        auth_date=FIXED_NOW_TS + DEFAULT_INIT_DATA_MAX_FUTURE_SKEW_SECONDS,
    )
    too_far_future, _ = build_signed_init_data(
        user={"id": 101},
        auth_date=FIXED_NOW_TS + DEFAULT_INIT_DATA_MAX_FUTURE_SKEW_SECONDS + 1,
    )

    assert verify_telegram_init_data(within_skew, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW) == 101

    with pytest.raises(AuthError):
        verify_telegram_init_data(too_far_future, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW)


@pytest.mark.parametrize(
    "bad_hash",
    [
        "",
        "0" * 64,
        "short",
        "підроблений-геш-не-ascii",
    ],
)
def test_verify_telegram_init_data_rejects_forged_hash_without_leaking_secrets(
    bad_hash: str,
) -> None:
    init_data, expected_hash = build_signed_init_data(
        user={"id": 555},
        auth_date=FIXED_NOW_TS,
        hash_override=bad_hash,
    )

    with pytest.raises(AuthError) as exc_info:
        verify_telegram_init_data(init_data, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW)

    message = str(exc_info.value)
    assert TEST_BOT_TOKEN not in message
    assert expected_hash not in message


def test_verify_telegram_init_data_rejects_uppercase_hash() -> None:
    _, expected_hash = build_signed_init_data(user={"id": 555}, auth_date=FIXED_NOW_TS)
    init_data, _ = build_signed_init_data(
        user={"id": 555},
        auth_date=FIXED_NOW_TS,
        hash_override=expected_hash.upper(),
    )

    with pytest.raises(AuthError):
        verify_telegram_init_data(init_data, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW)


def test_verify_telegram_init_data_rejects_tampered_field_after_signing() -> None:
    signed_init_data, _ = build_signed_init_data(
        user={"id": 111, "first_name": "Анна"},
        auth_date=FIXED_NOW_TS,
    )
    tampered = signed_init_data.replace("111", "222")

    with pytest.raises(AuthError):
        verify_telegram_init_data(tampered, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW)


@pytest.mark.parametrize(
    "bad_init_data",
    [
        "",
        "   ",
        "not_a_query_string",
        "auth_date=1777636800&&hash=abc",
        "user=%FF%FE&auth_date=1777636800&hash=abc",
        "=empty_key&auth_date=1777636800&hash=abc",
    ],
)
def test_verify_telegram_init_data_rejects_malformed_query_strings(bad_init_data: str) -> None:
    with pytest.raises(AuthError):
        verify_telegram_init_data(bad_init_data, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW)


def test_verify_telegram_init_data_rejects_duplicate_keys() -> None:
    init_data, expected_hash = build_signed_init_data(user={"id": 12}, auth_date=FIXED_NOW_TS)
    duplicated = f"{init_data}&hash={expected_hash}"

    with pytest.raises(AuthError):
        verify_telegram_init_data(duplicated, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"include_hash": False},
        {"include_auth_date": False},
        {"include_user": False},
        {"user": "{corrupted-json"},
        {"user": ""},
        {"user": "[1, 2, 3]"},
        {"user": "null"},
        {"user": {}},
        {"user": {"id": None}},
        {"user": {"id": 0}},
        {"user": {"id": -1}},
        {"user": {"id": True}},
        {"user": {"id": False}},
        {"user": {"id": "12345"}},
        {"user": {"id": 12345.0}},
        {"user": {"id": MAX_TELEGRAM_USER_ID + 1}},
        {"auth_date": ""},
        {"auth_date": "0"},
        {"auth_date": "-10"},
        {"auth_date": "not-a-timestamp"},
        {"auth_date": "1777636800.5"},
        {"auth_date": "1_777_636_800"},
        {"auth_date": f"+{FIXED_NOW_TS}"},
        {"auth_date": f"0{FIXED_NOW_TS}"},
        {"auth_date": "9" * 40},
        {"auth_date": "١٧٧٧٦٣٦٨٠٠"},
    ],
)
def test_verify_telegram_init_data_rejects_missing_or_invalid_fields(
    kwargs: dict[str, Any],
) -> None:
    init_data, _ = build_signed_init_data(**kwargs)

    with pytest.raises(AuthError):
        verify_telegram_init_data(init_data, bot_token=TEST_BOT_TOKEN, now=FIXED_NOW)


@pytest.mark.parametrize("empty_token", [None, "", "   "])
def test_verify_telegram_init_data_fails_closed_when_bot_token_is_empty(
    empty_token: str | None,
) -> None:
    # Even if attacker signs initData with the exact empty/whitespace string as bot_token
    signing_secret = empty_token if empty_token is not None else ""
    init_data, _ = build_signed_init_data(
        bot_token=signing_secret,
        user={"id": 999},
        auth_date=FIXED_NOW_TS,
    )

    with pytest.raises(AuthError):
        verify_telegram_init_data(init_data, bot_token=empty_token, now=FIXED_NOW)


@pytest.mark.parametrize(
    ("raw_header", "expected_payload"),
    [
        ("tma auth_date=1&hash=2", "auth_date=1&hash=2"),
        ("TMA auth_date=1&hash=2", "auth_date=1&hash=2"),
        ("  tma   auth_date=1&hash=2  ", "auth_date=1&hash=2"),
    ],
)
def test_extract_tma_init_data_accepts_tma_scheme(raw_header: str, expected_payload: str) -> None:
    assert extract_tma_init_data(raw_header) == expected_payload


@pytest.mark.parametrize(
    "bad_header",
    [
        "",
        "   ",
        "tma",
        "tma   ",
        "Bearer auth_date=1&hash=2",
        "Basic dXNlcjpwYXNz",
        "auth_date=1&hash=2",
    ],
)
def test_extract_tma_init_data_rejects_invalid_scheme_or_empty_payload(bad_header: str) -> None:
    with pytest.raises(AuthError):
        extract_tma_init_data(bad_header)


@pytest.mark.parametrize(
    ("raw_id", "expected"),
    [
        ("1", 1),
        ("123456789", 123456789),
        (str(LARGE_64BIT_USER_ID), LARGE_64BIT_USER_ID),
        (str(MAX_TELEGRAM_USER_ID), MAX_TELEGRAM_USER_ID),
        ("  42  ", 42),
    ],
)
def test_parse_telegram_user_id_accepts_valid_positive_64bit_ids(
    raw_id: str,
    expected: int,
) -> None:
    assert parse_telegram_user_id(raw_id) == expected


@pytest.mark.parametrize(
    "invalid_id",
    [
        None,
        "",
        "   ",
        "0",
        "00",
        "0123",
        "-1",
        "-999999",
        "+123",
        "123.0",
        "1_000",
        "abc",
        "123abc",
        "12 34",
        str(MAX_TELEGRAM_USER_ID + 1),
        "9" * 30,
        "١٢٣٤٥",
    ],
)
def test_parse_telegram_user_id_rejects_invalid_values(invalid_id: str | None) -> None:
    with pytest.raises(AuthError):
        parse_telegram_user_id(invalid_id)


def test_verify_internal_bot_auth_accepts_valid_key_and_64bit_user_id() -> None:
    user_id = verify_internal_bot_auth(
        api_key=TEST_INTERNAL_API_KEY,
        telegram_user_id=str(LARGE_64BIT_USER_ID),
        expected_api_key=TEST_INTERNAL_API_KEY,
    )

    assert user_id == LARGE_64BIT_USER_ID


@pytest.mark.parametrize(
    ("provided_key", "expected_key"),
    [
        ("wrong-key", TEST_INTERNAL_API_KEY),
        ("неправильний-ключ", TEST_INTERNAL_API_KEY),
        (f"{TEST_INTERNAL_API_KEY} ", TEST_INTERNAL_API_KEY),
        ("", TEST_INTERNAL_API_KEY),
        ("   ", TEST_INTERNAL_API_KEY),
        (None, TEST_INTERNAL_API_KEY),
        ("", ""),
        ("   ", "   "),
        ("", None),
        (TEST_INTERNAL_API_KEY, None),
        (TEST_INTERNAL_API_KEY, ""),
        (TEST_INTERNAL_API_KEY, "   "),
    ],
)
def test_verify_internal_bot_auth_rejects_invalid_or_unconfigured_keys(
    provided_key: str | None,
    expected_key: str | None,
) -> None:
    with pytest.raises(AuthError) as exc_info:
        verify_internal_bot_auth(
            api_key=provided_key,
            telegram_user_id="123456",
            expected_api_key=expected_key,
        )

    assert TEST_INTERNAL_API_KEY not in str(exc_info.value)


# ---------------------------------------------------------------------------
# HTTP / FastAPI dependency tests (ASGITransport, no DB, no network)
# ---------------------------------------------------------------------------


async def test_http_protected_route_accepts_valid_tma_init_data_with_cyrillic_and_64bit_id() -> (
    None
):
    test_app = create_protected_test_app()
    init_data, _ = build_signed_init_data(
        user={
            "id": LARGE_64BIT_USER_ID,
            "first_name": "Катерина",
            "last_name": "Коваленко",
            "language_code": "uk",
        },
        auth_date=FIXED_NOW_TS - 60,
        extra_fields={"query_id": "AAHdF6IQAAAAAN0XohDhrOrc"},
    )

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get(
            "/api/_test/me",
            headers={"Authorization": f"tma {init_data}"},
        )

    assert response.status_code == 200
    assert response.json() == {"telegram_id": LARGE_64BIT_USER_ID}


async def test_http_protected_route_respects_get_settings_dependency_override() -> None:
    test_app = create_protected_test_app(use_settings_override=True)
    init_data, _ = build_signed_init_data(user={"id": 888999}, auth_date=FIXED_NOW_TS)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        tma_response = await async_client.get(
            "/api/_test/me",
            headers={"Authorization": f"tma {init_data}"},
        )
        bot_response = await async_client.get(
            "/api/_test/me",
            headers={
                "X-Internal-API-Key": TEST_INTERNAL_API_KEY,
                "X-Telegram-User-Id": "777666",
            },
        )

    assert tma_response.status_code == 200
    assert tma_response.json() == {"telegram_id": 888999}
    assert bot_response.status_code == 200
    assert bot_response.json() == {"telegram_id": 777666}


async def test_http_protected_route_handles_24h_boundary_and_expired_auth_date() -> None:
    test_app = create_protected_test_app()
    boundary_init_data, _ = build_signed_init_data(
        user={"id": 321},
        auth_date=FIXED_NOW_TS - 86_400,
    )
    expired_init_data, expired_hash = build_signed_init_data(
        user={"id": 321},
        auth_date=FIXED_NOW_TS - 86_401,
    )

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        ok_response = await async_client.get(
            "/api/_test/me",
            headers={"Authorization": f"tma {boundary_init_data}"},
        )
        expired_response = await async_client.get(
            "/api/_test/me",
            headers={"Authorization": f"tma {expired_init_data}"},
        )

    assert ok_response.status_code == 200
    assert ok_response.json() == {"telegram_id": 321}
    assert expired_response.status_code == 401
    assert TEST_BOT_TOKEN not in expired_response.text
    assert expired_hash not in expired_response.text
    assert "Traceback" not in expired_response.text


async def test_http_protected_route_respects_custom_init_data_max_age_seconds() -> None:
    test_app = create_protected_test_app(init_data_max_age_seconds=3600)
    fresh_data, _ = build_signed_init_data(user={"id": 500}, auth_date=FIXED_NOW_TS - 3600)
    stale_data, _ = build_signed_init_data(user={"id": 500}, auth_date=FIXED_NOW_TS - 3601)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        ok_response = await async_client.get(
            "/api/_test/me",
            headers={"Authorization": f"tma {fresh_data}"},
        )
        stale_response = await async_client.get(
            "/api/_test/me",
            headers={"Authorization": f"tma {stale_data}"},
        )

    assert ok_response.status_code == 200
    assert ok_response.json() == {"telegram_id": 500}
    assert stale_response.status_code == 401


async def test_http_protected_route_rejects_future_auth_date() -> None:
    test_app = create_protected_test_app()
    within_skew, _ = build_signed_init_data(user={"id": 600}, auth_date=FIXED_NOW_TS + 300)
    far_future, _ = build_signed_init_data(user={"id": 600}, auth_date=FIXED_NOW_TS + 301)
    very_far_future, _ = build_signed_init_data(user={"id": 600}, auth_date=FIXED_NOW_TS + 3600)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        ok_response = await async_client.get(
            "/api/_test/me",
            headers={"Authorization": f"tma {within_skew}"},
        )
        rejected_301 = await async_client.get(
            "/api/_test/me",
            headers={"Authorization": f"tma {far_future}"},
        )
        rejected_3600 = await async_client.get(
            "/api/_test/me",
            headers={"Authorization": f"tma {very_far_future}"},
        )

    assert ok_response.status_code == 200
    assert rejected_301.status_code == 401
    assert rejected_3600.status_code == 401


@pytest.mark.parametrize(
    "authorization_builder",
    [
        lambda: f"tma {build_signed_init_data(hash_override='a' * 64)[0]}",
        lambda: f"tma {build_signed_init_data(include_hash=False)[0]}",
        lambda: f"tma {build_signed_init_data(include_auth_date=False)[0]}",
        lambda: f"tma {build_signed_init_data(include_user=False)[0]}",
        lambda: f"tma {build_signed_init_data(user='{broken')[0]}",
        lambda: f"tma {build_signed_init_data(user={'id': 0})[0]}",
        lambda: f"tma {build_signed_init_data(user={'id': -42})[0]}",
        lambda: f"tma {build_signed_init_data(user={'id': True})[0]}",
        lambda: f"tma {build_signed_init_data(user={'id': MAX_TELEGRAM_USER_ID + 1})[0]}",
        lambda: f"tma {build_signed_init_data()[0].replace('123456789', '987654321')}",
        lambda: f"Bearer {build_signed_init_data()[0]}",
        lambda: f"Basic {build_signed_init_data()[0]}",
        lambda: build_signed_init_data()[0],
        lambda: "tma",
        lambda: "tma   ",
        lambda: "",
    ],
)
async def test_http_protected_route_rejects_invalid_tma_headers_without_leaking_secrets(
    authorization_builder: Any,
) -> None:
    test_app = create_protected_test_app()
    _, valid_hash = build_signed_init_data()
    authorization_value = authorization_builder()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get(
            "/api/_test/me",
            headers={"Authorization": authorization_value},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing authentication credentials"}
    assert TEST_BOT_TOKEN not in response.text
    assert TEST_INTERNAL_API_KEY not in response.text
    assert valid_hash not in response.text
    assert "Traceback" not in response.text


async def test_http_protected_route_does_not_silently_fallback_on_invalid_tma() -> None:
    """Invalid Authorization header must fail with 401 even when valid bot headers are present."""
    test_app = create_protected_test_app()
    forged_init_data, _ = build_signed_init_data(hash_override="f" * 64)
    expired_init_data, _ = build_signed_init_data(auth_date=FIXED_NOW_TS - 90_000)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        for bad_auth in (
            f"tma {forged_init_data}",
            f"tma {expired_init_data}",
            "Bearer some-token",
            "",
        ):
            response = await async_client.get(
                "/api/_test/me",
                headers={
                    "Authorization": bad_auth,
                    "X-Internal-API-Key": TEST_INTERNAL_API_KEY,
                    "X-Telegram-User-Id": "123456",
                },
            )
            assert response.status_code == 401


@pytest.mark.parametrize("user_id", [1, 123456, LARGE_64BIT_USER_ID, MAX_TELEGRAM_USER_ID])
async def test_http_protected_route_accepts_valid_internal_bot_headers(user_id: int) -> None:
    test_app = create_protected_test_app()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get(
            "/api/_test/me",
            headers={
                "X-Internal-API-Key": TEST_INTERNAL_API_KEY,
                "X-Telegram-User-Id": str(user_id),
            },
        )

    assert response.status_code == 200
    assert response.json() == {"telegram_id": user_id}


async def test_http_protected_route_accepts_lowercase_internal_bot_header_names() -> None:
    test_app = create_protected_test_app()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get(
            "/api/_test/me",
            headers={
                "x-internal-api-key": TEST_INTERNAL_API_KEY,
                "x-telegram-user-id": "424242",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"telegram_id": 424242}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Telegram-User-Id": "123456"},
        {"X-Internal-API-Key": TEST_INTERNAL_API_KEY},
        {"X-Internal-API-Key": "wrong-key", "X-Telegram-User-Id": "123456"},
        {"X-Internal-API-Key": "", "X-Telegram-User-Id": "123456"},
        {"X-Internal-API-Key": "   ", "X-Telegram-User-Id": "123456"},
        {"X-Internal-API-Key": TEST_INTERNAL_API_KEY, "X-Telegram-User-Id": ""},
        {"X-Internal-API-Key": TEST_INTERNAL_API_KEY, "X-Telegram-User-Id": "abc"},
        {"X-Internal-API-Key": TEST_INTERNAL_API_KEY, "X-Telegram-User-Id": "0"},
        {"X-Internal-API-Key": TEST_INTERNAL_API_KEY, "X-Telegram-User-Id": "-1"},
        {"X-Internal-API-Key": TEST_INTERNAL_API_KEY, "X-Telegram-User-Id": "12.34"},
        {"X-Internal-API-Key": TEST_INTERNAL_API_KEY, "X-Telegram-User-Id": "+123"},
        {
            "X-Internal-API-Key": TEST_INTERNAL_API_KEY,
            "X-Telegram-User-Id": str(MAX_TELEGRAM_USER_ID + 1),
        },
    ],
)
async def test_http_protected_route_rejects_invalid_or_missing_bot_headers(
    headers: dict[str, str],
) -> None:
    test_app = create_protected_test_app()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get("/api/_test/me", headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing authentication credentials"}
    assert TEST_INTERNAL_API_KEY not in response.text
    assert TEST_BOT_TOKEN not in response.text
    assert "Traceback" not in response.text


@pytest.mark.parametrize("server_bot_token", [None, "", "   "])
async def test_http_protected_route_fails_closed_when_server_bot_token_is_unset(
    server_bot_token: str | None,
) -> None:
    test_app = create_protected_test_app(bot_token=server_bot_token)
    signing_secret = server_bot_token if server_bot_token is not None else ""
    init_data, _ = build_signed_init_data(bot_token=signing_secret, user={"id": 42})

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get(
            "/api/_test/me",
            headers={"Authorization": f"tma {init_data}"},
        )

    assert response.status_code == 401


@pytest.mark.parametrize("server_api_key", [None, "", "   "])
@pytest.mark.parametrize("client_api_key", ["", "   ", TEST_INTERNAL_API_KEY])
async def test_http_protected_route_fails_closed_when_server_internal_key_is_unset(
    server_api_key: str | None,
    client_api_key: str,
) -> None:
    test_app = create_protected_test_app(internal_api_key=server_api_key)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get(
            "/api/_test/me",
            headers={
                "X-Internal-API-Key": client_api_key,
                "X-Telegram-User-Id": "42",
            },
        )

    assert response.status_code == 401


async def test_health_endpoint_remains_public_without_auth_headers() -> None:
    test_app = create_app(
        settings=Settings(
            _env_file=None,
            app_env="test",
            bot_token=TEST_BOT_TOKEN,
            internal_api_key=TEST_INTERNAL_API_KEY,
        ),
    )

    async def healthy_db() -> bool:
        return True

    test_app.dependency_overrides[get_db_health_checker] = lambda: healthy_db

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_production_app_does_not_expose_temporary_debug_auth_routes() -> None:
    schema_paths = set(production_app.openapi()["paths"])
    assert "/api/health" in schema_paths
    assert not any("_test" in path or "debug" in path for path in schema_paths)


def test_settings_init_data_max_age_seconds_default_and_validation() -> None:
    default_settings = Settings(_env_file=None)
    assert default_settings.init_data_max_age_seconds == 86_400

    custom_settings = Settings(_env_file=None, init_data_max_age_seconds=3600)
    assert custom_settings.init_data_max_age_seconds == 3600

    with pytest.raises(ValidationError):
        Settings(_env_file=None, init_data_max_age_seconds=0)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, init_data_max_age_seconds=-60)


async def test_http_protected_route_works_via_explicit_depends_signature() -> None:
    """Verify ``Depends(get_current_user_id)`` works on a bare FastAPI app with overrides."""
    bare_app = FastAPI()
    bare_app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        app_env="test",
        bot_token=TEST_BOT_TOKEN,
        internal_api_key=TEST_INTERNAL_API_KEY,
    )
    bare_app.dependency_overrides[get_utc_now] = lambda: FIXED_NOW

    @bare_app.get("/api/_test/bare")
    async def bare_route(
        user_id: Annotated[int, Depends(get_current_user_id)],
    ) -> dict[str, int]:
        return {"user_id": user_id}

    init_data, _ = build_signed_init_data(user={"id": 102030}, auth_date=FIXED_NOW_TS)
    transport = ASGITransport(app=bare_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get(
            "/api/_test/bare",
            headers={"Authorization": f"tma {init_data}"},
        )

    assert response.status_code == 200
    assert response.json() == {"user_id": 102030}
