"""End-to-end MVP smoke tests against isolated PostgreSQL with a mocked LLM."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_utc_now
from app.api.sessions import get_llm_service
from app.config import Settings
from app.core.auth import TELEGRAM_WEBAPP_HMAC_KEY
from app.core.fsrs_engine import review_card
from app.db.base import get_db
from app.db.models import Attempt, Deck, UserWordFSRS
from app.db.users import DEFAULT_DECK_TITLE
from app.main import create_app
from app.schemas.exercises import (
    ExerciseEvaluation,
    ExerciseType,
    GeneratedExercise,
    WordRating,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

TEST_BOT_TOKEN = "987654321:AAH_e2e_smoke_test_bot_token_XYZ"
TEST_INTERNAL_API_KEY = "e2e-smoke-internal-api-key-654321"
FIXED_NOW = datetime(2026, 8, 15, 10, 0, 0, tzinfo=UTC)
FIXED_NOW_TS = int(FIXED_NOW.timestamp())

USER_A_ID = 8_900_000_001
USER_B_ID = 8_900_000_002

_EXERCISE_TYPE_RE = re.compile(r"- exercise_type:\s*(\w+)")
_LEVEL_RE = re.compile(r"- level:\s*(A1|A2|B1)")
_TOPIC_RE = re.compile(r"- grammar_topic:\s*(.+)")
_WORD_IDS_RE = re.compile(r"- expected target_word_ids:\s*(\[[^\]]*\])")
_WORD_LINE_RE = re.compile(r"- id=(\d+)\s*\|\s*word_de=([^|]+?)\s*\|\s*translation_ua=")


def _build_tma_headers(
    telegram_id: int,
    *,
    bot_token: str = TEST_BOT_TOKEN,
    auth_date: int = FIXED_NOW_TS,
    hash_override: str | None = None,
) -> dict[str, str]:
    """Create a signed ``Authorization: tma <initData>`` header for Mini App requests."""
    fields = {
        "user": json.dumps(
            {"id": telegram_id, "first_name": "Тарас", "language_code": "uk"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "auth_date": str(auth_date),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
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
    pairs = [
        *fields.items(),
        ("hash", hash_override if hash_override is not None else digest),
    ]
    return {"Authorization": f"tma {urlencode(pairs, encoding='utf-8')}"}


def _build_bot_headers(
    telegram_id: int,
    *,
    api_key: str = TEST_INTERNAL_API_KEY,
) -> dict[str, str]:
    """Create internal bot authentication headers."""
    return {
        "X-Internal-API-Key": api_key,
        "X-Telegram-User-Id": str(telegram_id),
    }


@dataclass
class DeterministicSmokeLLM:
    """Offline in-memory LLM stub returning structured exercises and evaluations."""

    generation_calls: int = 0
    evaluation_calls: int = 0
    evaluation_scores: list[int] = field(default_factory=lambda: [85, 90])

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
        del messages, system_prompt, temperature, max_tokens
        prompt_text = user_prompt or ""

        if response_model is GeneratedExercise:
            self.generation_calls += 1
            return self._build_exercise(prompt_text, self.generation_calls)

        if response_model is ExerciseEvaluation:
            self.evaluation_calls += 1
            return self._build_evaluation(prompt_text, self.evaluation_calls)

        raise AssertionError(f"Unexpected response_model: {response_model!r}")

    def _build_exercise(self, prompt_text: str, idx: int) -> GeneratedExercise:
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
            task_text = f"Der Zug kommt ___ Verspätung am Bahnhof an (#{idx})."
            instruction_ua = "Заповни пропуск правильним прийменником:"
        elif exercise_type == "find_error":
            task_text = f"Gestern habe ich mit dem Zug gefahren (#{idx})."
            instruction_ua = "Знайди й виправ одну граматичну помилку:"
        elif exercise_type == "combine_sentences":
            task_text = f"Der Zug hat Verspätung. Ich komme spät (#{idx}). (weil)"
            instruction_ua = "Об'єднай два речення в одне:"
        else:
            task_text = f"Потяг прибуває на вокзал без затримки (#{idx})."
            instruction_ua = f"Переклади речення #{idx} німецькою:"

        return GeneratedExercise(
            exercise_type=exercise_type,
            level=level,  # type: ignore[arg-type]
            grammar_topic=grammar_topic,
            target_word_ids=word_ids,
            instruction_ua=instruction_ua,
            task_text=task_text,
            hint_ua=f"E2E_SECRET_HINT_{idx}: Пам'ятай про артикль і порядок слів.",
            reference_answer=f"E2E_SECRET_REF_{idx}: Der Zug kommt ohne Verspätung am Bahnhof an.",
        )

    def _build_evaluation(self, prompt_text: str, idx: int) -> ExerciseEvaluation:
        parsed_words = [
            (int(match.group(1)), match.group(2).strip())
            for match in _WORD_LINE_RE.finditer(prompt_text)
        ]
        grammar_score = (
            self.evaluation_scores[idx - 1]
            if idx - 1 < len(self.evaluation_scores)
            else 85
        )
        word_ratings = [
            WordRating(
                word_id=word_id,
                word_de=word_de,
                fsrs_rating=4,  # Always propose Easy (4) to test backend hint anti-cheat cap (<= 2)
                note_ua=f"Слово {word_de} вжито правильно.",
            )
            for word_id, word_de in parsed_words
        ]
        return ExerciseEvaluation(
            is_correct=True,
            grammar_score=grammar_score,
            corrected_answer="Der Zug kommt ohne Verspätung am Bahnhof an.",
            word_ratings=word_ratings,
            errors=[],
            youtube_topic="Порядок слів у німецькому реченні A1",
            summary_ua="Чудова відповідь, лексика підібрана точно.",
        )


def test_infra_files_use_lf_line_endings() -> None:
    """Verify Docker/Caddy/Compose files use Unix LF endings without Windows CRLF."""
    infra_files = (
        REPO_ROOT / "docker-compose.yml",
        REPO_ROOT / "docker-compose.dev.yml",
        REPO_ROOT / "Caddyfile",
        REPO_ROOT / "backend" / "Dockerfile",
        REPO_ROOT / "miniapp" / "Dockerfile",
        REPO_ROOT / "miniapp" / ".dockerignore",
        REPO_ROOT / ".gitattributes",
    )
    for file_path in infra_files:
        assert file_path.exists(), f"Expected infra file missing: {file_path}"
        raw = file_path.read_bytes()
        assert b"\r\n" not in raw, f"CRLF line ending found in {file_path}"


async def test_mvp_full_e2e_smoke_flow(
    migrated_test_db_url: str,
    db_session: AsyncSession,
) -> None:
    """Run the complete MVP end-to-end flow against isolated PostgreSQL with mocked LLM."""
    fake_llm = DeterministicSmokeLLM()
    test_settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=migrated_test_db_url,
        bot_token=TEST_BOT_TOKEN,
        internal_api_key=TEST_INTERNAL_API_KEY,
        llm_api_key="test-mocked-key",
        daily_llm_limit=50,
    )
    app = create_app(settings=test_settings)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def _override_get_llm_service() -> AsyncIterator[DeterministicSmokeLLM]:
        yield fake_llm

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_llm_service] = _override_get_llm_service
    app.dependency_overrides[get_utc_now] = lambda: FIXED_NOW

    user_a_bot = _build_bot_headers(USER_A_ID)
    user_a_tma = _build_tma_headers(USER_A_ID)
    user_b_bot = _build_bot_headers(USER_B_ID)
    user_b_tma = _build_tma_headers(USER_B_ID)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 0. Healthcheck & nonexistent API route return JSON (never HTML)
        health_resp = await client.get("/api/health")
        assert health_resp.status_code == 200
        assert health_resp.json() == {"status": "ok", "database": "ok"}

        missing_resp = await client.get("/api/nonexistent")
        assert missing_resp.status_code == 404
        assert missing_resp.headers["content-type"].startswith("application/json")

        # 1. Unauthenticated and invalid credentials -> 401 across protected routes
        for method, path in (
            ("GET", "/api/users/me"),
            ("GET", "/api/words"),
            ("POST", "/api/sessions/start"),
            ("GET", "/api/stats/radar"),
        ):
            unauth_resp = await client.request(method, path)
            assert unauth_resp.status_code == 401, f"{method} {path} must reject missing auth"

        bad_key_resp = await client.get(
            "/api/users/me",
            headers=_build_bot_headers(USER_A_ID, api_key="wrong-key"),
        )
        assert bad_key_resp.status_code == 401

        forged_tma_resp = await client.get(
            "/api/users/me",
            headers=_build_tma_headers(USER_A_ID, hash_override="0" * 64),
        )
        assert forged_tma_resp.status_code == 401

        # 2. Auth via X-Internal-API-Key AND Authorization: tma -> auto-init profile + "Мій словник"
        me_bot_resp = await client.get("/api/users/me", headers=user_a_bot)
        assert me_bot_resp.status_code == 200
        me_bot_data = me_bot_resp.json()
        assert me_bot_data["telegram_id"] == USER_A_ID
        assert me_bot_data["level"] == "A1"
        assert me_bot_data["exercises_per_set"] == 5
        assert me_bot_data["theme"] == "system"

        me_tma_resp = await client.get("/api/users/me", headers=user_a_tma)
        assert me_tma_resp.status_code == 200
        assert me_tma_resp.json()["telegram_id"] == USER_A_ID

        decks_a = (
            await db_session.execute(select(Deck).where(Deck.owner_id == USER_A_ID))
        ).scalars().all()
        assert len(decks_a) == 1
        assert decks_a[0].title == DEFAULT_DECK_TITLE

        # Baseline radar stats before training
        baseline_radar_resp = await client.get("/api/stats/radar", headers=user_a_tma)
        assert baseline_radar_resp.status_code == 200
        baseline_axes = {axis["axis"]: axis for axis in baseline_radar_resp.json()["axes"]}
        assert set(baseline_axes) == {"vocab", "grammar", "consistency", "writing", "listening"}
        assert baseline_axes["vocab"]["score"] == 0
        assert baseline_axes["grammar"]["score"] == 0
        assert baseline_axes["consistency"]["score"] == 0
        assert baseline_axes["writing"]["status"] == "coming_soon"
        assert baseline_axes["writing"]["score"] == 0
        assert baseline_axes["listening"]["status"] == "coming_soon"
        assert baseline_axes["listening"]["score"] == 0

        # 3. Import words (POST /api/words/import) and verify via TMA GET /api/words
        import_resp = await client.post(
            "/api/words/import",
            headers=user_a_bot,
            json={
                "text": (
                    "das Treffen - зустріч\n"
                    "die Verspätung - затримка\n"
                    "der Bahnhof - вокзал\n"
                    "der Zug - потяг\n"
                ),
            },
        )
        assert import_resp.status_code == 200
        assert import_resp.json() == {"imported": 4, "duplicates": 0, "skipped": 0}

        words_resp = await client.get("/api/words", headers=user_a_tma)
        assert words_resp.status_code == 200
        words_data = words_resp.json()
        assert words_data["total"] == 4
        user_a_first_word_id = words_data["items"][0]["id"]

        # 4. Start training session (POST /api/sessions/start) without leaking reference/hint
        start_resp = await client.post(
            "/api/sessions/start",
            headers=user_a_bot,
            json={"mode": "mix", "total_exercises": 2},
        )
        assert start_resp.status_code == 201
        start_data = start_resp.json()
        session_id = start_data["session_id"]
        attempt_1_id = start_data["attempt_id"]
        assert start_data["status"] == "active"
        assert start_data["total_exercises"] == 2
        assert start_data["completed_exercises"] == 0
        assert start_data["used_hint"] is False
        assert "reference_answer" not in start_data
        assert "hint_ua" not in start_data
        assert "reference_answer" not in start_data["exercise"]
        assert "hint_ua" not in start_data["exercise"]
        assert "E2E_SECRET_REF" not in start_resp.text
        assert "E2E_SECRET_HINT" not in start_resp.text

        # Capture the target word IDs for attempt #1 directly from DB
        attempt_1 = (
            await db_session.execute(select(Attempt).where(Attempt.id == attempt_1_id))
        ).scalar_one()
        attempt_1_word_ids = list(attempt_1.word_ids)
        assert len(attempt_1_word_ids) >= 1

        # Active session endpoint also hides hint/reference before hint is requested
        active_resp = await client.get("/api/sessions/active", headers=user_a_bot)
        assert active_resp.status_code == 200
        assert "hint_ua" not in active_resp.json()
        assert "E2E_SECRET_REF" not in active_resp.text

        # 5. Request hint (POST /api/sessions/{id}/hint) -> sets used_hint=True
        hint_resp = await client.post(f"/api/sessions/{session_id}/hint", headers=user_a_bot)
        assert hint_resp.status_code == 200
        hint_data = hint_resp.json()
        assert hint_data["used_hint"] is True
        assert "E2E_SECRET_HINT_1" in hint_data["hint_ua"]

        await db_session.refresh(attempt_1)
        assert attempt_1.used_hint is True

        # 6. Submit answer #1 -> anti-cheat caps FSRS ratings to <= 2 in response and user_word_fsrs
        answer_1_resp = await client.post(
            f"/api/sessions/{session_id}/answer",
            headers=user_a_bot,
            json={"user_answer": "Der Zug kommt ohne Verspätung am Bahnhof an."},
        )
        assert answer_1_resp.status_code == 200
        answer_1_data = answer_1_resp.json()
        assert answer_1_data["status"] == "active"
        assert answer_1_data["used_hint"] is True
        assert answer_1_data["completed_exercises"] == 1
        assert answer_1_data["next_exercise"] is not None
        assert "reference_answer" not in answer_1_data["next_exercise"]
        assert "hint_ua" not in answer_1_data["next_exercise"]

        ratings_1 = answer_1_data["evaluation"]["word_ratings"]
        assert len(ratings_1) == len(attempt_1_word_ids)
        assert all(item["fsrs_rating"] == 2 for item in ratings_1)

        # Verify persisted anti-cheat effect directly in user_word_fsrs
        fresh_hard = UserWordFSRS(
            user_id=USER_A_ID,
            word_id=attempt_1_word_ids[0],
            state=0,
            stability=0.0,
            difficulty=0.0,
            due_at=FIXED_NOW,
            reps=0,
            lapses=0,
            last_review=None,
        )
        fresh_easy = UserWordFSRS(
            user_id=USER_A_ID,
            word_id=attempt_1_word_ids[0],
            state=0,
            stability=0.0,
            difficulty=0.0,
            due_at=FIXED_NOW,
            reps=0,
            lapses=0,
            last_review=None,
        )
        expected_hard = review_card(fresh_hard, rating=2, now=FIXED_NOW)
        expected_easy = review_card(fresh_easy, rating=4, now=FIXED_NOW)
        assert expected_hard.stability < expected_easy.stability

        fsrs_cards_1 = (
            await db_session.execute(
                select(UserWordFSRS).where(
                    UserWordFSRS.user_id == USER_A_ID,
                    UserWordFSRS.word_id.in_(attempt_1_word_ids),
                ),
            )
        ).scalars().all()
        assert len(fsrs_cards_1) == len(attempt_1_word_ids)
        for card in fsrs_cards_1:
            assert card.reps == 1
            assert card.last_review == FIXED_NOW
            assert card.stability == pytest.approx(expected_hard.stability)
            assert card.difficulty == pytest.approx(expected_hard.difficulty)
            assert card.due_at == expected_hard.due_at
            assert card.stability < expected_easy.stability

        # Complete answer #2 WITHOUT hint -> full Easy (4) rating & session completed
        answer_2_resp = await client.post(
            f"/api/sessions/{session_id}/answer",
            headers=user_a_bot,
            json={"user_answer": "Der Zug kommt ohne Verspätung am Bahnhof an."},
        )
        assert answer_2_resp.status_code == 200
        answer_2_data = answer_2_resp.json()
        assert answer_2_data["status"] == "completed"
        assert answer_2_data["used_hint"] is False
        assert answer_2_data["completed_exercises"] == 2
        assert answer_2_data["next_exercise"] is None
        assert answer_2_data["summary"] is not None
        assert answer_2_data["summary"]["total_exercises"] == 2
        assert answer_2_data["summary"]["correct_count"] == 2
        assert answer_2_data["summary"]["avg_grammar_score"] == pytest.approx(87.5)
        assert all(
            item["fsrs_rating"] == 4 for item in answer_2_data["evaluation"]["word_ratings"]
        )

        # Confirm mocked LLM was called (2 generations + 2 evaluations), zero external network calls
        assert fake_llm.generation_calls == 2
        assert fake_llm.evaluation_calls == 2

        # 7. Updated GET /api/stats/radar reflects completed attempts & FSRS progress
        updated_radar_resp = await client.get("/api/stats/radar", headers=user_a_tma)
        assert updated_radar_resp.status_code == 200
        updated_radar = updated_radar_resp.json()
        updated_axes = {axis["axis"]: axis for axis in updated_radar["axes"]}
        assert updated_axes["vocab"]["score"] > 0
        assert updated_axes["grammar"]["score"] > 0
        assert updated_axes["consistency"]["score"] > 0
        assert updated_axes["writing"] == {
            "axis": "writing",
            "label_ua": "Шрайбен",
            "score": 0,
            "status": "coming_soon",
        }
        assert updated_axes["listening"] == {
            "axis": "listening",
            "label_ua": "Хьорен",
            "score": 0,
            "status": "coming_soon",
        }
        assert len(updated_radar["weak_topics"]) >= 1

        # 8. Theme & settings sync: PATCH via Mini App TMA -> verify via Bot GET /api/users/me
        patch_resp = await client.patch(
            "/api/users/settings",
            headers=user_a_tma,
            json={"theme": "dark", "level": "B1", "exercises_per_set": 7},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["theme"] == "dark"
        assert patch_resp.json()["level"] == "B1"
        assert patch_resp.json()["exercises_per_set"] == 7

        me_after_sync = await client.get("/api/users/me", headers=user_a_bot)
        assert me_after_sync.status_code == 200
        assert me_after_sync.json()["theme"] == "dark"
        assert me_after_sync.json()["level"] == "B1"
        assert me_after_sync.json()["exercises_per_set"] == 7

        # 9. Full User B isolation across profile, words, sessions, and radar stats
        me_b_resp = await client.get("/api/users/me", headers=user_b_tma)
        assert me_b_resp.status_code == 200
        assert me_b_resp.json()["telegram_id"] == USER_B_ID
        assert me_b_resp.json()["theme"] == "system"
        assert me_b_resp.json()["level"] == "A1"

        words_b_resp = await client.get("/api/words", headers=user_b_tma)
        assert words_b_resp.status_code == 200
        assert words_b_resp.json() == {"items": [], "total": 0}

        delete_a_word_as_b = await client.delete(
            f"/api/words/{user_a_first_word_id}",
            headers=user_b_tma,
        )
        assert delete_a_word_as_b.status_code == 404

        assert (await client.get("/api/sessions/active", headers=user_b_bot)).status_code == 404
        assert (
            await client.post(f"/api/sessions/{session_id}/hint", headers=user_b_bot)
        ).status_code == 404
        assert (
            await client.post(
                f"/api/sessions/{session_id}/answer",
                headers=user_b_bot,
                json={"user_answer": "Ich bin User B."},
            )
        ).status_code == 404
        assert (
            await client.post(f"/api/sessions/{session_id}/abandon", headers=user_b_bot)
        ).status_code == 404

        radar_b_resp = await client.get("/api/stats/radar", headers=user_b_tma)
        assert radar_b_resp.status_code == 200
        radar_b_axes = {axis["axis"]: axis["score"] for axis in radar_b_resp.json()["axes"]}
        assert radar_b_axes == {
            "vocab": 0,
            "grammar": 0,
            "consistency": 0,
            "writing": 0,
            "listening": 0,
        }
        assert radar_b_resp.json()["weak_topics"] == []

        # Confirm User A's word was untouched by User B's DELETE attempt
        words_a_final = await client.get("/api/words", headers=user_a_tma)
        assert words_a_final.status_code == 200
        assert words_a_final.json()["total"] == 4
