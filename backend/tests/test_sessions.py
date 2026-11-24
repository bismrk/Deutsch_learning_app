"""Unit and PostgreSQL integration tests for training sessions, hints, answers, and abandon."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_utc_now
from app.api.sessions import get_llm_service
from app.config import Settings
from app.core.auth import TELEGRAM_WEBAPP_HMAC_KEY
from app.core.fsrs_engine import review_card
from app.core.llm_service import LLMInvalidResponseError, LLMUnavailableError
from app.core.prompts import GRAMMAR_TOPICS
from app.core.sessions_service import (
    build_session_summary,
    choose_grammar_topic,
    choose_next_exercise_type,
)
from app.db.base import get_db
from app.db.models import Attempt, Deck, TrainingSession, User, UserWordFSRS, Word
from app.db.users import DEFAULT_DECK_LEVEL, DEFAULT_DECK_TITLE
from app.main import create_app
from app.schemas.exercises import (
    EXERCISE_TYPES,
    ExerciseError,
    ExerciseEvaluation,
    ExerciseType,
    GeneratedExercise,
    WordRating,
)
from app.schemas.sessions import SessionAnswerRequest, SessionStartRequest

TEST_BOT_TOKEN = "123456789:AAH_sessions_test_bot_token_secret_XYZ"
TEST_INTERNAL_API_KEY = "sessions-test-internal-api-key-987654"
FIXED_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
FIXED_NOW_TS = int(FIXED_NOW.timestamp())

USER_A_ID = 7_300_000_001
USER_B_ID = 7_300_000_002

_EXERCISE_TYPE_RE = re.compile(r"- exercise_type:\s*(\w+)")
_LEVEL_RE = re.compile(r"- level:\s*(A1|A2|B1)")
_TOPIC_RE = re.compile(r"- grammar_topic:\s*(.+)")
_WORD_IDS_RE = re.compile(r"- expected target_word_ids:\s*(\[[^\]]*\])")
_WORD_LINE_RE = re.compile(r"- id=(\d+)\s*\|\s*word_de=([^|]+?)\s*\|\s*translation_ua=")


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
    """Build internal Telegram bot authentication headers."""
    return {
        "X-Internal-API-Key": api_key,
        "X-Telegram-User-Id": str(telegram_id),
    }


@dataclass
class RecordedLLMCall:
    """Captured invocation passed into ``FakeLLMService.generate_structured``."""

    response_model: type[BaseModel]
    system_prompt: str | None
    user_prompt: str | None


@dataclass
class FakeLLMService:
    """Deterministic in-memory mock of ``LLMService`` with zero external network calls."""

    calls: list[RecordedLLMCall] = field(default_factory=list)
    generation_counter: int = 0
    evaluation_counter: int = 0
    next_errors: list[Exception] = field(default_factory=list)
    generation_errors: list[Exception] = field(default_factory=list)
    evaluation_errors: list[Exception] = field(default_factory=list)
    evaluation_script: list[dict[str, Any]] = field(default_factory=list)
    extra_rating_word_ids: list[int] = field(default_factory=list)
    custom_rating_override: int | None = None

    async def generate_structured(
        self,
        *,
        response_model: type[BaseModel],
        messages: Any = None,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> BaseModel:
        del messages, temperature, max_tokens
        self.calls.append(
            RecordedLLMCall(
                response_model=response_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            ),
        )

        if self.next_errors:
            raise self.next_errors.pop(0)

        prompt_text = user_prompt or ""

        if response_model is GeneratedExercise:
            if self.generation_errors:
                raise self.generation_errors.pop(0)
            self.generation_counter += 1
            return self._build_generated_exercise(prompt_text, self.generation_counter)

        if response_model is ExerciseEvaluation:
            if self.evaluation_errors:
                raise self.evaluation_errors.pop(0)
            self.evaluation_counter += 1
            return self._build_evaluation(prompt_text, self.evaluation_counter)

        raise AssertionError(f"Unexpected response_model: {response_model!r}")

    def _build_generated_exercise(self, prompt_text: str, index: int) -> GeneratedExercise:
        ex_type_match = _EXERCISE_TYPE_RE.search(prompt_text)
        level_match = _LEVEL_RE.search(prompt_text)
        topic_match = _TOPIC_RE.search(prompt_text)
        ids_match = _WORD_IDS_RE.search(prompt_text)

        exercise_type: ExerciseType = (
            ex_type_match.group(1)  # type: ignore[assignment]
            if ex_type_match
            else "translation"
        )
        level = level_match.group(1) if level_match else "A1"
        grammar_topic = topic_match.group(1).strip() if topic_match else "Präsens"
        word_ids = json.loads(ids_match.group(1)) if ids_match else []

        if exercise_type == "fill_blank":
            task_text = f"Der Zug kommt ___ Verspätung an ({index})."
            reference_answer = f"SECRET_REF_FILL_BLANK_{index}: Der Zug kommt mit Verspätung an."
            instruction_ua = "Заповни пропуск правильним прийменником:"
        elif exercise_type == "find_error":
            task_text = f"Gestern habe ich mit dem Zug gefahren ({index})."
            reference_answer = (
                f"SECRET_REF_FIND_ERROR_{index}: Gestern bin ich mit dem Zug gefahren."
            )
            instruction_ua = "Знайди й виправ одну граматичну помилку:"
        elif exercise_type == "combine_sentences":
            task_text = f"Es regnet stark. Wir gehen spazieren ({index}). (obwohl)"
            reference_answer = (
                f"SECRET_REF_COMBINE_{index}: Obwohl es stark regnet, gehen wir spazieren."
            )
            instruction_ua = "Об'єднай два речення в одне:"
        else:
            task_text = f"Переклади речення #{index} німецькою."
            reference_answer = f"SECRET_REF_TRANSLATION_{index}: Ich fahre heute mit dem Zug."
            instruction_ua = "Переклади речення німецькою:"

        return GeneratedExercise(
            exercise_type=exercise_type,
            level=level,  # type: ignore[arg-type]
            grammar_topic=grammar_topic,
            target_word_ids=word_ids,
            instruction_ua=instruction_ua,
            task_text=task_text,
            hint_ua=f"SECRET_HINT_UA_{index}: Зверни увагу на порядок слів і артикль.",
            reference_answer=reference_answer,
        )

    def _build_evaluation(self, prompt_text: str, index: int) -> ExerciseEvaluation:
        script_entry: dict[str, Any] = (
            self.evaluation_script[index - 1]
            if index - 1 < len(self.evaluation_script)
            else {}
        )
        parsed_words = [
            (int(match.group(1)), match.group(2).strip())
            for match in _WORD_LINE_RE.finditer(prompt_text)
        ]

        default_rating = (
            self.custom_rating_override
            if self.custom_rating_override is not None
            else script_entry.get("fsrs_rating", 4)
        )
        per_word_ratings: dict[int, int] = script_entry.get("per_word_ratings", {})

        word_ratings = [
            WordRating(
                word_id=wid,
                word_de=word_de,
                fsrs_rating=per_word_ratings.get(wid, default_rating),
                note_ua=f"Оцінка слова {word_de}.",
            )
            for wid, word_de in parsed_words
        ]

        for extra_wid in self.extra_rating_word_ids:
            word_ratings.append(
                WordRating(
                    word_id=extra_wid,
                    word_de="das Phantom",
                    fsrs_rating=4,
                    note_ua="Вигадане слово поза спробою.",
                ),
            )

        is_correct = script_entry.get("is_correct", True)
        grammar_score = script_entry.get("grammar_score", 95 if is_correct else 55)
        errors = (
            []
            if is_correct
            else [
                ExerciseError(
                    wrong_part="weil ich habe Zeit",
                    correct_part="weil ich Zeit habe",
                    explanation_ua="Після сполучника weil дієслово стоїть у кінці речення.",
                ),
            ]
        )

        return ExerciseEvaluation(
            is_correct=is_correct,
            grammar_score=grammar_score,
            corrected_answer=script_entry.get(
                "corrected_answer",
                f"Korrekte Antwort #{index}.",
            ),
            word_ratings=word_ratings,
            errors=errors,
            youtube_topic=script_entry.get(
                "youtube_topic",
                f"Граматична тема #{index} німецька",
            ),
            summary_ua=script_entry.get(
                "summary_ua",
                "Гарна робота!" if is_correct else "Є граматична помилка, варто повторити правило.",
            ),
        )


async def seed_user_with_words(
    db_session: AsyncSession,
    *,
    telegram_id: int,
    words_data: list[tuple[str, str]],
    level: str = "B1",
    exercises_per_set: int = 5,
    daily_llm_calls: int = 0,
    llm_calls_reset_date: datetime | None = None,
) -> tuple[User, list[Word]]:
    """Seed a user, personal deck, words, and FSRS cards inside the test transaction."""
    user = User(
        telegram_id=telegram_id,
        level=level,
        exercises_per_set=exercises_per_set,
        daily_llm_calls=daily_llm_calls,
        llm_calls_reset_date=(
            llm_calls_reset_date.date() if llm_calls_reset_date is not None else None
        ),
        created_at=FIXED_NOW,
    )
    db_session.add(user)
    await db_session.flush()

    deck = Deck(
        owner_id=telegram_id,
        title=DEFAULT_DECK_TITLE,
        level=DEFAULT_DECK_LEVEL,
    )
    db_session.add(deck)
    await db_session.flush()

    words: list[Word] = []
    for idx, (word_de, translation_ua) in enumerate(words_data):
        word = Word(
            deck_id=deck.id,
            word_de=word_de,
            translation_ua=translation_ua,
            part_of_speech="noun",
            source="text",
        )
        db_session.add(word)
        await db_session.flush()
        db_session.add(
            UserWordFSRS(
                user_id=telegram_id,
                word_id=word.id,
                state=0,
                stability=0.0,
                difficulty=0.0,
                due_at=FIXED_NOW - timedelta(minutes=len(words_data) - idx),
                reps=0,
                lapses=0,
                last_review=None,
            ),
        )
        words.append(word)

    await db_session.commit()
    return user, words


def _create_sessions_test_app(
    db_session: AsyncSession,
    fake_llm: FakeLLMService,
    *,
    daily_llm_limit: int = 50,
    now_provider: Callable[[], datetime] | None = None,
) -> Any:
    test_app = create_app(
        settings=Settings(
            _env_file=None,
            app_env="test",
            database_url=None,
            bot_token=TEST_BOT_TOKEN,
            internal_api_key=TEST_INTERNAL_API_KEY,
            daily_llm_limit=daily_llm_limit,
        ),
    )

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_utc_now] = now_provider or (lambda: FIXED_NOW)
    test_app.dependency_overrides[get_llm_service] = lambda: fake_llm
    return test_app


@pytest.fixture
def fake_llm() -> FakeLLMService:
    """Provide a fresh deterministic FakeLLMService for each test."""
    return FakeLLMService()


@pytest.fixture
async def sessions_client(
    db_session: AsyncSession,
    fake_llm: FakeLLMService,
) -> AsyncIterator[AsyncClient]:
    """ASGI AsyncClient wired to isolated PostgreSQL test session and mocked LLMService."""
    test_app = _create_sessions_test_app(db_session, fake_llm)
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# Unit tests: exercise alternation, grammar topics, summary, request schemas
# ---------------------------------------------------------------------------


def test_choose_next_exercise_type_evenly_alternates_without_repeats() -> None:
    history: list[str] = []
    for _ in range(8):
        nxt = choose_next_exercise_type(history)
        if history:
            assert nxt != history[-1]
        history.append(nxt)

    assert history[:4] == list(EXERCISE_TYPES)
    assert history[4:8] == list(EXERCISE_TYPES)

    # 5-exercise set covers all 4 types with counts differing by at most 1
    five = history[:5]
    counts = {ex_type: five.count(ex_type) for ex_type in EXERCISE_TYPES}
    assert set(counts.keys()) == set(EXERCISE_TYPES)
    assert max(counts.values()) - min(counts.values()) == 1


def test_choose_next_exercise_type_handles_skewed_history() -> None:
    nxt = choose_next_exercise_type(["translation", "translation", "fill_blank"])
    assert nxt in {"find_error", "combine_sentences"}
    assert nxt != "fill_blank"


def test_choose_grammar_topic_mix_and_topic_modes() -> None:
    assert (
        choose_grammar_topic(
            level="A2",
            mode="topic",
            target_topic="  Perfekt mit haben und sein  ",
            previous_topics=["Perfekt mit haben und sein"],
        )
        == "Perfekt mit haben und sein"
    )

    with pytest.raises(ValueError, match="target_topic"):
        choose_grammar_topic(level="A1", mode="topic", target_topic="   ", previous_topics=[])

    used_topics: list[str] = []
    catalog_a1 = GRAMMAR_TOPICS["A1"]
    for _ in range(len(catalog_a1) + 2):
        topic = choose_grammar_topic(
            level="A1",
            mode="mix",
            target_topic=None,
            previous_topics=used_topics,
        )
        if used_topics:
            assert topic != used_topics[-1]
        used_topics.append(topic)

    assert set(used_topics[: len(catalog_a1)]) == set(catalog_a1)


def test_build_session_summary_aggregates_metrics_and_weak_topics() -> None:
    def _make_attempt(topic: str, is_correct: bool, score: int) -> Attempt:
        return Attempt(
            session_id=1,
            user_id=USER_A_ID,
            word_ids=[1],
            exercise_type="translation",
            level="B1",
            grammar_topic=topic,
            question_payload={},
            user_answer="ans",
            used_hint=False,
            is_correct=is_correct,
            grammar_score=score,
            fsrs_ratings=[],
            youtube_topic="yt",
            created_at=FIXED_NOW,
        )

    perfect = build_session_summary(
        [
            _make_attempt("Topic A", True, 100),
            _make_attempt("Topic B", True, 90),
        ],
    )
    assert perfect.total_exercises == 2
    assert perfect.correct_count == 2
    assert perfect.avg_grammar_score == 95.0
    assert perfect.weak_topics == []

    mixed = build_session_summary(
        [
            _make_attempt("Topic Strong", True, 95),
            _make_attempt("Topic Medium", False, 70),
            _make_attempt("Topic Worst", False, 30),
            _make_attempt("Topic Weak Low", True, 65),
            _make_attempt("Topic Fourth Weak", False, 75),
        ],
    )
    assert mixed.total_exercises == 5
    assert mixed.correct_count == 2
    assert mixed.avg_grammar_score == 67.0
    assert mixed.weak_topics == ["Topic Worst", "Topic Weak Low", "Topic Medium"]


def test_session_request_schema_validations() -> None:
    default_req = SessionStartRequest()
    assert default_req.mode == "mix"
    assert default_req.target_topic is None
    assert default_req.total_exercises is None

    topic_req = SessionStartRequest(mode="topic", target_topic="  Passiv  ", total_exercises=10)
    assert topic_req.target_topic == "Passiv"
    assert topic_req.total_exercises == 10

    with pytest.raises(ValidationError):
        SessionStartRequest(mode="topic")
    with pytest.raises(ValidationError):
        SessionStartRequest(mode="topic", target_topic="   ")
    with pytest.raises(ValidationError):
        SessionStartRequest(total_exercises=0)
    with pytest.raises(ValidationError):
        SessionStartRequest(total_exercises=31)
    with pytest.raises(ValidationError):
        SessionStartRequest(total_exercises=True)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        SessionAnswerRequest(user_answer="")
    with pytest.raises(ValidationError):
        SessionAnswerRequest(user_answer="   \n\t ")
    assert SessionAnswerRequest(user_answer="  Ich komme mit. ").user_answer == "Ich komme mit."


# ---------------------------------------------------------------------------
# PostgreSQL integration tests: auth & start preconditions
# ---------------------------------------------------------------------------


async def test_all_session_endpoints_require_auth(sessions_client: AsyncClient) -> None:
    endpoints = [
        ("POST", "/api/sessions/start", {}),
        ("GET", "/api/sessions/active", None),
        ("POST", "/api/sessions/1/hint", None),
        ("POST", "/api/sessions/1/answer", {"user_answer": "Ich bin da."}),
        ("POST", "/api/sessions/1/abandon", None),
    ]
    for method, url, body in endpoints:
        resp = await sessions_client.request(method, url, json=body)
        assert resp.status_code == 401, f"{method} {url} should return 401 without auth"

        bad_resp = await sessions_client.request(
            method,
            url,
            json=body,
            headers=build_bot_headers(USER_A_ID, api_key="wrong-key"),
        )
        assert bad_resp.status_code == 401, f"{method} {url} should return 401 on wrong key"


async def test_start_empty_dictionary_returns_400_without_quota_or_session(
    sessions_client: AsyncClient,
    db_session: AsyncSession,
    fake_llm: FakeLLMService,
) -> None:
    await seed_user_with_words(db_session, telegram_id=USER_A_ID, words_data=[])

    resp = await sessions_client.post(
        "/api/sessions/start",
        json={},
        headers=build_tma_headers(USER_A_ID),
    )
    assert resp.status_code == 400
    assert len(fake_llm.calls) == 0

    user = (
        await db_session.execute(select(User).where(User.telegram_id == USER_A_ID))
    ).scalar_one()
    assert user.daily_llm_calls == 0

    sessions_rows = (
        await db_session.execute(
            select(TrainingSession).where(TrainingSession.user_id == USER_A_ID),
        )
    ).scalars().all()
    assert sessions_rows == []


async def test_start_validation_errors_and_conflict_on_existing_active_session(
    sessions_client: AsyncClient,
    db_session: AsyncSession,
    fake_llm: FakeLLMService,
) -> None:
    await seed_user_with_words(
        db_session,
        telegram_id=USER_A_ID,
        words_data=[("der Zug", "потяг"), ("die Zeit", "час")],
        exercises_per_set=4,
    )
    headers = build_bot_headers(USER_A_ID)

    # mode="topic" without target_topic -> 422, no LLM call
    resp_missing = await sessions_client.post(
        "/api/sessions/start",
        json={"mode": "topic"},
        headers=headers,
    )
    assert resp_missing.status_code == 422

    resp_blank = await sessions_client.post(
        "/api/sessions/start",
        json={"mode": "topic", "target_topic": "   "},
        headers=headers,
    )
    assert resp_blank.status_code == 422

    resp_oob = await sessions_client.post(
        "/api/sessions/start",
        json={"total_exercises": 31},
        headers=headers,
    )
    assert resp_oob.status_code == 422
    assert len(fake_llm.calls) == 0

    # Valid start with default total_exercises from user.exercises_per_set (4)
    resp_start = await sessions_client.post("/api/sessions/start", headers=headers)
    assert resp_start.status_code == 201
    start_data = resp_start.json()
    assert start_data["total_exercises"] == 4
    assert start_data["mode"] == "mix"
    assert start_data["target_topic"] is None
    assert start_data["status"] == "active"
    assert start_data["completed_exercises"] == 0
    assert start_data["current_exercise_number"] == 1
    assert len(fake_llm.calls) == 1

    # Second start while active -> 409 without calling LLM or incrementing quota
    resp_conflict = await sessions_client.post("/api/sessions/start", json={}, headers=headers)
    assert resp_conflict.status_code == 409
    assert len(fake_llm.calls) == 1

    user = (
        await db_session.execute(select(User).where(User.telegram_id == USER_A_ID))
    ).scalar_one()
    assert user.daily_llm_calls == 1


# ---------------------------------------------------------------------------
# PostgreSQL integration tests: full lifecycle, alternation, small dictionary
# ---------------------------------------------------------------------------


async def test_full_5_exercise_lifecycle_alternation_hiding_and_summary(
    sessions_client: AsyncClient,
    db_session: AsyncSession,
    fake_llm: FakeLLMService,
) -> None:
    vocab = [
        ("der Zug", "потяг"),
        ("die Verspätung", "затримка"),
        ("das Treffen", "зустріч"),
        ("der Bahnhof", "вокзал"),
        ("die Fahrkarte", "квиток"),
        ("der Koffer", "валіза"),
        ("die Reise", "подорож"),
        ("das Hotel", "готель"),
        ("der Flug", "переліт"),
        ("die Stadt", "місто"),
    ]
    _, seeded_words = await seed_user_with_words(
        db_session,
        telegram_id=USER_A_ID,
        words_data=vocab,
        level="B1",
        exercises_per_set=5,
    )
    headers = build_tma_headers(USER_A_ID)

    fake_llm.evaluation_script = [
        {"is_correct": True, "grammar_score": 100, "fsrs_rating": 4},
        {"is_correct": False, "grammar_score": 50, "fsrs_rating": 2},
        {"is_correct": True, "grammar_score": 90, "fsrs_rating": 3},
        {"is_correct": False, "grammar_score": 60, "fsrs_rating": 2},
        {"is_correct": True, "grammar_score": 85, "fsrs_rating": 4},
    ]

    # 1. Start session
    start_resp = await sessions_client.post("/api/sessions/start", json={}, headers=headers)
    assert start_resp.status_code == 201
    start_body = start_resp.json()
    session_id = start_body["session_id"]

    # Secrets must never leak in start response
    assert "reference_answer" not in start_body["exercise"]
    assert "hint_ua" not in start_body["exercise"]
    assert "hint_ua" not in start_body
    assert "SECRET_REF_" not in start_resp.text
    assert "SECRET_HINT_UA_" not in start_resp.text

    # DB attempt stores full payload including hidden reference_answer & hint_ua
    first_attempt_db = (
        await db_session.execute(
            select(Attempt).where(Attempt.id == start_body["attempt_id"]),
        )
    ).scalar_one()
    assert "reference_answer" in first_attempt_db.question_payload
    assert "hint_ua" in first_attempt_db.question_payload
    assert first_attempt_db.user_answer is None

    exercise_types: list[str] = [start_body["exercise"]["exercise_type"]]
    exercise_topics: list[str] = [start_body["exercise"]["grammar_topic"]]
    word_batches: list[list[int]] = [list(start_body["exercise"]["target_word_ids"])]

    # 2. Answer all 5 exercises
    last_answer_data: dict[str, Any] = {}
    for step in range(1, 6):
        ans_resp = await sessions_client.post(
            f"/api/sessions/{session_id}/answer",
            json={"user_answer": f"  Meine Antwort Nummer {step}.  "},
            headers=headers,
        )
        assert ans_resp.status_code == 200
        ans_data = ans_resp.json()
        last_answer_data = ans_data
        assert ans_data["completed_exercises"] == step
        assert ans_data["total_exercises"] == 5

        if step < 5:
            assert ans_data["status"] == "active"
            assert ans_data["summary"] is None
            assert ans_data["next_exercise"] is not None
            assert ans_data["next_attempt_id"] is not None
            assert "reference_answer" not in ans_data["next_exercise"]
            assert "hint_ua" not in ans_data["next_exercise"]
            assert "SECRET_REF_" not in json.dumps(ans_data["next_exercise"])
            assert "SECRET_HINT_UA_" not in json.dumps(ans_data["next_exercise"])

            exercise_types.append(ans_data["next_exercise"]["exercise_type"])
            exercise_topics.append(ans_data["next_exercise"]["grammar_topic"])
            word_batches.append(list(ans_data["next_exercise"]["target_word_ids"]))
        else:
            assert ans_data["status"] == "completed"
            assert ans_data["next_exercise"] is None
            assert ans_data["next_attempt_id"] is None
            assert ans_data["summary"] is not None

    # Verify all 4 exercise types alternated evenly without consecutive repeats
    assert set(exercise_types) == set(EXERCISE_TYPES)
    for prev_type, curr_type in zip(exercise_types, exercise_types[1:], strict=False):
        assert prev_type != curr_type
    type_counts = {t: exercise_types.count(t) for t in EXERCISE_TYPES}
    assert max(type_counts.values()) - min(type_counts.values()) <= 1

    # First 4 exercises cover all 10 words without repeats (3 + 3 + 3 + 1), 5th falls back
    assert len(word_batches[0]) == 3
    assert len(word_batches[1]) == 3
    assert len(word_batches[2]) == 3
    assert len(word_batches[3]) == 1
    first_four_union = set().union(*word_batches[:4])
    assert first_four_union == {w.id for w in seeded_words}
    assert len(word_batches[4]) == 3  # Fallback reused full dictionary

    # Verify summary accuracy
    summary = last_answer_data["summary"]
    assert summary["total_exercises"] == 5
    assert summary["correct_count"] == 3
    assert summary["avg_grammar_score"] == 77.0  # (100+50+90+60+85)/5 = 77.0
    # Step 2 (score 50) was worse than step 4 (score 60)
    assert summary["weak_topics"] == [exercise_topics[1], exercise_topics[3]]

    # Exact LLM call count: 1 start + 4*(eval+gen) + 1 final eval = 10
    assert len(fake_llm.calls) == 10

    # Active session is now gone (404)
    active_after = await sessions_client.get("/api/sessions/active", headers=headers)
    assert active_after.status_code == 404

    # All FSRS cards were reviewed at least once
    fsrs_rows = (
        await db_session.execute(
            select(UserWordFSRS).where(UserWordFSRS.user_id == USER_A_ID),
        )
    ).scalars().all()
    assert all(card.reps >= 1 for card in fsrs_rows)
    assert all(card.last_review == FIXED_NOW for card in fsrs_rows)

    # User can immediately start a brand-new session after completing the previous one
    restart_resp = await sessions_client.post(
        "/api/sessions/start",
        json={"mode": "topic", "target_topic": "Nebensätze mit weil", "total_exercises": 1},
        headers=headers,
    )
    assert restart_resp.status_code == 201
    assert restart_resp.json()["mode"] == "topic"
    assert restart_resp.json()["target_topic"] == "Nebensätze mit weil"
    assert restart_resp.json()["exercise"]["grammar_topic"] == "Nebensätze mit weil"


async def test_single_exercise_and_small_dictionary_fallback(
    sessions_client: AsyncClient,
    db_session: AsyncSession,
    fake_llm: FakeLLMService,
) -> None:
    # User A has only ONE word in dictionary and runs a 3-exercise topic set
    _, words_a = await seed_user_with_words(
        db_session,
        telegram_id=USER_A_ID,
        words_data=[("der Apfel", "яблуко")],
        level="A1",
    )
    headers_a = build_bot_headers(USER_A_ID)

    start_resp = await sessions_client.post(
        "/api/sessions/start",
        json={
            "mode": "topic",
            "target_topic": "Akkusativ nach Verben",
            "total_exercises": 3,
        },
        headers=headers_a,
    )
    assert start_resp.status_code == 201
    session_id = start_resp.json()["session_id"]
    assert start_resp.json()["exercise"]["target_word_ids"] == [words_a[0].id]

    for step in range(1, 4):
        ans_resp = await sessions_client.post(
            f"/api/sessions/{session_id}/answer",
            json={"user_answer": f"Ich esse einen Apfel ({step})."},
            headers=headers_a,
        )
        assert ans_resp.status_code == 200
        body = ans_resp.json()
        if step < 3:
            # Fallback kept picking the single word and preserved custom target_topic
            assert body["next_exercise"]["target_word_ids"] == [words_a[0].id]
            assert body["next_exercise"]["grammar_topic"] == "Akkusativ nach Verben"
        else:
            assert body["status"] == "completed"
            assert body["summary"]["total_exercises"] == 3

    # User B runs a 1-exercise set (only 1 gen + 1 eval = 2 LLM calls)
    await seed_user_with_words(
        db_session,
        telegram_id=USER_B_ID,
        words_data=[("das Buch", "книга"), ("der Tisch", "стіл")],
        level="A2",
    )
    calls_before = len(fake_llm.calls)
    headers_b = build_tma_headers(USER_B_ID)
    start_b = await sessions_client.post(
        "/api/sessions/start",
        json={"total_exercises": 1},
        headers=headers_b,
    )
    assert start_b.status_code == 201
    ans_b = await sessions_client.post(
        f"/api/sessions/{start_b.json()['session_id']}/answer",
        json={"user_answer": "Das Buch liegt auf dem Tisch."},
        headers=headers_b,
    )
    assert ans_b.status_code == 200
    assert ans_b.json()["status"] == "completed"
    assert ans_b.json()["next_exercise"] is None
    assert ans_b.json()["summary"]["total_exercises"] == 1
    assert len(fake_llm.calls) - calls_before == 2


# ---------------------------------------------------------------------------
# PostgreSQL integration tests: active session, hint, anti-cheat min(rating, 2)
# ---------------------------------------------------------------------------


async def test_active_session_restoration_and_hint_visibility(
    sessions_client: AsyncClient,
    db_session: AsyncSession,
    fake_llm: FakeLLMService,
) -> None:
    await seed_user_with_words(
        db_session,
        telegram_id=USER_A_ID,
        words_data=[("der Hund", "собака"), ("die Katze", "кіт")],
    )
    headers = build_tma_headers(USER_A_ID)

    # No active session -> 404
    assert (await sessions_client.get("/api/sessions/active", headers=headers)).status_code == 404

    start_resp = await sessions_client.post(
        "/api/sessions/start",
        json={"total_exercises": 2},
        headers=headers,
    )
    assert start_resp.status_code == 201
    session_id = start_resp.json()["session_id"]

    # Active before hint: no reference_answer and no hint_ua
    active_before = await sessions_client.get("/api/sessions/active", headers=headers)
    assert active_before.status_code == 200
    before_json = active_before.json()
    assert before_json["session_id"] == session_id
    assert before_json["used_hint"] is False
    assert "hint_ua" not in before_json
    assert "reference_answer" not in before_json["exercise"]
    assert "hint_ua" not in before_json["exercise"]
    assert "SECRET_REF_" not in active_before.text
    assert "SECRET_HINT_UA_" not in active_before.text

    # Request hint (idempotent, 0 LLM calls, no reference_answer)
    calls_before_hint = len(fake_llm.calls)
    hint_1 = await sessions_client.post(f"/api/sessions/{session_id}/hint", headers=headers)
    assert hint_1.status_code == 200
    assert hint_1.json() == {
        "hint_ua": "SECRET_HINT_UA_1: Зверни увагу на порядок слів і артикль.",
        "used_hint": True,
    }
    assert "reference_answer" not in hint_1.text
    assert "SECRET_REF_" not in hint_1.text

    hint_2 = await sessions_client.post(f"/api/sessions/{session_id}/hint", headers=headers)
    assert hint_2.status_code == 200
    assert hint_2.json() == hint_1.json()
    assert len(fake_llm.calls) == calls_before_hint

    # Active after hint: hint_ua is included, reference_answer STILL hidden
    active_after = await sessions_client.get("/api/sessions/active", headers=headers)
    assert active_after.status_code == 200
    after_json = active_after.json()
    assert after_json["used_hint"] is True
    assert after_json["hint_ua"] == hint_1.json()["hint_ua"]
    assert "reference_answer" not in after_json["exercise"]
    assert "SECRET_REF_" not in active_after.text

    # Answer exercise 1 -> active now shows exercise 2 with used_hint=False and no hint_ua
    ans_1 = await sessions_client.post(
        f"/api/sessions/{session_id}/answer",
        json={"user_answer": "Der Hund spielt."},
        headers=headers,
    )
    assert ans_1.status_code == 200

    active_ex2 = await sessions_client.get("/api/sessions/active", headers=headers)
    assert active_ex2.status_code == 200
    ex2_json = active_ex2.json()
    assert ex2_json["completed_exercises"] == 1
    assert ex2_json["current_exercise_number"] == 2
    assert ex2_json["used_hint"] is False
    assert "hint_ua" not in ex2_json
    assert "SECRET_HINT_UA_" not in active_ex2.text
    assert "SECRET_REF_" not in active_ex2.text


async def test_hint_backend_anticheat_caps_fsrs_rating_at_hard_2(
    sessions_client: AsyncClient,
    db_session: AsyncSession,
    fake_llm: FakeLLMService,
) -> None:
    _, words = await seed_user_with_words(
        db_session,
        telegram_id=USER_A_ID,
        words_data=[("der Bahnhof", "вокзал"), ("die Reise", "подорож")],
    )
    headers = build_bot_headers(USER_A_ID)

    # Start 2-exercise set; exercise 1 uses hint with ratings 4 and 1, exercise 2 has no hint
    start_resp = await sessions_client.post(
        "/api/sessions/start",
        json={"total_exercises": 2},
        headers=headers,
    )
    assert start_resp.status_code == 201
    session_id = start_resp.json()["session_id"]
    attempt_1_id = start_resp.json()["attempt_id"]
    word_1_id, word_2_id = words[0].id, words[1].id

    # Request hint -> sets used_hint=True in DB
    hint_resp = await sessions_client.post(f"/api/sessions/{session_id}/hint", headers=headers)
    assert hint_resp.status_code == 200

    # LLM awards rating=4 (Easy) to word_1 and rating=1 (Again) to word_2, plus hallucinated id
    fake_llm.evaluation_script = [
        {
            "is_correct": True,
            "grammar_score": 100,
            "per_word_ratings": {word_1_id: 4, word_2_id: 1},
        },
    ]
    fake_llm.extra_rating_word_ids = [999_999]

    ans_resp = await sessions_client.post(
        f"/api/sessions/{session_id}/answer",
        json={"user_answer": "Der Bahnhof ist groß."},
        headers=headers,
    )
    assert ans_resp.status_code == 200
    ans_data = ans_resp.json()
    assert ans_data["used_hint"] is True

    # Effective ratings in API response: word_1 capped 4 -> 2, word_2 stays 1, phantom stripped
    ratings_by_id = {
        item["word_id"]: item["fsrs_rating"]
        for item in ans_data["evaluation"]["word_ratings"]
    }
    assert ratings_by_id == {word_1_id: 2, word_2_id: 1}

    # Effective ratings persisted in attempts.fsrs_ratings
    attempt_row = (
        await db_session.execute(select(Attempt).where(Attempt.id == attempt_1_id))
    ).scalar_one()
    assert attempt_row.used_hint is True
    assert isinstance(attempt_row.fsrs_ratings, list)
    saved_ratings = {item["word_id"]: item["fsrs_rating"] for item in attempt_row.fsrs_ratings}
    assert saved_ratings == {word_1_id: 2, word_2_id: 1}

    # Verify user_word_fsrs for word_1 matches exact FSRS Hard (2), NOT Easy (4)
    card_1 = (
        await db_session.execute(
            select(UserWordFSRS).where(
                UserWordFSRS.user_id == USER_A_ID,
                UserWordFSRS.word_id == word_1_id,
            ),
        )
    ).scalar_one()

    expected_hard = review_card(
        UserWordFSRS(
            user_id=USER_A_ID,
            word_id=word_1_id,
            state=0,
            stability=0.0,
            difficulty=0.0,
            due_at=FIXED_NOW,
            reps=0,
            lapses=0,
            last_review=None,
        ),
        rating=2,
        used_hint=False,
        now=FIXED_NOW,
    )
    unrestricted_easy = review_card(
        UserWordFSRS(
            user_id=USER_A_ID,
            word_id=word_1_id,
            state=0,
            stability=0.0,
            difficulty=0.0,
            due_at=FIXED_NOW,
            reps=0,
            lapses=0,
            last_review=None,
        ),
        rating=4,
        used_hint=False,
        now=FIXED_NOW,
    )
    assert card_1.state == expected_hard.state
    assert card_1.stability == pytest.approx(expected_hard.stability)
    assert card_1.difficulty == pytest.approx(expected_hard.difficulty)
    assert card_1.due_at == expected_hard.due_at
    assert card_1.reps == 1
    assert card_1.stability != pytest.approx(unrestricted_easy.stability)


# ---------------------------------------------------------------------------
# PostgreSQL integration tests: abandon, closed sessions, invalid inputs
# ---------------------------------------------------------------------------


async def test_abandon_idempotency_and_closed_session_409_guards(
    sessions_client: AsyncClient,
    db_session: AsyncSession,
    fake_llm: FakeLLMService,
) -> None:
    await seed_user_with_words(
        db_session,
        telegram_id=USER_A_ID,
        words_data=[("das Haus", "будинок")],
    )
    headers = build_tma_headers(USER_A_ID)

    # 1. Start and abandon an active session
    start_1 = await sessions_client.post(
        "/api/sessions/start",
        json={"total_exercises": 3},
        headers=headers,
    )
    assert start_1.status_code == 201
    sid_1 = start_1.json()["session_id"]

    abandon_1 = await sessions_client.post(f"/api/sessions/{sid_1}/abandon", headers=headers)
    assert abandon_1.status_code == 200
    assert abandon_1.json() == {
        "session_id": sid_1,
        "status": "abandoned",
        "completed_exercises": 0,
        "total_exercises": 3,
    }

    # Idempotent abandon on already abandoned session -> 200
    abandon_again = await sessions_client.post(f"/api/sessions/{sid_1}/abandon", headers=headers)
    assert abandon_again.status_code == 200
    assert abandon_again.json()["status"] == "abandoned"

    # Active session cleared -> 404
    assert (await sessions_client.get("/api/sessions/active", headers=headers)).status_code == 404

    # Hint and answer on abandoned session -> 409
    assert (
        await sessions_client.post(f"/api/sessions/{sid_1}/hint", headers=headers)
    ).status_code == 409
    assert (
        await sessions_client.post(
            f"/api/sessions/{sid_1}/answer",
            json={"user_answer": "Das Haus."},
            headers=headers,
        )
    ).status_code == 409

    # 2. Start a 1-exercise session, complete it, then test 409 on abandon/hint/answer
    start_2 = await sessions_client.post(
        "/api/sessions/start",
        json={"total_exercises": 1},
        headers=headers,
    )
    assert start_2.status_code == 201
    sid_2 = start_2.json()["session_id"]

    # Blank / whitespace user_answer rejected with 422 before touching LLM
    calls_before_422 = len(fake_llm.calls)
    assert (
        await sessions_client.post(
            f"/api/sessions/{sid_2}/answer",
            json={"user_answer": "   "},
            headers=headers,
        )
    ).status_code == 422
    assert len(fake_llm.calls) == calls_before_422

    # Complete session 2
    ans_2 = await sessions_client.post(
        f"/api/sessions/{sid_2}/answer",
        json={"user_answer": "Das Haus ist groß."},
        headers=headers,
    )
    assert ans_2.status_code == 200
    assert ans_2.json()["status"] == "completed"

    # Abandon, hint, and answer on completed session -> 409
    assert (
        await sessions_client.post(f"/api/sessions/{sid_2}/abandon", headers=headers)
    ).status_code == 409
    assert (
        await sessions_client.post(f"/api/sessions/{sid_2}/hint", headers=headers)
    ).status_code == 409
    assert (
        await sessions_client.post(
            f"/api/sessions/{sid_2}/answer",
            json={"user_answer": "Noch einmal."},
            headers=headers,
        )
    ).status_code == 409


# ---------------------------------------------------------------------------
# PostgreSQL integration tests: quota 429 & transactional LLM failure rollback
# ---------------------------------------------------------------------------


async def test_daily_llm_quota_429_on_start_and_answer_without_losing_progress(
    db_session: AsyncSession,
    fake_llm: FakeLLMService,
) -> None:
    await seed_user_with_words(
        db_session,
        telegram_id=USER_A_ID,
        words_data=[("der Tisch", "стіл"), ("der Stuhl", "стілець")],
        daily_llm_calls=2,
        llm_calls_reset_date=FIXED_NOW,
    )
    # Configure strict daily_llm_limit = 2 (user already at 2/2)
    test_app = _create_sessions_test_app(db_session, fake_llm, daily_llm_limit=2)
    headers = build_bot_headers(USER_A_ID)

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        # 1. Start fails with 429 when quota exhausted, leaving 0 sessions in DB
        resp_start_429 = await client.post(
            "/api/sessions/start",
            json={"total_exercises": 2},
            headers=headers,
        )
        assert resp_start_429.status_code == 429
        assert len(fake_llm.calls) == 0
        sessions_count = (
            await db_session.execute(
                select(TrainingSession).where(TrainingSession.user_id == USER_A_ID),
            )
        ).scalars().all()
        assert sessions_count == []

    # 2. Now allow daily_llm_limit = 2 starting from 0 calls:
    # start takes 1 call (1/2); answer #1 on a 2-exercise set needs 2 calls -> 429 rollback!
    user = (
        await db_session.execute(select(User).where(User.telegram_id == USER_A_ID))
    ).scalar_one()
    user.daily_llm_calls = 0
    await db_session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        resp_start = await client.post(
            "/api/sessions/start",
            json={"total_exercises": 2},
            headers=headers,
        )
        assert resp_start.status_code == 201
        session_id = resp_start.json()["session_id"]
        attempt_id = resp_start.json()["attempt_id"]
        assert len(fake_llm.calls) == 1

        # Non-final answer requires 2 LLM calls (eval + next gen), but only 1 remains -> 429
        resp_ans_429 = await client.post(
            f"/api/sessions/{session_id}/answer",
            json={"user_answer": "Der Tisch ist neu."},
            headers=headers,
        )
        assert resp_ans_429.status_code == 429
        assert len(fake_llm.calls) == 1  # No wasted LLM call

        # Verify rollback: quota remains 1, attempt remains unfinished, active session intact
        refreshed_user = (
            await db_session.execute(
                select(User)
                .where(User.telegram_id == USER_A_ID)
                .execution_options(populate_existing=True),
            )
        ).scalar_one()
        assert refreshed_user.daily_llm_calls == 1

        refreshed_attempt = (
            await db_session.execute(
                select(Attempt)
                .where(Attempt.id == attempt_id)
                .execution_options(populate_existing=True),
            )
        ).scalar_one()
        assert refreshed_attempt.user_answer is None

        active_resp = await client.get("/api/sessions/active", headers=headers)
        assert active_resp.status_code == 200
        assert active_resp.json()["attempt_id"] == attempt_id

    # 3. Single remaining quota call IS enough for a 1-exercise final answer (eval only)
    await seed_user_with_words(
        db_session,
        telegram_id=USER_B_ID,
        words_data=[("der Stuhl", "стілець")],
        daily_llm_calls=0,
        llm_calls_reset_date=FIXED_NOW,
    )
    headers_b = build_bot_headers(USER_B_ID)
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        start_b = await client.post(
            "/api/sessions/start",
            json={"total_exercises": 1},
            headers=headers_b,
        )
        assert start_b.status_code == 201  # Uses 1/2 quota
        ans_b = await client.post(
            f"/api/sessions/{start_b.json()['session_id']}/answer",
            json={"user_answer": "Der Stuhl ist bequem."},
            headers=headers_b,
        )
        assert ans_b.status_code == 200  # Uses 2/2 quota (final exercise needs only 1 call)
        assert ans_b.json()["status"] == "completed"


async def test_llm_failures_rollback_cleanly_preserving_transactional_invariant(
    sessions_client: AsyncClient,
    db_session: AsyncSession,
    fake_llm: FakeLLMService,
) -> None:
    _, words = await seed_user_with_words(
        db_session,
        telegram_id=USER_A_ID,
        words_data=[("das Fenster", "вікно"), ("die Tür", "двері")],
    )
    seeded_word_ids = [w.id for w in words]
    headers = build_tma_headers(USER_A_ID)

    # 1. LLMUnavailableError on start -> 503, no session/attempt, quota rolled back to 0
    fake_llm.generation_errors.append(LLMUnavailableError("Provider timeout"))
    resp_503 = await sessions_client.post(
        "/api/sessions/start",
        json={"total_exercises": 2},
        headers=headers,
    )
    assert resp_503.status_code == 503
    assert (
        await db_session.execute(
            select(TrainingSession).where(TrainingSession.user_id == USER_A_ID),
        )
    ).scalars().all() == []
    user_after_503 = (
        await db_session.execute(
            select(User)
            .where(User.telegram_id == USER_A_ID)
            .execution_options(populate_existing=True),
        )
    ).scalar_one()
    assert user_after_503.daily_llm_calls == 0

    # 2. LLMInvalidResponseError on start -> 502, no session/attempt, quota rolled back to 0
    fake_llm.generation_errors.append(LLMInvalidResponseError("Malformed JSON"))
    resp_502 = await sessions_client.post(
        "/api/sessions/start",
        json={"total_exercises": 2},
        headers=headers,
    )
    assert resp_502.status_code == 502
    assert (
        await db_session.execute(
            select(TrainingSession).where(TrainingSession.user_id == USER_A_ID),
        )
    ).scalars().all() == []

    # 3. Valid start succeeds (1 LLM call used)
    start_resp = await sessions_client.post(
        "/api/sessions/start",
        json={"total_exercises": 2},
        headers=headers,
    )
    assert start_resp.status_code == 201
    session_id = start_resp.json()["session_id"]
    attempt_1_id = start_resp.json()["attempt_id"]

    # 4. Evaluation itself fails with LLMUnavailableError -> 503 with full rollback
    fake_llm.evaluation_errors.append(LLMUnavailableError("Provider 503 on evaluation"))
    fail_eval_503 = await sessions_client.post(
        f"/api/sessions/{session_id}/answer",
        json={"user_answer": "Das Fenster ist offen."},
        headers=headers,
    )
    assert fail_eval_503.status_code == 503

    # 5. Evaluation succeeds, but NEXT-exercise generation fails with LLMUnavailableError -> 503!
    # The entire transaction MUST roll back so no active session lacks an unfinished attempt,
    # and FSRS cards are NOT mutated prematurely.
    fake_llm.generation_errors.append(LLMUnavailableError("Downstream 503 on next exercise"))
    fail_next_503 = await sessions_client.post(
        f"/api/sessions/{session_id}/answer",
        json={"user_answer": "Das Fenster ist offen."},
        headers=headers,
    )
    assert fail_next_503.status_code == 503

    # Also verify LLMInvalidResponseError on next-exercise generation -> 502 with full rollback
    fake_llm.generation_errors.append(LLMInvalidResponseError("Invalid JSON on next exercise"))
    fail_next_502 = await sessions_client.post(
        f"/api/sessions/{session_id}/answer",
        json={"user_answer": "Das Fenster ist offen."},
        headers=headers,
    )
    assert fail_next_502.status_code == 502

    # Check DB invariants after all rollbacks:
    attempts_after = (
        await db_session.execute(
            select(Attempt)
            .where(Attempt.session_id == session_id)
            .execution_options(populate_existing=True),
        )
    ).scalars().all()
    assert len(attempts_after) == 1
    assert attempts_after[0].id == attempt_1_id
    assert attempts_after[0].user_answer is None
    assert attempts_after[0].is_correct is None
    assert attempts_after[0].fsrs_ratings is None

    cards_after = (
        await db_session.execute(
            select(UserWordFSRS)
            .where(
                UserWordFSRS.user_id == USER_A_ID,
                UserWordFSRS.word_id.in_(seeded_word_ids),
            )
            .execution_options(populate_existing=True),
        )
    ).scalars().all()
    assert all(card.reps == 0 for card in cards_after)

    user_after_rollback = (
        await db_session.execute(
            select(User)
            .where(User.telegram_id == USER_A_ID)
            .execution_options(populate_existing=True),
        )
    ).scalar_one()
    assert user_after_rollback.daily_llm_calls == 1

    # Active session is still recoverable and retrying the answer succeeds cleanly!
    active_resp = await sessions_client.get("/api/sessions/active", headers=headers)
    assert active_resp.status_code == 200
    assert active_resp.json()["attempt_id"] == attempt_1_id

    retry_ans = await sessions_client.post(
        f"/api/sessions/{session_id}/answer",
        json={"user_answer": "Das Fenster ist offen."},
        headers=headers,
    )
    assert retry_ans.status_code == 200
    assert retry_ans.json()["status"] == "active"
    assert retry_ans.json()["next_exercise"] is not None


# ---------------------------------------------------------------------------
# PostgreSQL integration tests: int32 bounds & strict user_id isolation
# ---------------------------------------------------------------------------


async def test_out_of_range_and_non_positive_session_ids_return_404(
    sessions_client: AsyncClient,
) -> None:
    headers = build_bot_headers(USER_A_ID)
    invalid_ids = [0, -1, -999, 2_147_483_648, 999_999_999_999]

    for bad_id in invalid_ids:
        assert (
            await sessions_client.post(f"/api/sessions/{bad_id}/hint", headers=headers)
        ).status_code == 404
        assert (
            await sessions_client.post(
                f"/api/sessions/{bad_id}/answer",
                json={"user_answer": "Hallo Welt."},
                headers=headers,
            )
        ).status_code == 404
        assert (
            await sessions_client.post(f"/api/sessions/{bad_id}/abandon", headers=headers)
        ).status_code == 404


async def test_strict_user_id_isolation_between_user_a_and_user_b(
    sessions_client: AsyncClient,
    db_session: AsyncSession,
    fake_llm: FakeLLMService,
) -> None:
    await seed_user_with_words(
        db_session,
        telegram_id=USER_A_ID,
        words_data=[("der Freund", "друг")],
    )
    await seed_user_with_words(
        db_session,
        telegram_id=USER_B_ID,
        words_data=[("die Freundin", "подруга")],
    )
    headers_a = build_tma_headers(USER_A_ID)
    headers_b = build_tma_headers(USER_B_ID)

    # User A starts a session
    start_a = await sessions_client.post(
        "/api/sessions/start",
        json={"total_exercises": 2},
        headers=headers_a,
    )
    assert start_a.status_code == 201
    session_a_id = start_a.json()["session_id"]
    attempt_a_id = start_a.json()["attempt_id"]
    calls_after_a_start = len(fake_llm.calls)

    # User B sees 404 on active and 404 on all operations targeting User A's session
    assert (await sessions_client.get("/api/sessions/active", headers=headers_b)).status_code == 404
    assert (
        await sessions_client.post(f"/api/sessions/{session_a_id}/hint", headers=headers_b)
    ).status_code == 404
    assert (
        await sessions_client.post(
            f"/api/sessions/{session_a_id}/answer",
            json={"user_answer": "Der Freund kommt."},
            headers=headers_b,
        )
    ).status_code == 404
    assert (
        await sessions_client.post(f"/api/sessions/{session_a_id}/abandon", headers=headers_b)
    ).status_code == 404

    # Zero LLM calls triggered by User B's unauthorized attempts
    assert len(fake_llm.calls) == calls_after_a_start

    # User A's session and attempt remain completely untouched
    active_a = await sessions_client.get("/api/sessions/active", headers=headers_a)
    assert active_a.status_code == 200
    assert active_a.json()["session_id"] == session_a_id
    assert active_a.json()["attempt_id"] == attempt_a_id
    assert active_a.json()["used_hint"] is False

    # User B can start and manage their own completely isolated session
    start_b = await sessions_client.post(
        "/api/sessions/start",
        json={"total_exercises": 2},
        headers=headers_b,
    )
    assert start_b.status_code == 201
    session_b_id = start_b.json()["session_id"]
    assert session_b_id != session_a_id

    active_b = await sessions_client.get("/api/sessions/active", headers=headers_b)
    assert active_b.status_code == 200
    assert active_b.json()["session_id"] == session_b_id

    # User A still sees only User A's session
    active_a_again = await sessions_client.get("/api/sessions/active", headers=headers_a)
    assert active_a_again.status_code == 200
    assert active_a_again.json()["session_id"] == session_a_id
