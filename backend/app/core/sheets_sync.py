"""Vocabulary parser (.txt/.csv/.xlsx) and SSRF-hardened Google Sheets CSV synchronizer."""

from __future__ import annotations

import csv
import io
import math
import re
import zipfile
import zlib
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

import httpx
import openpyxl

ALLOWED_SHEETS_HOST = "docs.google.com"
ALLOWED_REDIRECT_SUFFIX = ".googleusercontent.com"
GOOGLE_ACCOUNTS_HOST = "accounts.google.com"

MAX_SHEETS_URL_LENGTH = 2048
MAX_IMPORT_BYTES = 2 * 1024 * 1024  # 2 MiB
MAX_XLSX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024  # 20 MiB
MAX_XLSX_ZIP_ENTRIES = 512
_XLSX_ZIP_READ_CHUNK_SIZE = 64 * 1024  # 64 KiB
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
DEFAULT_SHEETS_TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 5

ParseFormatHint = Literal["text", "csv", "sheets"]

_STANDARD_SHEETS_PATH_RE = re.compile(
    r"^/spreadsheets(?:/u/\d+)?/d/([A-Za-z0-9_-]+)(?:/(?:edit|view|export|htmlview|copy))?/?$",
)
_PUBLISHED_SHEETS_PATH_RE = re.compile(
    r"^/spreadsheets(?:/u/\d+)?/d/e/([A-Za-z0-9_-]+)/(?:pub|pubhtml)/?$",
)
_ASCII_DIGITS_RE = re.compile(r"^[0-9]+$")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

_SPACED_DASH_RE = re.compile(r"\s+[-\u2013\u2014]\s+")
_UNICODE_DASH_RE = re.compile(r"\s*[\u2013\u2014]\s*")
_LEADING_DASH_RE = re.compile(r"^\s*[-\u2013\u2014]\s+")
_QUOTED_COMMA_RE = re.compile(r'(?:,\s*"|"\s*,)')
_QUOTED_SEMICOLON_RE = re.compile(r'(?:;\s*"|"\s*;)')
_HEADER_NORMALIZE_SPACE_RE = re.compile(r"[\s_\-]+")
_HEADER_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")

_HEADER_WORD_LABELS = frozenset(
    {
        "word",
        "words",
        "word de",
        "de",
        "deutsch",
        "german",
        "wort",
        "worter",
        "wörter",
        "vokabel",
        "vokabeln",
        "term",
        "слово",
        "слова",
        "німецька",
        "німецьке слово",
        "німецькою",
        "термін",
    },
)

_HEADER_TRANSLATION_LABELS = frozenset(
    {
        "translation",
        "translations",
        "translation ua",
        "ua",
        "uk",
        "ukrainian",
        "ukrainisch",
        "ubersetzung",
        "übersetzung",
        "meaning",
        "переклад",
        "українська",
        "українською",
        "значення",
    },
)

_HTML_PREFIXES = ("<!doctype html", "<html", "<head", "<script", "<body")


class SheetsSyncError(Exception):
    """Base error for vocabulary import and Google Sheets synchronization."""


class InvalidSheetsUrlError(SheetsSyncError):
    """Raised when a Google Sheets URL or redirect target fails validation or SSRF checks."""


class SheetsAccessError(SheetsSyncError):
    """Raised when a Google Sheet is private, missing, or returns HTML instead of CSV."""


class PayloadTooLargeError(SheetsSyncError):
    """Raised when an imported file or Google Sheets CSV exceeds the 2 MiB size limit."""


class InvalidImportEncodingError(SheetsSyncError):
    """Raised when an imported payload is not valid UTF-8 text or contains binary bytes."""


class XlsxParseError(SheetsSyncError):
    """Raised when an uploaded .xlsx archive is corrupted, unsafe (ZIP bomb), or invalid."""


class SheetsTimeoutError(SheetsSyncError):
    """Raised when downloading a Google Sheets CSV times out."""


class SheetsUpstreamError(SheetsSyncError):
    """Raised when Google Sheets returns a 5xx response or fails due to a transport error."""


@dataclass(frozen=True, slots=True)
class ParsedWordEntry:
    """Single parsed vocabulary record ready for deduplication and DB storage."""

    word_de: str
    translation_ua: str
    part_of_speech: str | None = None


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Result of parsing vocabulary text, uploaded file, or Google Sheets CSV."""

    entries: list[ParsedWordEntry]
    skipped: int


def normalize_word_key(word_de: str) -> str:
    """Return the canonical deduplication key matching ``lower(TRIM(BOTH FROM word_de))``."""
    return word_de.strip().lower()


def decode_utf8_bytes(data: bytes) -> str:
    """Decode UTF-8 / UTF-8-SIG bytes and reject binary or non-UTF-8 payloads."""
    if b"\x00" in data:
        raise InvalidImportEncodingError(
            "Файл містить бінарні дані. Завантажте текстовий файл (.txt або .csv) "
            "у кодуванні UTF-8.",
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidImportEncodingError(
            "Некоректне кодування файлу. Файл повинен бути у кодуванні UTF-8 "
            "(підтримуються .txt та .csv).",
        ) from exc


def _normalize_header_token(value: str) -> str:
    cleaned = _HEADER_PAREN_RE.sub(" ", value.strip().lower())
    return _HEADER_NORMALIZE_SPACE_RE.sub(" ", cleaned).strip()


def _is_header_row(col1: str, col2: str) -> bool:
    return (
        _normalize_header_token(col1) in _HEADER_WORD_LABELS
        and _normalize_header_token(col2) in _HEADER_TRANSLATION_LABELS
    )


def _extract_entry_from_columns(columns: list[str]) -> ParsedWordEntry | None:
    if len(columns) < 2:
        return None
    word_de = columns[0].strip()
    translation_ua = columns[1].strip()
    if not word_de or not translation_ua:
        return None
    part_of_speech: str | None = None
    if len(columns) >= 3:
        candidate = columns[2].strip()
        if candidate:
            part_of_speech = candidate
    return ParsedWordEntry(
        word_de=word_de,
        translation_ua=translation_ua,
        part_of_speech=part_of_speech,
    )


def _parse_csv_line(raw_line: str, delimiter: str) -> ParsedWordEntry | None:
    try:
        rows = list(csv.reader([raw_line], delimiter=delimiter, skipinitialspace=True))
    except csv.Error:
        return None
    if not rows:
        return None
    return _extract_entry_from_columns(rows[0])


def _parse_single_text_line(raw_line: str) -> ParsedWordEntry | None:
    """Parse one non-empty vocabulary line supporting all configured delimiters."""
    line_no_eol = raw_line.rstrip("\r\n")
    stripped = line_no_eol.strip()
    if not stripped:
        return None

    # 1. Tab-separated columns (preserve leading/trailing tabs to detect missing columns)
    if "\t" in line_no_eol:
        return _parse_csv_line(line_no_eol, "\t")

    # 2. Quoted CSV with semicolon or comma (handles commas/dashes inside quoted cells)
    if '"' in stripped:
        if _QUOTED_SEMICOLON_RE.search(stripped):
            return _parse_csv_line(stripped, ";")
        if _QUOTED_COMMA_RE.search(stripped):
            return _parse_csv_line(stripped, ",")

    # 3. Reject lines starting with a bare separator dash (missing German word)
    if _LEADING_DASH_RE.match(stripped):
        return None

    # 4. Compare semicolon vs spaced dash (' - ', ' – ', ' — ') so translations like
    #    "der Zug - потяг; поїзд" keep the dash separator while "der Tisch; стіл - меблі; noun"
    #    uses semicolon columns.
    dash_match = _SPACED_DASH_RE.search(stripped)
    semi_idx = stripped.find(";")
    if semi_idx != -1 and (dash_match is None or semi_idx < dash_match.start()):
        return _parse_csv_line(stripped, ";")

    if dash_match is not None:
        parts = _SPACED_DASH_RE.split(stripped, maxsplit=2)
        return _extract_entry_from_columns(parts)

    # 5. Unspaced Unicode en-dash (–) or em-dash (—); never split on ASCII hyphen '-'
    #    so hyphenated German words like E-Mail or U-Bahn remain intact.
    if "\u2013" in stripped or "\u2014" in stripped:
        parts = _UNICODE_DASH_RE.split(stripped, maxsplit=2)
        return _extract_entry_from_columns(parts)

    # 6. Plain comma-separated values
    if "," in stripped:
        return _parse_csv_line(stripped, ",")

    return None


def _detect_csv_delimiter(text: str) -> str:
    """Detect delimiter (',', ';', or '\\t') for uploaded .csv files."""
    sample_lines = [line for line in text.splitlines() if line.strip()][:20]
    if not sample_lines:
        return ","

    candidates = ("\t", ";", ",")
    scores: dict[str, tuple[int, int]] = {}
    for delimiter in candidates:
        valid_rows = 0
        quoted_hits = 0
        for line in sample_lines:
            if (delimiter == "," and _QUOTED_COMMA_RE.search(line)) or (
                delimiter == ";" and _QUOTED_SEMICOLON_RE.search(line)
            ):
                quoted_hits += 1
            try:
                parsed = next(csv.reader([line], delimiter=delimiter, skipinitialspace=True), [])
            except csv.Error:
                continue
            non_empty_cells = sum(1 for cell in parsed if cell.strip())
            if non_empty_cells >= 2:
                valid_rows += 1
        scores[delimiter] = (valid_rows, quoted_hits)

    # Prefer delimiter with the highest valid multi-column row count; break ties by
    # quoted-delimiter signal, then priority order: tab > semicolon > comma.
    best_delimiter = ","
    best_rank = (-1, -1, -1)
    tie_priority = {"\t": 3, ";": 2, ",": 1}
    for delimiter in candidates:
        valid_rows, quoted_hits = scores[delimiter]
        if valid_rows == 0:
            continue
        rank = (valid_rows, quoted_hits, tie_priority[delimiter])
        if rank > best_rank:
            best_rank = rank
            best_delimiter = delimiter
    return best_delimiter


def _parse_lines_mode(text: str) -> ParseResult:
    entries: list[ParsedWordEntry] = []
    skipped = 0
    saw_non_empty = False

    for raw_line in text.split("\n"):
        if not raw_line.strip():
            continue
        is_first = not saw_non_empty
        saw_non_empty = True

        entry = _parse_single_text_line(raw_line)
        if entry is None:
            skipped += 1
            continue
        if is_first and _is_header_row(entry.word_de, entry.translation_ua):
            continue
        entries.append(entry)

    return ParseResult(entries=entries, skipped=skipped)


def _parse_csv_mode(text: str, *, delimiter: str) -> ParseResult:
    entries: list[ParsedWordEntry] = []
    skipped = 0
    saw_non_empty = False

    try:
        reader = csv.reader(
            io.StringIO(text, newline=""),
            delimiter=delimiter,
            skipinitialspace=True,
        )
        rows = list(reader)
    except csv.Error:
        return _parse_lines_mode(text)

    for row in rows:
        if not any(cell.strip() for cell in row):
            continue
        is_first = not saw_non_empty
        saw_non_empty = True

        if len(row) == 1:
            entry = _parse_single_text_line(row[0])
        else:
            entry = _extract_entry_from_columns(row)

        if entry is None:
            skipped += 1
            continue
        if is_first and _is_header_row(entry.word_de, entry.translation_ua):
            continue
        entries.append(entry)

    return ParseResult(entries=entries, skipped=skipped)


def parse_vocabulary_text(
    text: str,
    *,
    format_hint: ParseFormatHint = "text",
) -> ParseResult:
    """Parse vocabulary entries from plain text, ``.txt``/``.csv`` content, or Sheets CSV.

    Supports UTF-8 BOM, ``\\r\\n``/``\\n`` line endings, `` - ``/`` – ``/`` — ``/``;``/``\\t``
    delimiters, quoted CSV fields, optional 3rd ``part_of_speech`` column, German hyphenated
    words (e.g. ``E-Mail``, ``U-Bahn``), header row skipping, and per-line ``skipped`` counts.
    """
    if "\x00" in text:
        raise InvalidImportEncodingError(
            "Текст містить неприпустимі бінарні символи.",
        )

    cleaned = text[1:] if text.startswith("\ufeff") else text
    normalized = cleaned.replace("\r\n", "\n").replace("\r", "\n")

    if format_hint == "sheets":
        return _parse_csv_mode(normalized, delimiter=",")
    if format_hint == "csv":
        delimiter = _detect_csv_delimiter(normalized)
        return _parse_csv_mode(normalized, delimiter=delimiter)
    return _parse_lines_mode(normalized)


def parse_vocabulary_bytes(
    data: bytes,
    *,
    format_hint: ParseFormatHint = "text",
    max_bytes: int = MAX_IMPORT_BYTES,
) -> ParseResult:
    """Validate size and UTF-8 encoding of raw bytes, then parse vocabulary entries."""
    if len(data) > max_bytes:
        raise PayloadTooLargeError("Файл завеликий: максимальний розмір становить 2 МБ.")
    text = decode_utf8_bytes(data)
    return parse_vocabulary_text(text, format_hint=format_hint)


def _xlsx_cell_to_str(value: object) -> str:
    """Safely convert any Excel cell value (str, int, float, bool, date/datetime, None) to str."""
    if value is None:
        return ""
    if isinstance(value, str):
        if "\x00" in value:
            raise XlsxParseError("Файл .xlsx містить неприпустимі бінарні символи.")
        return value.strip()
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, datetime):
        if (
            value.hour == 0
            and value.minute == 0
            and value.second == 0
            and value.microsecond == 0
        ):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    rendered = str(value).strip()
    if "\x00" in rendered:
        raise XlsxParseError("Файл .xlsx містить неприпустимі бінарні символи.")
    return rendered


def _validate_xlsx_zip_bytes(
    data: bytes,
    *,
    max_uncompressed_bytes: int = MAX_XLSX_UNCOMPRESSED_BYTES,
    max_zip_entries: int = MAX_XLSX_ZIP_ENTRIES,
) -> None:
    """Validate ZIP archive structure and defend against ZIP bombs before invoking openpyxl."""
    if data.startswith(_OLE2_MAGIC):
        raise XlsxParseError(
            "Застарілий або захищений паролем формат Excel (.xls) не підтримується. "
            "Збережіть таблицю як .xlsx, .csv або .txt.",
        )

    try:
        with zipfile.ZipFile(io.BytesIO(data), mode="r") as archive:
            infos = archive.infolist()
            if not infos:
                raise XlsxParseError(
                    "Некоректний або пошкоджений файл .xlsx. "
                    "Переконайтеся, що файл збережено у форматі Excel (.xlsx).",
                )
            if len(infos) > max_zip_entries:
                raise XlsxParseError(
                    "Файл .xlsx містить занадто багато внутрішніх елементів "
                    "і не може бути безпечно оброблений.",
                )

            declared_total = 0
            for info in infos:
                if info.file_size < 0:
                    raise XlsxParseError("Некоректний або пошкоджений файл .xlsx.")
                declared_total += info.file_size
                if declared_total > max_uncompressed_bytes:
                    raise XlsxParseError(
                        "Файл .xlsx перевищує безпечний ліміт розпакованого розміру (20 МБ).",
                    )

            actual_total = 0
            for info in infos:
                if info.is_dir():
                    continue
                with archive.open(info, "r") as member:
                    while True:
                        chunk = member.read(_XLSX_ZIP_READ_CHUNK_SIZE)
                        if not chunk:
                            break
                        actual_total += len(chunk)
                        if actual_total > max_uncompressed_bytes:
                            raise XlsxParseError(
                                "Файл .xlsx перевищує безпечний ліміт розпакованого розміру "
                                "(20 МБ).",
                            )
    except XlsxParseError:
        raise
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
        RuntimeError,
        OSError,
        EOFError,
        ValueError,
    ) as exc:
        raise XlsxParseError(
            "Не вдалося прочитати файл .xlsx: файл пошкоджений, захищений паролем "
            "або не є дійсною таблицею Excel (.xlsx).",
        ) from exc


def _parse_xlsx_sheet(sheet: Any) -> ParseResult:
    """Parse rows from a single openpyxl worksheet (2-3 columns or 1-column text pairs)."""
    non_empty_rows: list[tuple[str, str, str]] = []
    has_multi_col = False

    for raw_row in sheet.iter_rows(values_only=True):
        if not raw_row:
            continue
        c0 = _xlsx_cell_to_str(raw_row[0]) if len(raw_row) >= 1 else ""
        c1 = _xlsx_cell_to_str(raw_row[1]) if len(raw_row) >= 2 else ""
        c2 = _xlsx_cell_to_str(raw_row[2]) if len(raw_row) >= 3 else ""
        if not (c0 or c1 or c2) and not any(_xlsx_cell_to_str(cell) for cell in raw_row[3:]):
            continue
        if c1 or c2:
            has_multi_col = True
        non_empty_rows.append((c0, c1, c2))

    entries: list[ParsedWordEntry] = []
    skipped = 0

    for index, (c0, c1, c2) in enumerate(non_empty_rows):
        if has_multi_col:
            entry = _extract_entry_from_columns([c0, c1, c2])
        else:
            entry = _parse_single_text_line(c0)

        if entry is None:
            skipped += 1
            continue
        if index == 0 and _is_header_row(entry.word_de, entry.translation_ua):
            continue
        entries.append(entry)

    return ParseResult(entries=entries, skipped=skipped)


def parse_xlsx_bytes(
    data: bytes,
    *,
    max_bytes: int = MAX_IMPORT_BYTES,
    max_uncompressed_bytes: int = MAX_XLSX_UNCOMPRESSED_BYTES,
    max_zip_entries: int = MAX_XLSX_ZIP_ENTRIES,
) -> ParseResult:
    """Validate size/ZIP safety and parse vocabulary entries from an ``.xlsx`` workbook."""
    if len(data) > max_bytes:
        raise PayloadTooLargeError("Файл завеликий: максимальний розмір становить 2 МБ.")

    _validate_xlsx_zip_bytes(
        data,
        max_uncompressed_bytes=max_uncompressed_bytes,
        max_zip_entries=max_zip_entries,
    )

    workbook = None
    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data),
            read_only=True,
            data_only=True,
        )
        sheet = workbook.active
        if sheet is None:
            sheet = workbook.worksheets[0] if workbook.worksheets else None
        if sheet is None:
            return ParseResult(entries=[], skipped=0)
        return _parse_xlsx_sheet(sheet)
    except SheetsSyncError:
        raise
    except Exception as exc:
        raise XlsxParseError(
            "Не вдалося прочитати файл .xlsx: файл пошкоджений або не є дійсною таблицею "
            "Excel (.xlsx).",
        ) from exc
    finally:
        if workbook is not None:
            workbook.close()


def _validate_base_https_url(raw_url: str, *, error_context: str) -> tuple[str, str, str, str, str]:
    """Validate strict HTTPS syntax, reject credentials/ports/control chars, return URL parts."""
    trimmed = raw_url.strip()
    if not trimmed:
        raise InvalidSheetsUrlError(f"{error_context}: посилання не може бути порожнім.")
    if len(trimmed) > MAX_SHEETS_URL_LENGTH:
        raise InvalidSheetsUrlError(f"{error_context}: посилання занадто довге.")
    if "\\" in trimmed or any(ch.isspace() or ord(ch) < 32 for ch in trimmed):
        raise InvalidSheetsUrlError(f"{error_context}: посилання містить неприпустимі символи.")

    try:
        parsed = urlsplit(trimmed)
    except ValueError as exc:
        raise InvalidSheetsUrlError(f"{error_context}: некоректний формат URL.") from exc

    if parsed.scheme.lower() != "https":
        raise InvalidSheetsUrlError(f"{error_context}: дозволено лише протокол HTTPS.")

    if not trimmed[len(parsed.scheme) :].startswith("://"):
        raise InvalidSheetsUrlError(f"{error_context}: посилання має бути абсолютним HTTPS URL.")

    if parsed.username is not None or parsed.password is not None:
        raise InvalidSheetsUrlError(
            f"{error_context}: посилання не повинно містити облікові дані.",
        )

    raw_host = parsed.hostname
    if not raw_host:
        raise InvalidSheetsUrlError(f"{error_context}: відсутній домен у посиланні.")

    # Reject trailing dot or uppercase-evasion surprises by normalizing and checking explicitly
    netloc_host_part = parsed.netloc.rsplit("@", 1)[-1]
    if netloc_host_part.startswith("["):
        raise InvalidSheetsUrlError(f"{error_context}: IP-адреси заборонені.")
    host_only = netloc_host_part.split(":", 1)[0]
    if host_only.endswith("."):
        raise InvalidSheetsUrlError(f"{error_context}: некоректний домен.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidSheetsUrlError(f"{error_context}: некоректний порт у посиланні.") from exc

    if port is not None and port != 443:
        raise InvalidSheetsUrlError(f"{error_context}: нестандартні порти заборонені.")

    hostname = raw_host.lower()
    return hostname, parsed.path, parsed.query, parsed.fragment, trimmed


def _is_allowed_googleusercontent_host(hostname: str) -> bool:
    if not hostname.endswith(ALLOWED_REDIRECT_SUFFIX):
        return False
    prefix = hostname[: -len(ALLOWED_REDIRECT_SUFFIX)]
    if not prefix:
        return False
    labels = prefix.split(".")
    return all(bool(_DNS_LABEL_RE.fullmatch(label)) for label in labels)


def _extract_valid_gid(query: str, fragment: str) -> str | None:
    query_params = parse_qs(query, keep_blank_values=True)
    fragment_params = parse_qs(fragment, keep_blank_values=True)

    raw_gid: str | None = None
    if "gid" in query_params and query_params["gid"]:
        raw_gid = query_params["gid"][0]
    elif "gid" in fragment_params and fragment_params["gid"]:
        raw_gid = fragment_params["gid"][0]

    if raw_gid is None:
        return None
    if not _ASCII_DIGITS_RE.fullmatch(raw_gid):
        raise InvalidSheetsUrlError(
            "Некоректний параметр gid у посиланні Google Sheets (очікуються лише цифри).",
        )
    return raw_gid


def build_google_sheets_csv_url(raw_url: str) -> str:
    """Validate a public Google Sheets URL and convert it to a canonical CSV export URL."""
    if not isinstance(raw_url, str):
        raise InvalidSheetsUrlError("Посилання на Google Sheets має бути рядком.")

    hostname, path, query, fragment, _ = _validate_base_https_url(
        raw_url,
        error_context="Некоректне посилання Google Sheets",
    )

    if hostname != ALLOWED_SHEETS_HOST:
        raise InvalidSheetsUrlError(
            "Дозволені лише публічні посилання Google Sheets з домену docs.google.com.",
        )

    for segment in path.split("/"):
        decoded = unquote(segment)
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded:
            raise InvalidSheetsUrlError(
                "Некоректний шлях у посиланні Google Sheets.",
            )

    gid = _extract_valid_gid(query, fragment)

    published_match = _PUBLISHED_SHEETS_PATH_RE.fullmatch(path)
    if published_match is not None:
        pub_id = published_match.group(1)
        params: list[tuple[str, str]] = [("output", "csv")]
        if gid is not None:
            params.append(("gid", gid))
            params.append(("single", "true"))
        return f"https://{ALLOWED_SHEETS_HOST}/spreadsheets/d/e/{pub_id}/pub?{urlencode(params)}"

    standard_match = _STANDARD_SHEETS_PATH_RE.fullmatch(path)
    if standard_match is not None:
        sheet_id = standard_match.group(1)
        if sheet_id == "e":
            raise InvalidSheetsUrlError(
                "Некоректний ідентифікатор таблиці в посиланні Google Sheets.",
            )
        params = [("format", "csv")]
        if gid is not None:
            params.append(("gid", gid))
        return f"https://{ALLOWED_SHEETS_HOST}/spreadsheets/d/{sheet_id}/export?{urlencode(params)}"

    raise InvalidSheetsUrlError(
        "Посилання має вести на таблицю Google Sheets "
        "(https://docs.google.com/spreadsheets/d/<id>/edit).",
    )


def validate_redirect_url(redirect_url: str) -> str:
    """Validate that a redirect target stays strictly inside the HTTPS Google Sheets whitelist."""
    hostname, _, _, _, normalized_url = _validate_base_https_url(
        redirect_url,
        error_context="Недовірений редирект Google Sheets",
    )

    if hostname == GOOGLE_ACCOUNTS_HOST or hostname.endswith(f".{GOOGLE_ACCOUNTS_HOST}"):
        raise SheetsAccessError(
            "Таблиця Google Sheets приватна або потребує входу в акаунт. "
            "Відкрийте доступ на перегляд за посиланням.",
        )

    if hostname == ALLOWED_SHEETS_HOST or _is_allowed_googleusercontent_host(hostname):
        return normalized_url

    raise InvalidSheetsUrlError(
        "Редирект на недовірений домен заблоковано. Дозволені лише docs.google.com "
        "та *.googleusercontent.com.",
    )


def _is_html_content_type(content_type_header: str) -> bool:
    media_type = content_type_header.split(";", 1)[0].strip().lower()
    return media_type == "text/html" or "html" in media_type


def _looks_like_html_document(text: str) -> bool:
    prefix = text.lstrip("\ufeff \t\r\n")[:1024].lower()
    if any(prefix.startswith(tag) for tag in _HTML_PREFIXES):
        return True
    return "<html" in prefix and "accounts.google.com" in prefix


async def _download_with_safe_redirects(
    client: httpx.AsyncClient,
    export_url: str,
    *,
    max_bytes: int,
    max_redirects: int,
) -> bytes:
    current_url = export_url
    redirects_followed = 0

    while True:
        try:
            async with client.stream("GET", current_url, follow_redirects=False) as response:
                status_code = response.status_code

                if status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise SheetsUpstreamError(
                            "Google Sheets повернув некоректне перенаправлення без адреси.",
                        )
                    redirects_followed += 1
                    if redirects_followed > max_redirects:
                        raise SheetsUpstreamError(
                            "Забагато перенаправлень при завантаженні Google Sheets.",
                        )
                    next_url = urljoin(current_url, location.strip())
                    current_url = validate_redirect_url(next_url)
                    continue

                if status_code in {401, 403}:
                    raise SheetsAccessError(
                        "Таблиця Google Sheets приватна або недоступна. "
                        "Відкрийте доступ на перегляд за посиланням.",
                    )

                if status_code == 404:
                    raise SheetsAccessError(
                        "Таблицю Google Sheets не знайдено. Перевірте посилання та параметр gid.",
                    )

                if status_code >= 500 or status_code == 429:
                    raise SheetsUpstreamError(
                        "Сервіс Google Sheets тимчасово недоступний. Спробуйте пізніше.",
                    )

                if status_code != 200:
                    raise SheetsAccessError(
                        "Не вдалося відкрити таблицю Google Sheets. "
                        "Перевірте, що доступ відкритий за посиланням.",
                    )

                content_type = response.headers.get("content-type", "")
                if _is_html_content_type(content_type):
                    raise SheetsAccessError(
                        "Отримано HTML-сторінку замість CSV. Переконайтеся, що таблиця "
                        "Google Sheets відкрита для перегляду за посиланням.",
                    )

                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length.strip())
                    except ValueError:
                        declared_size = -1
                    if declared_size > max_bytes:
                        raise PayloadTooLargeError(
                            "Файл Google Sheets завеликий: максимальний розмір становить 2 МБ.",
                        )

                buffer = bytearray()
                total_bytes = 0
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise PayloadTooLargeError(
                            "Файл Google Sheets завеликий: максимальний розмір становить 2 МБ.",
                        )
                    buffer.extend(chunk)
                return bytes(buffer)
        except SheetsSyncError:
            raise
        except httpx.TimeoutException as exc:
            raise SheetsTimeoutError(
                "Перевищено час очікування відповіді від Google Sheets. Спробуйте пізніше.",
            ) from exc
        except httpx.HTTPError as exc:
            raise SheetsUpstreamError(
                "Не вдалося завантажити таблицю з Google Sheets. Спробуйте пізніше.",
            ) from exc


async def fetch_google_sheets_csv(
    sheets_url: str,
    *,
    timeout: float = DEFAULT_SHEETS_TIMEOUT_SECONDS,
    max_bytes: int = MAX_IMPORT_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Download a public Google Sheets CSV safely with SSRF, redirect, HTML, and size checks."""
    export_url = build_google_sheets_csv_url(sheets_url)

    if http_client is not None:
        raw_bytes = await _download_with_safe_redirects(
            http_client,
            export_url,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
        )
    else:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            raw_bytes = await _download_with_safe_redirects(
                client,
                export_url,
                max_bytes=max_bytes,
                max_redirects=max_redirects,
            )

    decoded = decode_utf8_bytes(raw_bytes)
    if _looks_like_html_document(decoded):
        raise SheetsAccessError(
            "Отримано HTML-сторінку замість CSV. Переконайтеся, що таблиця "
            "Google Sheets відкрита для перегляду за посиланням.",
        )
    return decoded


async def sync_google_sheet(
    sheets_url: str,
    *,
    timeout: float = DEFAULT_SHEETS_TIMEOUT_SECONDS,
    max_bytes: int = MAX_IMPORT_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    http_client: httpx.AsyncClient | None = None,
) -> ParseResult:
    """Download and parse vocabulary entries from a public Google Sheets URL."""
    csv_text = await fetch_google_sheets_csv(
        sheets_url,
        timeout=timeout,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        http_client=http_client,
    )
    return parse_vocabulary_text(csv_text, format_hint="sheets")


__all__ = [
    "ALLOWED_REDIRECT_SUFFIX",
    "ALLOWED_SHEETS_HOST",
    "DEFAULT_SHEETS_TIMEOUT_SECONDS",
    "GOOGLE_ACCOUNTS_HOST",
    "MAX_IMPORT_BYTES",
    "MAX_REDIRECTS",
    "MAX_SHEETS_URL_LENGTH",
    "MAX_XLSX_UNCOMPRESSED_BYTES",
    "MAX_XLSX_ZIP_ENTRIES",
    "InvalidImportEncodingError",
    "InvalidSheetsUrlError",
    "ParseFormatHint",
    "ParseResult",
    "ParsedWordEntry",
    "PayloadTooLargeError",
    "SheetsAccessError",
    "SheetsSyncError",
    "SheetsTimeoutError",
    "SheetsUpstreamError",
    "XlsxParseError",
    "build_google_sheets_csv_url",
    "decode_utf8_bytes",
    "fetch_google_sheets_csv",
    "normalize_word_key",
    "parse_vocabulary_bytes",
    "parse_vocabulary_text",
    "parse_xlsx_bytes",
    "sync_google_sheet",
    "validate_redirect_url",
]
