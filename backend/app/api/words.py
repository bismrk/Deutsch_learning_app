"""FastAPI endpoints for personal vocabulary management, file/text import, and Sheets sync."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path as FilePath
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from app.api.deps import get_current_user_id, get_utc_now
from app.core.sheets_sync import (
    MAX_IMPORT_BYTES,
    InvalidImportEncodingError,
    InvalidSheetsUrlError,
    ParsedWordEntry,
    ParseFormatHint,
    ParseResult,
    PayloadTooLargeError,
    SheetsAccessError,
    SheetsSyncError,
    SheetsTimeoutError,
    SheetsUpstreamError,
    XlsxParseError,
    decode_utf8_bytes,
    normalize_word_key,
    parse_vocabulary_text,
    parse_xlsx_bytes,
    sync_google_sheet,
)
from app.db.base import get_db
from app.db.models import Deck, UserWordFSRS, Word
from app.db.users import DEFAULT_DECK_TITLE, ensure_user_initialized
from app.schemas.words import (
    SheetsSyncRequest,
    WordImportJsonRequest,
    WordImportResponse,
    WordItemResponse,
    WordListResponse,
)

router = APIRouter(prefix="/words", tags=["words"])

_ALLOWED_UPLOAD_EXTENSIONS = frozenset({".txt", ".csv", ".xlsx"})
_XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_XLS_MIME_TYPE = "application/vnd.ms-excel"
_UPLOAD_CHUNK_SIZE = 64 * 1024  # 64 KiB
_INSERT_BATCH_SIZE = 1000
_PG_INT32_MAX = 2_147_483_647
_WORDS_CONFLICT_INDEX_ELEMENTS = [Word.deck_id, text("lower(TRIM(BOTH FROM word_de))")]


def _raise_http_for_sync_error(exc: SheetsSyncError) -> NoReturn:
    if isinstance(exc, PayloadTooLargeError):
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(exc),
        ) from exc
    if isinstance(exc, SheetsTimeoutError):
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    if isinstance(exc, SheetsUpstreamError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    if isinstance(
        exc,
        (InvalidSheetsUrlError, SheetsAccessError, InvalidImportEncodingError, XlsxParseError),
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=str(exc),
    ) from exc


async def _get_personal_deck(db: AsyncSession, user_id: int) -> Deck:
    stmt = (
        select(Deck)
        .where(Deck.owner_id == user_id, Deck.title == DEFAULT_DECK_TITLE)
        .order_by(Deck.id.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one()


async def _read_upload_bounded(
    upload: StarletteUploadFile,
    *,
    max_bytes: int = MAX_IMPORT_BYTES,
) -> bytes:
    buffer = bytearray()
    total = 0
    try:
        while True:
            chunk = await upload.read(_UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PayloadTooLargeError(
                    "Файл завеликий: максимальний розмір становить 2 МБ.",
                )
            buffer.extend(chunk)
    finally:
        await upload.close()
    return bytes(buffer)


def _escape_like_pattern(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _persist_parsed_words(
    db: AsyncSession,
    *,
    user_id: int,
    deck_id: int,
    parse_result: ParseResult,
    source: Literal["text", "file", "sheets"],
    now: datetime,
) -> WordImportResponse:
    seen_keys: set[str] = set()
    unique_entries: list[ParsedWordEntry] = []
    intra_batch_duplicates = 0

    for entry in parse_result.entries:
        key = normalize_word_key(entry.word_de)
        if key in seen_keys:
            intra_batch_duplicates += 1
            continue
        seen_keys.add(key)
        unique_entries.append(entry)

    inserted_word_ids: list[int] = []
    for start in range(0, len(unique_entries), _INSERT_BATCH_SIZE):
        batch = unique_entries[start : start + _INSERT_BATCH_SIZE]
        word_rows = [
            {
                "deck_id": deck_id,
                "word_de": entry.word_de,
                "translation_ua": entry.translation_ua,
                "part_of_speech": entry.part_of_speech,
                "source": source,
            }
            for entry in batch
        ]
        insert_words_stmt = (
            pg_insert(Word)
            .values(word_rows)
            .on_conflict_do_nothing(index_elements=_WORDS_CONFLICT_INDEX_ELEMENTS)
            .returning(Word.id)
        )
        result = await db.execute(insert_words_stmt)
        inserted_word_ids.extend(result.scalars().all())

    if inserted_word_ids:
        for start in range(0, len(inserted_word_ids), _INSERT_BATCH_SIZE):
            id_batch = inserted_word_ids[start : start + _INSERT_BATCH_SIZE]
            fsrs_rows = [
                {
                    "user_id": user_id,
                    "word_id": word_id,
                    "state": 0,
                    "stability": 0.0,
                    "difficulty": 0.0,
                    "due_at": now,
                    "reps": 0,
                    "lapses": 0,
                    "last_review": None,
                }
                for word_id in id_batch
            ]
            insert_fsrs_stmt = (
                pg_insert(UserWordFSRS)
                .values(fsrs_rows)
                .on_conflict_do_nothing(
                    index_elements=[UserWordFSRS.user_id, UserWordFSRS.word_id],
                )
            )
            await db.execute(insert_fsrs_stmt)

    await db.commit()

    imported = len(inserted_word_ids)
    db_duplicates = len(unique_entries) - imported
    return WordImportResponse(
        imported=imported,
        duplicates=intra_batch_duplicates + db_duplicates,
        skipped=parse_result.skipped,
    )


async def _read_request_body_bounded(
    request: Request,
    *,
    max_bytes: int = MAX_IMPORT_BYTES,
) -> bytes:
    buffer = bytearray()
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError("Запит завеликий: максимальний розмір становить 2 МБ.")
        buffer.extend(chunk)
    return bytes(buffer)


async def _extract_and_parse_import(
    request: Request,
) -> tuple[ParseResult, Literal["text", "file"]]:
    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()

    if content_type == "application/json":
        raw_body = await _read_request_body_bounded(request, max_bytes=MAX_IMPORT_BYTES)
        if not raw_body.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Передайте текст або файл (.txt / .csv / .xlsx) для імпорту.",
            )
        try:
            decoded_body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Некоректний JSON у запиті імпорту.",
            ) from exc

        try:
            parsed_json = WordImportJsonRequest.model_validate(decoded_body)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Передайте непорожнє текстове поле 'text' для імпорту.",
            ) from exc

        if not parsed_json.text.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Текст для імпорту не може бути порожнім.",
            )
        return parse_vocabulary_text(parsed_json.text, format_hint="text"), "text"

    if content_type in {"multipart/form-data", "application/x-www-form-urlencoded"}:
        # URL-encoded UTF-8 expands up to 3x (%XX per byte); multipart text parts are raw UTF-8.
        form_max_part_size = (
            MAX_IMPORT_BYTES * 3
            if content_type == "application/x-www-form-urlencoded"
            else MAX_IMPORT_BYTES
        )
        try:
            form = await request.form(max_part_size=form_max_part_size)
        except (MultiPartException, StarletteHTTPException) as exc:
            detail_text = (
                exc.message if isinstance(exc, MultiPartException) else str(exc.detail)
            ).lower()
            if "exceeded maximum size" in detail_text:
                raise PayloadTooLargeError(
                    "Текст завеликий: максимальний розмір становить 2 МБ.",
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Некоректний формат multipart-запиту.",
            ) from exc

        raw_file = form.get("file")
        raw_text = form.get("text")

        if isinstance(raw_text, str) and len(raw_text.encode("utf-8")) > MAX_IMPORT_BYTES:
            if isinstance(raw_file, StarletteUploadFile):
                await raw_file.close()
            raise PayloadTooLargeError("Текст завеликий: максимальний розмір становить 2 МБ.")

        upload = raw_file if isinstance(raw_file, StarletteUploadFile) else None
        text_value = raw_text if isinstance(raw_text, str) and raw_text.strip() else None

        if upload is not None and text_value is not None:
            await upload.close()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Передайте або текст, або файл — не обидва одночасно.",
            )

        if upload is not None:
            raw_bytes = await _read_upload_bounded(upload, max_bytes=MAX_IMPORT_BYTES)
            if not raw_bytes:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Завантажений файл порожній.",
                )
            suffix = FilePath(upload.filename or "").suffix.lower()
            upload_content_type = (
                (upload.content_type or "").split(";", 1)[0].strip().lower()
            )
            if suffix == ".xls" or (not suffix and upload_content_type == _XLS_MIME_TYPE):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Застарілий формат Excel (.xls) не підтримується. "
                        "Збережіть таблицю як .xlsx, .csv або .txt."
                    ),
                )
            if suffix and suffix not in _ALLOWED_UPLOAD_EXTENSIONS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Непідтримуваний формат файлу. "
                        "Підтримуються лише файли форматів .txt, .csv та .xlsx."
                    ),
                )
            if suffix == ".xlsx" or (not suffix and upload_content_type == _XLSX_MIME_TYPE):
                return parse_xlsx_bytes(raw_bytes), "file"

            decoded_text = decode_utf8_bytes(raw_bytes)
            if not decoded_text.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Завантажений файл порожній.",
                )
            format_hint: ParseFormatHint = "csv" if suffix == ".csv" else "text"
            return parse_vocabulary_text(decoded_text, format_hint=format_hint), "file"

        if text_value is not None:
            return parse_vocabulary_text(text_value, format_hint="text"), "text"

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Передайте текст або файл (.txt / .csv / .xlsx) для імпорту.",
        )

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="Передайте текст або файл (.txt / .csv / .xlsx) для імпорту.",
    )


@router.get("", response_model=WordListResponse)
async def list_words(
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
    search: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=_PG_INT32_MAX)] = 0,
) -> WordListResponse:
    """List words in the current user's personal deck with FSRS card state, search & pagination."""
    _, changed = await ensure_user_initialized(db, user_id)
    if changed:
        await db.commit()

    filters = [Deck.owner_id == user_id]
    search_clean = search.strip() if search is not None else ""
    if search_clean:
        pattern = f"%{_escape_like_pattern(search_clean)}%"
        filters.append(
            or_(
                Word.word_de.ilike(pattern, escape="\\"),
                Word.translation_ua.ilike(pattern, escape="\\"),
            ),
        )

    count_stmt = (
        select(func.count(Word.id))
        .join(Deck, Word.deck_id == Deck.id)
        .where(*filters)
    )
    total = int((await db.execute(count_stmt)).scalar_one())

    if total == 0:
        return WordListResponse(items=[], total=0)

    items_stmt = (
        select(Word, UserWordFSRS)
        .join(Deck, Word.deck_id == Deck.id)
        .outerjoin(
            UserWordFSRS,
            (UserWordFSRS.word_id == Word.id) & (UserWordFSRS.user_id == user_id),
        )
        .where(*filters)
        .order_by(Word.id.asc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(items_stmt)).all()

    items = [
        WordItemResponse(
            id=word.id,
            word_de=word.word_de,
            translation_ua=word.translation_ua,
            part_of_speech=word.part_of_speech,
            source=word.source,  # type: ignore[arg-type]
            state=card.state if card is not None else 0,
            stability=card.stability if card is not None else 0.0,
            difficulty=card.difficulty if card is not None else 0.0,
            due_at=card.due_at if card is not None else None,
            reps=card.reps if card is not None else 0,
            lapses=card.lapses if card is not None else 0,
        )
        for word, card in rows
    ]
    return WordListResponse(items=items, total=total)


@router.post(
    "/import",
    response_model=WordImportResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": WordImportJsonRequest.model_json_schema(),
                },
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "file": {"type": "string", "format": "binary"},
                        },
                    },
                },
            },
        },
    },
)
async def import_words(
    request: Request,
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
    now: Annotated[datetime, Depends(get_utc_now)],
) -> WordImportResponse:
    """Import vocabulary words from JSON/form text or an uploaded ``.txt``/``.csv``/``.xlsx``."""
    _, changed = await ensure_user_initialized(db, user_id)
    if changed:
        await db.commit()

    try:
        parse_result, source = await _extract_and_parse_import(request)
    except SheetsSyncError as exc:
        _raise_http_for_sync_error(exc)

    deck = await _get_personal_deck(db, user_id)
    return await _persist_parsed_words(
        db,
        user_id=user_id,
        deck_id=deck.id,
        parse_result=parse_result,
        source=source,
        now=now,
    )


@router.post("/sync-sheets", response_model=WordImportResponse)
async def sync_sheets_words(
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
    now: Annotated[datetime, Depends(get_utc_now)],
    payload: Annotated[SheetsSyncRequest | None, Body()] = None,
) -> WordImportResponse:
    """Sync vocabulary words from a public Google Sheets CSV URL into the user's personal deck."""
    user, changed = await ensure_user_initialized(db, user_id)
    if changed:
        await db.commit()

    effective_url: str | None
    if payload is not None and payload.sheets_url is not None:
        if not payload.sheets_url.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Посилання на Google Sheets не може бути порожнім.",
            )
        effective_url = payload.sheets_url
    else:
        effective_url = user.sheets_url

    if not effective_url or not effective_url.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Посилання на Google Sheets не налаштоване. "
                "Передайте sheets_url або збережіть його в налаштуваннях."
            ),
        )

    try:
        parse_result = await sync_google_sheet(effective_url)
    except SheetsSyncError as exc:
        _raise_http_for_sync_error(exc)

    deck = await _get_personal_deck(db, user_id)
    return await _persist_parsed_words(
        db,
        user_id=user_id,
        deck_id=deck.id,
        parse_result=parse_result,
        source="sheets",
        now=now,
    )


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_word(
    word_id: Annotated[int, Path(alias="id")],
    user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Delete a word owned by the current user along with its FSRS card."""
    _, changed = await ensure_user_initialized(db, user_id)
    if changed:
        await db.commit()

    if word_id < 1 or word_id > _PG_INT32_MAX:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Слово не знайдено.",
        )

    stmt = (
        select(Word)
        .join(Deck, Word.deck_id == Deck.id)
        .where(Word.id == word_id, Deck.owner_id == user_id)
    )
    word = (await db.execute(stmt)).scalar_one_or_none()
    if word is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Слово не знайдено.",
        )

    await db.delete(word)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
