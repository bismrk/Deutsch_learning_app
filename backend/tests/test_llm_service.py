"""Unit tests for provider-agnostic LLMService with respx mocks (zero real network calls)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.core.llm_service import (
    DEFAULT_LLM_TIMEOUT_SECONDS,
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMInvalidResponseError,
    LLMRequestError,
    LLMService,
    LLMUnavailableError,
)

TEST_BASE_URL = "https://mock-llm-provider.test/v1"
TEST_ENDPOINT = f"{TEST_BASE_URL}/chat/completions"
TEST_API_KEY = "sk-test-secret-llm-api-key-987654321"
TEST_MODEL = "mock-chat-model"


class SampleNestedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    word_de: str
    rating: int = Field(ge=1, le=4)


class SampleStructuredOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grammar_score: int = Field(ge=0, le=100)
    summary_ua: str = Field(min_length=1)
    items: list[SampleNestedItem] = Field(min_length=1)


def _chat_completion_response(
    content: str | None,
    *,
    model: str = TEST_MODEL,
    choices: list[dict[str, Any]] | None = None,
) -> httpx.Response:
    if choices is None:
        choices = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            },
        ]
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test-001",
            "object": "chat.completion",
            "created": 1_777_000_000,
            "model": model,
            "choices": choices,
        },
    )


def _valid_payload_json() -> str:
    return json.dumps(
        {
            "grammar_score": 88,
            "summary_ua": "Гарна відповідь! Зверни увагу на порядок слів із weil.",
            "items": [
                {"word_de": "die Verspätung", "rating": 4},
                {"word_de": "das Treffen", "rating": 3},
            ],
        },
        ensure_ascii=False,
    )


def _build_service(
    *,
    base_url: str = TEST_BASE_URL,
    api_key: str | None = TEST_API_KEY,
    model: str = TEST_MODEL,
    timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    transport_max_attempts: int = 3,
    backoff_multiplier: float = 1.0,
    backoff_min_seconds: float = 1.0,
    backoff_max_seconds: float = 8.0,
    sleep_delays: list[float] | None = None,
) -> LLMService:
    settings = Settings(
        _env_file=None,
        llm_base_url=base_url,
        llm_api_key=api_key,
        llm_model=model,
        llm_timeout_seconds=timeout,
    )

    async def fake_sleep(seconds: float) -> None:
        if sleep_delays is not None:
            sleep_delays.append(seconds)

    return LLMService(
        settings=settings,
        transport_max_attempts=transport_max_attempts,
        backoff_multiplier=backoff_multiplier,
        backoff_min_seconds=backoff_min_seconds,
        backoff_max_seconds=backoff_max_seconds,
        sleep=fake_sleep,
    )


# ---------------------------------------------------------------------------
# Configuration, provider-agnosticism, and SDK defaults
# ---------------------------------------------------------------------------


def test_llm_service_module_has_no_hardcoded_deepseek_url() -> None:
    module_path = Path(__file__).resolve().parent.parent / "app" / "core" / "llm_service.py"
    source_lower = module_path.read_text(encoding="utf-8").lower()

    assert "api.deepseek.com" not in source_lower
    assert "deepseek" not in source_lower


def test_llm_service_uses_settings_defaults_timeout_20s_and_sdk_max_retries_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", f"  {TEST_BASE_URL}  ")
    monkeypatch.setenv("LLM_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("LLM_MODEL", f"  {TEST_MODEL}  ")
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    get_settings.cache_clear()

    try:
        service = LLMService()
        assert service.base_url == TEST_BASE_URL
        assert service.model == TEST_MODEL
        assert service.timeout == 20.0
        assert service.client is not None
        assert service.client.max_retries == 0
        assert service.client.timeout == 20.0
        assert str(service.client.base_url).rstrip("/") == TEST_BASE_URL
        assert TEST_API_KEY not in repr(service)
    finally:
        get_settings.cache_clear()


def test_settings_rejects_blank_llm_strings_and_invalid_timeout() -> None:
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None, llm_base_url="   ")
    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None, llm_model="   ")
    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None, llm_timeout_seconds=0)
    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None, llm_timeout_seconds=-5.0)


@pytest.mark.parametrize(
    ("provider_url", "provider_model"),
    [
        ("https://api.openai.com/v1", "gpt-4o-mini"),
        ("https://openrouter.ai/api/v1", "google/gemini-2.5-flash"),
        ("https://custom-gateway.internal", "custom-local-llm"),
    ],
)
async def test_llm_service_switches_provider_via_settings_without_code_changes(
    provider_url: str,
    provider_model: str,
) -> None:
    expected_endpoint = f"{provider_url.rstrip('/')}/chat/completions"
    service = _build_service(
        base_url=provider_url,
        api_key=TEST_API_KEY,
        model=provider_model,
    )

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(expected_endpoint).mock(
            return_value=_chat_completion_response(_valid_payload_json(), model=provider_model),
        )

        async with service:
            result = await service.generate_structured(
                response_model=SampleStructuredOutput,
                system_prompt="Відповідай виключно у форматі JSON.",
                user_prompt="Перевір речення.",
                temperature=0.2,
                max_tokens=400,
            )

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {TEST_API_KEY}"

    sent_body = json.loads(request.content.decode("utf-8"))
    assert sent_body["model"] == provider_model
    assert sent_body["response_format"] == {"type": "json_object"}
    assert sent_body["temperature"] == 0.2
    assert sent_body["max_tokens"] == 400
    assert sent_body["messages"] == [
        {"role": "system", "content": "Відповідай виключно у форматі JSON."},
        {"role": "user", "content": "Перевір речення."},
    ]
    assert result.grammar_score == 88
    assert result.items[0].word_de == "die Verspätung"
    assert "порядок слів" in result.summary_ua


async def test_generate_structured_accepts_explicit_messages_and_surrounding_whitespace() -> None:
    service = _build_service()

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(TEST_ENDPOINT).mock(
            return_value=_chat_completion_response(f"\n  {_valid_payload_json()}  \n"),
        )

        async with service:
            result = await service.generate_structured(
                response_model=SampleStructuredOutput,
                messages=[
                    {"role": "system", "content": "JSON only"},
                    {"role": "user", "content": "Evaluate answer"},
                ],
            )

    assert route.call_count == 1
    sent_body = json.loads(route.calls[0].request.content.decode("utf-8"))
    assert sent_body["model"] == TEST_MODEL
    assert sent_body["response_format"] == {"type": "json_object"}
    assert len(sent_body["messages"]) == 2
    assert result.grammar_score == 88


# ---------------------------------------------------------------------------
# Structured JSON validation and single automatic retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_first_response",
    [
        _chat_completion_response("{not valid json"),
        _chat_completion_response('{"grammar_score": 88, "summary_ua":'),
        _chat_completion_response('```json\n{"grammar_score": 88}\n```'),
        _chat_completion_response(
            json.dumps(
                {
                    "grammar_score": 150,
                    "summary_ua": "ok",
                    "items": [{"word_de": "a", "rating": 3}],
                },
            ),
        ),
        _chat_completion_response(
            json.dumps(
                {
                    "grammar_score": 80,
                    "summary_ua": "ok",
                    "items": [{"word_de": "a", "rating": 3}],
                    "unexpected_extra": True,
                },
            ),
        ),
        _chat_completion_response(json.dumps({"grammar_score": 80})),
        _chat_completion_response("[]"),
        _chat_completion_response("null"),
        _chat_completion_response(""),
        _chat_completion_response("   \n\t  "),
        _chat_completion_response(None),
        _chat_completion_response(None, choices=[]),
        httpx.Response(
            200,
            text="<html>broken gateway</html>",
            headers={"content-type": "text/html"},
        ),
        httpx.Response(200, text="{broken", headers={"content-type": "application/json"}),
    ],
)
async def test_parse_or_validation_error_retries_once_and_succeeds_on_second_attempt(
    bad_first_response: httpx.Response,
) -> None:
    delays: list[float] = []
    service = _build_service(sleep_delays=delays)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(TEST_ENDPOINT).mock(
            side_effect=[
                bad_first_response,
                _chat_completion_response(_valid_payload_json()),
            ],
        )

        async with service:
            result = await service.generate_structured(
                response_model=SampleStructuredOutput,
                user_prompt="Generate JSON",
            )

    assert route.call_count == 2
    assert delays == []  # Parse retry must not trigger transport exponential backoff
    assert result.grammar_score == 88
    assert len(result.items) == 2


@pytest.mark.parametrize(
    "bad_response",
    [
        _chat_completion_response("plain text without json"),
        _chat_completion_response(
            json.dumps({"grammar_score": "invalid", "summary_ua": "", "items": []}),
        ),
        _chat_completion_response(""),
        _chat_completion_response(None),
        _chat_completion_response(None, choices=[]),
    ],
)
async def test_parse_or_validation_error_raises_invalid_response_after_two_failures(
    bad_response: httpx.Response,
) -> None:
    delays: list[float] = []
    service = _build_service(sleep_delays=delays)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(TEST_ENDPOINT).mock(
            side_effect=[
                bad_response,
                bad_response,
                _chat_completion_response(_valid_payload_json()),  # Must NEVER be reached
            ],
        )

        async with service:
            with pytest.raises(LLMInvalidResponseError) as exc_info:
                await service.generate_structured(
                    response_model=SampleStructuredOutput,
                    user_prompt="Generate JSON",
                )

    assert route.call_count == 2
    assert delays == []
    assert TEST_API_KEY not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Transient provider/network errors with tenacity exponential backoff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transient_failure",
    [
        httpx.Response(429, json={"error": {"message": "rate limit exceeded"}}),
        httpx.Response(500, json={"error": {"message": "internal server error"}}),
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(504, text="Gateway Timeout"),
        httpx.Response(408, json={"error": {"message": "request timeout"}}),
        httpx.ReadTimeout("request timed out after 20s"),
        httpx.ConnectError("connection reset by peer"),
        httpx.RemoteProtocolError("server disconnected unexpectedly"),
    ],
)
async def test_transient_errors_retry_with_exponential_backoff_and_recover(
    transient_failure: httpx.Response | Exception,
) -> None:
    delays: list[float] = []
    service = _build_service(
        transport_max_attempts=3,
        backoff_multiplier=1.0,
        backoff_min_seconds=1.0,
        backoff_max_seconds=8.0,
        sleep_delays=delays,
    )

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(TEST_ENDPOINT).mock(
            side_effect=[
                transient_failure,
                transient_failure,
                _chat_completion_response(_valid_payload_json()),
            ],
        )

        async with service:
            result = await service.generate_structured(
                response_model=SampleStructuredOutput,
                user_prompt="Generate JSON",
            )

    assert route.call_count == 3
    assert len(delays) == 2
    assert delays[0] >= 1.0
    assert delays[1] > delays[0]
    assert result.grammar_score == 88


@pytest.mark.parametrize(
    ("transient_failure", "expected_status"),
    [
        (httpx.Response(429, json={"error": {"message": f"limit {TEST_API_KEY}"}}), 429),
        (httpx.Response(500, json={"error": {"message": f"boom {TEST_API_KEY}"}}), 500),
        (httpx.Response(503, text=f"unavailable {TEST_API_KEY}"), 503),
        (httpx.ReadTimeout(f"timeout using {TEST_API_KEY}"), None),
        (httpx.ConnectError(f"disconnect using {TEST_API_KEY}"), None),
    ],
)
async def test_transient_errors_raise_llm_unavailable_after_exhausting_retries(
    transient_failure: httpx.Response | Exception,
    expected_status: int | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    delays: list[float] = []
    service = _build_service(
        transport_max_attempts=3,
        backoff_multiplier=1.0,
        backoff_min_seconds=1.0,
        backoff_max_seconds=8.0,
        sleep_delays=delays,
    )

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(TEST_ENDPOINT).mock(
            side_effect=[transient_failure, transient_failure, transient_failure],
        )

        with caplog.at_level("WARNING"):
            async with service:
                with pytest.raises(LLMUnavailableError) as exc_info:
                    await service.generate_structured(
                        response_model=SampleStructuredOutput,
                        user_prompt="Generate JSON",
                    )

    # Exactly 3 transport attempts (never multiplied by outer parse retry loop)
    assert route.call_count == 3
    assert len(delays) == 2
    assert delays[1] > delays[0] > 0
    assert exc_info.value.status_code == expected_status
    assert exc_info.value.__cause__ is None
    assert TEST_API_KEY not in str(exc_info.value)
    assert TEST_API_KEY not in caplog.text


async def test_transient_recovery_plus_malformed_json_performs_single_parse_retry() -> None:
    delays: list[float] = []
    service = _build_service(transport_max_attempts=3, sleep_delays=delays)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(TEST_ENDPOINT).mock(
            side_effect=[
                httpx.Response(429, json={"error": {"message": "too many requests"}}),
                _chat_completion_response("{malformed"),
                _chat_completion_response(_valid_payload_json()),
            ],
        )

        async with service:
            result = await service.generate_structured(
                response_model=SampleStructuredOutput,
                user_prompt="Generate JSON",
            )

    assert route.call_count == 3
    assert len(delays) == 1
    assert result.grammar_score == 88


async def test_parse_retry_propagates_non_retriable_auth_error_without_masking() -> None:
    delays: list[float] = []
    service = _build_service(transport_max_attempts=3, sleep_delays=delays)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(TEST_ENDPOINT).mock(
            side_effect=[
                _chat_completion_response("{malformed"),
                httpx.Response(401, json={"error": {"message": f"revoked {TEST_API_KEY}"}}),
            ],
        )

        async with service:
            with pytest.raises(LLMAuthenticationError) as exc_info:
                await service.generate_structured(
                    response_model=SampleStructuredOutput,
                    user_prompt="Generate JSON",
                )

    assert route.call_count == 2
    assert delays == []
    assert exc_info.value.status_code == 401
    assert TEST_API_KEY not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Non-retriable errors: fast-fail without retries or API key leakage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected_exc_type"),
    [
        (400, LLMRequestError),
        (401, LLMAuthenticationError),
        (403, LLMAuthenticationError),
        (404, LLMRequestError),
        (422, LLMRequestError),
    ],
)
async def test_non_retriable_http_errors_fail_immediately_without_retries_or_key_leak(
    status_code: int,
    expected_exc_type: type[LLMError],
    caplog: pytest.LogCaptureFixture,
) -> None:
    delays: list[float] = []
    service = _build_service(transport_max_attempts=3, sleep_delays=delays)

    # Deliberately echo the API key in the upstream error body to prove zero leakage
    leaking_body = {
        "error": {
            "message": f"Upstream rejected key {TEST_API_KEY} for request",
            "type": "invalid_request_error",
        },
    }

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(TEST_ENDPOINT).mock(
            return_value=httpx.Response(status_code, json=leaking_body),
        )

        with caplog.at_level("WARNING"):
            async with service:
                with pytest.raises(expected_exc_type) as exc_info:
                    await service.generate_structured(
                        response_model=SampleStructuredOutput,
                        user_prompt="Generate JSON",
                    )

    assert route.call_count == 1
    assert delays == []
    assert getattr(exc_info.value, "status_code", None) == status_code
    assert exc_info.value.__cause__ is None
    assert TEST_API_KEY not in str(exc_info.value)
    assert TEST_API_KEY not in repr(exc_info.value)
    assert TEST_API_KEY not in caplog.text


@pytest.mark.parametrize("missing_key", [None, "", "   ", "\t\n"])
async def test_missing_or_empty_llm_api_key_fails_immediately_with_zero_http_calls(
    missing_key: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even if OPENAI_API_KEY exists in the environment, LLMService must NOT fall back to it
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-fallback-must-be-ignored")

    delays: list[float] = []
    service = _build_service(api_key=missing_key, sleep_delays=delays)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(TEST_ENDPOINT).mock(
            return_value=_chat_completion_response(_valid_payload_json()),
        )

        async with service:
            with pytest.raises(LLMConfigurationError) as exc_info:
                await service.generate_structured(
                    response_model=SampleStructuredOutput,
                    user_prompt="Generate JSON",
                )

    assert route.call_count == 0
    assert delays == []
    assert "sk-env-fallback-must-be-ignored" not in str(exc_info.value)


@pytest.mark.parametrize(
    "override_kwargs",
    [
        {"base_url": ""},
        {"base_url": "   "},
        {"model": ""},
        {"model": "   "},
    ],
)
async def test_missing_base_url_or_model_override_raises_configuration_error(
    override_kwargs: dict[str, str],
) -> None:
    settings = Settings(
        _env_file=None,
        llm_base_url=TEST_BASE_URL,
        llm_api_key=TEST_API_KEY,
        llm_model=TEST_MODEL,
    )
    service = LLMService(settings=settings, **override_kwargs)

    async with service:
        with pytest.raises(LLMConfigurationError):
            await service.generate_structured(
                response_model=SampleStructuredOutput,
                user_prompt="Generate JSON",
            )


# ---------------------------------------------------------------------------
# Argument validation edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"timeout": 0},
        {"timeout": -5.0},
        {"timeout": float("inf")},
        {"timeout": float("nan")},
        {"timeout": True},
        {"max_parse_retries": -1},
        {"max_parse_retries": True},
        {"transport_max_attempts": 0},
        {"transport_max_attempts": False},
        {"backoff_multiplier": -1.0},
        {"backoff_min_seconds": -0.5},
        {"backoff_min_seconds": 5.0, "backoff_max_seconds": 2.0},
    ],
)
def test_llm_service_rejects_invalid_numeric_configuration(bad_kwargs: dict[str, Any]) -> None:
    settings = Settings(
        _env_file=None,
        llm_base_url=TEST_BASE_URL,
        llm_api_key=TEST_API_KEY,
        llm_model=TEST_MODEL,
    )
    with pytest.raises(ValueError):
        LLMService(settings=settings, **bad_kwargs)


async def test_generate_structured_validates_response_model_and_prompt_arguments() -> None:
    service = _build_service()

    async with service:
        with pytest.raises(TypeError):
            await service.generate_structured(
                response_model=dict,  # type: ignore[arg-type]
                user_prompt="Hi",
            )

        with pytest.raises(ValueError):
            await service.generate_structured(
                response_model=SampleStructuredOutput,
            )

        with pytest.raises(ValueError):
            await service.generate_structured(
                response_model=SampleStructuredOutput,
                messages=[{"role": "user", "content": "Hi"}],
                user_prompt="Also prompt",
            )

        with pytest.raises(ValueError):
            await service.generate_structured(
                response_model=SampleStructuredOutput,
                messages=[],
            )

        with pytest.raises(ValueError):
            await service.generate_structured(
                response_model=SampleStructuredOutput,
                user_prompt="   ",
            )

        with pytest.raises(ValueError):
            await service.generate_structured(
                response_model=SampleStructuredOutput,
                user_prompt="Hi",
                max_tokens=0,
            )
