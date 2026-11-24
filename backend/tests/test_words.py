"""Unit and PostgreSQL integration tests for vocabulary words & Google Sheets API endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_utc_now
from app.config import Settings
from app.core.auth import TELEGRAM_WEBAPP_HMAC_KEY
from app.core.sheets_sync import MAX_IMPORT_BYTES
from app.db.base import get_db
from app.db.models import Deck, User, UserWordFSRS, Word
from app.db.users import DEFAULT_DECK_LEVEL, DEFAULT_DECK_TITLE
from app.main import create_app

TEST_BOT_TOKEN = "123456789:AAH_words_test_bot_token_secret_ABC"
TEST_INTERNAL_API_KEY = "words-test-internal-api-key-654321"
FIXED_NOW = datetime(2026, 6, 15, 10, 30, 0, tzinfo=UTC)
FIXED_NOW_TS = int(FIXED_NOW.timestamp())
FIXED_REVIEW_AT = datetime(2026, 6, 10, 9, 0, 0, tzinfo=UTC)
FIXED_FUTURE_DUE = datetime(2026, 7, 1, 10, 30, 0, tzinfo=UTC)

USER_A_ID = 6_200_000_001
USER_B_ID = 6_200_000_002
SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"

EXPECTED_WORD_ITEM_KEYS = frozenset(
    {
        "id",
        "word_de",
        "translation_ua",
        "part_of_speech",
        "source",
        "state",
        "stability",
        "difficulty",
        "due_at",
        "reps",
        "lapses",
    },
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
    """Build internal Telegram bot authentication headers."""
    return {
        "X-Internal-API-Key": api_key,
        "X-Telegram-User-Id": str(telegram_id),
    }


@pytest.fixture
async def words_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
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
# Offline tests: OpenAPI routing & 401 unauthorized enforcement
# ---------------------------------------------------------------------------


async def test_openapi_schema_mounts_words_routes_under_api_prefix(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]

    assert "/api/words" in paths
    assert "get" in paths["/api/words"]
    assert "/api/words/{id}" in paths
    assert "delete" in paths["/api/words/{id}"]
    assert "/api/words/import" in paths
    assert "post" in paths["/api/words/import"]
    assert "/api/words/sync-sheets" in paths
    assert "post" in paths["/api/words/sync-sheets"]

    assert "/words" not in paths
    assert "/words/import" not in paths


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/words", None),
        ("DELETE", "/api/words/1", None),
        ("POST", "/api/words/import", {"text": "der Zug - потяг"}),
        (
            "POST",
            "/api/words/sync-sheets",
            {"sheets_url": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"},
        ),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer invalid"},
        build_tma_headers(USER_A_ID, hash_override="0" * 64),
        build_tma_headers(USER_A_ID, auth_date=FIXED_NOW_TS - 86_401),
        build_bot_headers(USER_A_ID, api_key="wrong-key"),
        {"X-Internal-API-Key": TEST_INTERNAL_API_KEY},
        {"X-Telegram-User-Id": str(USER_A_ID)},
    ],
)
async def test_words_endpoints_return_401_on_missing_or_invalid_auth(
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
# PostgreSQL integration tests: auto-init, GET, DELETE, import, sync-sheets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("auth_mode", ["tma", "bot"])
async def test_get_words_empty_state_and_auto_initializes_profile_and_deck(
    words_client: AsyncClient,
    db_session: AsyncSession,
    auth_mode: str,
) -> None:
    user_id = USER_A_ID if auth_mode == "tma" else USER_B_ID
    headers = (
        build_tma_headers(user_id) if auth_mode == "tma" else build_bot_headers(user_id)
    )

    response = await words_client.get("/api/words", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}

    user = await db_session.get(User, user_id)
    assert user is not None

    decks = (
        await db_session.execute(select(Deck).where(Deck.owner_id == user_id))
    ).scalars().all()
    assert len(decks) == 1
    assert decks[0].title == DEFAULT_DECK_TITLE
    assert decks[0].level == DEFAULT_DECK_LEVEL


async def test_delete_import_and_sync_auto_initialize_on_first_access(
    words_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    # 1. DELETE on brand-new user auto-initializes user & deck, returns 404
    user_1 = 6_200_000_101
    del_resp = await words_client.delete("/api/words/999999", headers=build_bot_headers(user_1))
    assert del_resp.status_code == 404
    assert await db_session.get(User, user_1) is not None
    decks_1 = (
        await db_session.execute(select(Deck).where(Deck.owner_id == user_1))
    ).scalars().all()
    assert len(decks_1) == 1
    assert decks_1[0].title == DEFAULT_DECK_TITLE

    # 2. POST /api/words/import on brand-new user auto-initializes user & deck
    user_2 = 6_200_000_102
    imp_resp = await words_client.post(
        "/api/words/import",
        headers=build_tma_headers(user_2),
        json={"text": "der Zug - потяг"},
    )
    assert imp_resp.status_code == 200
    assert imp_resp.json() == {"imported": 1, "duplicates": 0, "skipped": 0}
    assert await db_session.get(User, user_2) is not None

    # 3. POST /api/words/sync-sheets on brand-new user auto-initializes user & deck
    user_3 = 6_200_000_103
    sync_resp = await words_client.post(
        "/api/words/sync-sheets",
        headers=build_bot_headers(user_3),
    )
    assert sync_resp.status_code == 400
    assert await db_session.get(User, user_3) is not None
    decks_3 = (
        await db_session.execute(select(Deck).where(Deck.owner_id == user_3))
    ).scalars().all()
    assert len(decks_3) == 1
    assert decks_3[0].title == DEFAULT_DECK_TITLE


async def test_import_json_text_delimiters_hyphens_dedup_and_fsrs_preservation(
    words_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    headers = build_tma_headers(USER_A_ID)

    # First import with mixed delimiters, hyphenated German words, header, intra-batch dup, invalid
    first_payload = (
        "Слово - Переклад\n"
        "E-Mail - електронна пошта\n"
        "U-Bahn — метро\n"
        "die S-Bahn – міська електричка\n"
        "das Haus; будинок; noun\n"
        "die Katze\tкішка\tnoun\n"
        '"der Zug, die Züge","потяг, потяги",noun\n'
        "  e-mail  - пошта (дублікат у пакеті)\n"
        "E-Mail\n"
        "некоректний рядок\n"
    )
    first_resp = await words_client.post(
        "/api/words/import",
        headers=headers,
        json={"text": first_payload},
    )
    assert first_resp.status_code == 200
    assert first_resp.json() == {"imported": 6, "duplicates": 1, "skipped": 2}

    # Verify GET /api/words shape & initial FSRS card values
    list_resp = await words_client.get("/api/words", headers=headers)
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert list_data["total"] == 6
    assert len(list_data["items"]) == 6

    by_word = {item["word_de"]: item for item in list_data["items"]}
    assert set(by_word.keys()) == {
        "E-Mail",
        "U-Bahn",
        "die S-Bahn",
        "das Haus",
        "die Katze",
        "der Zug, die Züge",
    }
    for item in list_data["items"]:
        assert set(item.keys()) == EXPECTED_WORD_ITEM_KEYS
        assert item["source"] == "text"
        assert item["state"] == 0
        assert item["stability"] == 0.0
        assert item["difficulty"] == 0.0
        assert item["reps"] == 0
        assert item["lapses"] == 0
        assert datetime.fromisoformat(item["due_at"]) == FIXED_NOW

    assert by_word["das Haus"]["part_of_speech"] == "noun"
    assert by_word["E-Mail"]["translation_ua"] == "електронна пошта"
    assert by_word["E-Mail"]["part_of_speech"] is None

    # Simulate existing FSRS progress on "E-Mail"
    email_word_id = by_word["E-Mail"]["id"]
    card = await db_session.get(UserWordFSRS, (USER_A_ID, email_word_id))
    assert card is not None
    card.state = 2
    card.stability = 18.5
    card.difficulty = 4.3
    card.reps = 6
    card.lapses = 1
    card.last_review = FIXED_REVIEW_AT
    card.due_at = FIXED_FUTURE_DUE
    await db_session.commit()

    # Re-import containing duplicate "  E-MAIL " (case + spaces) + 1 brand-new word
    second_resp = await words_client.post(
        "/api/words/import",
        headers=headers,
        json={"text": "  E-MAIL  - дубль\ndas Buch - книга - noun\n"},
    )
    assert second_resp.status_code == 200
    assert second_resp.json() == {"imported": 1, "duplicates": 1, "skipped": 0}

    # Verify FSRS progress on "E-Mail" was NOT reset
    db_session.expire_all()
    preserved_card = await db_session.get(UserWordFSRS, (USER_A_ID, email_word_id))
    assert preserved_card is not None
    assert preserved_card.state == 2
    assert preserved_card.stability == pytest.approx(18.5)
    assert preserved_card.difficulty == pytest.approx(4.3)
    assert preserved_card.reps == 6
    assert preserved_card.lapses == 1
    assert preserved_card.last_review == FIXED_REVIEW_AT
    assert preserved_card.due_at == FIXED_FUTURE_DUE

    # Verify newly imported word received a fresh FSRS card
    after_list = (await words_client.get("/api/words", headers=headers)).json()
    assert after_list["total"] == 7
    buch = next(item for item in after_list["items"] if item["word_de"] == "das Buch")
    assert buch["part_of_speech"] == "noun"
    assert buch["state"] == 0
    assert buch["reps"] == 0


async def test_import_supports_form_text_txt_and_csv_uploads_with_utf8_bom(
    words_client: AsyncClient,
) -> None:
    headers = build_bot_headers(USER_A_ID)

    # 1. Form field `text` (application/x-www-form-urlencoded) -> source="text"
    form_resp = await words_client.post(
        "/api/words/import",
        headers=headers,
        data={"text": "der Tisch - стіл\ndie Lampe - лампа"},
    )
    assert form_resp.status_code == 200
    assert form_resp.json() == {"imported": 2, "duplicates": 0, "skipped": 0}

    # 2. Multipart `.txt` upload with UTF-8 BOM and CRLF -> source="file"
    txt_bytes = (
        "\ufeff"
        "E-Mail - електронна пошта\r\n"
        "U-Bahn — метро\r\n"
        "der Tisch - дубль з попереднього імпорту\r\n"
        "некоректний\r\n"
    ).encode("utf-8")
    txt_resp = await words_client.post(
        "/api/words/import",
        headers=headers,
        files={"file": ("vocab.txt", txt_bytes, "text/plain")},
    )
    assert txt_resp.status_code == 200
    assert txt_resp.json() == {"imported": 2, "duplicates": 1, "skipped": 1}

    # 3. Multipart `.csv` upload with UTF-8 BOM, header, and quoted commas -> source="file"
    csv_bytes = (
        "\ufeff"
        "word_de,translation_ua,part_of_speech\r\n"
        '"der Zug, die Züge","потяг, потяги",noun\r\n'
        "das Fenster,вікно,noun\r\n"
        "E-Mail,електронна пошта,noun\r\n"
    ).encode("utf-8")
    csv_resp = await words_client.post(
        "/api/words/import",
        headers=headers,
        files={"file": ("my_words.csv", csv_bytes, "text/csv")},
    )
    assert csv_resp.status_code == 200
    assert csv_resp.json() == {"imported": 2, "duplicates": 1, "skipped": 0}

    items = (await words_client.get("/api/words", headers=headers)).json()["items"]
    sources = {item["word_de"]: item["source"] for item in items}
    assert sources == {
        "der Tisch": "text",
        "die Lampe": "text",
        "E-Mail": "file",
        "U-Bahn": "file",
        "der Zug, die Züge": "file",
        "das Fenster": "file",
    }


async def test_import_rejects_empty_binary_unsupported_and_oversized_payloads(
    words_client: AsyncClient,
) -> None:
    headers = build_bot_headers(USER_A_ID)

    # Empty requests -> 422
    assert (await words_client.post("/api/words/import", headers=headers)).status_code == 422
    assert (
        await words_client.post("/api/words/import", headers=headers, json={})
    ).status_code == 422
    assert (
        await words_client.post("/api/words/import", headers=headers, json={"text": "   \n"})
    ).status_code == 422
    assert (
        await words_client.post("/api/words/import", headers=headers, data={"text": "  "})
    ).status_code == 422
    assert (
        await words_client.post(
            "/api/words/import",
            headers=headers,
            files={"file": ("empty.txt", b"", "text/plain")},
        )
    ).status_code == 422
    assert (
        await words_client.post(
            "/api/words/import",
            headers=headers,
            data={"text": "der Zug - потяг"},
            files={"file": ("words.txt", b"das Haus - \xd0\xb4\xd1\x96\xd0\xbc", "text/plain")},
        )
    ).status_code == 422

    # Non-UTF-8 binary file -> 400 with Ukrainian message
    bin_resp = await words_client.post(
        "/api/words/import",
        headers=headers,
        files={
            "file": (
                "words.txt",
                b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01",
                "application/octet-stream",
            ),
        },
    )
    assert bin_resp.status_code in {400, 422}
    assert bin_resp.json()["detail"]

    # Unsupported extension -> 400
    ext_resp = await words_client.post(
        "/api/words/import",
        headers=headers,
        files={"file": ("notes.pdf", b"der Zug - train", "application/pdf")},
    )
    assert ext_resp.status_code == 400
    assert ".txt" in ext_resp.json()["detail"]

    # File exceeding 2 MiB -> 413
    oversized_bytes = b"der Zug - train\n" * ((MAX_IMPORT_BYTES // 16) + 2)
    assert len(oversized_bytes) > MAX_IMPORT_BYTES
    big_resp = await words_client.post(
        "/api/words/import",
        headers=headers,
        files={"file": ("huge.txt", oversized_bytes, "text/plain")},
    )
    assert big_resp.status_code == 413
    assert "2 МБ" in big_resp.json()["detail"]

    # Verify no garbage was imported into DB
    assert (await words_client.get("/api/words", headers=headers)).json() == {
        "items": [],
        "total": 0,
    }


async def test_get_words_case_insensitive_search_and_pagination(
    words_client: AsyncClient,
) -> None:
    headers = build_tma_headers(USER_A_ID)

    await words_client.post(
        "/api/words/import",
        headers=headers,
        json={
            "text": (
                "die Übung - вправа\n"
                "der Zug - Потяг\n"
                "die Verspätung - затримка\n"
                "E-Mail - електронна пошта\n"
                "100% sicher - абсолютно впевнений\n"
            ),
        },
    )

    # Case-insensitive German search (including umlaut Ü/ü)
    res_de = await words_client.get("/api/words", headers=headers, params={"search": "übung"})
    assert res_de.status_code == 200
    assert res_de.json()["total"] == 1
    assert res_de.json()["items"][0]["word_de"] == "die Übung"

    # Case-insensitive Ukrainian Cyrillic search (Потяг / потяг)
    res_ua = await words_client.get("/api/words", headers=headers, params={"search": "потяг"})
    assert res_ua.status_code == 200
    assert res_ua.json()["total"] == 1
    assert res_ua.json()["items"][0]["word_de"] == "der Zug"

    # LIKE wildcard `%` must be escaped literally (matches only "100% sicher", not all 5 rows)
    res_wild = await words_client.get("/api/words", headers=headers, params={"search": "100%"})
    assert res_wild.status_code == 200
    assert res_wild.json()["total"] == 1
    assert res_wild.json()["items"][0]["word_de"] == "100% sicher"

    # Pagination: total stays 5 across pages, items do not overlap
    page_1 = (
        await words_client.get("/api/words", headers=headers, params={"limit": 2, "offset": 0})
    ).json()
    page_2 = (
        await words_client.get("/api/words", headers=headers, params={"limit": 2, "offset": 2})
    ).json()
    page_3 = (
        await words_client.get("/api/words", headers=headers, params={"limit": 2, "offset": 4})
    ).json()

    assert page_1["total"] == 5
    assert page_2["total"] == 5
    assert page_3["total"] == 5
    assert len(page_1["items"]) == 2
    assert len(page_2["items"]) == 2
    assert len(page_3["items"]) == 1

    all_ids = [item["id"] for page in (page_1, page_2, page_3) for item in page["items"]]
    assert len(all_ids) == len(set(all_ids)) == 5


async def test_delete_word_and_strict_user_id_and_shared_deck_isolation(
    words_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    headers_a = build_tma_headers(USER_A_ID)
    headers_b = build_bot_headers(USER_B_ID)

    # Import words for User A and User B
    await words_client.post(
        "/api/words/import",
        headers=headers_a,
        json={"text": "der Zug - потяг\nE-Mail - електронна пошта"},
    )
    await words_client.post(
        "/api/words/import",
        headers=headers_b,
        json={"text": "der Zug - поїзд користувача B\ndas Haus - будинок"},
    )

    # Create a shared system deck (owner_id IS NULL) with a shared word + FSRS card for User A
    shared_deck = Deck(owner_id=None, title="Goethe A1", level="A1")
    db_session.add(shared_deck)
    await db_session.flush()
    shared_word = Word(
        deck_id=shared_deck.id,
        word_de="der Bahnhof",
        translation_ua="вокзал",
        source="system",
    )
    db_session.add(shared_word)
    await db_session.flush()
    db_session.add(UserWordFSRS(user_id=USER_A_ID, word_id=shared_word.id, due_at=FIXED_NOW))
    await db_session.commit()
    shared_word_id = shared_word.id

    # GET /api/words for User A returns ONLY User A's personal words (not User B's, not shared)
    words_a = (await words_client.get("/api/words", headers=headers_a)).json()
    words_b = (await words_client.get("/api/words", headers=headers_b)).json()
    assert words_a["total"] == 2
    assert {w["word_de"] for w in words_a["items"]} == {"der Zug", "E-Mail"}
    assert words_b["total"] == 2
    assert {w["word_de"] for w in words_b["items"]} == {"der Zug", "das Haus"}

    word_a_id = words_a["items"][0]["id"]
    word_b_id = words_b["items"][0]["id"]

    # User A CANNOT delete User B's word -> 404, User B's word & FSRS untouched
    del_foreign = await words_client.delete(f"/api/words/{word_b_id}", headers=headers_a)
    assert del_foreign.status_code == 404
    assert await db_session.get(Word, word_b_id) is not None
    assert await db_session.get(UserWordFSRS, (USER_B_ID, word_b_id)) is not None

    # User A CANNOT delete a shared deck word (owner_id IS NULL) -> 404, DB untouched
    del_shared = await words_client.delete(f"/api/words/{shared_word_id}", headers=headers_a)
    assert del_shared.status_code == 404
    assert await db_session.get(Word, shared_word_id) is not None
    assert await db_session.get(UserWordFSRS, (USER_A_ID, shared_word_id)) is not None

    # Deleting non-existent word -> 404
    del_missing = await words_client.delete("/api/words/99999999", headers=headers_a)
    assert del_missing.status_code == 404

    # User A CAN delete own word -> 204, removes both Word and UserWordFSRS
    del_own = await words_client.delete(f"/api/words/{word_a_id}", headers=headers_a)
    assert del_own.status_code == 204
    db_session.expire_all()
    assert await db_session.get(Word, word_a_id) is None
    assert await db_session.get(UserWordFSRS, (USER_A_ID, word_a_id)) is None

    # User B's words remain completely intact
    assert (await words_client.get("/api/words", headers=headers_b)).json()["total"] == 2


async def test_sync_sheets_explicit_and_saved_url_resync_preserves_fsrs(
    words_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    headers = build_tma_headers(USER_A_ID)
    edit_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=12345"
    export_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=12345"
    cdn_url = "https://doc-0c-4s-sheets.googleusercontent.com/export/l5_vocab_csv"

    csv_v1 = (
        "\ufeff"
        "Word,Translation,Part of speech\r\n"
        "E-Mail,електронна пошта,noun\r\n"
        '"der Zug, die Züge","потяг, потяги",noun\r\n'
        "e-mail,дубль у таблиці,noun\r\n"
        "некоректний рядок\r\n"
    ).encode("utf-8")

    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(307, headers={"Location": cdn_url}),
        )
        mock.get(cdn_url).mock(
            return_value=httpx.Response(
                200,
                content=csv_v1,
                headers={"Content-Type": "text/csv; charset=utf-8"},
            ),
        )
        sync_resp = await words_client.post(
            "/api/words/sync-sheets",
            headers=headers,
            json={"sheets_url": edit_url},
        )

    assert sync_resp.status_code == 200
    assert sync_resp.json() == {"imported": 2, "duplicates": 1, "skipped": 1}

    words_after_v1 = (await words_client.get("/api/words", headers=headers)).json()
    assert words_after_v1["total"] == 2
    assert all(item["source"] == "sheets" for item in words_after_v1["items"])

    # Advance FSRS progress on "E-Mail" and save `sheets_url` into user profile
    email_id = next(item["id"] for item in words_after_v1["items"] if item["word_de"] == "E-Mail")
    card = await db_session.get(UserWordFSRS, (USER_A_ID, email_id))
    assert card is not None
    card.state = 2
    card.stability = 25.0
    card.difficulty = 3.8
    card.reps = 9
    card.lapses = 0
    card.last_review = FIXED_REVIEW_AT
    card.due_at = FIXED_FUTURE_DUE

    user = await db_session.get(User, USER_A_ID)
    assert user is not None
    user.sheets_url = edit_url
    await db_session.commit()

    # Re-sync WITHOUT passing sheets_url in body (reads saved user.sheets_url)
    csv_v2 = (
        "Word,Translation,Part of speech\n"
        "E-Mail,електронна пошта,noun\n"
        '"der Zug, die Züge","потяг, потяги",noun\n'
        "U-Bahn,метро,noun\n"
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(
                200,
                text=csv_v2,
                headers={"Content-Type": "text/csv; charset=utf-8"},
            ),
        )
        resync_resp = await words_client.post("/api/words/sync-sheets", headers=headers)

    assert resync_resp.status_code == 200
    assert resync_resp.json() == {"imported": 1, "duplicates": 2, "skipped": 0}

    # Existing FSRS card progress MUST be preserved intact
    db_session.expire_all()
    preserved = await db_session.get(UserWordFSRS, (USER_A_ID, email_id))
    assert preserved is not None
    assert preserved.state == 2
    assert preserved.stability == pytest.approx(25.0)
    assert preserved.difficulty == pytest.approx(3.8)
    assert preserved.reps == 9
    assert preserved.last_review == FIXED_REVIEW_AT
    assert preserved.due_at == FIXED_FUTURE_DUE


@pytest.mark.parametrize(
    "ssrf_url",
    [
        f"http://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
        "https://localhost/spreadsheets/d/123/edit",
        "https://127.0.0.1/spreadsheets/d/123/edit",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.1/spreadsheets/d/123/edit",
        "https://evil.com/spreadsheets/d/123/edit",
        "https://docs.google.com.evil.com/spreadsheets/d/123/edit",
    ],
)
async def test_sync_sheets_rejects_ssrf_urls_with_400_and_zero_network_calls(
    words_client: AsyncClient,
    ssrf_url: str,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        response = await words_client.post(
            "/api/words/sync-sheets",
            headers=build_bot_headers(USER_A_ID),
            json={"sheets_url": ssrf_url},
        )
        assert len(mock.calls) == 0

    assert response.status_code == 400
    assert response.json()["detail"]


async def test_sync_sheets_handles_untrusted_redirect_private_html_oversize_timeout_and_5xx(
    words_client: AsyncClient,
) -> None:
    headers = build_bot_headers(USER_A_ID)
    edit_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    export_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"

    # 1. Missing sheets_url -> 400
    no_url_resp = await words_client.post("/api/words/sync-sheets", headers=headers, json={})
    assert no_url_resp.status_code == 400

    # 2. Untrusted redirect -> 400 without hitting untrusted host
    with respx.mock(assert_all_called=False) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(302, headers={"Location": "https://evil.com/steal"}),
        )
        evil_route = mock.get("https://evil.com/steal").mock(
            return_value=httpx.Response(200, text="evil"),
        )
        redir_resp = await words_client.post(
            "/api/words/sync-sheets",
            headers=headers,
            json={"sheets_url": edit_url},
        )
        assert not evil_route.called
    assert redir_resp.status_code == 400

    # 3. Private sheet redirect to accounts.google.com -> 400
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(
                302,
                headers={"Location": "https://accounts.google.com/ServiceLogin?service=wise"},
            ),
        )
        login_resp = await words_client.post(
            "/api/words/sync-sheets",
            headers=headers,
            json={"sheets_url": edit_url},
        )
    assert login_resp.status_code == 400

    # 4. Private sheet returning text/html -> 400
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(
                200,
                text="<html><body>Sign in to Google</body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            ),
        )
        html_resp = await words_client.post(
            "/api/words/sync-sheets",
            headers=headers,
            json={"sheets_url": edit_url},
        )
    assert html_resp.status_code == 400

    # 5. Oversized payload (> 2 MiB) -> 413
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(
                200,
                content=b"a,b\n" * ((MAX_IMPORT_BYTES // 4) + 10),
                headers={"Content-Type": "text/csv"},
            ),
        )
        big_resp = await words_client.post(
            "/api/words/sync-sheets",
            headers=headers,
            json={"sheets_url": edit_url},
        )
    assert big_resp.status_code == 413

    # 6. Non-UTF-8 binary response -> 400
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(
                200,
                content=b"\xff\xfe\x00\x01\x89PNG",
                headers={"Content-Type": "text/csv"},
            ),
        )
        bin_resp = await words_client.post(
            "/api/words/sync-sheets",
            headers=headers,
            json={"sheets_url": edit_url},
        )
    assert bin_resp.status_code == 400

    # 7. Timeout from Google -> 504
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(side_effect=httpx.ReadTimeout("timeout"))
        timeout_resp = await words_client.post(
            "/api/words/sync-sheets",
            headers=headers,
            json={"sheets_url": edit_url},
        )
    assert timeout_resp.status_code == 504

    # 8. 5xx from Google -> 502
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(return_value=httpx.Response(503, text="Upstream down"))
        upstream_resp = await words_client.post(
            "/api/words/sync-sheets",
            headers=headers,
            json={"sheets_url": edit_url},
        )
    assert upstream_resp.status_code == 502

    # Ensure zero garbage words were ever persisted across all failure cases
    assert (await words_client.get("/api/words", headers=headers)).json() == {
        "items": [],
        "total": 0,
    }


async def test_import_multipart_text_semicolon_csv_all_invalid_and_large_batch(
    words_client: AsyncClient,
) -> None:
    headers = build_bot_headers(USER_A_ID)

    # 1. Multipart form-data with `text` field -> source="text"
    mp_text_resp = await words_client.post(
        "/api/words/import",
        headers=headers,
        files={"text": (None, "der Garten - сад\ndie Blume - квітка")},
    )
    assert mp_text_resp.status_code == 200
    assert mp_text_resp.json() == {"imported": 2, "duplicates": 0, "skipped": 0}

    # 2. Semicolon-delimited `.csv` upload -> source="file"
    semi_csv = "Слово;Переклад;Частина мови\r\nder Berg;гора, вершина;іменник\r\n".encode()
    semi_resp = await words_client.post(
        "/api/words/import",
        headers=headers,
        files={"file": ("excel_export.csv", semi_csv, "text/csv")},
    )
    assert semi_resp.status_code == 200
    assert semi_resp.json() == {"imported": 1, "duplicates": 0, "skipped": 0}

    # 3. All invalid rows still return 200 with skipped count (no crash)
    invalid_only_resp = await words_client.post(
        "/api/words/import",
        headers=headers,
        json={"text": "E-Mail\nпросто текст без перекладу"},
    )
    assert invalid_only_resp.status_code == 200
    assert invalid_only_resp.json() == {"imported": 0, "duplicates": 0, "skipped": 2}

    # 4. Batch > 1000 words exercises chunked PostgreSQL bulk insert cleanly
    large_lines = "\n".join(f"das Wort{i} - слово{i}" for i in range(1050))
    large_resp = await words_client.post(
        "/api/words/import",
        headers=headers,
        json={"text": large_lines},
    )
    assert large_resp.status_code == 200
    assert large_resp.json() == {"imported": 1050, "duplicates": 0, "skipped": 0}

    # 5. Whitespace-only search returns full total; out-of-range pagination returns 422
    ws_search = await words_client.get(
        "/api/words",
        headers=headers,
        params={"search": "   ", "limit": 10},
    )
    assert ws_search.status_code == 200
    assert ws_search.json()["total"] == 1053
    assert len(ws_search.json()["items"]) == 10

    assert (
        await words_client.get("/api/words", headers=headers, params={"limit": 0})
    ).status_code == 422
    assert (
        await words_client.get("/api/words", headers=headers, params={"offset": -1})
    ).status_code == 422


async def test_sync_sheets_explicit_url_overrides_saved_without_mutating_profile(
    words_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    headers = build_tma_headers(USER_A_ID)
    saved_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0"
    override_sheet_id = "2OverrideSheetIdExample99999"
    override_url = f"https://docs.google.com/spreadsheets/d/{override_sheet_id}/view#gid=99"
    override_export = (
        f"https://docs.google.com/spreadsheets/d/{override_sheet_id}/export?format=csv&gid=99"
    )

    # Initialize user and persist saved_url in profile
    await words_client.get("/api/words", headers=headers)
    user = await db_session.get(User, USER_A_ID)
    assert user is not None
    user.sheets_url = saved_url
    await db_session.commit()

    # Passing explicit sheets_url uses it, but does NOT overwrite user.sheets_url
    with respx.mock(assert_all_called=True) as mock:
        mock.get(override_export).mock(
            return_value=httpx.Response(
                200,
                text="die Straße,вулиця,noun\n",
                headers={"Content-Type": "text/csv; charset=utf-8"},
            ),
        )
        resp = await words_client.post(
            "/api/words/sync-sheets",
            headers=headers,
            json={"sheets_url": override_url},
        )

    assert resp.status_code == 200
    assert resp.json() == {"imported": 1, "duplicates": 0, "skipped": 0}

    db_session.expire_all()
    refreshed_user = await db_session.get(User, USER_A_ID)
    assert refreshed_user is not None
    assert refreshed_user.sheets_url == saved_url

    # Explicit empty string in body rejects with 400 (does not silently fall back)
    empty_override_resp = await words_client.post(
        "/api/words/sync-sheets",
        headers=headers,
        json={"sheets_url": "   "},
    )
    assert empty_override_resp.status_code == 400


async def test_import_form_text_allows_1_2_mib_rejects_over_2_mib_with_413_and_malformed_multipart(
    words_client: AsyncClient,
) -> None:
    headers = build_bot_headers(USER_A_ID)

    # Build a ~1.2 MiB UTF-8 payload with Cyrillic (expands > 2 MiB when URL-encoded)
    sample_line = "der Zug - потяг\n"
    sample_line_bytes = len(sample_line.encode("utf-8"))
    repeat_1_2_mib = (1_250_000 // sample_line_bytes) + 1
    text_1_2_mib = "E-Mail - електронна пошта\n" + (sample_line * repeat_1_2_mib)
    assert 1_200_000 < len(text_1_2_mib.encode("utf-8")) < MAX_IMPORT_BYTES

    # 1a. 1.2 MiB multipart/form-data text field succeeds (previously failed at Starlette 1 MiB cap)
    mp_ok = await words_client.post(
        "/api/words/import",
        headers=headers,
        files={"text": (None, text_1_2_mib)},
    )
    assert mp_ok.status_code == 200
    assert mp_ok.json() == {"imported": 2, "duplicates": repeat_1_2_mib - 1, "skipped": 0}

    # 1b. 1.2 MiB application/x-www-form-urlencoded Cyrillic text succeeds despite 3x URL expansion
    text_1_2_mib_2 = "U-Bahn - метро\n" + (sample_line * repeat_1_2_mib)
    form_ok = await words_client.post(
        "/api/words/import",
        headers=headers,
        data={"text": text_1_2_mib_2},
    )
    assert form_ok.status_code == 200
    assert form_ok.json() == {"imported": 1, "duplicates": repeat_1_2_mib, "skipped": 0}

    # 2a. 2.1 MiB multipart/form-data text field -> 413 with Ukrainian error message
    repeat_2_1_mib = (2_200_000 // sample_line_bytes) + 1
    text_2_1_mib = "das Auto - авто\n" + (sample_line * repeat_2_1_mib)
    assert len(text_2_1_mib.encode("utf-8")) > MAX_IMPORT_BYTES

    mp_too_big = await words_client.post(
        "/api/words/import",
        headers=headers,
        files={"text": (None, text_2_1_mib)},
    )
    assert mp_too_big.status_code == 413
    assert "2 МБ" in mp_too_big.json()["detail"]
    assert "Part exceeded" not in mp_too_big.text

    # 2b. 2.1 MiB application/x-www-form-urlencoded (Cyrillic AND ASCII) -> 413 Ukrainian
    form_too_big_cyr = await words_client.post(
        "/api/words/import",
        headers=headers,
        data={"text": text_2_1_mib},
    )
    assert form_too_big_cyr.status_code == 413
    assert "2 МБ" in form_too_big_cyr.json()["detail"]
    assert "Field exceeded" not in form_too_big_cyr.text

    ascii_line = "der Apfel - apple\n"
    text_2_1_mib_ascii = ascii_line * ((2_200_000 // len(ascii_line)) + 1)
    assert len(text_2_1_mib_ascii.encode("utf-8")) > MAX_IMPORT_BYTES
    form_too_big_ascii = await words_client.post(
        "/api/words/import",
        headers=headers,
        data={"text": text_2_1_mib_ascii},
    )
    assert form_too_big_ascii.status_code == 413
    assert "2 МБ" in form_too_big_ascii.json()["detail"]

    # 3. Malformed multipart boundary & missing boundary -> 400 with Ukrainian error message
    bad_boundary = await words_client.post(
        "/api/words/import",
        headers={**headers, "Content-Type": "multipart/form-data; boundary=broken"},
        content=b"--not-the-boundary\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\nhi",
    )
    assert bad_boundary.status_code == 400
    assert "multipart" in bad_boundary.json()["detail"].lower()
    assert "Invalid multipart data" not in bad_boundary.text

    missing_boundary = await words_client.post(
        "/api/words/import",
        headers={**headers, "Content-Type": "multipart/form-data"},
        content=b"garbage",
    )
    assert missing_boundary.status_code == 400
    assert "Missing boundary" not in missing_boundary.text

    # Verify rejected oversized/malformed requests imported zero new words ("das Auto"/"der Apfel")
    final_words = (await words_client.get("/api/words", headers=headers)).json()
    assert final_words["total"] == 3
    assert {item["word_de"] for item in final_words["items"]} == {"E-Mail", "der Zug", "U-Bahn"}


@pytest.mark.parametrize(
    "out_of_range_id",
    [0, -1, -2_147_483_649, 2_147_483_647, 2_147_483_648, 99_999_999_999_999],
)
async def test_delete_word_out_of_int32_range_and_non_positive_returns_404_not_500(
    words_client: AsyncClient,
    out_of_range_id: int,
) -> None:
    headers = build_tma_headers(USER_A_ID)
    await words_client.post(
        "/api/words/import",
        headers=headers,
        json={"text": "der Zug - потяг"},
    )

    resp = await words_client.delete(f"/api/words/{out_of_range_id}", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Слово не знайдено."

    # Existing word remains untouched
    words_after = (await words_client.get("/api/words", headers=headers)).json()
    assert words_after["total"] == 1


@pytest.mark.parametrize("bad_offset", [-1, 2_147_483_648, 99_999_999_999_999_999_999])
async def test_get_words_rejects_out_of_range_pagination_offset_with_422_not_500(
    words_client: AsyncClient,
    bad_offset: int,
) -> None:
    headers = build_bot_headers(USER_A_ID)
    await words_client.post(
        "/api/words/import",
        headers=headers,
        json={"text": "der Zug - потяг"},
    )

    resp = await words_client.get("/api/words", headers=headers, params={"offset": bad_offset})
    assert resp.status_code == 422

    # Max valid int32 offset succeeds cleanly with empty page and accurate total
    max_ok = await words_client.get(
        "/api/words",
        headers=headers,
        params={"offset": 2_147_483_647},
    )
    assert max_ok.status_code == 200
    assert max_ok.json() == {"items": [], "total": 1}
