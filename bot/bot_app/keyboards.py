"""Inline and reply keyboards for the Telegram bot."""

from __future__ import annotations

from typing import Final

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from bot_app.config import normalize_https_miniapp_url
from bot_app.schemas import USER_LEVELS, UserLevel

LEVEL_CALLBACK_PREFIX: Final = "level:"

MENU_BTN_TRAINING: Final = "🎯 Тренування"
MENU_BTN_VOCABULARY: Final = "📚 Словник"
MENU_BTN_SETTINGS: Final = "⚙️ Налаштування"
MENU_BTN_MINIAPP: Final = "📊 Статистика"


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
    "LEVEL_CALLBACK_PREFIX",
    "MENU_BTN_MINIAPP",
    "MENU_BTN_SETTINGS",
    "MENU_BTN_TRAINING",
    "MENU_BTN_VOCABULARY",
    "build_level_keyboard",
    "build_main_menu_keyboard",
]
