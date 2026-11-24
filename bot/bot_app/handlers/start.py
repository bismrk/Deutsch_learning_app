"""Onboarding ``/start`` command and CEFR level selection handlers."""

from __future__ import annotations

from typing import Final, cast

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from bot_app.api_client import BackendAPIClient, BackendAPIError
from bot_app.config import BotSettings
from bot_app.keyboards import (
    LEVEL_CALLBACK_PREFIX,
    build_level_keyboard,
    build_main_menu_keyboard,
)
from bot_app.schemas import VALID_USER_LEVELS, UserLevel, UserProfile

router = Router(name="start")

LEVEL_PROMPT_TEXT_UA: Final = (
    "Обери свій поточний рівень німецької, щоб завдання відповідали твоїй підготовці:"
)
UNKNOWN_LEVEL_MESSAGE_UA: Final = (
    "Невідомий рівень. Обери один із варіантів: A1, A2 або B1."
)


def build_welcome_text(profile: UserProfile) -> str:
    """Format the Ukrainian onboarding greeting using the user's current profile."""
    return (
        "Вітаю! Я твій тренажер німецької мови (A1–B1).\n\n"
        "Допоможу регулярно повторювати слова за алгоритмом FSRS і відпрацьовувати "
        "граматику на практичних вправах із миттєвим розбором помилок.\n\n"
        f"Поточний рівень: {profile.level} • Вправ у сеті: {profile.exercises_per_set}"
    )


def build_level_updated_text(profile: UserProfile) -> str:
    """Format the Ukrainian confirmation message after saving a CEFR level."""
    return (
        f"Чудово! Твій рівень оновлено до {profile.level}.\n\n"
        "Обирай дію в головному меню нижче — можемо починати тренування або поповнити словник."
    )


@router.message(CommandStart())
async def handle_start(
    message: Message,
    api_client: BackendAPIClient,
    settings: BotSettings,
) -> None:
    """Auto-initialize the user via Backend API, show main menu, and offer level selection."""
    if message.from_user is None:
        return

    try:
        profile = await api_client.get_me(message.from_user.id)
    except BackendAPIError as exc:
        await message.answer(exc.user_message)
        return

    await message.answer(
        build_welcome_text(profile),
        reply_markup=build_main_menu_keyboard(settings.miniapp_url),
    )
    await message.answer(
        LEVEL_PROMPT_TEXT_UA,
        reply_markup=build_level_keyboard(current_level=profile.level),
    )


@router.callback_query(F.data.startswith(LEVEL_CALLBACK_PREFIX))
async def handle_level_selection(
    callback: CallbackQuery,
    api_client: BackendAPIClient,
    settings: BotSettings,
) -> None:
    """Persist a chosen CEFR level (A1–B1) via ``PATCH /api/users/settings``."""
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return

    raw_level = callback.data.removeprefix(LEVEL_CALLBACK_PREFIX)
    if raw_level not in VALID_USER_LEVELS:
        await callback.answer(UNKNOWN_LEVEL_MESSAGE_UA)
        return

    selected_level = cast(UserLevel, raw_level)
    try:
        updated_profile = await api_client.update_settings(
            callback.from_user.id,
            level=selected_level,
        )
    except BackendAPIError as exc:
        await callback.answer()
        if callback.message is not None and not isinstance(
            callback.message,
            InaccessibleMessage,
        ):
            await callback.message.answer(exc.user_message)
        return

    await callback.answer(f"Рівень {updated_profile.level} збережено.")
    if callback.message is not None and not isinstance(
        callback.message,
        InaccessibleMessage,
    ):
        await callback.message.answer(
            build_level_updated_text(updated_profile),
            reply_markup=build_main_menu_keyboard(settings.miniapp_url),
        )


__all__ = [
    "LEVEL_PROMPT_TEXT_UA",
    "UNKNOWN_LEVEL_MESSAGE_UA",
    "build_level_updated_text",
    "build_welcome_text",
    "handle_level_selection",
    "handle_start",
    "router",
]
