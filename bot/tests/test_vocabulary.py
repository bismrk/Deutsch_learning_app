"""Unit and dispatcher tests for vocabulary import (text, files, Google Sheets)."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import GetFile
from aiogram.types import (
    Chat,
    Document,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from bot_app.api_client import (
    RATE_LIMIT_MESSAGE_UA,
    TIMEOUT_ERROR_MESSAGE_UA,
    UNAVAILABLE_ERROR_MESSAGE_UA,
    APIClientError,
    APIUnavailableError,
    BackendAPIClient,
    BadRequestError,
    RateLimitError,
    ValidationAPIError,
)
from bot_app.config import BotSettings
from bot_app.handlers.training import NON_TEXT_ANSWER_MESSAGE_UA, TrainingStates
from bot_app.handlers.vocabulary import (
    ALLOWED_IMPORT_EXTENSIONS,
    MAX_IMPORT_FILE_BYTES,
    VOCAB_CANCELLED_UA,
    VOCAB_EMPTY_FILE_UA,
    VOCAB_EMPTY_TEXT_UA,
    VOCAB_FILE_DOWNLOAD_ERROR_UA,
    VOCAB_FILE_PARSE_ERROR_UA,
    VOCAB_FILE_TOO_LARGE_UA,
    VOCAB_IMPORT_PROMPT_UA,
    VOCAB_LEGACY_XLS_UA,
    VOCAB_MENU_TEXT_UA,
    VOCAB_SHEETS_ACCESS_ERROR_UA,
    VOCAB_SHEETS_BIND_PROMPT_UA,
    VOCAB_SHEETS_INVALID_URL_UA,
    VOCAB_SHEETS_NOT_BOUND_UA,
    VOCAB_SHEETS_SAVED_UA,
    VOCAB_SHEETS_TIMEOUT_UA,
    VOCAB_SHEETS_TOO_LARGE_UA,
    VOCAB_SHEETS_UPSTREAM_ERROR_UA,
    VOCAB_UNSUPPORTED_CONTENT_UA,
    VOCAB_UNSUPPORTED_EXTENSION_UA,
    XLSX_CONTENT_TYPE,
    VocabStates,
    format_import_summary,
    handle_vocab_cancel,
    handle_vocab_import_document,
    handle_vocab_import_text,
    handle_vocab_import_unsupported_content,
    handle_vocab_sheets_sync,
    handle_vocab_sheets_url_input,
    handle_vocab_sheets_url_non_text,
    handle_vocab_start_import,
    handle_vocab_start_sheets_bind,
    handle_vocabulary_menu,
)
from bot_app.keyboards import (
    MENU_BTN_SETTINGS,
    MENU_BTN_VOCABULARY,
    VOCAB_CANCEL_CALLBACK,
    VOCAB_IMPORT_CALLBACK,
    VOCAB_SHEETS_BIND_CALLBACK,
    VOCAB_SHEETS_SYNC_CALLBACK,
)
from bot_app.main import create_dispatcher
from bot_app.schemas import (
    ExerciseEvaluation,
    SessionAnswerResponse,
    SessionSummary,
    UserProfile,
    WordImportResult,
)

BASE_URL = "http://backend.test:8000"
SECRET_API_KEY = "test-internal-api-key-15"
BOT_ID = 777015
USER_ID = 555111222
CHAT_ID = 555111222


def _make_settings(miniapp_url: str | None = "https://deutsch.example.com") -> BotSettings:
    return BotSettings(
        _env_file=None,
        bot_token="123456:TEST-TOKEN",
        internal_api_key=SECRET_API_KEY,
        backend_api_url=BASE_URL,
        miniapp_url=miniapp_url,
    )


def _make_fsm() -> tuple[MemoryStorage, FSMContext]:
    storage = MemoryStorage()
    key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
    return storage, FSMContext(storage=storage, key=key)


def _make_api_mock() -> AsyncMock:
    return AsyncMock(spec=BackendAPIClient)


def _make_message(
    text: str | None = None,
    *,
    document: SimpleNamespace | None = None,
    bot: object | None = None,
    user_id: int = USER_ID,
) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        document=document,
        bot=bot,
        answer=AsyncMock(),
    )


def _make_callback(data: str, user_id: int = USER_ID) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        data=data,
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
    )


def _make_document(
    file_name: str | None = "words.txt",
    file_size: int | None = 128,
    file_id: str = "doc-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        file_name=file_name,
        file_size=file_size,
        file_id=file_id,
    )


def _make_bot_with_download(content: bytes) -> SimpleNamespace:
    async def _download(file: object, destination: io.BytesIO | None = None, **_: object) -> object:
        assert destination is not None
        destination.write(content)
        destination.seek(0)
        return destination

    return SimpleNamespace(download=AsyncMock(side_effect=_download))


def _make_profile(sheets_url: str | None = None) -> UserProfile:
    return UserProfile(
        telegram_id=USER_ID,
        level="A2",
        exercises_per_set=5,
        theme="system",
        sheets_url=sheets_url,
        reminder_hour=19,
        timezone="Europe/Kyiv",
        daily_llm_calls=0,
        created_at=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
    )


def _flatten_inline_callbacks(markup: InlineKeyboardMarkup) -> list[str]:
    return [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data is not None
    ]


async def test_vocabulary_menu_clears_fsm_and_shows_inline_actions() -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_sheets_url)
    message = _make_message(MENU_BTN_VOCABULARY)

    await handle_vocabulary_menu(message=message, state=state)

    assert await state.get_state() is None
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == VOCAB_MENU_TEXT_UA
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert _flatten_inline_callbacks(markup) == [
        VOCAB_IMPORT_CALLBACK,
        VOCAB_SHEETS_BIND_CALLBACK,
        VOCAB_SHEETS_SYNC_CALLBACK,
    ]


async def test_start_import_sets_waiting_import_with_cancel_button() -> None:
    _, state = _make_fsm()
    callback = _make_callback(VOCAB_IMPORT_CALLBACK)

    await handle_vocab_start_import(callback=callback, state=state)

    callback.answer.assert_awaited_once()
    assert await state.get_state() == VocabStates.waiting_import.state
    callback.message.answer.assert_awaited_once()
    assert callback.message.answer.await_args.args[0] == VOCAB_IMPORT_PROMPT_UA
    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_CANCEL_CALLBACK]


async def test_text_import_success_clears_state_and_reports_counters() -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    api_client.import_words_text.return_value = WordImportResult(
        imported=3,
        duplicates=1,
        skipped=0,
    )
    raw_text = "der Zug - потяг\ndie Verspätung - затримка\ndas Treffen - зустріч"
    message = _make_message(raw_text)

    await handle_vocab_import_text(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    api_client.import_words_text.assert_awaited_once_with(USER_ID, raw_text)
    assert await state.get_state() is None
    message.answer.assert_awaited_once()
    reply = message.answer.await_args.args[0]
    assert "3" in reply
    assert "1" in reply
    assert "0" in reply
    assert "der Zug - потяг" not in reply  # no format error hint on success
    assert isinstance(message.answer.await_args.kwargs["reply_markup"], ReplyKeyboardMarkup)


async def test_text_import_all_duplicates_reports_counters_and_clears_state() -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    api_client.import_words_text.return_value = WordImportResult(
        imported=0,
        duplicates=4,
        skipped=0,
    )
    message = _make_message("der Zug - потяг")

    await handle_vocab_import_text(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    assert await state.get_state() is None
    reply = message.answer.await_args.args[0]
    assert "0" in reply
    assert "4" in reply
    assert "Не вдалося розпізнати" not in reply


async def test_text_import_zero_imported_and_skipped_adds_format_hint_and_keeps_state() -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    api_client.import_words_text.return_value = WordImportResult(
        imported=0,
        duplicates=0,
        skipped=3,
    )
    message = _make_message("некоректний рядок без розділювача")

    await handle_vocab_import_text(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    assert await state.get_state() == VocabStates.waiting_import.state
    reply = message.answer.await_args.args[0]
    assert "0" in reply
    assert "3" in reply
    assert "der Zug - потяг" in reply
    assert "der Zug; потяг" in reply
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_CANCEL_CALLBACK]


@pytest.mark.parametrize(
    ("exc", "expected_fragment"),
    [
        (
            ValidationAPIError("Текст порожній.", status_code=422, detail="Текст порожній."),
            "der Zug",
        ),
        (RateLimitError(RATE_LIMIT_MESSAGE_UA, status_code=429), "ліміт"),
        (APIUnavailableError(UNAVAILABLE_ERROR_MESSAGE_UA, status_code=503), "недоступний"),
        (APIUnavailableError(TIMEOUT_ERROR_MESSAGE_UA, status_code=None), "довго"),
    ],
)
async def test_text_import_handles_api_errors_preserving_state_and_cancel(
    exc: Exception,
    expected_fragment: str,
) -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    api_client.import_words_text.side_effect = exc
    message = _make_message("der Zug - потяг")

    await handle_vocab_import_text(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    assert await state.get_state() == VocabStates.waiting_import.state
    reply = message.answer.await_args.args[0]
    assert expected_fragment.lower() in reply.lower()
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_CANCEL_CALLBACK]


@pytest.mark.parametrize(
    ("filename", "expected_content_type"),
    [
        ("words.txt", "text/plain"),
        ("vocab.csv", "text/csv"),
        ("UPPERCASE.CSV", "text/csv"),
        ("words.xlsx", XLSX_CONTENT_TYPE),
        ("VOCAB.XLSX", XLSX_CONTENT_TYPE),
    ],
)
async def test_document_import_valid_txt_csv_and_xlsx_uploads_to_backend(
    filename: str,
    expected_content_type: str,
) -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    api_client.import_words_file.return_value = WordImportResult(
        imported=5,
        duplicates=2,
        skipped=1,
    )
    payload = "der Zug;потяг\ndie Verspätung;затримка\n".encode()
    bot = _make_bot_with_download(payload)
    doc = _make_document(file_name=filename, file_size=len(payload))
    message = _make_message(document=doc, bot=bot)

    await handle_vocab_import_document(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    bot.download.assert_awaited_once()
    api_client.import_words_file.assert_awaited_once_with(
        USER_ID,
        filename,
        payload,
        content_type=expected_content_type,
    )
    assert await state.get_state() is None
    reply = message.answer.await_args.args[0]
    assert "5" in reply
    assert "2" in reply
    assert "1" in reply


async def test_document_import_exact_2mb_file_is_allowed() -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    api_client.import_words_file.return_value = WordImportResult(
        imported=1,
        duplicates=0,
        skipped=0,
    )
    payload = b"a" * MAX_IMPORT_FILE_BYTES
    bot = _make_bot_with_download(payload)
    doc = _make_document(file_name="exact.txt", file_size=MAX_IMPORT_FILE_BYTES)
    message = _make_message(document=doc, bot=bot)

    await handle_vocab_import_document(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    bot.download.assert_awaited_once()
    api_client.import_words_file.assert_awaited_once()
    assert await state.get_state() is None


@pytest.mark.parametrize(
    "bad_filename",
    ["book.pdf", "notes.docx", "archive.zip", "no_extension", ""],
)
async def test_document_import_rejects_unsupported_extension_before_download(
    bad_filename: str,
) -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    bot = _make_bot_with_download(b"should-never-be-downloaded")
    doc = _make_document(file_name=bad_filename, file_size=512)
    message = _make_message(document=doc, bot=bot)

    await handle_vocab_import_document(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    bot.download.assert_not_awaited()
    api_client.import_words_file.assert_not_awaited()
    assert await state.get_state() == VocabStates.waiting_import.state
    assert message.answer.await_args.args[0] == VOCAB_UNSUPPORTED_EXTENSION_UA
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_CANCEL_CALLBACK]


async def test_document_import_rejects_oversized_file_before_download() -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    bot = _make_bot_with_download(b"should-never-be-downloaded")
    doc = _make_document(file_name="huge.csv", file_size=MAX_IMPORT_FILE_BYTES + 1)
    message = _make_message(document=doc, bot=bot)

    await handle_vocab_import_document(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    bot.download.assert_not_awaited()
    api_client.import_words_file.assert_not_awaited()
    assert await state.get_state() == VocabStates.waiting_import.state
    assert message.answer.await_args.args[0] == VOCAB_FILE_TOO_LARGE_UA
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_CANCEL_CALLBACK]


async def test_document_import_rejects_oversized_bytes_when_telegram_omits_file_size() -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    bot = _make_bot_with_download(b"x" * (MAX_IMPORT_FILE_BYTES + 10))
    doc = _make_document(file_name="unknown_size.txt", file_size=None)
    message = _make_message(document=doc, bot=bot)

    await handle_vocab_import_document(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    api_client.import_words_file.assert_not_awaited()
    assert message.answer.await_args.args[0] == VOCAB_FILE_TOO_LARGE_UA
    assert await state.get_state() == VocabStates.waiting_import.state


async def test_document_import_rejects_empty_file_without_api_call() -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    bot = _make_bot_with_download(b"")
    doc = _make_document(file_name="empty.txt", file_size=0)
    message = _make_message(document=doc, bot=bot)

    await handle_vocab_import_document(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    api_client.import_words_file.assert_not_awaited()
    assert message.answer.await_args.args[0] == VOCAB_EMPTY_FILE_UA
    assert await state.get_state() == VocabStates.waiting_import.state


@pytest.mark.parametrize(
    ("exc", "expected_fragment"),
    [
        (
            BadRequestError(
                "Файл має некоректне кодування UTF-8.",
                status_code=400,
                detail="Файл має некоректне кодування UTF-8.",
            ),
            "utf-8",
        ),
        (
            APIClientError("Payload too large", status_code=413),
            "2 мб",
        ),
        (
            ValidationAPIError(
                "Завантажений файл порожній.",
                status_code=422,
                detail="Завантажений файл порожній.",
            ),
            "der zug",
        ),
        (
            ValidationAPIError(
                "Invalid format",
                status_code=422,
                detail="Invalid format",
            ),
            "utf-8",
        ),
        (
            RateLimitError(RATE_LIMIT_MESSAGE_UA, status_code=429),
            "ліміт",
        ),
        (
            APIUnavailableError(UNAVAILABLE_ERROR_MESSAGE_UA, status_code=503),
            "недоступний",
        ),
    ],
)
async def test_document_import_handles_backend_errors_gracefully(
    exc: Exception,
    expected_fragment: str,
) -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    api_client.import_words_file.side_effect = exc
    bot = _make_bot_with_download(b"broken;content\n")
    doc = _make_document(file_name="broken.csv", file_size=15)
    message = _make_message(document=doc, bot=bot)

    await handle_vocab_import_document(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    assert await state.get_state() == VocabStates.waiting_import.state
    reply = message.answer.await_args.args[0]
    assert expected_fragment in reply.lower()
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_CANCEL_CALLBACK]


async def test_document_import_handles_telegram_download_error() -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    bot = SimpleNamespace(
        download=AsyncMock(
            side_effect=TelegramBadRequest(
                method=GetFile(file_id="doc-1"),
                message="Bad Request: file is temporarily unavailable",
            ),
        ),
    )
    doc = _make_document(file_name="words.txt", file_size=20)
    message = _make_message(document=doc, bot=bot)

    await handle_vocab_import_document(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    api_client.import_words_file.assert_not_awaited()
    assert message.answer.await_args.args[0] == VOCAB_FILE_DOWNLOAD_ERROR_UA
    assert await state.get_state() == VocabStates.waiting_import.state


async def test_document_import_zero_imported_skipped_shows_hint_and_keeps_state() -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    api_client.import_words_file.return_value = WordImportResult(
        imported=0,
        duplicates=0,
        skipped=5,
    )
    bot = _make_bot_with_download(b"just single words\nwithout translations\n")
    doc = _make_document(file_name="bad_lines.txt", file_size=39)
    message = _make_message(document=doc, bot=bot)

    await handle_vocab_import_document(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    assert await state.get_state() == VocabStates.waiting_import.state
    reply = message.answer.await_args.args[0]
    assert "der Zug - потяг" in reply
    assert "der Zug; потяг" in reply


async def test_waiting_import_rejects_non_text_non_document_content() -> None:
    message = _make_message(text=None, document=None)
    await handle_vocab_import_unsupported_content(message=message)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == VOCAB_UNSUPPORTED_CONTENT_UA
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_CANCEL_CALLBACK]


async def test_start_sheets_bind_sets_waiting_sheets_url_with_cancel_button() -> None:
    _, state = _make_fsm()
    callback = _make_callback(VOCAB_SHEETS_BIND_CALLBACK)

    await handle_vocab_start_sheets_bind(callback=callback, state=state)

    callback.answer.assert_awaited_once()
    assert await state.get_state() == VocabStates.waiting_sheets_url.state
    assert callback.message.answer.await_args.args[0] == VOCAB_SHEETS_BIND_PROMPT_UA
    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_CANCEL_CALLBACK]


async def test_sheets_bind_invalid_url_422_shows_ukrainian_hint_and_allows_retry() -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_sheets_url)
    api_client = _make_api_mock()
    api_client.update_settings.side_effect = ValidationAPIError(
        "Value error, sheets_url must point to docs.google.com",
        status_code=422,
        detail="Value error, sheets_url must point to docs.google.com",
    )
    bad_msg = _make_message("https://example.com/not-a-sheet")

    await handle_vocab_sheets_url_input(
        message=bad_msg,
        state=state,
        api_client=api_client,
    )

    api_client.update_settings.assert_awaited_once_with(
        USER_ID,
        sheets_url="https://example.com/not-a-sheet",
    )
    assert await state.get_state() == VocabStates.waiting_sheets_url.state
    assert bad_msg.answer.await_args.args[0] == VOCAB_SHEETS_INVALID_URL_UA
    assert "Value error" not in bad_msg.answer.await_args.args[0]
    markup = bad_msg.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_CANCEL_CALLBACK]

    # Retry with a valid public Google Sheets link
    valid_url = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjK/edit#gid=0"
    api_client.update_settings.side_effect = None
    api_client.update_settings.return_value = _make_profile(sheets_url=valid_url)
    good_msg = _make_message(valid_url)

    await handle_vocab_sheets_url_input(
        message=good_msg,
        state=state,
        api_client=api_client,
    )

    assert await state.get_state() is None
    assert good_msg.answer.await_args.args[0] == VOCAB_SHEETS_SAVED_UA
    sync_markup = good_msg.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(sync_markup) == [VOCAB_SHEETS_SYNC_CALLBACK]


@pytest.mark.parametrize(
    ("exc", "expected_fragment"),
    [
        (RateLimitError(RATE_LIMIT_MESSAGE_UA, status_code=429), "ліміт"),
        (APIUnavailableError(UNAVAILABLE_ERROR_MESSAGE_UA, status_code=503), "недоступний"),
    ],
)
async def test_sheets_bind_handles_rate_limit_and_unavailable(
    exc: Exception,
    expected_fragment: str,
) -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_sheets_url)
    api_client = _make_api_mock()
    api_client.update_settings.side_effect = exc
    msg = _make_message("https://docs.google.com/spreadsheets/d/1AbCdEf/edit")

    await handle_vocab_sheets_url_input(message=msg, state=state, api_client=api_client)

    assert await state.get_state() == VocabStates.waiting_sheets_url.state
    assert expected_fragment in msg.answer.await_args.args[0].lower()
    assert _flatten_inline_callbacks(msg.answer.await_args.kwargs["reply_markup"]) == [
        VOCAB_CANCEL_CALLBACK,
    ]


async def test_sheets_bind_rejects_non_text_input() -> None:
    msg = _make_message(text=None)
    await handle_vocab_sheets_url_non_text(message=msg)

    msg.answer.assert_awaited_once()
    assert _flatten_inline_callbacks(msg.answer.await_args.kwargs["reply_markup"]) == [
        VOCAB_CANCEL_CALLBACK,
    ]


async def test_sheets_sync_success_outputs_summary() -> None:
    api_client = _make_api_mock()
    api_client.sync_sheets.return_value = WordImportResult(
        imported=12,
        duplicates=3,
        skipped=0,
    )
    callback = _make_callback(VOCAB_SHEETS_SYNC_CALLBACK)

    await handle_vocab_sheets_sync(callback=callback, api_client=api_client)

    callback.answer.assert_awaited_once()
    api_client.sync_sheets.assert_awaited_once_with(USER_ID)
    reply = callback.message.answer.await_args.args[0]
    assert "Google Sheets" in reply
    assert "12" in reply
    assert "3" in reply
    assert "0" in reply


async def test_sheets_sync_missing_url_prompts_to_bind() -> None:
    api_client = _make_api_mock()
    api_client.sync_sheets.side_effect = BadRequestError(
        "Посилання на Google Sheets не налаштоване.",
        status_code=400,
        detail="Посилання на Google Sheets не налаштоване.",
    )
    callback = _make_callback(VOCAB_SHEETS_SYNC_CALLBACK)

    await handle_vocab_sheets_sync(callback=callback, api_client=api_client)

    callback.answer.assert_awaited_once()
    assert callback.message.answer.await_args.args[0] == VOCAB_SHEETS_NOT_BOUND_UA
    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_SHEETS_BIND_CALLBACK]


@pytest.mark.parametrize(
    ("exc", "expected_fragment"),
    [
        (
            BadRequestError(
                "Таблиця Google Sheets приватна або недоступна.",
                status_code=400,
                detail="Таблиця Google Sheets приватна або недоступна.",
            ),
            "приватна",
        ),
        (
            BadRequestError("Sheet is private", status_code=400, detail="Sheet is private"),
            VOCAB_SHEETS_ACCESS_ERROR_UA,
        ),
        (
            ValidationAPIError("Invalid sheet", status_code=422, detail="Invalid sheet"),
            VOCAB_SHEETS_ACCESS_ERROR_UA,
        ),
        (
            APIClientError("Too large", status_code=413),
            VOCAB_SHEETS_TOO_LARGE_UA,
        ),
        (
            APIUnavailableError(UNAVAILABLE_ERROR_MESSAGE_UA, status_code=502),
            VOCAB_SHEETS_UPSTREAM_ERROR_UA,
        ),
        (
            APIUnavailableError(TIMEOUT_ERROR_MESSAGE_UA, status_code=504),
            VOCAB_SHEETS_TIMEOUT_UA,
        ),
        (
            RateLimitError(RATE_LIMIT_MESSAGE_UA, status_code=429),
            "ліміт",
        ),
    ],
)
async def test_sheets_sync_handles_private_sheet_network_timeout_and_rate_limit(
    exc: Exception,
    expected_fragment: str,
) -> None:
    api_client = _make_api_mock()
    api_client.sync_sheets.side_effect = exc
    callback = _make_callback(VOCAB_SHEETS_SYNC_CALLBACK)

    await handle_vocab_sheets_sync(callback=callback, api_client=api_client)

    callback.answer.assert_awaited_once()
    reply = callback.message.answer.await_args.args[0]
    assert expected_fragment.lower() in reply.lower()


@pytest.mark.parametrize(
    "active_state",
    [VocabStates.waiting_import, VocabStates.waiting_sheets_url],
)
async def test_vocab_cancel_clears_fsm_and_restores_main_menu(active_state: object) -> None:
    _, state = _make_fsm()
    await state.set_state(active_state)  # type: ignore[arg-type]
    callback = _make_callback(VOCAB_CANCEL_CALLBACK)

    await handle_vocab_cancel(callback=callback, state=state, settings=_make_settings())

    assert await state.get_state() is None
    callback.answer.assert_awaited_once()
    assert callback.message.answer.await_args.args[0] == VOCAB_CANCELLED_UA
    reply_markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert isinstance(reply_markup, ReplyKeyboardMarkup)


def test_format_import_summary_for_sheets_with_zero_imported_adds_column_hint() -> None:
    rendered = format_import_summary(
        WordImportResult(imported=0, duplicates=0, skipped=4),
        from_sheets=True,
    )
    assert "Google Sheets" in rendered
    assert "стовпці" in rendered


async def test_dispatcher_training_waiting_answer_is_not_hijacked_by_vocabulary_router() -> None:
    api_client = _make_api_mock()
    api_client.submit_answer.return_value = SessionAnswerResponse(
        session_id=42,
        attempt_id=101,
        status="completed",
        completed_exercises=1,
        total_exercises=1,
        used_hint=False,
        evaluation=ExerciseEvaluation(
            is_correct=True,
            grammar_score=100,
            corrected_answer="der Zug - потяг",
            word_ratings=[],
            errors=[],
            youtube_topic="Wortschatz A1",
            summary_ua="Чудово!",
        ),
        summary=SessionSummary(
            total_exercises=1,
            correct_count=1,
            avg_grammar_score=100.0,
            weak_topics=[],
        ),
    )
    dispatcher = create_dispatcher(api_client=api_client, settings=_make_settings())
    key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
    await dispatcher.storage.set_state(key, TrainingStates.waiting_answer)
    await dispatcher.storage.set_data(key, {"session_id": 42})

    bot = MagicMock()
    bot.id = BOT_ID
    sent_payloads: list[object] = []

    async def _capture(method: object, request_timeout: int | None = None) -> None:
        sent_payloads.append(method)

    bot.side_effect = _capture

    # Text matching vocabulary format or menu button must still go to training answer handler.
    msg = Message(
        message_id=10,
        date=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="Tester"),
        text="der Zug - потяг",
    )
    await dispatcher.feed_update(bot, Update(update_id=1, message=msg))

    api_client.submit_answer.assert_awaited_once_with(USER_ID, 42, "der Zug - потяг")
    api_client.import_words_text.assert_not_awaited()


async def test_dispatcher_document_in_waiting_answer_goes_to_training_not_vocabulary() -> None:
    api_client = _make_api_mock()
    dispatcher = create_dispatcher(api_client=api_client, settings=_make_settings())
    key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
    await dispatcher.storage.set_state(key, TrainingStates.waiting_answer)
    await dispatcher.storage.set_data(key, {"session_id": 42})

    bot = MagicMock()
    bot.id = BOT_ID
    sent_payloads: list[object] = []

    async def _capture(method: object, request_timeout: int | None = None) -> None:
        sent_payloads.append(method)

    bot.side_effect = _capture

    msg = Message(
        message_id=11,
        date=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="Tester"),
        document=Document(
            file_id="doc-file-1",
            file_unique_id="doc-uniq-1",
            file_name="words.txt",
            file_size=40,
        ),
    )
    await dispatcher.feed_update(bot, Update(update_id=2, message=msg))

    api_client.import_words_file.assert_not_awaited()
    assert await dispatcher.storage.get_state(key) == TrainingStates.waiting_answer.state
    assert getattr(sent_payloads[0], "text", "") == NON_TEXT_ANSWER_MESSAGE_UA


@pytest.mark.parametrize(
    "initial_state",
    [VocabStates.waiting_import, VocabStates.waiting_sheets_url],
)
@respx.mock
async def test_dispatcher_start_command_clears_vocabulary_fsm_state(
    initial_state: object,
) -> None:
    respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "telegram_id": USER_ID,
                "level": "A2",
                "exercises_per_set": 5,
                "theme": "system",
                "sheets_url": None,
                "reminder_hour": 19,
                "timezone": "Europe/Kyiv",
                "daily_llm_calls": 0,
                "created_at": "2026-11-24T12:00:00Z",
            },
        ),
    )
    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        dispatcher = create_dispatcher(api_client=client, settings=_make_settings())
        key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
        await dispatcher.storage.set_state(key, initial_state)  # type: ignore[arg-type]

        bot = MagicMock()
        bot.id = BOT_ID
        sent_payloads: list[object] = []

        async def _capture(method: object, request_timeout: int | None = None) -> None:
            sent_payloads.append(method)

        bot.side_effect = _capture

        start_msg = Message(
            message_id=20,
            date=datetime(2026, 11, 24, 12, 5, tzinfo=UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=USER_ID, is_bot=False, first_name="Tester"),
            text="/start",
        )
        await dispatcher.feed_update(bot, Update(update_id=3, message=start_msg))

        assert await dispatcher.storage.get_state(key) is None
        assert any("Вітаю!" in getattr(p, "text", "") for p in sent_payloads)


async def test_dispatcher_tapping_settings_menu_in_waiting_import_opens_settings() -> None:
    api_client = _make_api_mock()
    api_client.get_me.return_value = _make_profile()
    dispatcher = create_dispatcher(api_client=api_client, settings=_make_settings())
    key = StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)
    await dispatcher.storage.set_state(key, VocabStates.waiting_import)

    bot = MagicMock()
    bot.id = BOT_ID
    sent_payloads: list[object] = []

    async def _capture(method: object, request_timeout: int | None = None) -> None:
        sent_payloads.append(method)

    bot.side_effect = _capture

    msg = Message(
        message_id=25,
        date=datetime(2026, 11, 24, 12, 6, tzinfo=UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="Tester"),
        text=MENU_BTN_SETTINGS,
    )
    await dispatcher.feed_update(bot, Update(update_id=4, message=msg))

    api_client.import_words_text.assert_not_awaited()
    api_client.get_me.assert_awaited_once_with(USER_ID)
    assert await dispatcher.storage.get_state(key) is None
    assert "Налаштування" in getattr(sent_payloads[0], "text", "")


@respx.mock
async def test_vocabulary_text_import_and_sheets_sync_end_to_end_via_api_client() -> None:
    import_route = respx.post(f"{BASE_URL}/api/words/import").mock(
        return_value=httpx.Response(200, json={"imported": 2, "duplicates": 1, "skipped": 0}),
    )
    sync_route = respx.post(f"{BASE_URL}/api/words/sync-sheets").mock(
        return_value=httpx.Response(
            504,
            json={"detail": "Google Sheets timeout"},
        ),
    )
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    msg = _make_message("der Zug - потяг\ndie Verspätung - затримка")
    sync_cb = _make_callback(VOCAB_SHEETS_SYNC_CALLBACK)

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        await handle_vocab_import_text(
            message=msg,
            state=state,
            api_client=client,
            settings=_make_settings(),
        )
        await handle_vocab_sheets_sync(callback=sync_cb, api_client=client)

    assert import_route.called
    assert sync_route.called
    assert await state.get_state() is None
    assert "2" in msg.answer.await_args.args[0]
    sync_cb.answer.assert_awaited_once()
    assert sync_cb.message.answer.await_args.args[0] == VOCAB_SHEETS_TIMEOUT_UA


def test_vocabulary_copy_constants_mention_xlsx_alongside_txt_and_csv() -> None:
    expected_exts = frozenset({".txt", ".csv", ".xlsx"})
    assert expected_exts == ALLOWED_IMPORT_EXTENSIONS
    for text in (
        VOCAB_MENU_TEXT_UA,
        VOCAB_IMPORT_PROMPT_UA,
        VOCAB_EMPTY_TEXT_UA,
        VOCAB_UNSUPPORTED_EXTENSION_UA,
        VOCAB_UNSUPPORTED_CONTENT_UA,
        VOCAB_FILE_PARSE_ERROR_UA,
    ):
        assert ".txt" in text
        assert ".csv" in text
        assert ".xlsx" in text

    assert ".xls" in VOCAB_LEGACY_XLS_UA
    assert ".xlsx" in VOCAB_LEGACY_XLS_UA
    assert ".csv" in VOCAB_LEGACY_XLS_UA
    assert ".txt" in VOCAB_LEGACY_XLS_UA


@pytest.mark.parametrize("legacy_filename", ["old_words.xls", "LEGACY.XLS"])
async def test_document_import_rejects_legacy_xls_before_download_with_guidance(
    legacy_filename: str,
) -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    bot = _make_bot_with_download(b"should-never-be-downloaded")
    doc = _make_document(file_name=legacy_filename, file_size=512)
    message = _make_message(document=doc, bot=bot)

    await handle_vocab_import_document(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    bot.download.assert_not_awaited()
    api_client.import_words_file.assert_not_awaited()
    assert await state.get_state() == VocabStates.waiting_import.state
    assert message.answer.await_args.args[0] == VOCAB_LEGACY_XLS_UA
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_CANCEL_CALLBACK]


async def test_document_import_xlsx_enforces_pre_and_post_download_2mb_limits() -> None:
    # 1. Pre-download check rejects > 2 MiB .xlsx without downloading
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    bot = _make_bot_with_download(b"should-never-download")
    oversized_doc = _make_document(file_name="huge.xlsx", file_size=MAX_IMPORT_FILE_BYTES + 1)
    msg_pre = _make_message(document=oversized_doc, bot=bot)

    await handle_vocab_import_document(
        message=msg_pre,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    bot.download.assert_not_awaited()
    api_client.import_words_file.assert_not_awaited()
    assert msg_pre.answer.await_args.args[0] == VOCAB_FILE_TOO_LARGE_UA
    assert await state.get_state() == VocabStates.waiting_import.state

    # 2. Post-download check rejects > 2 MiB .xlsx when Telegram omits file_size
    bot_post = _make_bot_with_download(b"PK\x03\x04" + b"0" * MAX_IMPORT_FILE_BYTES)
    unknown_size_doc = _make_document(file_name="HUGE.XLSX", file_size=None)
    msg_post = _make_message(document=unknown_size_doc, bot=bot_post)

    await handle_vocab_import_document(
        message=msg_post,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    bot_post.download.assert_awaited_once()
    api_client.import_words_file.assert_not_awaited()
    assert msg_post.answer.await_args.args[0] == VOCAB_FILE_TOO_LARGE_UA
    assert await state.get_state() == VocabStates.waiting_import.state


@pytest.mark.parametrize(
    ("exc", "expected_fragment"),
    [
        (
            BadRequestError(
                "Не вдалося прочитати файл .xlsx: файл пошкоджений.",
                status_code=400,
                detail="Не вдалося прочитати файл .xlsx: файл пошкоджений.",
            ),
            "пошкоджений",
        ),
        (
            BadRequestError(
                "Файл .xlsx перевищує безпечний ліміт розпакованого розміру (20 МБ).",
                status_code=400,
                detail="Файл .xlsx перевищує безпечний ліміт розпакованого розміру (20 МБ).",
            ),
            "20 мб",
        ),
        (
            APIClientError("Too large", status_code=413),
            "2 мб",
        ),
        (
            ValidationAPIError(
                "Завантажений файл порожній.",
                status_code=422,
                detail="Завантажений файл порожній.",
            ),
            "порожній",
        ),
        (
            APIUnavailableError(UNAVAILABLE_ERROR_MESSAGE_UA, status_code=503),
            "недоступний",
        ),
    ],
)
async def test_document_import_xlsx_handles_backend_errors_and_keeps_state(
    exc: Exception,
    expected_fragment: str,
) -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    api_client.import_words_file.side_effect = exc
    bot = _make_bot_with_download(b"PK\x03\x04corrupted")
    doc = _make_document(file_name="broken.xlsx", file_size=13)
    message = _make_message(document=doc, bot=bot)

    await handle_vocab_import_document(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    assert await state.get_state() == VocabStates.waiting_import.state
    reply = message.answer.await_args.args[0]
    assert expected_fragment in reply.lower()
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_CANCEL_CALLBACK]


async def test_document_import_xlsx_zero_imported_and_skipped_keeps_state_with_cancel() -> None:
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    api_client = _make_api_mock()
    api_client.import_words_file.return_value = WordImportResult(
        imported=0,
        duplicates=0,
        skipped=4,
    )
    bot = _make_bot_with_download(b"PK\x03\x04dummy-xlsx")
    doc = _make_document(file_name="bad_rows.xlsx", file_size=14)
    message = _make_message(document=doc, bot=bot)

    await handle_vocab_import_document(
        message=message,
        state=state,
        api_client=api_client,
        settings=_make_settings(),
    )

    assert await state.get_state() == VocabStates.waiting_import.state
    reply = message.answer.await_args.args[0]
    assert "0" in reply
    assert "4" in reply
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _flatten_inline_callbacks(markup) == [VOCAB_CANCEL_CALLBACK]


@respx.mock
async def test_document_import_xlsx_end_to_end_via_api_client_sends_filename_and_mime() -> None:
    xlsx_bytes = b"PK\x03\x04fake-xlsx-payload"
    import_route = respx.post(f"{BASE_URL}/api/words/import").mock(
        return_value=httpx.Response(200, json={"imported": 3, "duplicates": 1, "skipped": 0}),
    )
    _, state = _make_fsm()
    await state.set_state(VocabStates.waiting_import)
    bot = _make_bot_with_download(xlsx_bytes)
    doc = _make_document(file_name="MyDeck.XLSX", file_size=len(xlsx_bytes))
    msg = _make_message(document=doc, bot=bot)

    async with BackendAPIClient(base_url=BASE_URL, internal_api_key=SECRET_API_KEY) as client:
        await handle_vocab_import_document(
            message=msg,
            state=state,
            api_client=client,
            settings=_make_settings(),
        )

    assert import_route.called
    request = import_route.calls.last.request
    assert request.headers["X-Internal-API-Key"] == SECRET_API_KEY
    assert request.headers["X-Telegram-User-Id"] == str(USER_ID)
    assert b'filename="MyDeck.XLSX"' in request.content
    assert XLSX_CONTENT_TYPE.encode("ascii") in request.content
    assert xlsx_bytes in request.content
    assert await state.get_state() is None
    reply = msg.answer.await_args.args[0]
    assert "3" in reply
    assert "1" in reply
    assert "0" in reply
    assert isinstance(msg.answer.await_args.kwargs["reply_markup"], ReplyKeyboardMarkup)
