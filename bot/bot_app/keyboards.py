"""Inline and reply keyboards for the Telegram bot."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from bot_app.config import normalize_https_miniapp_url
from bot_app.schemas import USER_LEVELS, VALID_USER_LEVELS, UserLevel

LEVEL_CALLBACK_PREFIX: Final = "level:"

TRAIN_MODE_MIX_CALLBACK: Final = "train:mode:mix"
TRAIN_MODE_TOPIC_CALLBACK: Final = "train:mode:topic"
TRAIN_TOPIC_CALLBACK_PREFIX: Final = "train:topic:"
TRAIN_TOPIC_LEVEL_CALLBACK_PREFIX: Final = "train:topic_lvl:"
TRAIN_TOPIC_CANCEL_CALLBACK: Final = "train:topic:cancel"
TRAIN_HINT_CALLBACK_PREFIX: Final = "train:hint:"
TRAIN_ABANDON_CALLBACK_PREFIX: Final = "train:abandon:"

MENU_BTN_TRAINING: Final = "🎯 Тренування"
MENU_BTN_VOCABULARY: Final = "📚 Словник"
MENU_BTN_SETTINGS: Final = "⚙️ Налаштування"
MENU_BTN_MINIAPP: Final = "📊 Статистика"
MINIAPP_OPEN_BTN: Final = "📊 Відкрити статистику"

MAIN_MENU_BUTTONS: Final[frozenset[str]] = frozenset(
    {
        MENU_BTN_TRAINING,
        MENU_BTN_VOCABULARY,
        MENU_BTN_SETTINGS,
        MENU_BTN_MINIAPP,
    },
)

TRAIN_BTN_MIX: Final = "⚡ Розумний мікс"
TRAIN_BTN_TOPIC: Final = "📘 Обрати граматичну тему"
TRAIN_BTN_HINT: Final = "💡 Підказка"
TRAIN_BTN_ABANDON: Final = "🚪 Завершити достроково"
TRAIN_BTN_CANCEL_TOPIC: Final = "❌ Скасувати"

VOCAB_IMPORT_CALLBACK: Final = "vocab:import"
VOCAB_SHEETS_BIND_CALLBACK: Final = "vocab:sheets:bind"
VOCAB_SHEETS_SYNC_CALLBACK: Final = "vocab:sheets:sync"
VOCAB_CANCEL_CALLBACK: Final = "vocab:cancel"

VOCAB_BTN_IMPORT: Final = "➕ Додати слова (текст або файл)"
VOCAB_BTN_SHEETS_BIND: Final = "🔗 Прив'язати Google Sheets"
VOCAB_BTN_SHEETS_SYNC: Final = "🔄 Синхронізувати Google Sheets"
VOCAB_BTN_SHEETS_SYNC_NOW: Final = "🔄 Синхронізувати зараз"
VOCAB_BTN_CANCEL: Final = "❌ Скасувати"

SETTINGS_MENU_CALLBACK: Final = "settings:menu"
SETTINGS_OPEN_CALLBACK_PREFIX: Final = "settings:open:"
SETTINGS_OPEN_LEVEL_CALLBACK: Final = "settings:open:level"
SETTINGS_OPEN_EXERCISES_CALLBACK: Final = "settings:open:exercises"
SETTINGS_OPEN_REMINDER_CALLBACK: Final = "settings:open:reminder"
SETTINGS_OPEN_TIMEZONE_CALLBACK: Final = "settings:open:timezone"
SETTINGS_LEVEL_CALLBACK_PREFIX: Final = "settings:level:"
SETTINGS_EXERCISES_CALLBACK_PREFIX: Final = "settings:exercises:"
SETTINGS_EXERCISES_CUSTOM_CALLBACK: Final = "settings:custom:exercises"
SETTINGS_REMINDER_CALLBACK_PREFIX: Final = "settings:reminder:"
SETTINGS_REMINDER_CUSTOM_CALLBACK: Final = "settings:custom:reminder"
SETTINGS_TZ_CALLBACK_PREFIX: Final = "settings:tz:"
SETTINGS_TZ_CUSTOM_CALLBACK: Final = "settings:custom:tz"
SETTINGS_CANCEL_CALLBACK: Final = "settings:cancel"

SETTINGS_BTN_LEVEL: Final = "🎯 Рівень"
SETTINGS_BTN_EXERCISES: Final = "📝 Вправ у сеті"
SETTINGS_BTN_REMINDER: Final = "⏰ Година нагадування"
SETTINGS_BTN_TIMEZONE: Final = "🌍 Часовий пояс"
SETTINGS_BTN_CUSTOM_EXERCISES: Final = "✏️ Ввести своє число (1–30)"
SETTINGS_BTN_CUSTOM_REMINDER: Final = "✏️ Ввести годину (0–23)"
SETTINGS_BTN_CUSTOM_TIMEZONE: Final = "✏️ Ввести власну IANA-зону"
SETTINGS_BTN_BACK: Final = "⬅️ Назад до налаштувань"
SETTINGS_BTN_CANCEL: Final = "❌ Скасувати"

EXERCISES_PER_SET_PRESETS: Final[tuple[int, ...]] = (3, 5, 10, 15, 20)
POPULAR_TIMEZONES: Final[tuple[str, ...]] = (
    "Europe/Kyiv",
    "Europe/Berlin",
    "Europe/Warsaw",
    "Europe/Vienna",
    "Europe/London",
    "UTC",
)

GRAMMAR_TOPICS: Final[Mapping[UserLevel, tuple[str, ...]]] = MappingProxyType(
    {
        "A1": (
            "Bestimmte und unbestimmte Artikel (der, die, das, ein, eine)",
            "Präsens regelmäßiger und unregelmäßiger Verben",
            "Wortstellung im Aussagesatz und in W-Fragen",
            "Akkusativ nach Verben (haben, brauchen, sehen, suchen)",
            "Personalpronomen und Possessivartikel im Nominativ und Akkusativ",
            "Modalverben im Präsens (können, müssen, wollen, möchten, dürfen)",
            "Trennbare Verben im Präsens",
            "Negation mit nicht und kein",
        ),
        "A2": (
            "Perfekt mit haben und sein",
            "Dativ nach Verben und Präpositionen (mit, nach, aus, zu, von, bei)",
            "Wechselpräpositionen mit Dativ und Akkusativ (Wo? vs. Wohin?)",
            "Nebensätze mit weil und dass",
            "Temporalsätze mit wenn und als",
            "Reflexive Verben im Präsens und Perfekt",
            "Komparativ und Superlativ der Adjektive",
            "Präteritum der Modalverben und von sein/haben",
        ),
        "B1": (
            "Nebensätze mit obwohl und trotzdem",
            "Relativsätze im Nominativ, Akkusativ und Dativ",
            "Konjunktiv II für höfliche Bitten, Wünsche und irreale Bedingungen",
            "Passiv im Präsens und Präteritum",
            "Infinitivsätze mit zu, um ... zu und ohne ... zu",
            "Zweiteilige Konnektoren (entweder ... oder, sowohl ... als auch, zwar ... aber)",
            "Adjektivdeklination nach bestimmtem, unbestimmtem Artikel und Nullartikel",
            "Genitiv und Präpositionen mit Genitiv (wegen, trotz, während)",
        ),
    },
)


def resolve_grammar_topic(level: str, index_raw: str) -> str | None:
    """Resolve a CEFR level and 0-based index from callback data into a topic name."""
    if level not in VALID_USER_LEVELS:
        return None
    try:
        index = int(index_raw)
    except ValueError:
        return None
    topics = GRAMMAR_TOPICS[level]  # type: ignore[index]
    if 0 <= index < len(topics):
        return topics[index]
    return None


def build_level_keyboard(current_level: UserLevel | None = None) -> InlineKeyboardMarkup:
    """Build inline CEFR level selection buttons (A1 / A2 / B1)."""
    buttons = [
        InlineKeyboardButton(
            text=f"✓ {level}" if level == current_level else level,
            callback_data=f"{LEVEL_CALLBACK_PREFIX}{level}",
        )
        for level in USER_LEVELS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def build_training_mode_keyboard() -> InlineKeyboardMarkup:
    """Build inline buttons to start training in Smart Mix or Topic mode."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=TRAIN_BTN_MIX,
                    callback_data=TRAIN_MODE_MIX_CALLBACK,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=TRAIN_BTN_TOPIC,
                    callback_data=TRAIN_MODE_TOPIC_CALLBACK,
                ),
            ],
        ],
    )


def build_topic_keyboard(level: UserLevel) -> InlineKeyboardMarkup:
    """Build inline grammar topic selection keyboard for a CEFR level with cancel button."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=topic,
                callback_data=f"{TRAIN_TOPIC_CALLBACK_PREFIX}{level}:{idx}",
            ),
        ]
        for idx, topic in enumerate(GRAMMAR_TOPICS[level])
    ]
    level_row = [
        InlineKeyboardButton(
            text=f"✓ {lvl}" if lvl == level else lvl,
            callback_data=f"{TRAIN_TOPIC_LEVEL_CALLBACK_PREFIX}{lvl}",
        )
        for lvl in USER_LEVELS
    ]
    rows.append(level_row)
    rows.append(
        [
            InlineKeyboardButton(
                text=TRAIN_BTN_CANCEL_TOPIC,
                callback_data=TRAIN_TOPIC_CANCEL_CALLBACK,
            ),
        ],
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_exercise_keyboard(session_id: int) -> InlineKeyboardMarkup:
    """Build inline Hint and Abandon buttons bound to ``session_id``."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=TRAIN_BTN_HINT,
                    callback_data=f"{TRAIN_HINT_CALLBACK_PREFIX}{session_id}",
                ),
                InlineKeyboardButton(
                    text=TRAIN_BTN_ABANDON,
                    callback_data=f"{TRAIN_ABANDON_CALLBACK_PREFIX}{session_id}",
                ),
            ],
        ],
    )


def build_vocabulary_menu_keyboard() -> InlineKeyboardMarkup:
    """Build inline keyboard for the Vocabulary menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=VOCAB_BTN_IMPORT,
                    callback_data=VOCAB_IMPORT_CALLBACK,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=VOCAB_BTN_SHEETS_BIND,
                    callback_data=VOCAB_SHEETS_BIND_CALLBACK,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=VOCAB_BTN_SHEETS_SYNC,
                    callback_data=VOCAB_SHEETS_SYNC_CALLBACK,
                ),
            ],
        ],
    )


def build_vocab_cancel_keyboard() -> InlineKeyboardMarkup:
    """Build a single-button inline keyboard to cancel an active vocabulary FSM input."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=VOCAB_BTN_CANCEL,
                    callback_data=VOCAB_CANCEL_CALLBACK,
                ),
            ],
        ],
    )


def build_sheets_bound_keyboard() -> InlineKeyboardMarkup:
    """Build inline action button to trigger manual Google Sheets sync right after binding."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=VOCAB_BTN_SHEETS_SYNC_NOW,
                    callback_data=VOCAB_SHEETS_SYNC_CALLBACK,
                ),
            ],
        ],
    )


def build_sheets_bind_prompt_keyboard() -> InlineKeyboardMarkup:
    """Build inline action button prompting the user to bind a Google Sheets URL."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=VOCAB_BTN_SHEETS_BIND,
                    callback_data=VOCAB_SHEETS_BIND_CALLBACK,
                ),
            ],
        ],
    )


def build_settings_menu_keyboard() -> InlineKeyboardMarkup:
    """Build inline keyboard for the main Settings overview screen."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=SETTINGS_BTN_LEVEL,
                    callback_data=SETTINGS_OPEN_LEVEL_CALLBACK,
                ),
                InlineKeyboardButton(
                    text=SETTINGS_BTN_EXERCISES,
                    callback_data=SETTINGS_OPEN_EXERCISES_CALLBACK,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=SETTINGS_BTN_REMINDER,
                    callback_data=SETTINGS_OPEN_REMINDER_CALLBACK,
                ),
                InlineKeyboardButton(
                    text=SETTINGS_BTN_TIMEZONE,
                    callback_data=SETTINGS_OPEN_TIMEZONE_CALLBACK,
                ),
            ],
        ],
    )


def build_settings_level_keyboard(current_level: UserLevel) -> InlineKeyboardMarkup:
    """Build inline CEFR level selector inside Settings with active marker and Back button."""
    level_row = [
        InlineKeyboardButton(
            text=f"✓ {level}" if level == current_level else level,
            callback_data=f"{SETTINGS_LEVEL_CALLBACK_PREFIX}{level}",
        )
        for level in USER_LEVELS
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            level_row,
            [
                InlineKeyboardButton(
                    text=SETTINGS_BTN_BACK,
                    callback_data=SETTINGS_MENU_CALLBACK,
                ),
            ],
        ],
    )


def build_settings_exercises_keyboard(current_exercises: int) -> InlineKeyboardMarkup:
    """Build inline exercises-per-set preset keyboard with custom input and Back buttons."""
    preset_row = [
        InlineKeyboardButton(
            text=f"✓ {count}" if count == current_exercises else str(count),
            callback_data=f"{SETTINGS_EXERCISES_CALLBACK_PREFIX}{count}",
        )
        for count in EXERCISES_PER_SET_PRESETS
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            preset_row,
            [
                InlineKeyboardButton(
                    text=SETTINGS_BTN_CUSTOM_EXERCISES,
                    callback_data=SETTINGS_EXERCISES_CUSTOM_CALLBACK,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=SETTINGS_BTN_BACK,
                    callback_data=SETTINGS_MENU_CALLBACK,
                ),
            ],
        ],
    )


def build_settings_reminder_keyboard(current_hour: int) -> InlineKeyboardMarkup:
    """Build a 24-hour (00:00–23:00) inline keyboard with custom input and Back buttons."""
    rows: list[list[InlineKeyboardButton]] = []
    for row_start in range(0, 24, 4):
        row = [
            InlineKeyboardButton(
                text=f"✓ {hour:02d}:00" if hour == current_hour else f"{hour:02d}:00",
                callback_data=f"{SETTINGS_REMINDER_CALLBACK_PREFIX}{hour}",
            )
            for hour in range(row_start, row_start + 4)
        ]
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                text=SETTINGS_BTN_CUSTOM_REMINDER,
                callback_data=SETTINGS_REMINDER_CUSTOM_CALLBACK,
            ),
        ],
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=SETTINGS_BTN_BACK,
                callback_data=SETTINGS_MENU_CALLBACK,
            ),
        ],
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_settings_timezone_keyboard(current_timezone: str) -> InlineKeyboardMarkup:
    """Build popular IANA timezone buttons plus custom input and Back buttons."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(0, len(POPULAR_TIMEZONES), 2):
        chunk = POPULAR_TIMEZONES[idx : idx + 2]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✓ {tz}" if tz == current_timezone else tz,
                    callback_data=f"{SETTINGS_TZ_CALLBACK_PREFIX}{tz}",
                )
                for tz in chunk
            ],
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=SETTINGS_BTN_CUSTOM_TIMEZONE,
                callback_data=SETTINGS_TZ_CUSTOM_CALLBACK,
            ),
        ],
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=SETTINGS_BTN_BACK,
                callback_data=SETTINGS_MENU_CALLBACK,
            ),
        ],
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_settings_cancel_keyboard() -> InlineKeyboardMarkup:
    """Build a single-button inline keyboard to cancel an active settings FSM input."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=SETTINGS_BTN_CANCEL,
                    callback_data=SETTINGS_CANCEL_CALLBACK,
                ),
            ],
        ],
    )


def build_miniapp_inline_keyboard(miniapp_url: str | None) -> InlineKeyboardMarkup | None:
    """Build an inline Mini App button with signed ``initData`` support, or ``None`` if invalid."""
    safe_url = normalize_https_miniapp_url(miniapp_url)
    if safe_url is None:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=MINIAPP_OPEN_BTN,
                    web_app=WebAppInfo(url=safe_url),
                ),
            ],
        ],
    )


def build_main_menu_keyboard(miniapp_url: str | None = None) -> ReplyKeyboardMarkup:
    """Build the persistent main menu with a plain-text Statistics button when HTTPS URL is set."""
    first_row = [
        KeyboardButton(text=MENU_BTN_TRAINING),
        KeyboardButton(text=MENU_BTN_VOCABULARY),
    ]
    second_row = [
        KeyboardButton(text=MENU_BTN_SETTINGS),
    ]

    safe_miniapp_url = normalize_https_miniapp_url(miniapp_url)
    if safe_miniapp_url is not None:
        second_row.append(KeyboardButton(text=MENU_BTN_MINIAPP))

    return ReplyKeyboardMarkup(
        keyboard=[first_row, second_row],
        resize_keyboard=True,
    )


__all__ = [
    "EXERCISES_PER_SET_PRESETS",
    "GRAMMAR_TOPICS",
    "LEVEL_CALLBACK_PREFIX",
    "MAIN_MENU_BUTTONS",
    "MENU_BTN_MINIAPP",
    "MENU_BTN_SETTINGS",
    "MENU_BTN_TRAINING",
    "MENU_BTN_VOCABULARY",
    "MINIAPP_OPEN_BTN",
    "POPULAR_TIMEZONES",
    "SETTINGS_BTN_BACK",
    "SETTINGS_BTN_CANCEL",
    "SETTINGS_BTN_CUSTOM_EXERCISES",
    "SETTINGS_BTN_CUSTOM_REMINDER",
    "SETTINGS_BTN_CUSTOM_TIMEZONE",
    "SETTINGS_BTN_EXERCISES",
    "SETTINGS_BTN_LEVEL",
    "SETTINGS_BTN_REMINDER",
    "SETTINGS_BTN_TIMEZONE",
    "SETTINGS_CANCEL_CALLBACK",
    "SETTINGS_EXERCISES_CALLBACK_PREFIX",
    "SETTINGS_EXERCISES_CUSTOM_CALLBACK",
    "SETTINGS_LEVEL_CALLBACK_PREFIX",
    "SETTINGS_MENU_CALLBACK",
    "SETTINGS_OPEN_CALLBACK_PREFIX",
    "SETTINGS_OPEN_EXERCISES_CALLBACK",
    "SETTINGS_OPEN_LEVEL_CALLBACK",
    "SETTINGS_OPEN_REMINDER_CALLBACK",
    "SETTINGS_OPEN_TIMEZONE_CALLBACK",
    "SETTINGS_REMINDER_CALLBACK_PREFIX",
    "SETTINGS_REMINDER_CUSTOM_CALLBACK",
    "SETTINGS_TZ_CALLBACK_PREFIX",
    "SETTINGS_TZ_CUSTOM_CALLBACK",
    "TRAIN_ABANDON_CALLBACK_PREFIX",
    "TRAIN_BTN_ABANDON",
    "TRAIN_BTN_CANCEL_TOPIC",
    "TRAIN_BTN_HINT",
    "TRAIN_BTN_MIX",
    "TRAIN_BTN_TOPIC",
    "TRAIN_HINT_CALLBACK_PREFIX",
    "TRAIN_MODE_MIX_CALLBACK",
    "TRAIN_MODE_TOPIC_CALLBACK",
    "TRAIN_TOPIC_CALLBACK_PREFIX",
    "TRAIN_TOPIC_CANCEL_CALLBACK",
    "TRAIN_TOPIC_LEVEL_CALLBACK_PREFIX",
    "VOCAB_BTN_CANCEL",
    "VOCAB_BTN_IMPORT",
    "VOCAB_BTN_SHEETS_BIND",
    "VOCAB_BTN_SHEETS_SYNC",
    "VOCAB_BTN_SHEETS_SYNC_NOW",
    "VOCAB_CANCEL_CALLBACK",
    "VOCAB_IMPORT_CALLBACK",
    "VOCAB_SHEETS_BIND_CALLBACK",
    "VOCAB_SHEETS_SYNC_CALLBACK",
    "build_exercise_keyboard",
    "build_level_keyboard",
    "build_main_menu_keyboard",
    "build_miniapp_inline_keyboard",
    "build_settings_cancel_keyboard",
    "build_settings_exercises_keyboard",
    "build_settings_level_keyboard",
    "build_settings_menu_keyboard",
    "build_settings_reminder_keyboard",
    "build_settings_timezone_keyboard",
    "build_sheets_bind_prompt_keyboard",
    "build_sheets_bound_keyboard",
    "build_topic_keyboard",
    "build_training_mode_keyboard",
    "build_vocab_cancel_keyboard",
    "build_vocabulary_menu_keyboard",
    "resolve_grammar_topic",
]
