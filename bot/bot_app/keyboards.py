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

TRAIN_BTN_MIX: Final = "⚡ Розумний мікс"
TRAIN_BTN_TOPIC: Final = "📘 Обрати граматичну тему"
TRAIN_BTN_HINT: Final = "💡 Підказка"
TRAIN_BTN_ABANDON: Final = "🚪 Завершити достроково"
TRAIN_BTN_CANCEL_TOPIC: Final = "❌ Скасувати"

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


def build_main_menu_keyboard(miniapp_url: str | None = None) -> ReplyKeyboardMarkup:
    """Build the persistent main menu, adding Mini App ``WebAppInfo`` only for HTTPS URLs."""
    first_row = [
        KeyboardButton(text=MENU_BTN_TRAINING),
        KeyboardButton(text=MENU_BTN_VOCABULARY),
    ]
    second_row = [
        KeyboardButton(text=MENU_BTN_SETTINGS),
    ]

    safe_miniapp_url = normalize_https_miniapp_url(miniapp_url)
    if safe_miniapp_url is not None:
        second_row.append(
            KeyboardButton(
                text=MENU_BTN_MINIAPP,
                web_app=WebAppInfo(url=safe_miniapp_url),
            ),
        )

    return ReplyKeyboardMarkup(
        keyboard=[first_row, second_row],
        resize_keyboard=True,
    )


__all__ = [
    "GRAMMAR_TOPICS",
    "LEVEL_CALLBACK_PREFIX",
    "MENU_BTN_MINIAPP",
    "MENU_BTN_SETTINGS",
    "MENU_BTN_TRAINING",
    "MENU_BTN_VOCABULARY",
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
    "build_exercise_keyboard",
    "build_level_keyboard",
    "build_main_menu_keyboard",
    "build_topic_keyboard",
    "build_training_mode_keyboard",
    "resolve_grammar_topic",
]
