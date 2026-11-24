"""Pure Telegram HTML formatters for training exercises, hints, evaluations, and summaries."""

from __future__ import annotations

import html
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final
from urllib.parse import quote_plus

from bot_app.schemas import ExerciseError, ExerciseEvaluation, ExercisePublic, SessionSummary

TELEGRAM_MAX_MESSAGE_LENGTH: Final = 4096
YOUTUBE_SEARCH_BASE_URL: Final = "https://www.youtube.com/results?search_query="
MAX_YOUTUBE_QUERY_CHARS: Final = 160
MAX_YOUTUBE_LABEL_CHARS: Final = 220

EXERCISE_TYPE_LABELS_UA: Final[Mapping[str, str]] = MappingProxyType(
    {
        "translation": "Переклад (UA → DE)",
        "fill_blank": "Заповнення пропусків",
        "find_error": "Знайди й виправ помилку",
        "combine_sentences": "Об'єднай речення",
    },
)

HINT_ANTICHEAT_NOTE_UA: Final = (
    "ℹ️ Оскільки ти скористався підказкою, максимальна оцінка FSRS "
    "для цієї вправи буде обмежена рівнем «Hard» (2/4)."
)


def _escape(text: str) -> str:
    """Escape dynamic text safely for Telegram ``parse_mode='HTML'`` body content."""
    return html.escape(text, quote=False)


def _escape_attr(text: str) -> str:
    """Escape dynamic attribute text (such as ``href`` URLs) for Telegram HTML."""
    return html.escape(text, quote=True)


def _fit_escaped(text: str, max_escaped_len: int) -> str:
    """HTML-escape ``text`` while guaranteeing output length <= ``max_escaped_len``.

    Truncates plain text *before* escaping so HTML entities and tags are never split.
    """
    if max_escaped_len <= 0:
        return ""

    escaped = _escape(text)
    if len(escaped) <= max_escaped_len:
        return escaped
    if max_escaped_len == 1:
        return "…"

    low = 0
    high = len(text)
    best = "…"
    while low <= high:
        mid = (low + high) // 2
        candidate = f"{_escape(text[:mid].rstrip())}…"
        if len(candidate) <= max_escaped_len:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _format_score(score: float) -> str:
    """Format average grammar score cleanly (e.g. ``85`` or ``82.5``)."""
    rounded = round(score, 1)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.1f}"


def build_youtube_search_url(topic: str) -> str:
    """Build a properly URL-encoded YouTube search link for a grammar topic."""
    cleaned = " ".join(topic.split())
    if len(cleaned) > MAX_YOUTUBE_QUERY_CHARS:
        cleaned = cleaned[:MAX_YOUTUBE_QUERY_CHARS].rstrip()
    encoded = quote_plus(cleaned)
    while len(encoded) > 400 and cleaned:
        cleaned = cleaned[:-1].rstrip()
        encoded = quote_plus(cleaned)
    return f"{YOUTUBE_SEARCH_BASE_URL}{encoded}"


def _format_youtube_link_html(topic: str, *, max_label_len: int = MAX_YOUTUBE_LABEL_CHARS) -> str:
    url = _escape_attr(build_youtube_search_url(topic))
    label = _fit_escaped(topic.strip(), max_label_len)
    return f'<a href="{url}">{label}</a>'


def _format_error_item(error: ExerciseError, *, max_field_len: int | None = None) -> str:
    if max_field_len is None:
        wrong = _escape(error.wrong_part)
        correct = _escape(error.correct_part)
        explanation = _escape(error.explanation_ua)
    else:
        wrong = _fit_escaped(error.wrong_part, max_field_len)
        correct = _fit_escaped(error.correct_part, max_field_len)
        explanation = _fit_escaped(error.explanation_ua, max_field_len * 2)
    return f"• <s>{wrong}</s> → <b>{correct}</b>\n  {explanation}"


def format_exercise_message(
    exercise: ExercisePublic,
    *,
    current_number: int,
    total_exercises: int,
    resumed: bool = False,
    hint_ua: str | None = None,
) -> str:
    """Format an exercise prompt as safe Telegram HTML within 4096 characters."""
    type_label = EXERCISE_TYPE_LABELS_UA.get(exercise.exercise_type, exercise.exercise_type)

    header_lines: list[str] = []
    if resumed:
        header_lines.append("🔄 <b>Продовжуємо активне тренування!</b>\n")

    header_lines.extend(
        [
            f"📝 <b>Вправа {current_number} із {total_exercises}</b>",
            f"📌 <b>Тип:</b> {_escape(type_label)} • <b>Рівень:</b> {_escape(exercise.level)}",
            f"📚 <b>Тема:</b> {_fit_escaped(exercise.grammar_topic, 250)}",
        ],
    )
    header_block = "\n".join(header_lines)

    instruction_raw = exercise.instruction_ua
    task_raw = exercise.task_text
    hint_raw = hint_ua.strip() if hint_ua and hint_ua.strip() else None

    def _assemble(inst_limit: int | None, task_limit: int | None, hint_limit: int | None) -> str:
        inst_text = (
            _escape(instruction_raw)
            if inst_limit is None
            else _fit_escaped(instruction_raw, inst_limit)
        )
        task_text = (
            _escape(task_raw) if task_limit is None else _fit_escaped(task_raw, task_limit)
        )
        parts = [
            header_block,
            inst_text,
            f"<b>Завдання:</b>\n{task_text}",
        ]
        if hint_raw is not None:
            hint_text = (
                _escape(hint_raw) if hint_limit is None else _fit_escaped(hint_raw, hint_limit)
            )
            parts.append(f"💡 <b>Підказка:</b>\n{hint_text}")
        return "\n\n".join(parts)

    full = _assemble(None, None, None)
    if len(full) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        return full

    overhead = len(header_block) + len("<b>Завдання:</b>\n") + 4
    if hint_raw is not None:
        overhead += len("💡 <b>Підказка:</b>\n") + 2
    remaining = max(0, TELEGRAM_MAX_MESSAGE_LENGTH - overhead)

    hint_budget = min(len(_escape(hint_raw)), remaining // 4) if hint_raw is not None else 0
    content_remaining = max(0, remaining - hint_budget)
    inst_budget = min(len(_escape(instruction_raw)), content_remaining // 3)
    task_budget = max(0, content_remaining - inst_budget)

    return _assemble(inst_budget, task_budget, hint_budget if hint_raw is not None else None)


def format_hint_message(hint_ua: str) -> str:
    """Format an exercise hint with the FSRS anti-cheat notice as safe Telegram HTML."""
    prefix = "💡 <b>Підказка:</b>\n"
    suffix = f"\n\n<i>{_escape(HINT_ANTICHEAT_NOTE_UA)}</i>"
    budget = TELEGRAM_MAX_MESSAGE_LENGTH - len(prefix) - len(suffix)
    return f"{prefix}{_fit_escaped(hint_ua, budget)}{suffix}"


def format_evaluation_message(
    evaluation: ExerciseEvaluation,
    *,
    used_hint: bool = False,
) -> str:
    """Format LLM answer feedback as safe Telegram HTML within 4096 characters.

    Omits the errors section entirely when ``evaluation.errors`` is empty.
    """
    status_line = "✅ <b>Правильно!</b>" if evaluation.is_correct else "❌ <b>Є помилки</b>"
    hint_suffix = " (з підказкою)" if used_hint else ""
    score_line = f"📊 <b>Оцінка граматики:</b> {evaluation.grammar_score}/100{hint_suffix}"
    header_block = f"{status_line}\n{score_line}"

    youtube_line = (
        f"🎬 <b>Повторити на YouTube:</b> {_format_youtube_link_html(evaluation.youtube_topic)}"
    )

    summary_block = f"💬 {_escape(evaluation.summary_ua)}"
    corrected_block = f"✨ <b>Правильний варіант:</b>\n{_escape(evaluation.corrected_answer)}"

    sections: list[str] = [header_block, summary_block, corrected_block]
    if evaluation.errors:
        error_lines = [_format_error_item(err) for err in evaluation.errors]
        sections.append("🔎 <b>Розбір помилок:</b>\n" + "\n".join(error_lines))
    sections.append(youtube_line)

    full = "\n\n".join(sections)
    if len(full) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        return full

    # Budget-aware assembly guaranteeing balanced HTML and <= 4096 chars.
    fixed_overhead = (
        len(header_block)
        + len(youtube_line)
        + len("💬 ")
        + len("✨ <b>Правильний варіант:</b>\n")
        + 6  # separators between header, summary, corrected, youtube
    )
    available = max(0, TELEGRAM_MAX_MESSAGE_LENGTH - fixed_overhead)

    if not evaluation.errors:
        summary_budget = min(len(_escape(evaluation.summary_ua)), available // 2)
        corrected_budget = max(0, available - summary_budget)
        return "\n\n".join(
            [
                header_block,
                f"💬 {_fit_escaped(evaluation.summary_ua, summary_budget)}",
                f"✨ <b>Правильний варіант:</b>\n"
                f"{_fit_escaped(evaluation.corrected_answer, corrected_budget)}",
                youtube_line,
            ],
        )

    errors_header = "\n\n🔎 <b>Розбір помилок:</b>\n"
    max_summary = min(len(_escape(evaluation.summary_ua)), 700, available // 4)
    max_corrected = min(len(_escape(evaluation.corrected_answer)), 900, available // 3)
    summary_trimmed = f"💬 {_fit_escaped(evaluation.summary_ua, max_summary)}"
    corrected_trimmed = (
        f"✨ <b>Правильний варіант:</b>\n"
        f"{_fit_escaped(evaluation.corrected_answer, max_corrected)}"
    )

    base_without_errors = "\n\n".join([header_block, summary_trimmed, corrected_trimmed])
    # Reserve space for errors block + separator before youtube_line.
    errors_budget = (
        TELEGRAM_MAX_MESSAGE_LENGTH
        - len(base_without_errors)
        - len(errors_header)
        - len("\n\n")
        - len(youtube_line)
    )

    fitted_errors: list[str] = []
    used_chars = 0
    total_errors = len(evaluation.errors)

    for idx, err in enumerate(evaluation.errors):
        remaining_after = total_errors - (idx + 1)
        footer_reserve = (
            len(f"\n<i>…та ще {remaining_after} помилок скорочено через ліміт Telegram.</i>")
            if remaining_after > 0
            else 0
        )
        separator_len = 1 if fitted_errors else 0
        item_text = _format_error_item(err)
        if used_chars + separator_len + len(item_text) + footer_reserve <= errors_budget:
            fitted_errors.append(item_text)
            used_chars += separator_len + len(item_text)
            continue

        # Try fitting a compact version of this error item if space permits.
        compact_budget = errors_budget - used_chars - separator_len - footer_reserve - 40
        if compact_budget >= 60:
            compact_item = _format_error_item(err, max_field_len=max(15, compact_budget // 4))
            if used_chars + separator_len + len(compact_item) + footer_reserve <= errors_budget:
                fitted_errors.append(compact_item)
                used_chars += separator_len + len(compact_item)
                continue
        break

    omitted = total_errors - len(fitted_errors)
    if omitted > 0:
        notice = f"<i>…та ще {omitted} помилок скорочено через ліміт Telegram.</i>"
        errors_body = "\n".join(fitted_errors) + "\n" + notice if fitted_errors else notice
    else:
        errors_body = "\n".join(fitted_errors)

    return f"{base_without_errors}{errors_header}{errors_body}\n\n{youtube_line}"


def format_summary_message(summary: SessionSummary) -> str:
    """Format the end-of-set summary as safe Telegram HTML within 4096 characters."""
    header_block = (
        "🏁 <b>Тренувальний сет завершено!</b>\n\n"
        f"✅ <b>Правильних відповідей:</b> {summary.correct_count} із {summary.total_exercises}\n"
        f"📊 <b>Середній бал за граматику:</b> {_format_score(summary.avg_grammar_score)}/100"
    )

    if not summary.weak_topics:
        return (
            f"{header_block}\n\n"
            "🌟 <b>Чудовий результат!</b> Слабких тем у цьому сеті не виявлено — так тримати!"
        )

    topics_header = "\n\n🎯 <b>Теми, які варто повторити:</b>\n"
    budget = TELEGRAM_MAX_MESSAGE_LENGTH - len(header_block) - len(topics_header)

    lines: list[str] = []
    used_chars = 0
    for topic in summary.weak_topics:
        line = f"• {_format_youtube_link_html(topic)}"
        sep_len = 1 if lines else 0
        if used_chars + sep_len + len(line) <= budget:
            lines.append(line)
            used_chars += sep_len + len(line)
        else:
            break

    if not lines:
        fallback = f"• {_fit_escaped(summary.weak_topics[0], max(10, budget - 4))}"
        lines.append(fallback)

    return f"{header_block}{topics_header}" + "\n".join(lines)


__all__ = [
    "EXERCISE_TYPE_LABELS_UA",
    "HINT_ANTICHEAT_NOTE_UA",
    "MAX_YOUTUBE_LABEL_CHARS",
    "MAX_YOUTUBE_QUERY_CHARS",
    "TELEGRAM_MAX_MESSAGE_LENGTH",
    "YOUTUBE_SEARCH_BASE_URL",
    "build_youtube_search_url",
    "format_evaluation_message",
    "format_exercise_message",
    "format_hint_message",
    "format_summary_message",
]
