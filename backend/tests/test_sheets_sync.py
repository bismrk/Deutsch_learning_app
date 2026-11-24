"""Offline unit and respx-mocked HTTP tests for ``app.core.sheets_sync``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from app.core.sheets_sync import (
    MAX_IMPORT_BYTES,
    InvalidImportEncodingError,
    InvalidSheetsUrlError,
    ParsedWordEntry,
    PayloadTooLargeError,
    SheetsAccessError,
    SheetsTimeoutError,
    SheetsUpstreamError,
    build_google_sheets_csv_url,
    decode_utf8_bytes,
    fetch_google_sheets_csv,
    normalize_word_key,
    parse_vocabulary_bytes,
    parse_vocabulary_text,
    sync_google_sheet,
    validate_redirect_url,
)

SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"


# ---------------------------------------------------------------------------
# Vocabulary text & file parser tests
# ---------------------------------------------------------------------------


def test_parse_all_supported_delimiters_and_optional_part_of_speech() -> None:
    text = (
        "der Zug - потяг\n"
        "die Verspätung – затримка\n"
        "das Treffen — зустріч\n"
        "das Haus; будинок; noun\n"
        "die Katze\tкішка\tnoun\n"
        '"der Apfel, die Äpfel","яблуко, яблука",noun\n'
        "schnell - швидкий - adjective\n"
        "lernen, вчити, verb\n"
    )

    result = parse_vocabulary_text(text, format_hint="text")

    assert result.skipped == 0
    assert result.entries == [
        ParsedWordEntry("der Zug", "потяг", None),
        ParsedWordEntry("die Verspätung", "затримка", None),
        ParsedWordEntry("das Treffen", "зустріч", None),
        ParsedWordEntry("das Haus", "будинок", "noun"),
        ParsedWordEntry("die Katze", "кішка", "noun"),
        ParsedWordEntry("der Apfel, die Äpfel", "яблуко, яблука", "noun"),
        ParsedWordEntry("schnell", "швидкий", "adjective"),
        ParsedWordEntry("lernen", "вчити", "verb"),
    ]


def test_parse_preserves_hyphens_in_german_words_and_skips_lone_hyphenated_word() -> None:
    text = (
        "E-Mail - електронна пошта\n"
        "U-Bahn — метро\n"
        "die S-Bahn – міська електричка\n"
        "die E-Mail-Adresse; електронна адреса; noun\n"
        "das T-Shirt\tфутболка\tnoun\n"
        "ab- und anreisen - приїжджати та від'їжджати\n"
        "E-Mail\n"
        "E-Mail-Adresse\n"
    )

    result = parse_vocabulary_text(text, format_hint="text")

    assert result.entries == [
        ParsedWordEntry("E-Mail", "електронна пошта", None),
        ParsedWordEntry("U-Bahn", "метро", None),
        ParsedWordEntry("die S-Bahn", "міська електричка", None),
        ParsedWordEntry("die E-Mail-Adresse", "електронна адреса", "noun"),
        ParsedWordEntry("das T-Shirt", "футболка", "noun"),
        ParsedWordEntry("ab- und anreisen", "приїжджати та від'їжджати", None),
    ]
    assert result.skipped == 2


def test_parse_handles_utf8_bom_crlf_headers_blank_lines_and_invalid_rows() -> None:
    raw = (
        "\ufeff"
        "word_de,translation_ua,part_of_speech\r\n"
        "\r\n"
        "E-Mail,електронна пошта,noun\r\n"
        '"der Zug, die Züge","потяг, поїзд",noun\r\n'
        "invalid_row_without_translation\r\n"
        "der Tisch,\r\n"
        ",лише переклад\r\n"
        "   \r\n"
        "U-Bahn,метро,\r\n"
    ).encode("utf-8")

    result = parse_vocabulary_bytes(raw, format_hint="csv")

    assert result.entries == [
        ParsedWordEntry("E-Mail", "електронна пошта", "noun"),
        ParsedWordEntry("der Zug, die Züge", "потяг, поїзд", "noun"),
        ParsedWordEntry("U-Bahn", "метро", None),
    ]
    assert result.skipped == 3


@pytest.mark.parametrize(
    "header_line",
    [
        "word_de,translation_ua,part_of_speech",
        "Word,Translation,Part of speech",
        "Deutsch,Ukrainisch,Wortart",
        "German,Ukrainian",
        "DE,UA",
        "Слово,Переклад,Частина мови",
        "Німецьке слово;Переклад;Частина мови",
        "Слово - Переклад",
        "Word (DE), Translation (UA)",
    ],
)
def test_parse_skips_recognized_header_row_without_counting_skipped(header_line: str) -> None:
    text = f"{header_line}\nder Tisch - стіл\n"
    result = parse_vocabulary_text(text, format_hint="text")

    assert result.skipped == 0
    assert result.entries == [ParsedWordEntry("der Tisch", "стіл", None)]


def test_parse_does_not_falsely_skip_real_word_like_das_wort() -> None:
    text = "das Wort - слово\nder Zug - потяг\n"
    result = parse_vocabulary_text(text, format_hint="text")

    assert result.skipped == 0
    assert result.entries == [
        ParsedWordEntry("das Wort", "слово", None),
        ParsedWordEntry("der Zug", "потяг", None),
    ]


def test_parse_semicolon_csv_and_quoted_commas_and_dashes() -> None:
    csv_semicolon = (
        "Слово;Переклад;Частина мови\r\n"
        '"E-Mail";"електронна пошта, лист";іменник\r\n'
        "der Zug;потяг, поїзд;іменник\r\n"
    )
    result_semi = parse_vocabulary_text(csv_semicolon, format_hint="csv")
    assert result_semi.skipped == 0
    assert result_semi.entries == [
        ParsedWordEntry("E-Mail", "електронна пошта, лист", "іменник"),
        ParsedWordEntry("der Zug", "потяг, поїзд", "іменник"),
    ]

    mixed_text = (
        "der Zug - потяг, поїзд\n"
        "der Bahnhof - вокзал; станція\n"
        'E-Mail,"електронна пошта - лист",noun\n'
        'E-Mail, "електронна пошта, повідомлення", noun\n'
        " - лише переклад\n"
        "лише слово - \n"
        "\tбез німецького\n"
        "без перекладу\t\n"
    )
    result_mixed = parse_vocabulary_text(mixed_text, format_hint="text")
    assert result_mixed.entries == [
        ParsedWordEntry("der Zug", "потяг, поїзд", None),
        ParsedWordEntry("der Bahnhof", "вокзал; станція", None),
        ParsedWordEntry("E-Mail", "електронна пошта - лист", "noun"),
        ParsedWordEntry("E-Mail", "електронна пошта, повідомлення", "noun"),
    ]
    assert result_mixed.skipped == 4


def test_parse_sheets_csv_supports_quoted_multiline_and_extra_columns() -> None:
    sheets_csv = (
        "Word,Translation,Part of speech,Notes\r\n"
        '"der ""Chef""","начальник, ""бос""",noun,extra note\r\n'
        '"das Buch","книга\r\n(роман)",noun,ignored\r\n'
        "E-Mail,електронна пошта,,\r\n"
        "only_word,,,\r\n"
        ",,,\r\n"
    )
    result = parse_vocabulary_text(sheets_csv, format_hint="sheets")

    assert result.entries == [
        ParsedWordEntry('der "Chef"', 'начальник, "бос"', "noun"),
        ParsedWordEntry("das Buch", "книга\n(роман)", "noun"),
        ParsedWordEntry("E-Mail", "електронна пошта", None),
    ]
    assert result.skipped == 1


def test_decode_utf8_bytes_handles_bom_and_rejects_binary_or_non_utf8() -> None:
    assert decode_utf8_bytes(b"\xef\xbb\xbfE-Mail - \xd0\xbf\xd0\xbe\xd1\x88\xd1\x82\xd0\xb0") == (
        "E-Mail - пошта"
    )

    with pytest.raises(InvalidImportEncodingError):
        decode_utf8_bytes(b"\xff\xfe\x80\x90")

    with pytest.raises(InvalidImportEncodingError):
        decode_utf8_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    with pytest.raises(InvalidImportEncodingError):
        decode_utf8_bytes(b"der Zug\x00 - \xd0\xbf\xd0\xbe\xd1\x82\xd1\x8f\xd0\xb3")

    with pytest.raises(InvalidImportEncodingError):
        parse_vocabulary_text("der Zug\x00 - потяг")


def test_parse_vocabulary_bytes_rejects_payload_over_limit() -> None:
    oversized = b"a" * (MAX_IMPORT_BYTES + 1)
    with pytest.raises(PayloadTooLargeError):
        parse_vocabulary_bytes(oversized)


def test_normalize_word_key_matches_postgres_lower_trim() -> None:
    assert normalize_word_key("  Der Tisch  ") == "der tisch"
    assert normalize_word_key("DIE STRAßE") == "die straße"
    assert normalize_word_key("  Übung ") == "übung"


# ---------------------------------------------------------------------------
# Google Sheets URL conversion & SSRF whitelist tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_url", "expected_export_url"),
    [
        (
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv",
        ),
        (
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?usp=sharing",
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv",
        ),
        (
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0",
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0",
        ),
        (
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=987654321",
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=987654321",
        ),
        (
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?usp=sharing&gid=42",
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=42",
        ),
        (
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/view?usp=sharing#gid=15",
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=15",
        ),
        (
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=7",
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=7",
        ),
        (
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=xlsx&gid=7",
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=7",
        ),
        (
            f"https://docs.google.com/spreadsheets/u/0/d/{SHEET_ID}/edit#gid=3",
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=3",
        ),
        (
            f"  HTTPS://DOCS.GOOGLE.COM:443/spreadsheets/d/{SHEET_ID}  ",
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv",
        ),
        (
            "https://docs.google.com/spreadsheets/d/e/2PACX-1vTestPub123/pub?output=csv",
            "https://docs.google.com/spreadsheets/d/e/2PACX-1vTestPub123/pub?output=csv",
        ),
        (
            "https://docs.google.com/spreadsheets/d/e/2PACX-1vTestPub123/pubhtml#gid=12",
            "https://docs.google.com/spreadsheets/d/e/2PACX-1vTestPub123/pub?output=csv&gid=12&single=true",
        ),
    ],
)
def test_build_google_sheets_csv_url_converts_valid_urls(
    raw_url: str,
    expected_export_url: str,
) -> None:
    assert build_google_sheets_csv_url(raw_url) == expected_export_url


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "   ",
        "not-a-url",
        f"http://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
        "file:///etc/passwd",
        "ftp://docs.google.com/spreadsheets/d/123/edit",
        "javascript:alert(1)",
        f"//docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
        "https://localhost/spreadsheets/d/123/edit",
        "https://127.0.0.1/spreadsheets/d/123/edit",
        "https://[::1]/spreadsheets/d/123/edit",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.1/spreadsheets/d/123/edit",
        "https://192.168.1.1/spreadsheets/d/123/edit",
        "https://evil.com/spreadsheets/d/123/edit",
        "https://docs.google.com.evil.com/spreadsheets/d/123/edit",
        "https://sub.docs.google.com/spreadsheets/d/123/edit",
        "https://docs.google.com./spreadsheets/d/123/edit",
        "https://doc-0c-4s-sheets.googleusercontent.com/export/test",
        f"https://docs.google.com:8443/spreadsheets/d/{SHEET_ID}/edit",
        f"https://user:pass@docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
        f"https://docs.google.com\\@evil.com/spreadsheets/d/{SHEET_ID}/edit",
        "https://docs.google.com/document/d/123/edit",
        "https://docs.google.com/spreadsheets/d/e/edit",
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/../../secret",
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/%2e%2e/secret",
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?gid=abc",
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=123;drop",
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=²",
        f"https://docs.google.com/spreadsheets/d/{'a' * 2100}/edit",
    ],
)
def test_build_google_sheets_csv_url_rejects_ssrf_and_invalid_urls(bad_url: str) -> None:
    with pytest.raises(InvalidSheetsUrlError):
        build_google_sheets_csv_url(bad_url)


@pytest.mark.parametrize(
    "allowed_redirect",
    [
        "https://docs.google.com/spreadsheets/d/abc/export?format=csv",
        "https://doc-0c-4s-sheets.googleusercontent.com/export/l5abc/token",
        "https://lh3.sub.googleusercontent.com/export/data",
    ],
)
def test_validate_redirect_url_allows_whitelisted_https_hosts(allowed_redirect: str) -> None:
    assert validate_redirect_url(allowed_redirect) == allowed_redirect


@pytest.mark.parametrize(
    "blocked_redirect",
    [
        "http://doc-0c-4s-sheets.googleusercontent.com/export/l5abc",
        "https://doc-0c-4s-sheets.googleusercontent.com:8443/export/l5abc",
        "https://googleusercontent.com/export/l5abc",
        "https://evilgoogleusercontent.com/export/l5abc",
        "https://doc-0c.googleusercontent.com.evil.com/export/l5abc",
        "https://doc-0c.googleusercontent.com./export/l5abc",
        "https://evil.com/csv",
        "https://127.0.0.1/admin",
        "https://[::1]/admin",
        "https://169.254.169.254/latest/meta-data/",
        "https://localhost/internal",
        "https://drive.google.com/file/d/123",
    ],
)
def test_validate_redirect_url_blocks_untrusted_targets(blocked_redirect: str) -> None:
    with pytest.raises(InvalidSheetsUrlError):
        validate_redirect_url(blocked_redirect)


def test_validate_redirect_url_reports_private_sheet_for_google_accounts() -> None:
    with pytest.raises(SheetsAccessError, match="приватна|входу"):
        validate_redirect_url("https://accounts.google.com/ServiceLogin?continue=https://docs.google.com")


# ---------------------------------------------------------------------------
# respx-mocked HTTP download tests (zero real network calls)
# ---------------------------------------------------------------------------


async def test_sync_google_sheet_follows_allowed_googleusercontent_redirect() -> None:
    edit_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=42"
    export_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=42"
    cdn_url = "https://doc-0c-4s-sheets.googleusercontent.com/export/csv-payload"
    csv_body = (
        "\ufeffword_de,translation_ua,part_of_speech\r\n"
        "E-Mail,електронна пошта,noun\r\n"
        '"der Zug, die Züge","потяг, потяги",noun\r\n'
        "broken_row_only\r\n"
    ).encode("utf-8")

    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(307, headers={"Location": cdn_url}),
        )
        mock.get(cdn_url).mock(
            return_value=httpx.Response(
                200,
                content=csv_body,
                headers={"Content-Type": "text/csv; charset=utf-8"},
            ),
        )

        result = await sync_google_sheet(edit_url)

    assert result.entries == [
        ParsedWordEntry("E-Mail", "електронна пошта", "noun"),
        ParsedWordEntry("der Zug, die Züge", "потяг, потяги", "noun"),
    ]
    assert result.skipped == 1


async def test_fetch_google_sheets_blocks_untrusted_redirect_before_connecting() -> None:
    edit_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    export_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
    evil_url = "https://169.254.169.254/latest/meta-data/"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(302, headers={"Location": evil_url}),
        )
        evil_route = mock.get(evil_url).mock(
            return_value=httpx.Response(200, text="secret-metadata"),
        )

        with pytest.raises(InvalidSheetsUrlError):
            await fetch_google_sheets_csv(edit_url)

        assert not evil_route.called


    # 2. Protocol-relative redirect //evil.com/x and HTTPS downgrade must also be blocked
    with respx.mock(assert_all_called=False) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(302, headers={"Location": "//evil.com/steal"}),
        )
        evil_route_2 = mock.get("https://evil.com/steal").mock(
            return_value=httpx.Response(200, text="evil"),
        )
        with pytest.raises(InvalidSheetsUrlError):
            await fetch_google_sheets_csv(edit_url)
        assert not evil_route_2.called

    with respx.mock(assert_all_called=False) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(
                302,
                headers={"Location": "http://doc-0c-4s-sheets.googleusercontent.com/export"},
            ),
        )
        downgrade_route = mock.get("http://doc-0c-4s-sheets.googleusercontent.com/export").mock(
            return_value=httpx.Response(200, text="downgrade"),
        )
        with pytest.raises(InvalidSheetsUrlError):
            await fetch_google_sheets_csv(edit_url)
        assert not downgrade_route.called


async def test_fetch_google_sheets_allows_relative_docs_redirect() -> None:
    edit_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    export_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
    relative_target = f"/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0"
    resolved_target = f"https://docs.google.com{relative_target}"

    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(302, headers={"Location": relative_target}),
        )
        mock.get(resolved_target).mock(
            return_value=httpx.Response(
                200,
                text="E-Mail,електронна пошта\n",
                headers={"Content-Type": "text/csv; charset=utf-8"},
            ),
        )
        result = await sync_google_sheet(edit_url)

    assert result.entries == [ParsedWordEntry("E-Mail", "електронна пошта", None)]
    assert result.skipped == 0


async def test_fetch_google_sheets_rejects_accounts_redirect_and_html_responses() -> None:
    edit_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    export_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"

    # 1. Redirect to accounts.google.com
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(
                302,
                headers={"Location": "https://accounts.google.com/ServiceLogin?service=wise"},
            ),
        )
        with pytest.raises(SheetsAccessError):
            await fetch_google_sheets_csv(edit_url)

    # 2. 200 OK with text/html Content-Type
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(
                200,
                text="<!DOCTYPE html><html><body>Sign in</body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            ),
        )
        with pytest.raises(SheetsAccessError):
            await fetch_google_sheets_csv(edit_url)

    # 3. 200 OK with text/plain Content-Type but HTML document body
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(
                200,
                text="<!DOCTYPE html><html><head></head><body>Login</body></html>",
                headers={"Content-Type": "text/plain"},
            ),
        )
        with pytest.raises(SheetsAccessError):
            await fetch_google_sheets_csv(edit_url)

    # 4. 403 Forbidden
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(return_value=httpx.Response(403))
        with pytest.raises(SheetsAccessError):
            await fetch_google_sheets_csv(edit_url)


async def test_fetch_google_sheets_enforces_streaming_2mb_limit_without_content_length() -> None:
    edit_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    export_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"

    chunks_emitted = 0

    class ChunkedOverflowStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal chunks_emitted
            chunk = b"a,b\n" * 16_384  # 64 KiB per chunk
            for _ in range(100):  # 6.4 MiB total if not stopped early
                chunks_emitted += 1
                yield chunk

        async def aclose(self) -> None:
            return None

    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(
                200,
                stream=ChunkedOverflowStream(),
                headers={"Content-Type": "text/csv"},
            ),
        )
        with pytest.raises(PayloadTooLargeError):
            await fetch_google_sheets_csv(edit_url, max_bytes=MAX_IMPORT_BYTES)

    # Streaming stopped right after crossing 2 MiB (33 chunks of 64 KiB), never read all 100 chunks
    assert chunks_emitted == 33


async def test_fetch_google_sheets_rejects_oversized_content_length_header() -> None:
    edit_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    export_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"

    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(
                200,
                content=b"small",
                headers={
                    "Content-Type": "text/csv",
                    "Content-Length": str(MAX_IMPORT_BYTES + 1),
                },
            ),
        )
        with pytest.raises(PayloadTooLargeError):
            await fetch_google_sheets_csv(edit_url)


async def test_fetch_google_sheets_maps_timeout_and_5xx_and_redirect_loop() -> None:
    edit_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    export_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"

    # Timeout -> SheetsTimeoutError
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(side_effect=httpx.ReadTimeout("timed out"))
        with pytest.raises(SheetsTimeoutError):
            await fetch_google_sheets_csv(edit_url)

    # 503 -> SheetsUpstreamError
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(return_value=httpx.Response(503, text="Service Unavailable"))
        with pytest.raises(SheetsUpstreamError):
            await fetch_google_sheets_csv(edit_url)

    # Infinite redirect loop -> SheetsUpstreamError
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(302, headers={"Location": export_url}),
        )
        with pytest.raises(SheetsUpstreamError):
            await fetch_google_sheets_csv(edit_url, max_redirects=2)

    # Non-UTF-8 binary payload -> InvalidImportEncodingError
    with respx.mock(assert_all_called=True) as mock:
        mock.get(export_url).mock(
            return_value=httpx.Response(
                200,
                content=b"\x89PNG\r\n\x1a\n\xff\xfe",
                headers={"Content-Type": "text/csv"},
            ),
        )
        with pytest.raises(InvalidImportEncodingError):
            await fetch_google_sheets_csv(edit_url)


def test_build_google_sheets_csv_url_rejects_non_string_types() -> None:
    bad_inputs: list[Any] = [None, 123, ["https://docs.google.com"]]
    for bad in bad_inputs:
        with pytest.raises(InvalidSheetsUrlError):
            build_google_sheets_csv_url(bad)
