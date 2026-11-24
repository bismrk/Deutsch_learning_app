"""Unit tests for BackendAPIClient using respx mocks (zero real network calls)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from bot_app.api_client import (
    APIAuthError,
    APIClientError,
    APIUnavailableError,
    BackendAPIClient,
    BadRequestError,
    ConflictError,
    NotFoundError,
    RateLimitError,
    ValidationAPIError,
)
from bot_app.schemas import (
    ActiveSessionResponse,
    RadarStatsResponse,
    SessionAbandonResponse,
    SessionAnswerResponse,
    SessionHintResponse,
    SessionStartResponse,
    UserProfile,
    WordImportResult,
    WordList,
)

BASE_URL = "http://backend.test:8000"
SECRET_API_KEY = "super-secret-internal-key-999"
USER_ID = 123456789


def _user_profile_payload(level: str = "A1") -> dict[str, object]:
    return {
        "telegram_id": USER_ID,
        "level": level,
        "exercises_per_set": 5,
        "theme": "system",
        "sheets_url": None,
        "reminder_hour": 19,
        "timezone": "Europe/Kyiv",
        "daily_llm_calls": 2,
        "created_at": "2026-11-24T12:00:00Z",
    }


def _exercise_public_payload() -> dict[str, object]:
    return {
        "exercise_type": "translation",
        "level": "A2",
        "grammar_topic": "Nebensätze mit weil",
        "target_word_ids": [1, 2],
        "instruction_ua": "Переклади речення німецькою:",
        "task_text": "Я запізнився, бо потяг мав затримку.",
    }


def _session_start_payload() -> dict[str, object]:
    return {
        "session_id": 42,
        "mode": "mix",
        "target_topic": None,
        "total_exercises": 5,
        "completed_exercises": 0,
        "current_exercise_number": 1,
        "status": "active",
        "created_at": "2026-11-24T12:00:00Z",
        "attempt_id": 101,
        "used_hint": False,
        "exercise": _exercise_public_payload(),
    }


async def test_client_default_timeout_and_lifecycle() -> None:
    client = BackendAPIClient(base_url=f"{BASE_URL}/", internal_api_key=SECRET_API_KEY)
    assert client.base_url == BASE_URL
    assert client.timeout >= 30.0
    await client.aclose()
    await client.aclose()  # idempotent close


@pytest.mark.parametrize(
    ("base_url", "timeout"),
    [
        ("", 35.0),
        ("   ", 35.0),
        (BASE_URL, 0),
        (BASE_URL, -5.0),
        (BASE_URL, float("nan")),
        (BASE_URL, float("inf")),
    ],
)
def test_client_rejects_invalid_constructor_args(base_url: str, timeout: float) -> None:
    with pytest.raises(ValueError):
        BackendAPIClient(base_url=base_url, internal_api_key=SECRET_API_KEY, timeout=timeout)


@pytest.mark.parametrize("bad_user_id", [0, -1, True])
async def test_client_rejects_invalid_telegram_user_id(bad_user_id: int) -> None:
    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        with pytest.raises(ValueError):
            await client.get_me(bad_user_id)


@respx.mock
async def test_users_endpoints_send_auth_headers_and_parse_response() -> None:
    get_route = respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(200, json=_user_profile_payload("A1")),
    )
    patch_route = respx.patch(f"{BASE_URL}/api/users/settings").mock(
        return_value=httpx.Response(200, json=_user_profile_payload("B1")),
    )

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        profile = await client.get_me(USER_ID)
        updated = await client.update_settings(USER_ID, level="B1", sheets_url=None)

    assert isinstance(profile, UserProfile)
    assert profile.telegram_id == USER_ID
    assert profile.level == "A1"

    assert isinstance(updated, UserProfile)
    assert updated.level == "B1"

    assert get_route.called
    get_headers = get_route.calls.last.request.headers
    assert get_headers["X-Internal-API-Key"] == SECRET_API_KEY
    assert get_headers["X-Telegram-User-Id"] == str(USER_ID)

    assert patch_route.called
    patch_req = patch_route.calls.last.request
    assert patch_req.headers["X-Internal-API-Key"] == SECRET_API_KEY
    assert patch_req.headers["X-Telegram-User-Id"] == str(USER_ID)
    assert json.loads(patch_req.content) == {"level": "B1", "sheets_url": None}


@respx.mock
async def test_words_endpoints_cover_list_import_sync_and_delete() -> None:
    list_route = respx.get(f"{BASE_URL}/api/words").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": 7,
                        "word_de": "der Zug",
                        "translation_ua": "потяг",
                        "part_of_speech": "noun",
                        "source": "text",
                        "state": 0,
                        "stability": 0.0,
                        "difficulty": 0.0,
                        "due_at": "2026-11-24T12:00:00Z",
                        "reps": 0,
                        "lapses": 0,
                    },
                ],
                "total": 1,
            },
        ),
    )
    import_route = respx.post(f"{BASE_URL}/api/words/import").mock(
        return_value=httpx.Response(200, json={"imported": 2, "duplicates": 1, "skipped": 0}),
    )
    sync_route = respx.post(f"{BASE_URL}/api/words/sync-sheets").mock(
        return_value=httpx.Response(200, json={"imported": 5, "duplicates": 0, "skipped": 1}),
    )
    delete_route = respx.delete(f"{BASE_URL}/api/words/7").mock(
        return_value=httpx.Response(204),
    )

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        words = await client.list_words(USER_ID, search="Zug", limit=10, offset=0)
        text_res = await client.import_words_text(USER_ID, "der Zug - потяг")
        file_res = await client.import_words_file(USER_ID, "words.txt", b"der Zug - potiah")
        sheets_res = await client.sync_sheets(
            USER_ID,
            sheets_url="https://docs.google.com/spreadsheets/d/abc/edit",
        )
        await client.delete_word(USER_ID, 7)

    assert isinstance(words, WordList)
    assert words.total == 1
    assert words.items[0].word_de == "der Zug"
    assert list_route.calls.last.request.url.params["search"] == "Zug"

    assert isinstance(text_res, WordImportResult)
    assert text_res.imported == 2
    assert isinstance(file_res, WordImportResult)
    assert import_route.call_count == 2

    assert isinstance(sheets_res, WordImportResult)
    assert sheets_res.imported == 5
    assert sync_route.called

    assert delete_route.called
    assert delete_route.calls.last.request.headers["X-Internal-API-Key"] == SECRET_API_KEY
    assert delete_route.calls.last.request.headers["X-Telegram-User-Id"] == str(USER_ID)


@respx.mock
async def test_sessions_and_stats_endpoints() -> None:
    respx.post(f"{BASE_URL}/api/sessions/start").mock(
        return_value=httpx.Response(201, json=_session_start_payload()),
    )
    respx.get(f"{BASE_URL}/api/sessions/active").mock(
        return_value=httpx.Response(
            200,
            json={**_session_start_payload(), "used_hint": True, "hint_ua": "Дієслово в кінці."},
        ),
    )
    respx.post(f"{BASE_URL}/api/sessions/42/hint").mock(
        return_value=httpx.Response(
            200,
            json={"hint_ua": "Дієслово в кінці.", "used_hint": True},
        ),
    )
    respx.post(f"{BASE_URL}/api/sessions/42/answer").mock(
        return_value=httpx.Response(
            200,
            json={
                "session_id": 42,
                "attempt_id": 101,
                "status": "completed",
                "completed_exercises": 1,
                "total_exercises": 1,
                "used_hint": False,
                "evaluation": {
                    "is_correct": True,
                    "grammar_score": 95,
                    "corrected_answer": "Ich bin zu spät gekommen.",
                    "word_ratings": [
                        {
                            "word_id": 1,
                            "word_de": "der Zug",
                            "fsrs_rating": 4,
                            "note_ua": "Чудово!",
                        },
                    ],
                    "errors": [],
                    "youtube_topic": "Nebensätze mit weil",
                    "summary_ua": "Чудова робота!",
                },
                "next_attempt_id": None,
                "next_exercise": None,
                "summary": {
                    "total_exercises": 1,
                    "correct_count": 1,
                    "avg_grammar_score": 95.0,
                    "weak_topics": [],
                },
            },
        ),
    )
    respx.post(f"{BASE_URL}/api/sessions/42/abandon").mock(
        return_value=httpx.Response(
            200,
            json={
                "session_id": 42,
                "status": "abandoned",
                "completed_exercises": 1,
                "total_exercises": 5,
            },
        ),
    )
    stats_route = respx.get(f"{BASE_URL}/api/stats/radar").mock(
        return_value=httpx.Response(
            200,
            json={
                "axes": [
                    {"axis": "vocab", "label_ua": "Словник", "score": 30, "status": "active"},
                    {"axis": "grammar", "label_ua": "Граматика", "score": 45, "status": "active"},
                    {
                        "axis": "consistency",
                        "label_ua": "Стабільність",
                        "score": 80,
                        "status": "active",
                    },
                    {"axis": "writing", "label_ua": "Шрайбен", "score": 0, "status": "coming_soon"},
                    {
                        "axis": "listening",
                        "label_ua": "Хьорен",
                        "score": 0,
                        "status": "coming_soon",
                    },
                ],
                "weak_topics": [
                    {
                        "grammar_topic": "Nebensätze mit weil",
                        "avg_grammar_score": 60.0,
                        "attempts_count": 2,
                        "youtube_topic": "Порядок слів із weil",
                    },
                ],
            },
        ),
    )

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        started = await client.start_session(USER_ID, mode="mix", total_exercises=5)
        active = await client.get_active_session(USER_ID)
        hint = await client.request_hint(USER_ID, 42)
        answer = await client.submit_answer(USER_ID, 42, "Ich bin zu spät gekommen.")
        abandoned = await client.abandon_session(USER_ID, 42)
        radar = await client.get_radar_stats(USER_ID)

    assert isinstance(started, SessionStartResponse)
    assert started.session_id == 42
    assert isinstance(active, ActiveSessionResponse)
    assert active.hint_ua == "Дієслово в кінці."
    assert isinstance(hint, SessionHintResponse)
    assert hint.used_hint is True
    assert isinstance(answer, SessionAnswerResponse)
    assert answer.summary is not None
    assert answer.summary.correct_count == 1
    assert isinstance(abandoned, SessionAbandonResponse)
    assert abandoned.status == "abandoned"
    assert isinstance(radar, RadarStatsResponse)
    assert len(radar.axes) == 5
    assert stats_route.calls.last.request.headers["X-Internal-API-Key"] == SECRET_API_KEY


@respx.mock
async def test_handles_429_rate_limit_with_friendly_ukrainian_message_and_no_secret_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(
            429,
            json={"detail": f"Quota exceeded for key {SECRET_API_KEY}"},
        ),
    )

    with caplog.at_level(logging.DEBUG):
        async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
            with pytest.raises(RateLimitError) as exc_info:
                await client.get_me(USER_ID)

    err = exc_info.value
    assert err.status_code == 429
    assert "ліміт" in err.user_message.lower()
    assert SECRET_API_KEY not in str(err)
    assert SECRET_API_KEY not in (err.detail or "")
    assert SECRET_API_KEY not in caplog.text


@pytest.mark.parametrize("status_code", [401, 403])
@respx.mock
async def test_handles_401_and_403_auth_errors_without_secret_leak(
    status_code: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(
            status_code,
            json={"detail": f"Invalid key {SECRET_API_KEY}"},
        ),
    )

    with caplog.at_level(logging.DEBUG):
        async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
            with pytest.raises(APIAuthError) as exc_info:
                await client.get_me(USER_ID)

    err = exc_info.value
    assert err.status_code == status_code
    assert err.detail is None
    assert SECRET_API_KEY not in str(err)
    assert SECRET_API_KEY not in caplog.text


@pytest.mark.parametrize(
    ("status_code", "expected_cls", "detail_body", "expected_detail"),
    [
        (400, BadRequestError, {"detail": "Ваш словник порожній."}, "Ваш словник порожній."),
        (404, NotFoundError, {"detail": "Слово не знайдено."}, "Слово не знайдено."),
        (409, ConflictError, {"detail": "Активна сесія вже існує."}, "Активна сесія вже існує."),
        (
            422,
            ValidationAPIError,
            {"detail": [{"loc": ["body", "level"], "msg": "Input should be 'A1', 'A2' or 'B1'"}]},
            "Input should be 'A1', 'A2' or 'B1'",
        ),
    ],
)
@respx.mock
async def test_handles_4xx_client_errors_and_preserves_backend_detail(
    status_code: int,
    expected_cls: type[APIClientError],
    detail_body: dict[str, object],
    expected_detail: str,
) -> None:
    respx.post(f"{BASE_URL}/api/sessions/start").mock(
        return_value=httpx.Response(status_code, json=detail_body),
    )

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        with pytest.raises(expected_cls) as exc_info:
            await client.start_session(USER_ID)

    err = exc_info.value
    assert isinstance(err, APIClientError)
    assert err.status_code == status_code
    assert err.detail == expected_detail
    assert expected_detail in err.user_message


@pytest.mark.parametrize("status_code", [500, 502, 503])
@respx.mock
async def test_handles_5xx_errors_as_api_unavailable(status_code: int) -> None:
    respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(status_code, json={"detail": "Internal traceback"}),
    )

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        with pytest.raises(APIUnavailableError) as exc_info:
            await client.get_me(USER_ID)

    err = exc_info.value
    assert err.status_code == status_code
    assert "недоступний" in err.user_message.lower()
    assert "traceback" not in str(err).lower()


@respx.mock
async def test_handles_timeout_as_api_unavailable() -> None:
    respx.get(f"{BASE_URL}/api/users/me").mock(side_effect=httpx.ReadTimeout("Timed out"))

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        with pytest.raises(APIUnavailableError) as exc_info:
            await client.get_me(USER_ID)

    err = exc_info.value
    assert err.status_code is None
    assert "довго" in err.user_message.lower()
    assert SECRET_API_KEY not in str(err)


@respx.mock
async def test_handles_connection_error_as_api_unavailable() -> None:
    respx.get(f"{BASE_URL}/api/users/me").mock(
        side_effect=httpx.ConnectError("Connection refused"),
    )

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        with pytest.raises(APIUnavailableError) as exc_info:
            await client.get_me(USER_ID)

    err = exc_info.value
    assert err.status_code is None
    assert "недоступний" in err.user_message.lower()


@respx.mock
async def test_handles_non_json_response_as_api_unavailable() -> None:
    respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(
            200,
            text="<html><body>Gateway page</body></html>",
            headers={"content-type": "text/html"},
        ),
    )

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        with pytest.raises(APIUnavailableError) as exc_info:
            await client.get_me(USER_ID)

    assert exc_info.value.status_code == 200
    assert "недоступний" in exc_info.value.user_message.lower()


@respx.mock
async def test_handles_invalid_json_schema_as_api_unavailable() -> None:
    respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"}),
    )

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        with pytest.raises(APIUnavailableError):
            await client.get_me(USER_ID)
