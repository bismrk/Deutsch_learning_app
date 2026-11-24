"""Vocabulary import FSM flow: plain text, .txt/.csv/.xlsx files, and Google Sheets sync."""

from __future__ import annotations

import html
import io
from pathlib import Path
from typing import Final

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Document, InaccessibleMessage, Message

from bot_app.api_client import (
    APIClientError,
    APIUnavailableError,
    BackendAPIClient,
    BackendAPIError,
    BadRequestError,
    RateLimitError,
    ValidationAPIError,
)
from bot_app.config import BotSettings
from bot_app.keyboards import (
    MAIN_MENU_BUTTONS,
    MENU_BTN_VOCABULARY,
    VOCAB_CANCEL_CALLBACK,
    VOCAB_IMPORT_CALLBACK,
    VOCAB_SHEETS_BIND_CALLBACK,
    VOCAB_SHEETS_SYNC_CALLBACK,
    build_main_menu_keyboard,
    build_sheets_bind_prompt_keyboard,
    build_sheets_bound_keyboard,
    build_vocab_cancel_keyboard,
    build_vocabulary_menu_keyboard,
)
from bot_app.schemas import WordImportResult

router = Router(name="vocabulary")

MAX_IMPORT_FILE_BYTES: Final = 2 * 1024 * 1024  # 2 MiB
ALLOWED_IMPORT_EXTENSIONS: Final[frozenset[str]] = frozenset({".txt", ".csv", ".xlsx"})
XLSX_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
_IMPORT_CONTENT_TYPES: Final[dict[str, str]] = {
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".xlsx": XLSX_CONTENT_TYPE,
}

VOCAB_MENU_TEXT_UA: Final = (
    "📚 <b>Словник</b>\n\n"
    "Тут ти можеш поповнити свій особистий словник для тренувань:\n"
    "• надіслати слова повідомленням (наприклад, <code>der Zug - потяг</code>);\n"
    "• завантажити файл <code>.txt</code>, <code>.csv</code> або <code>.xlsx</code> (до 2 МБ);\n"
    "• підключити публічну таблицю Google Sheets і синхронізувати її в один дотик."
)
VOCAB_IMPORT_PROMPT_UA: Final = (
    "✍️ <b>Додавання слів</b>\n\n"
    "Надішли список слів повідомленням (кожна пара з нового рядка) "
    "або прикріпи файл <code>.txt</code>, <code>.csv</code> чи <code>.xlsx</code> до 2 МБ.\n\n"
    "<b>Приклади формату:</b>\n"
    "<code>der Zug - потяг</code>\n"
    "<code>die Verspätung; затримка</code>\n"
    "Для <code>.xlsx</code> — два або три стовпці: німецьке слово, переклад "
    "і (за бажанням) частина мови."
)
VOCAB_FORMAT_HINT_UA: Final = (
    "⚠️ Не вдалося розпізнати жодного слова. Перевір формат рядків і спробуй ще раз:\n"
    "• <code>der Zug - потяг</code>\n"
    "• <code>der Zug; потяг</code>"
)
VOCAB_SHEETS_FORMAT_HINT_UA: Final = (
    "⚠️ У таблиці не знайдено жодної коректної пари слів. "
    "Перевір, що в першому стовпці вказано німецьке слово, а в другому — переклад українською."
)
VOCAB_EMPTY_TEXT_UA: Final = (
    "Повідомлення порожнє. Надішли слова у форматі <code>der Zug - потяг</code> "
    "або прикріпи файл <code>.txt</code>, <code>.csv</code> чи <code>.xlsx</code>."
)
VOCAB_LEGACY_XLS_UA: Final = (
    "⚠️ Застарілий формат Excel (<code>.xls</code>) не підтримується.\n\n"
    "Збережи таблицю як <code>.xlsx</code>, <code>.csv</code> або <code>.txt</code> "
    "і надішли файл ще раз або натисни «Скасувати»."
)
VOCAB_UNSUPPORTED_EXTENSION_UA: Final = (
    "⚠️ Непідтримуваний формат файлу. Підтримуються лише файли <code>.txt</code>, "
    "<code>.csv</code> та <code>.xlsx</code>.\n\nНадішли коректний файл або натисни «Скасувати»."
)
VOCAB_FILE_TOO_LARGE_UA: Final = (
    "⚠️ Файл завеликий: максимальний розмір становить 2 МБ.\n\n"
    "Розділи список на менші частини й надішли файл ще раз або натисни «Скасувати»."
)
VOCAB_EMPTY_FILE_UA: Final = (
    "⚠️ Завантажений файл порожній. Перевір вміст і надішли інший файл або натисни «Скасувати»."
)
VOCAB_FILE_DOWNLOAD_ERROR_UA: Final = (
    "Не вдалося завантажити файл із Telegram. "
    "Спробуй надіслати його ще раз або натисни «Скасувати»."
)
VOCAB_FILE_PARSE_ERROR_UA: Final = (
    "⚠️ Не вдалося обробити файл. Для <code>.txt</code> і <code>.csv</code> переконайся, "
    "що файл збережено в кодуванні UTF-8 (<code>der Zug - потяг</code> або "
    "<code>der Zug; потяг</code>), а для <code>.xlsx</code> — що файл Excel не пошкоджений."
)
VOCAB_UNSUPPORTED_CONTENT_UA: Final = (
    "Будь ласка, надішли слова текстом (наприклад, <code>der Zug - потяг</code>) "
    "або прикріпи файл <code>.txt</code>, <code>.csv</code> чи <code>.xlsx</code>."
)
VOCAB_SHEETS_BIND_PROMPT_UA: Final = (
    "🔗 <b>Прив'язка Google Sheets</b>\n\n"
    "Надішли публічне посилання на свою таблицю Google Sheets у форматі:\n"
    "<code>https://docs.google.com/spreadsheets/d/...</code>\n\n"
    "Переконайся, що для таблиці відкрито доступ "
    "«Усі, хто має посилання — переглядач», а слова розміщені у двох стовпцях "
    "(німецьке слово — переклад українською)."
)
VOCAB_SHEETS_EMPTY_URL_UA: Final = (
    "Посилання не може бути порожнім. "
    "Надішли публічну адресу Google Sheets або натисни «Скасувати»."
)
VOCAB_SHEETS_NON_TEXT_UA: Final = (
    "Будь ласка, надішли посилання на Google Sheets текстовим повідомленням "
    "або натисни «Скасувати»."
)
VOCAB_SHEETS_INVALID_URL_UA: Final = (
    "⚠️ Некоректне посилання на Google Sheets.\n\n"
    "Переконайся, що адреса починається з "
    "<code>https://docs.google.com/spreadsheets/...</code>, "
    "і надішли посилання ще раз або натисни «Скасувати»."
)
VOCAB_SHEETS_SAVED_UA: Final = (
    "✅ Посилання на Google Sheets збережено!\n\n"
    "Натисни кнопку нижче, щоб синхронізувати слова просто зараз."
)
VOCAB_SHEETS_NOT_BOUND_UA: Final = (
    "⚠️ Посилання на Google Sheets ще не налаштоване.\n\n"
    "Спочатку прив'яжи публічну таблицю за кнопкою нижче."
)
VOCAB_SHEETS_ACCESS_ERROR_UA: Final = (
    "⚠️ Не вдалося прочитати таблицю Google Sheets.\n\n"
    "Перевір, чи коректне посилання та чи відкрито доступ "
    "«Усі, хто має посилання — переглядач»."
)
VOCAB_SHEETS_TIMEOUT_UA: Final = (
    "⏳ Google Sheets відповідає занадто довго. Спробуй синхронізувати ще раз за хвилину."
)
VOCAB_SHEETS_UPSTREAM_ERROR_UA: Final = (
    "⚠️ Не вдалося завантажити таблицю через тимчасовий збій Google Sheets або мережі. "
    "Спробуй ще раз трохи пізніше."
)
VOCAB_SHEETS_TOO_LARGE_UA: Final = (
    "⚠️ Таблиця Google Sheets завелика для синхронізації (максимум 2 МБ)."
)
VOCAB_CANCELLED_UA: Final = (
    "Дію скасовано. Обирай потрібний розділ у головному меню нижче."
)


class VocabStates(StatesGroup):
    """Finite state machine for vocabulary import and Google Sheets binding."""

    waiting_import = State()
    waiting_sheets_url = State()


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _get_accessible_message(callback: CallbackQuery) -> Message | None:
    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        return None
    return callback.message


def _contains_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


def _is_missing_sheets_url_error(detail: str | None) -> bool:
    if not detail:
        return False
    lowered = detail.lower()
    return (
        "не налаштоване" in lowered
        or "не прив'язане" in lowered
        or "sheets_url" in lowered
        or "not configured" in lowered
    )


def format_import_summary(result: WordImportResult, *, from_sheets: bool = False) -> str:
    """Format Ukrainian vocabulary import or Google Sheets sync counters."""
    title = (
        "🔄 <b>Синхронізацію Google Sheets завершено</b>"
        if from_sheets
        else "✅ <b>Імпорт завершено</b>"
    )
    summary = (
        f"{title}\n\n"
        f"• Додано нових слів: <b>{result.imported}</b>\n"
        f"• Дублікатів пропущено: <b>{result.duplicates}</b>\n"
        f"• Пропущено рядків: <b>{result.skipped}</b>"
    )
    if result.imported == 0 and result.skipped > 0:
        hint = VOCAB_SHEETS_FORMAT_HINT_UA if from_sheets else VOCAB_FORMAT_HINT_UA
        return f"{summary}\n\n{hint}"
    return summary


async def _download_document_bytes(bot: Bot, document: Document) -> bytes:
    """Download Telegram document bytes into an in-memory buffer."""
    buffer = io.BytesIO()
    result = await bot.download(document, destination=buffer)
    if result is not None and result is not buffer:
        result.seek(0)
        return result.read()
    return buffer.getvalue()


@router.message(F.text == MENU_BTN_VOCABULARY)
@router.message(Command("vocab", "vocabulary"))
async def handle_vocabulary_menu(message: Message, state: FSMContext) -> None:
    """Display the vocabulary management inline menu and clear any previous FSM input state."""
    if message.from_user is None:
        return

    await state.clear()
    await message.answer(
        VOCAB_MENU_TEXT_UA,
        parse_mode="HTML",
        reply_markup=build_vocabulary_menu_keyboard(),
    )


@router.callback_query(F.data == VOCAB_IMPORT_CALLBACK)
async def handle_vocab_start_import(callback: CallbackQuery, state: FSMContext) -> None:
    """Enter ``VocabStates.waiting_import`` to receive text or a ``.txt``/``.csv``/``.xlsx``."""
    await callback.answer()
    if callback.from_user is None:
        return

    await state.set_state(VocabStates.waiting_import)
    target = _get_accessible_message(callback)
    if target is not None:
        await target.answer(
            VOCAB_IMPORT_PROMPT_UA,
            parse_mode="HTML",
            reply_markup=build_vocab_cancel_keyboard(),
        )


@router.callback_query(F.data == VOCAB_SHEETS_BIND_CALLBACK)
async def handle_vocab_start_sheets_bind(callback: CallbackQuery, state: FSMContext) -> None:
    """Enter ``VocabStates.waiting_sheets_url`` to bind a public Google Sheets link."""
    await callback.answer()
    if callback.from_user is None:
        return

    await state.set_state(VocabStates.waiting_sheets_url)
    target = _get_accessible_message(callback)
    if target is not None:
        await target.answer(
            VOCAB_SHEETS_BIND_PROMPT_UA,
            parse_mode="HTML",
            reply_markup=build_vocab_cancel_keyboard(),
        )


@router.callback_query(F.data == VOCAB_CANCEL_CALLBACK)
async def handle_vocab_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    """Cancel active vocabulary FSM input, clear state, and restore the main menu."""
    await state.clear()
    await callback.answer("Дію скасовано.")

    target = _get_accessible_message(callback)
    if target is not None:
        await target.answer(
            VOCAB_CANCELLED_UA,
            reply_markup=build_main_menu_keyboard(settings.miniapp_url),
        )


@router.callback_query(F.data == VOCAB_SHEETS_SYNC_CALLBACK)
async def handle_vocab_sheets_sync(
    callback: CallbackQuery,
    api_client: BackendAPIClient,
) -> None:
    """Trigger manual Google Sheets vocabulary sync via ``POST /api/words/sync-sheets``."""
    await callback.answer()
    if callback.from_user is None:
        return

    target = _get_accessible_message(callback)
    if target is None:
        return

    try:
        result = await api_client.sync_sheets(callback.from_user.id)
    except BadRequestError as exc:
        if _is_missing_sheets_url_error(exc.detail):
            await target.answer(
                VOCAB_SHEETS_NOT_BOUND_UA,
                reply_markup=build_sheets_bind_prompt_keyboard(),
            )
            return
        if exc.detail and _contains_cyrillic(exc.detail):
            await target.answer(
                f"⚠️ {_escape(exc.detail)}\n\n"
                "Перевір налаштування доступу «Усі, хто має посилання — переглядач» "
                "або онови посилання на таблицю.",
                parse_mode="HTML",
                reply_markup=build_sheets_bind_prompt_keyboard(),
            )
            return
        await target.answer(
            VOCAB_SHEETS_ACCESS_ERROR_UA,
            reply_markup=build_sheets_bind_prompt_keyboard(),
        )
        return
    except ValidationAPIError:
        await target.answer(
            VOCAB_SHEETS_ACCESS_ERROR_UA,
            reply_markup=build_sheets_bind_prompt_keyboard(),
        )
        return
    except RateLimitError as exc:
        await target.answer(exc.user_message)
        return
    except APIClientError as exc:
        if exc.status_code == 413:
            await target.answer(VOCAB_SHEETS_TOO_LARGE_UA)
            return
        await target.answer(exc.user_message)
        return
    except APIUnavailableError as exc:
        if exc.status_code == 504:
            await target.answer(VOCAB_SHEETS_TIMEOUT_UA)
            return
        if exc.status_code == 502:
            await target.answer(VOCAB_SHEETS_UPSTREAM_ERROR_UA)
            return
        await target.answer(exc.user_message)
        return
    except BackendAPIError as exc:
        await target.answer(exc.user_message)
        return

    await target.answer(
        format_import_summary(result, from_sheets=True),
        parse_mode="HTML",
    )


@router.message(
    VocabStates.waiting_import,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MAIN_MENU_BUTTONS),
)
async def handle_vocab_import_text(
    message: Message,
    state: FSMContext,
    api_client: BackendAPIClient,
    settings: BotSettings,
) -> None:
    """Import vocabulary lines sent as plain text via ``POST /api/words/import``."""
    if message.from_user is None:
        return

    cleaned_text = (message.text or "").strip()
    if not cleaned_text:
        await message.answer(
            VOCAB_EMPTY_TEXT_UA,
            parse_mode="HTML",
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    try:
        result = await api_client.import_words_text(message.from_user.id, cleaned_text)
    except ValidationAPIError as exc:
        error_prefix = (
            f"⚠️ {_escape(exc.detail)}\n\n"
            if exc.detail and _contains_cyrillic(exc.detail)
            else ""
        )
        await message.answer(
            f"{error_prefix}{VOCAB_FORMAT_HINT_UA}",
            parse_mode="HTML",
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except BadRequestError as exc:
        body = (
            f"⚠️ {_escape(exc.detail)}"
            if exc.detail and _contains_cyrillic(exc.detail)
            else VOCAB_FORMAT_HINT_UA
        )
        await message.answer(
            body,
            parse_mode="HTML",
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except RateLimitError as exc:
        await message.answer(
            exc.user_message,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except APIClientError as exc:
        text_msg = (
            VOCAB_FILE_TOO_LARGE_UA
            if exc.status_code == 413
            else exc.user_message
        )
        await message.answer(
            text_msg,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except APIUnavailableError as exc:
        await message.answer(
            exc.user_message,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except BackendAPIError as exc:
        await message.answer(
            exc.user_message,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    summary_text = format_import_summary(result)
    if result.imported == 0 and result.skipped > 0:
        await message.answer(
            summary_text,
            parse_mode="HTML",
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    await state.clear()
    await message.answer(
        summary_text,
        parse_mode="HTML",
        reply_markup=build_main_menu_keyboard(settings.miniapp_url),
    )


@router.message(VocabStates.waiting_import, F.document)
async def handle_vocab_import_document(
    message: Message,
    state: FSMContext,
    api_client: BackendAPIClient,
    settings: BotSettings,
    bot: Bot | None = None,
) -> None:
    """Validate `.txt`/`.csv`/`.xlsx` extension and <= 2 MiB size before download, then import."""
    if message.from_user is None or message.document is None:
        return

    document = message.document
    filename = (document.file_name or "").strip()
    suffix = Path(filename).suffix.lower()

    if suffix == ".xls":
        await message.answer(
            VOCAB_LEGACY_XLS_UA,
            parse_mode="HTML",
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    if suffix not in ALLOWED_IMPORT_EXTENSIONS:
        await message.answer(
            VOCAB_UNSUPPORTED_EXTENSION_UA,
            parse_mode="HTML",
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    if document.file_size is not None and document.file_size > MAX_IMPORT_FILE_BYTES:
        await message.answer(
            VOCAB_FILE_TOO_LARGE_UA,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    active_bot = bot if bot is not None else getattr(message, "bot", None)
    if active_bot is None:
        await message.answer(
            VOCAB_FILE_DOWNLOAD_ERROR_UA,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    try:
        content = await _download_document_bytes(active_bot, document)
    except (TelegramAPIError, OSError):
        await message.answer(
            VOCAB_FILE_DOWNLOAD_ERROR_UA,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    if len(content) > MAX_IMPORT_FILE_BYTES:
        await message.answer(
            VOCAB_FILE_TOO_LARGE_UA,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    if not content:
        await message.answer(
            VOCAB_EMPTY_FILE_UA,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    content_type = _IMPORT_CONTENT_TYPES.get(suffix, "text/plain")
    try:
        result = await api_client.import_words_file(
            message.from_user.id,
            filename,
            content,
            content_type=content_type,
        )
    except BadRequestError as exc:
        body = (
            f"⚠️ {_escape(exc.detail)}"
            if exc.detail and _contains_cyrillic(exc.detail)
            else VOCAB_FILE_PARSE_ERROR_UA
        )
        await message.answer(
            body,
            parse_mode="HTML",
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except ValidationAPIError as exc:
        body = (
            f"⚠️ {_escape(exc.detail)}\n\n{VOCAB_FORMAT_HINT_UA}"
            if exc.detail and _contains_cyrillic(exc.detail)
            else VOCAB_FILE_PARSE_ERROR_UA
        )
        await message.answer(
            body,
            parse_mode="HTML",
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except RateLimitError as exc:
        await message.answer(
            exc.user_message,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except APIClientError as exc:
        text_msg = (
            VOCAB_FILE_TOO_LARGE_UA
            if exc.status_code == 413
            else exc.user_message
        )
        await message.answer(
            text_msg,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except APIUnavailableError as exc:
        await message.answer(
            exc.user_message,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except BackendAPIError as exc:
        await message.answer(
            exc.user_message,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    summary_text = format_import_summary(result)
    if result.imported == 0 and result.skipped > 0:
        await message.answer(
            summary_text,
            parse_mode="HTML",
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    await state.clear()
    await message.answer(
        summary_text,
        parse_mode="HTML",
        reply_markup=build_main_menu_keyboard(settings.miniapp_url),
    )


@router.message(VocabStates.waiting_import, ~F.text, ~F.document)
async def handle_vocab_import_unsupported_content(message: Message) -> None:
    """Reject stickers, photos, voice notes, etc. while waiting for vocabulary input."""
    await message.answer(
        VOCAB_UNSUPPORTED_CONTENT_UA,
        parse_mode="HTML",
        reply_markup=build_vocab_cancel_keyboard(),
    )


@router.message(
    VocabStates.waiting_sheets_url,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MAIN_MENU_BUTTONS),
)
async def handle_vocab_sheets_url_input(
    message: Message,
    state: FSMContext,
    api_client: BackendAPIClient,
) -> None:
    """Save a public Google Sheets URL via ``PATCH /api/users/settings``."""
    if message.from_user is None:
        return

    raw_url = (message.text or "").strip()
    if not raw_url:
        await message.answer(
            VOCAB_SHEETS_EMPTY_URL_UA,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    try:
        await api_client.update_settings(message.from_user.id, sheets_url=raw_url)
    except ValidationAPIError:
        await message.answer(
            VOCAB_SHEETS_INVALID_URL_UA,
            parse_mode="HTML",
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except RateLimitError as exc:
        await message.answer(
            exc.user_message,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except APIUnavailableError as exc:
        await message.answer(
            exc.user_message,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return
    except BackendAPIError as exc:
        await message.answer(
            exc.user_message,
            reply_markup=build_vocab_cancel_keyboard(),
        )
        return

    await state.clear()
    await message.answer(
        VOCAB_SHEETS_SAVED_UA,
        reply_markup=build_sheets_bound_keyboard(),
    )


@router.message(VocabStates.waiting_sheets_url, ~F.text)
async def handle_vocab_sheets_url_non_text(message: Message) -> None:
    """Reject non-text messages while waiting for a Google Sheets URL."""
    await message.answer(
        VOCAB_SHEETS_NON_TEXT_UA,
        reply_markup=build_vocab_cancel_keyboard(),
    )


__all__ = [
    "ALLOWED_IMPORT_EXTENSIONS",
    "MAX_IMPORT_FILE_BYTES",
    "VOCAB_CANCELLED_UA",
    "VOCAB_EMPTY_FILE_UA",
    "VOCAB_EMPTY_TEXT_UA",
    "VOCAB_FILE_DOWNLOAD_ERROR_UA",
    "VOCAB_FILE_PARSE_ERROR_UA",
    "VOCAB_FILE_TOO_LARGE_UA",
    "VOCAB_FORMAT_HINT_UA",
    "VOCAB_IMPORT_PROMPT_UA",
    "VOCAB_LEGACY_XLS_UA",
    "VOCAB_MENU_TEXT_UA",
    "VOCAB_SHEETS_ACCESS_ERROR_UA",
    "VOCAB_SHEETS_BIND_PROMPT_UA",
    "VOCAB_SHEETS_EMPTY_URL_UA",
    "VOCAB_SHEETS_FORMAT_HINT_UA",
    "VOCAB_SHEETS_INVALID_URL_UA",
    "VOCAB_SHEETS_NON_TEXT_UA",
    "VOCAB_SHEETS_NOT_BOUND_UA",
    "VOCAB_SHEETS_SAVED_UA",
    "VOCAB_SHEETS_TIMEOUT_UA",
    "VOCAB_SHEETS_TOO_LARGE_UA",
    "VOCAB_SHEETS_UPSTREAM_ERROR_UA",
    "VOCAB_UNSUPPORTED_CONTENT_UA",
    "VOCAB_UNSUPPORTED_EXTENSION_UA",
    "XLSX_CONTENT_TYPE",
    "VocabStates",
    "format_import_summary",
    "handle_vocab_cancel",
    "handle_vocab_import_document",
    "handle_vocab_import_text",
    "handle_vocab_import_unsupported_content",
    "handle_vocab_sheets_sync",
    "handle_vocab_sheets_url_input",
    "handle_vocab_sheets_url_non_text",
    "handle_vocab_start_import",
    "handle_vocab_start_sheets_bind",
    "handle_vocabulary_menu",
    "router",
]
