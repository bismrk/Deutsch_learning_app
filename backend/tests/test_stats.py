"""API unit and PostgreSQL integration tests for GET /api/stats/radar."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_utc_now
from app.config import Settings
from app.core.auth import TELEGRAM_WEBAPP_HMAC_KEY
from app.db.base import get_db
from app.db.models import Attempt, Deck, TrainingSession, User, UserWordFSRS, Word
from app.main import create_app
from app.schemas.stats import RADAR_AXIS_LABELS_UA, RADAR_AXIS_ORDER

TEST_BOT_TOKEN = "123456789:AAH_stats_test_bot_token_secret_XYZ"
TEST_INTERNAL_API_KEY = "stats-test-internal-api-key-654321"
FIXED_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
FIXED_NOW_TS = int(FIXED_NOW.timestamp())

USER_A_ID = 6_200_000_001
USER_B_ID = 6_200_000_002


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
            {"id": telegram_id, "first_name": "Тарас", "language_code": "uk"},
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


@pytest.fixture
async def stats_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
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
# Offline tests: OpenAPI mount & 401 auth enforcement
# ---------------------------------------------------------------------------


async def test_openapi_schema_mounts_stats_radar_under_api_prefix(
    client: AsyncClient,
) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/stats/radar" in paths
    assert "get" in paths["/api/stats/radar"]
    assert "/stats/radar" not in paths


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
async def test_stats_radar_returns_401_without_valid_auth(headers: dict[str, str]) -> None:
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
        response = await async_client.get("/api/stats/radar", headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing authentication credentials"}
    assert TEST_BOT_TOKEN not in response.text
    assert TEST_INTERNAL_API_KEY not in response.text


# ---------------------------------------------------------------------------
# PostgreSQL integration tests: GET /api/stats/radar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("auth_mode", ["tma", "bot"])
async def test_get_stats_radar_empty_db_returns_five_zero_axes_and_empty_weak_topics(
    stats_client: AsyncClient,
    auth_mode: str,
) -> None:
    headers = (
        build_tma_headers(USER_A_ID) if auth_mode == "tma" else build_bot_headers(USER_A_ID)
    )

    response = await stats_client.get("/api/stats/radar", headers=headers)
    assert response.status_code == 200
    payload = response.json()

    assert set(payload.keys()) == {"axes", "weak_topics"}
    assert payload["weak_topics"] == []

    axes = payload["axes"]
    assert len(axes) == 5
    assert tuple(item["axis"] for item in axes) == RADAR_AXIS_ORDER

    for item in axes:
        assert set(item.keys()) == {"axis", "label_ua", "score", "status"}
        assert item["label_ua"] == RADAR_AXIS_LABELS_UA[item["axis"]]
        assert item["score"] == 0
        expected_status = "coming_soon" if item["axis"] in {"writing", "listening"} else "active"
        assert item["status"] == expected_status


async def test_get_stats_radar_full_calculation_overdue_penalty_and_weak_topics(
    stats_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    user = User(telegram_id=USER_A_ID, level="A2", timezone="Europe/Kyiv")
    db_session.add(user)
    await db_session.flush()

    deck = Deck(owner_id=USER_A_ID, title="Мій словник", level="mixed")
    db_session.add(deck)
    await db_session.flush()

    words = [
        Word(deck_id=deck.id, word_de=f"das Wort {idx}", translation_ua=f"слово {idx}")
        for idx in range(5)
    ]
    db_session.add_all(words)
    await db_session.flush()

    # 5 cards:
    # - word 0: reps=0 (new, due 5d ago) -> NOT active, stability=0.0
    # - word 1: reps=3, stability=21.0, due in future -> active, NOT overdue
    # - word 2: reps=2, stability=40.0 (capped at 21d), due exactly 24h ago -> active, NOT overdue
    # - word 3: reps=1, stability=10.5 (0.5 eff word), due 24h+1m ago -> active, OVERDUE
    # - word 4: reps=2, stability=0.0, due 3d ago -> active, OVERDUE
    # Effective words = 0 + 1.0 + 1.0 + 0.5 + 0 = 2.5
    # Active reviewed words = 4; overdue > 24h = 2 -> backlog_health = 1 - 2/4 = 0.5
    db_session.add_all(
        [
            UserWordFSRS(
                user_id=USER_A_ID,
                word_id=words[0].id,
                state=0,
                stability=0.0,
                difficulty=0.0,
                due_at=FIXED_NOW - timedelta(days=5),
                reps=0,
                lapses=0,
                last_review=None,
            ),
            UserWordFSRS(
                user_id=USER_A_ID,
                word_id=words[1].id,
                state=2,
                stability=21.0,
                difficulty=4.0,
                due_at=FIXED_NOW + timedelta(days=10),
                reps=3,
                lapses=0,
                last_review=FIXED_NOW - timedelta(days=2),
            ),
            UserWordFSRS(
                user_id=USER_A_ID,
                word_id=words[2].id,
                state=2,
                stability=40.0,
                difficulty=3.5,
                due_at=FIXED_NOW - timedelta(hours=24),
                reps=2,
                lapses=0,
                last_review=FIXED_NOW - timedelta(days=3),
            ),
            UserWordFSRS(
                user_id=USER_A_ID,
                word_id=words[3].id,
                state=2,
                stability=10.5,
                difficulty=6.0,
                due_at=FIXED_NOW - timedelta(hours=24, minutes=1),
                reps=1,
                lapses=0,
                last_review=FIXED_NOW - timedelta(days=2),
            ),
            UserWordFSRS(
                user_id=USER_A_ID,
                word_id=words[4].id,
                state=3,
                stability=0.0,
                difficulty=7.0,
                due_at=FIXED_NOW - timedelta(days=3),
                reps=2,
                lapses=1,
                last_review=FIXED_NOW - timedelta(days=5),
            ),
        ],
    )

    session = TrainingSession(
        user_id=USER_A_ID,
        mode="mix",
        total_exercises=10,
        status="completed",
        created_at=FIXED_NOW - timedelta(days=20),
    )
    db_session.add(session)
    await db_session.flush()

    # Completed attempts inside 14d (across 3 distinct local days in Europe/Kyiv):
    # Day 1 (May 10): Topic "Nebensätze mit weil", score 40, B1, rating 2 (Hard)
    # Day 2 (May 11): Topic "Dativ", score 60, A2, rating 3 (Good)
    # Day 2 (May 11): Topic "Dativ", score 80, A2, rating 4 (Easy) -> avg Dativ = 70.0 (2 attempts)
    # Day 3 (May 12): Topic "Akkusativ", score 70, A2, rating 3 (Good) -> avg 70.0 (1 attempt)
    # Day 3 (May 12): Topic "Präsens", score 100, A1, rating 4 (Easy) -> avg 100 (NOT weak)
    # Day 3 (May 12): Topic "Perfekt", score 85, A2, rating 1 (Again) -> 4th weak topic (cut off)
    # Older than 14d (May 1, 11:00Z = 14d + 1h ago): Topic "Old Weak", score 10, A1, rating 3
    #   -> included in grammar_score & 30d recall, EXCLUDED from 14d consistency & weak_topics!
    # Unfinished attempt (user_answer=None) on May 14: MUST be ignored completely!
    db_session.add_all(
        [
            Attempt(
                session_id=session.id,
                user_id=USER_A_ID,
                word_ids=[words[1].id],
                exercise_type="translation",
                level="B1",
                grammar_topic="Nebensätze mit weil",
                question_payload={"task_text": "1"},
                user_answer="ans 1",
                is_correct=False,
                grammar_score=40,
                fsrs_ratings=[{"word_id": words[1].id, "fsrs_rating": 2}],
                youtube_topic="Порядок слів weil B1",
                created_at=datetime(2026, 5, 10, 10, 0, 0, tzinfo=UTC),
            ),
            Attempt(
                session_id=session.id,
                user_id=USER_A_ID,
                word_ids=[words[2].id],
                exercise_type="fill_blank",
                level="A2",
                grammar_topic="Dativ",
                question_payload={"task_text": "2"},
                user_answer="ans 2",
                is_correct=False,
                grammar_score=60,
                fsrs_ratings=[{"word_id": words[2].id, "fsrs_rating": 3}],
                youtube_topic="Dativ старий",
                created_at=datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC),
            ),
            Attempt(
                session_id=session.id,
                user_id=USER_A_ID,
                word_ids=[words[2].id],
                exercise_type="find_error",
                level="A2",
                grammar_topic="Dativ",
                question_payload={"task_text": "3"},
                user_answer="ans 3",
                is_correct=True,
                grammar_score=80,
                fsrs_ratings=[{"word_id": words[2].id, "fsrs_rating": 4}],
                youtube_topic="Dativ A2 пояснення",
                created_at=datetime(2026, 5, 11, 15, 0, 0, tzinfo=UTC),
            ),
            Attempt(
                session_id=session.id,
                user_id=USER_A_ID,
                word_ids=[words[3].id],
                exercise_type="combine_sentences",
                level="A2",
                grammar_topic="Akkusativ",
                question_payload={"task_text": "4"},
                user_answer="ans 4",
                is_correct=False,
                grammar_score=70,
                fsrs_ratings=[{"word_id": words[3].id, "fsrs_rating": 3}],
                youtube_topic="Akkusativ A2",
                created_at=datetime(2026, 5, 12, 9, 0, 0, tzinfo=UTC),
            ),
            Attempt(
                session_id=session.id,
                user_id=USER_A_ID,
                word_ids=[words[1].id],
                exercise_type="translation",
                level="A1",
                grammar_topic="Präsens",
                question_payload={"task_text": "5"},
                user_answer="ans 5",
                is_correct=True,
                grammar_score=100,
                fsrs_ratings=[{"word_id": words[1].id, "fsrs_rating": 4}],
                youtube_topic="Präsens A1",
                created_at=datetime(2026, 5, 12, 11, 0, 0, tzinfo=UTC),
            ),
            Attempt(
                session_id=session.id,
                user_id=USER_A_ID,
                word_ids=[words[4].id],
                exercise_type="fill_blank",
                level="A2",
                grammar_topic="Perfekt",
                question_payload={"task_text": "6"},
                user_answer="ans 6",
                is_correct=True,
                grammar_score=85,
                fsrs_ratings=[{"word_id": words[4].id, "fsrs_rating": 1}],
                youtube_topic="Perfekt A2",
                created_at=datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
            ),
            Attempt(
                session_id=session.id,
                user_id=USER_A_ID,
                word_ids=[words[1].id],
                exercise_type="translation",
                level="A1",
                grammar_topic="Old Weak",
                question_payload={"task_text": "7"},
                user_answer="ans 7",
                is_correct=False,
                grammar_score=10,
                fsrs_ratings=[{"word_id": words[1].id, "fsrs_rating": 3}],
                youtube_topic="Old Weak YT",
                created_at=FIXED_NOW - timedelta(days=14, hours=1),
            ),
            Attempt(
                session_id=session.id,
                user_id=USER_A_ID,
                word_ids=[words[1].id],
                exercise_type="translation",
                level="B1",
                grammar_topic="Unfinished",
                question_payload={"task_text": "8"},
                user_answer=None,
                is_correct=False,
                grammar_score=0,
                fsrs_ratings=[{"word_id": words[1].id, "fsrs_rating": 1}],
                youtube_topic="Unfinished YT",
                created_at=FIXED_NOW - timedelta(days=1),
            ),
        ],
    )
    await db_session.flush()

    response = await stats_client.get(
        "/api/stats/radar",
        headers=build_tma_headers(USER_A_ID),
    )
    assert response.status_code == 200
    data = response.json()

    axes_by_key = {item["axis"]: item for item in data["axes"]}

    # Vocab: eff = 2.5, 30d ratings = [2, 3, 4, 3, 4, 1, 3] -> 5 good/easy out of 7
    expected_vocab = round(100 * (0.75 * ((2.5 / 1500.0) ** 0.5) + 0.25 * (5 / 7)))
    assert axes_by_key["vocab"]["score"] == expected_vocab
    assert axes_by_key["vocab"]["status"] == "active"

    # Grammar: 7 completed attempts, 6 unique topics (coverage_factor = 1.0)
    # weighted: 40*1.0 + 60*0.7 + 80*0.7 + 70*0.7 + 100*0.35 + 85*0.7 + 10*0.35
    weighted_sum = 40.0 + 42.0 + 56.0 + 49.0 + 35.0 + 59.5 + 3.5
    expected_grammar = round((weighted_sum / 7) * 1.0)
    assert axes_by_key["grammar"]["score"] == expected_grammar
    assert axes_by_key["grammar"]["status"] == "active"

    # Consistency: 3 active days in 14d (May 10, 11, 12; May 14 unfinished ignored),
    # 2 overdue > 24h out of 4 active words -> backlog_health = 0.5
    expected_consistency = round(100 * (0.6 * (3 / 12.0) + 0.4 * 0.5))
    assert axes_by_key["consistency"]["score"] == expected_consistency
    assert axes_by_key["consistency"]["status"] == "active"

    assert axes_by_key["writing"] == {
        "axis": "writing",
        "label_ua": "Шрайбен",
        "score": 0,
        "status": "coming_soon",
    }
    assert axes_by_key["listening"] == {
        "axis": "listening",
        "label_ua": "Хьорен",
        "score": 0,
        "status": "coming_soon",
    }

    # Weak topics: only last 14d (excludes "Old Weak", "Unfinished", and perfect "Präsens")
    # Order: 1) Nebensätze mit weil (40.0), 2) Dativ (70.0, 2 attempts), 3) Akkusativ (70.0, 1)
    assert data["weak_topics"] == [
        {
            "grammar_topic": "Nebensätze mit weil",
            "avg_grammar_score": 40.0,
            "attempts_count": 1,
            "youtube_topic": "Порядок слів weil B1",
        },
        {
            "grammar_topic": "Dativ",
            "avg_grammar_score": 70.0,
            "attempts_count": 2,
            "youtube_topic": "Dativ A2 пояснення",
        },
        {
            "grammar_topic": "Akkusativ",
            "avg_grammar_score": 70.0,
            "attempts_count": 1,
            "youtube_topic": "Akkusativ A2",
        },
    ]


async def test_get_stats_radar_timezone_boundary_changes_consistency_score(
    stats_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    # Two completed attempts at 20:30Z and 21:30Z on May 10:
    # - UTC: 1 local calendar day -> consistency = round(100 * 0.6 * (1/12)) = 5
    # - Europe/Kyiv (+03:00): 2 local calendar days -> consistency = round(100 * 0.6 * (2/12)) = 10
    user = User(telegram_id=USER_A_ID, level="A1", timezone="UTC")
    db_session.add(user)
    await db_session.flush()

    session = TrainingSession(
        user_id=USER_A_ID,
        mode="mix",
        total_exercises=2,
        status="completed",
        created_at=datetime(2026, 5, 10, 20, 0, 0, tzinfo=UTC),
    )
    db_session.add(session)
    await db_session.flush()

    db_session.add_all(
        [
            Attempt(
                session_id=session.id,
                user_id=USER_A_ID,
                word_ids=[],
                exercise_type="translation",
                level="A1",
                grammar_topic="Präsens",
                question_payload={"task_text": "a"},
                user_answer="a",
                is_correct=True,
                grammar_score=100,
                created_at=datetime(2026, 5, 10, 20, 30, 0, tzinfo=UTC),
            ),
            Attempt(
                session_id=session.id,
                user_id=USER_A_ID,
                word_ids=[],
                exercise_type="translation",
                level="A1",
                grammar_topic="Präsens",
                question_payload={"task_text": "b"},
                user_answer="b",
                is_correct=True,
                grammar_score=100,
                created_at=datetime(2026, 5, 10, 21, 30, 0, tzinfo=UTC),
            ),
        ],
    )
    await db_session.flush()

    headers = build_bot_headers(USER_A_ID)
    resp_utc = await stats_client.get("/api/stats/radar", headers=headers)
    assert resp_utc.status_code == 200
    utc_consistency = {a["axis"]: a["score"] for a in resp_utc.json()["axes"]}["consistency"]
    assert utc_consistency == 5

    # Switch user timezone to Europe/Kyiv
    user.timezone = "Europe/Kyiv"
    await db_session.flush()

    resp_kyiv = await stats_client.get("/api/stats/radar", headers=headers)
    assert resp_kyiv.status_code == 200
    kyiv_consistency = {a["axis"]: a["score"] for a in resp_kyiv.json()["axes"]}["consistency"]
    assert kyiv_consistency == 10


async def test_get_stats_radar_strict_user_id_isolation_between_users_a_and_b(
    stats_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    user_a = User(telegram_id=USER_A_ID, level="B1", timezone="Europe/Kyiv")
    user_b = User(telegram_id=USER_B_ID, level="A1", timezone="UTC")
    db_session.add_all([user_a, user_b])
    await db_session.flush()

    deck_a = Deck(owner_id=USER_A_ID, title="Мій словник", level="mixed")
    db_session.add(deck_a)
    await db_session.flush()

    word_a = Word(deck_id=deck_a.id, word_de="die Prüfung", translation_ua="іспит")
    db_session.add(word_a)
    await db_session.flush()

    db_session.add(
        UserWordFSRS(
            user_id=USER_A_ID,
            word_id=word_a.id,
            state=2,
            stability=21.0,
            difficulty=4.0,
            due_at=FIXED_NOW + timedelta(days=5),
            reps=3,
            lapses=0,
            last_review=FIXED_NOW - timedelta(days=1),
        ),
    )

    session_a = TrainingSession(
        user_id=USER_A_ID,
        mode="mix",
        total_exercises=6,
        status="completed",
        created_at=FIXED_NOW - timedelta(days=3),
    )
    db_session.add(session_a)
    await db_session.flush()

    for idx in range(6):
        db_session.add(
            Attempt(
                session_id=session_a.id,
                user_id=USER_A_ID,
                word_ids=[word_a.id],
                exercise_type="translation",
                level="B1",
                grammar_topic=f"User A Topic {idx}",
                question_payload={"task_text": f"t{idx}"},
                user_answer=f"a{idx}",
                is_correct=False,
                grammar_score=50 + idx * 5,
                fsrs_ratings=[{"word_id": word_a.id, "fsrs_rating": 4}],
                youtube_topic=f"User A YT {idx}",
                created_at=FIXED_NOW - timedelta(days=idx + 1),
            ),
        )
    await db_session.flush()

    # User A sees non-zero stats and their own weak topics
    resp_a = await stats_client.get("/api/stats/radar", headers=build_tma_headers(USER_A_ID))
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    scores_a = {a["axis"]: a["score"] for a in data_a["axes"]}
    assert scores_a["vocab"] > 0
    assert scores_a["grammar"] > 0
    assert scores_a["consistency"] > 0
    assert len(data_a["weak_topics"]) == 3
    assert all(t["grammar_topic"].startswith("User A Topic") for t in data_a["weak_topics"])

    # User B must see strictly zeroed stats and empty weak_topics (zero leakage from User A)
    resp_b = await stats_client.get("/api/stats/radar", headers=build_bot_headers(USER_B_ID))
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    scores_b = {a["axis"]: a["score"] for a in data_b["axes"]}
    assert scores_b == {
        "vocab": 0,
        "grammar": 0,
        "consistency": 0,
        "writing": 0,
        "listening": 0,
    }
    assert data_b["weak_topics"] == []
