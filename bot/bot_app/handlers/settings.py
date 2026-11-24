"""User settings handlers: level, exercises per set, reminder hour, and IANA timezone."""

from __future__ import annotations

import html
from typing import Final, cast

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Message

from bot_app.api_client import (
    BackendAPIClient,
    BackendAPIError,
    ValidationAPIError,
)
from bot_app.config import BotSettings
from bot_app.keyboards import (
    MAIN_MENU_BUTTONS,
    MENU_BTN_SETTINGS,
    SETTINGS_CANCEL_CALLBACK,
    SETTINGS_EXERCISES_CALLBACK_PREFIX,
    SETTINGS_EXERCISES_CUSTOM_CALLBACK,
    SETTINGS_LEVEL_CALLBACK_PREFIX,
    SETTINGS_MENU_CALLBACK,
    SETTINGS_OPEN_EXERCISES_CALLBACK,
    SETTINGS_OPEN_LEVEL_CALLBACK,
    SETTINGS_OPEN_REMINDER_CALLBACK,
    SETTINGS_OPEN_TIMEZONE_CALLBACK,
    SETTINGS_REMINDER_CALLBACK_PREFIX,
    SETTINGS_REMINDER_CUSTOM_CALLBACK,
    SETTINGS_TZ_CALLBACK_PREFIX,
    SETTINGS_TZ_CUSTOM_CALLBACK,
    build_main_menu_keyboard,
    build_settings_cancel_keyboard,
    build_settings_exercises_keyboard,
    build_settings_level_keyboard,
    build_settings_menu_keyboard,
    build_settings_reminder_keyboard,
    build_settings_timezone_keyboard,
)
from bot_app.schemas import VALID_USER_LEVELS, UserLevel, UserProfile

router = Router(name="settings")

MIN_EXERCISES_PER_SET: Final = 1
MAX_EXERCISES_PER_SET: Final = 30
MIN_REMINDER_HOUR: Final = 0
MAX_REMINDER_HOUR: Final = 23

SETTINGS_INVALID_LEVEL_UA: Final = (
    "Невідомий рівень. Обери один із варіантів: A1, A2 або B1."
)
SETTINGS_CUSTOM_EXERCISES_PROMPT_UA: Final = (
    "✏️ Введи кількість вправ у сеті — ціле число від <b>1</b> до <b>30</b>:"
)
SETTINGS_INVALID_EXERCISES_UA: Final = (
    "⚠️ Кількість вправ у сеті має бути цілим числом від 1 до 30. "
    "Спробуй ще раз або натисни «Скасувати»."
)
SETTINGS_CUSTOM_REMINDER_PROMPT_UA: Final = (
    "✏️ Введи годину щоденного нагадування — ціле число від <b>0</b> до <b>23</b>:"
)
SETTINGS_INVALID_REMINDER_UA: Final = (
    "⚠️ Година нагадування має бути цілим числом від 0 до 23. "
    "Спробуй ще раз або натисни «Скасувати»."
)
SETTINGS_CUSTOM_TIMEZONE_PROMPT_UA: Final = (
    "✏️ Введи назву часового поясу у форматі IANA, наприклад:\n"
    "<code>Europe/Kyiv</code>, <code>Europe/Berlin</code>, <code>Europe/Warsaw</code>, "
    "<code>America/New_York</code> або <code>UTC</code>."
)
SETTINGS_INVALID_TIMEZONE_UA: Final = (
    "⚠️ Не вдалося розпізнати такий часовий пояс.\n\n"
    "Введи коректну IANA-назву (наприклад, <code>Europe/Kyiv</code>, "
    "<code>Europe/Berlin</code>, <code>Europe/Warsaw</code> або <code>UTC</code>) "
    "або натисни «Скасувати»."
)
SETTINGS_NON_TEXT_INPUT_UA: Final = (
    "Будь ласка, надішли значення текстовим повідомленням або натисни «Скасувати»."
)
SETTINGS_CANCELLED_UA: Final = (
    "Зміну налаштування скасовано. Обирай дію в головному меню нижче."
)


class SettingsStates(StatesGroup):
    """Finite state machine for custom text settings inputs."""

    waiting_exercises_per_set = State()
    waiting_reminder_hour = State()
    waiting_timezone = State()


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _get_accessible_message(callback: CallbackQuery) -> Message | None:
    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        return None
    return callback.message


def _parse_int_in_range(raw: str, *, min_value: int, max_value: int) -> int | None:
    cleaned = raw.strip()
    if not cleaned:
        return None
    # Reject signs/spaces other than plain digits (including negative numbers).
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    if min_value <= value <= max_value:
        return value
    return None


def format_settings_overview(profile: UserProfile) -> str:
    """Format the user's current settings summary as safe Telegram HTML."""
    sheets_status = "підключено" if profile.sheets_url else "не налаштовано"
    return (
        "⚙️ <b>Налаштування</b>\n\n"
        f"🎯 <b>Рівень:</b> {_escape(profile.level)}\n"
        f"📝 <b>Вправ у сеті:</b> {profile.exercises_per_set}\n"
        f"⏰ <b>Година нагадування:</b> {profile.reminder_hour:02d}:00\n"
        f"🌍 <b>Часовий пояс:</b> {_escape(profile.timezone)}\n"
        f"📊 <b>Google Sheets:</b> {sheets_status}\n\n"
        "Обери параметр, який хочеш змінити:"
    )


def format_level_section_text(profile: UserProfile) -> str:
    """Format the CEFR level settings sub-menu text."""
    return (
        "🎯 <b>Рівень німецької</b>\n\n"
        f"Поточний рівень: <b>{_escape(profile.level)}</b>\n"
        "Обери новий рівень для підбору вправ і граматичних тем:"
    )


def format_exercises_section_text(profile: UserProfile) -> str:
    """Format the exercises-per-set settings sub-menu text."""
    return (
        "📝 <b>Кількість вправ у сеті</b>\n\n"
        f"Поточне значення: <b>{profile.exercises_per_set}</b>\n"
        "Обери кількість вправ в одному тренувальному сеті або введи власне число (1–30):"
    )


def format_reminder_section_text(profile: UserProfile) -> str:
    """Format the reminder hour settings sub-menu text."""
    return (
        "⏰ <b>Година щоденного нагадування</b>\n\n"
        f"Поточний час: <b>{profile.reminder_hour:02d}:00</b> ({_escape(profile.timezone)})\n"
        "Обери зручну годину (0–23) або введи її вручну:"
    )


def format_timezone_section_text(profile: UserProfile) -> str:
    """Format the IANA timezone settings sub-menu text."""
    return (
        "🌍 <b>Часовий пояс</b>\n\n"
        f"Поточний часовий пояс: <b>{_escape(profile.timezone)}</b>\n"
        "Обери зі списку поширених IANA-зон або введи власну назву:"
    )


async def _safe_edit_text(
    target: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit message text safely, ignoring Telegram's 'message is not modified' error."""
    if hasattr(target, "edit_text"):
        try:
            await target.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
    await target.answer(
        text,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


@router.message(F.text == MENU_BTN_SETTINGS)
@router.message(Command("settings"))
async def handle_settings_menu(
    message: Message,
    state: FSMContext,
    api_client: BackendAPIClient,
) -> None:
    """Load the user's profile via ``GET /api/users/me`` and display settings overview."""
    if message.from_user is None:
        return

    await state.clear()
    try:
        profile = await api_client.get_me(message.from_user.id)
    except BackendAPIError as exc:
        await message.answer(exc.user_message)
        return

    await message.answer(
        format_settings_overview(profile),
        parse_mode="HTML",
        reply_markup=build_settings_menu_keyboard(),
    )


@router.callback_query(F.data == SETTINGS_MENU_CALLBACK)
async def handle_settings_back_to_menu(
    callback: CallbackQuery,
    state: FSMContext,
    api_client: BackendAPIClient,
) -> None:
    """Return to the main Settings overview screen."""
    if callback.from_user is None:
        await callback.answer()
        return

    await state.clear()
    target = _get_accessible_message(callback)
    try:
        profile = await api_client.get_me(callback.from_user.id)
    except BackendAPIError as exc:
        await callback.answer()
        if target is not None:
            await target.answer(exc.user_message)
        return

    await callback.answer()
    if target is not None:
        await _safe_edit_text(
            target,
            format_settings_overview(profile),
            reply_markup=build_settings_menu_keyboard(),
        )


@router.callback_query(F.data == SETTINGS_OPEN_LEVEL_CALLBACK)
async def handle_settings_open_level(
    callback: CallbackQuery,
    api_client: BackendAPIClient,
) -> None:
    """Open the CEFR level selection sub-menu."""
    if callback.from_user is None:
        await callback.answer()
        return

    target = _get_accessible_message(callback)
    try:
        profile = await api_client.get_me(callback.from_user.id)
    except BackendAPIError as exc:
        await callback.answer()
        if target is not None:
            await target.answer(exc.user_message)
        return

    await callback.answer()
    if target is not None:
        await _safe_edit_text(
            target,
            format_level_section_text(profile),
            reply_markup=build_settings_level_keyboard(profile.level),
        )


@router.callback_query(F.data == SETTINGS_OPEN_EXERCISES_CALLBACK)
async def handle_settings_open_exercises(
    callback: CallbackQuery,
    api_client: BackendAPIClient,
) -> None:
    """Open the exercises-per-set selection sub-menu."""
    if callback.from_user is None:
        await callback.answer()
        return

    target = _get_accessible_message(callback)
    try:
        profile = await api_client.get_me(callback.from_user.id)
    except BackendAPIError as exc:
        await callback.answer()
        if target is not None:
            await target.answer(exc.user_message)
        return

    await callback.answer()
    if target is not None:
        await _safe_edit_text(
            target,
            format_exercises_section_text(profile),
            reply_markup=build_settings_exercises_keyboard(profile.exercises_per_set),
        )


@router.callback_query(F.data == SETTINGS_OPEN_REMINDER_CALLBACK)
async def handle_settings_open_reminder(
    callback: CallbackQuery,
    api_client: BackendAPIClient,
) -> None:
    """Open the daily reminder hour selection sub-menu."""
    if callback.from_user is None:
        await callback.answer()
        return

    target = _get_accessible_message(callback)
    try:
        profile = await api_client.get_me(callback.from_user.id)
    except BackendAPIError as exc:
        await callback.answer()
        if target is not None:
            await target.answer(exc.user_message)
        return

    await callback.answer()
    if target is not None:
        await _safe_edit_text(
            target,
            format_reminder_section_text(profile),
            reply_markup=build_settings_reminder_keyboard(profile.reminder_hour),
        )


@router.callback_query(F.data == SETTINGS_OPEN_TIMEZONE_CALLBACK)
async def handle_settings_open_timezone(
    callback: CallbackQuery,
    api_client: BackendAPIClient,
) -> None:
    """Open the IANA timezone selection sub-menu."""
    if callback.from_user is None:
        await callback.answer()
        return

    target = _get_accessible_message(callback)
    try:
        profile = await api_client.get_me(callback.from_user.id)
    except BackendAPIError as exc:
        await callback.answer()
        if target is not None:
            await target.answer(exc.user_message)
        return

    await callback.answer()
    if target is not None:
        await _safe_edit_text(
            target,
            format_timezone_section_text(profile),
            reply_markup=build_settings_timezone_keyboard(profile.timezone),
        )


@router.callback_query(F.data.startswith(SETTINGS_LEVEL_CALLBACK_PREFIX))
async def handle_settings_level_select(
    callback: CallbackQuery,
    api_client: BackendAPIClient,
) -> None:
    """Update CEFR level via ``PATCH /api/users/settings``."""
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return

    raw_level = callback.data.removeprefix(SETTINGS_LEVEL_CALLBACK_PREFIX)
    if raw_level not in VALID_USER_LEVELS:
        await callback.answer(SETTINGS_INVALID_LEVEL_UA)
        return

    target = _get_accessible_message(callback)
    selected_level = cast(UserLevel, raw_level)
    try:
        updated = await api_client.update_settings(
            callback.from_user.id,
            level=selected_level,
        )
    except ValidationAPIError:
        await callback.answer()
        if target is not None:
            await target.answer(SETTINGS_INVALID_LEVEL_UA)
        return
    except BackendAPIError as exc:
        await callback.answer()
        if target is not None:
            await target.answer(exc.user_message)
        return

    await callback.answer(f"Рівень {updated.level} збережено.")
    if target is not None:
        await _safe_edit_text(
            target,
            format_level_section_text(updated),
            reply_markup=build_settings_level_keyboard(updated.level),
        )


@router.callback_query(F.data.startswith(SETTINGS_EXERCISES_CALLBACK_PREFIX))
async def handle_settings_exercises_select(
    callback: CallbackQuery,
    api_client: BackendAPIClient,
) -> None:
    """Update exercises_per_set from a preset button via ``PATCH /api/users/settings``."""
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return

    raw_value = callback.data.removeprefix(SETTINGS_EXERCISES_CALLBACK_PREFIX)
    parsed = _parse_int_in_range(
        raw_value,
        min_value=MIN_EXERCISES_PER_SET,
        max_value=MAX_EXERCISES_PER_SET,
    )
    if parsed is None:
        await callback.answer("Кількість вправ має бути від 1 до 30.")
        return

    target = _get_accessible_message(callback)
    try:
        updated = await api_client.update_settings(
            callback.from_user.id,
            exercises_per_set=parsed,
        )
    except ValidationAPIError:
        await callback.answer()
        if target is not None:
            await target.answer(SETTINGS_INVALID_EXERCISES_UA)
        return
    except BackendAPIError as exc:
        await callback.answer()
        if target is not None:
            await target.answer(exc.user_message)
        return

    await callback.answer(f"Кількість вправ у сеті: {updated.exercises_per_set}.")
    if target is not None:
        await _safe_edit_text(
            target,
            format_exercises_section_text(updated),
            reply_markup=build_settings_exercises_keyboard(updated.exercises_per_set),
        )


@router.callback_query(F.data == SETTINGS_EXERCISES_CUSTOM_CALLBACK)
async def handle_settings_exercises_custom_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Prompt the user to type a custom exercises-per-set integer (1–30)."""
    await callback.answer()
    if callback.from_user is None:
        return

    await state.set_state(SettingsStates.waiting_exercises_per_set)
    target = _get_accessible_message(callback)
    if target is not None:
        await target.answer(
            SETTINGS_CUSTOM_EXERCISES_PROMPT_UA,
            parse_mode="HTML",
            reply_markup=build_settings_cancel_keyboard(),
        )


@router.callback_query(F.data.startswith(SETTINGS_REMINDER_CALLBACK_PREFIX))
async def handle_settings_reminder_select(
    callback: CallbackQuery,
    api_client: BackendAPIClient,
) -> None:
    """Update reminder_hour (0–23) from an inline button via ``PATCH /api/users/settings``."""
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return

    raw_hour = callback.data.removeprefix(SETTINGS_REMINDER_CALLBACK_PREFIX)
    parsed_hour = _parse_int_in_range(
        raw_hour,
        min_value=MIN_REMINDER_HOUR,
        max_value=MAX_REMINDER_HOUR,
    )
    if parsed_hour is None:
        await callback.answer("Година нагадування має бути від 0 до 23.")
        return

    target = _get_accessible_message(callback)
    try:
        updated = await api_client.update_settings(
            callback.from_user.id,
            reminder_hour=parsed_hour,
        )
    except ValidationAPIError:
        await callback.answer()
        if target is not None:
            await target.answer(SETTINGS_INVALID_REMINDER_UA)
        return
    except BackendAPIError as exc:
        await callback.answer()
        if target is not None:
            await target.answer(exc.user_message)
        return

    await callback.answer(f"Нагадування о {updated.reminder_hour:02d}:00 збережено.")
    if target is not None:
        await _safe_edit_text(
            target,
            format_reminder_section_text(updated),
            reply_markup=build_settings_reminder_keyboard(updated.reminder_hour),
        )


@router.callback_query(F.data == SETTINGS_REMINDER_CUSTOM_CALLBACK)
async def handle_settings_reminder_custom_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Prompt the user to type a custom reminder hour (0–23)."""
    await callback.answer()
    if callback.from_user is None:
        return

    await state.set_state(SettingsStates.waiting_reminder_hour)
    target = _get_accessible_message(callback)
    if target is not None:
        await target.answer(
            SETTINGS_CUSTOM_REMINDER_PROMPT_UA,
            parse_mode="HTML",
            reply_markup=build_settings_cancel_keyboard(),
        )


@router.callback_query(F.data.startswith(SETTINGS_TZ_CALLBACK_PREFIX))
async def handle_settings_timezone_select(
    callback: CallbackQuery,
    api_client: BackendAPIClient,
) -> None:
    """Update IANA timezone from a preset button via ``PATCH /api/users/settings``."""
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return

    zone = callback.data.removeprefix(SETTINGS_TZ_CALLBACK_PREFIX).strip()
    if not zone:
        await callback.answer("Некоректний часовий пояс.")
        return

    target = _get_accessible_message(callback)
    try:
        updated = await api_client.update_settings(
            callback.from_user.id,
            timezone=zone,
        )
    except ValidationAPIError:
        await callback.answer()
        if target is not None:
            await target.answer(SETTINGS_INVALID_TIMEZONE_UA, parse_mode="HTML")
        return
    except BackendAPIError as exc:
        await callback.answer()
        if target is not None:
            await target.answer(exc.user_message)
        return

    await callback.answer(f"Часовий пояс {updated.timezone} збережено.")
    if target is not None:
        await _safe_edit_text(
            target,
            format_timezone_section_text(updated),
            reply_markup=build_settings_timezone_keyboard(updated.timezone),
        )


@router.callback_query(F.data == SETTINGS_TZ_CUSTOM_CALLBACK)
async def handle_settings_timezone_custom_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Prompt the user to type a custom IANA timezone name."""
    await callback.answer()
    if callback.from_user is None:
        return

    await state.set_state(SettingsStates.waiting_timezone)
    target = _get_accessible_message(callback)
    if target is not None:
        await target.answer(
            SETTINGS_CUSTOM_TIMEZONE_PROMPT_UA,
            parse_mode="HTML",
            reply_markup=build_settings_cancel_keyboard(),
        )


@router.callback_query(F.data == SETTINGS_CANCEL_CALLBACK)
async def handle_settings_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    """Cancel custom settings FSM input, clear state, and restore the main menu."""
    await state.clear()
    await callback.answer("Введення скасовано.")

    target = _get_accessible_message(callback)
    if target is not None:
        await target.answer(
            SETTINGS_CANCELLED_UA,
            reply_markup=build_main_menu_keyboard(settings.miniapp_url),
        )


@router.message(
    SettingsStates.waiting_exercises_per_set,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MAIN_MENU_BUTTONS),
)
async def handle_settings_exercises_text_input(
    message: Message,
    state: FSMContext,
    api_client: BackendAPIClient,
) -> None:
    """Validate and save a custom exercises_per_set value (1–30)."""
    if message.from_user is None:
        return

    parsed = _parse_int_in_range(
        message.text or "",
        min_value=MIN_EXERCISES_PER_SET,
        max_value=MAX_EXERCISES_PER_SET,
    )
    if parsed is None:
        await message.answer(
            SETTINGS_INVALID_EXERCISES_UA,
            reply_markup=build_settings_cancel_keyboard(),
        )
        return

    try:
        updated = await api_client.update_settings(
            message.from_user.id,
            exercises_per_set=parsed,
        )
    except ValidationAPIError:
        await message.answer(
            SETTINGS_INVALID_EXERCISES_UA,
            reply_markup=build_settings_cancel_keyboard(),
        )
        return
    except BackendAPIError as exc:
        await message.answer(
            exc.user_message,
            reply_markup=build_settings_cancel_keyboard(),
        )
        return

    await state.clear()
    await message.answer(
        f"✅ Кількість вправ у сеті оновлено: <b>{updated.exercises_per_set}</b>.\n\n"
        f"{format_settings_overview(updated)}",
        parse_mode="HTML",
        reply_markup=build_settings_menu_keyboard(),
    )


@router.message(
    SettingsStates.waiting_reminder_hour,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MAIN_MENU_BUTTONS),
)
async def handle_settings_reminder_text_input(
    message: Message,
    state: FSMContext,
    api_client: BackendAPIClient,
) -> None:
    """Validate and save a custom reminder_hour value (0–23)."""
    if message.from_user is None:
        return

    parsed_hour = _parse_int_in_range(
        message.text or "",
        min_value=MIN_REMINDER_HOUR,
        max_value=MAX_REMINDER_HOUR,
    )
    if parsed_hour is None:
        await message.answer(
            SETTINGS_INVALID_REMINDER_UA,
            reply_markup=build_settings_cancel_keyboard(),
        )
        return

    try:
        updated = await api_client.update_settings(
            message.from_user.id,
            reminder_hour=parsed_hour,
        )
    except ValidationAPIError:
        await message.answer(
            SETTINGS_INVALID_REMINDER_UA,
            reply_markup=build_settings_cancel_keyboard(),
        )
        return
    except BackendAPIError as exc:
        await message.answer(
            exc.user_message,
            reply_markup=build_settings_cancel_keyboard(),
        )
        return

    await state.clear()
    await message.answer(
        f"✅ Годину нагадування оновлено: <b>{updated.reminder_hour:02d}:00</b>.\n\n"
        f"{format_settings_overview(updated)}",
        parse_mode="HTML",
        reply_markup=build_settings_menu_keyboard(),
    )


@router.message(
    SettingsStates.waiting_timezone,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MAIN_MENU_BUTTONS),
)
async def handle_settings_timezone_text_input(
    message: Message,
    state: FSMContext,
    api_client: BackendAPIClient,
) -> None:
    """Send a custom IANA timezone to Backend API and handle ``422`` validation gracefully."""
    if message.from_user is None:
        return

    cleaned_tz = (message.text or "").strip()
    if not cleaned_tz:
        await message.answer(
            SETTINGS_INVALID_TIMEZONE_UA,
            parse_mode="HTML",
            reply_markup=build_settings_cancel_keyboard(),
        )
        return

    try:
        updated = await api_client.update_settings(
            message.from_user.id,
            timezone=cleaned_tz,
        )
    except ValidationAPIError:
        await message.answer(
            SETTINGS_INVALID_TIMEZONE_UA,
            parse_mode="HTML",
            reply_markup=build_settings_cancel_keyboard(),
        )
        return
    except BackendAPIError as exc:
        await message.answer(
            exc.user_message,
            reply_markup=build_settings_cancel_keyboard(),
        )
        return

    await state.clear()
    await message.answer(
        f"✅ Часовий пояс оновлено: <b>{_escape(updated.timezone)}</b>.\n\n"
        f"{format_settings_overview(updated)}",
        parse_mode="HTML",
        reply_markup=build_settings_menu_keyboard(),
    )


@router.message(SettingsStates.waiting_exercises_per_set, ~F.text)
@router.message(SettingsStates.waiting_reminder_hour, ~F.text)
@router.message(SettingsStates.waiting_timezone, ~F.text)
async def handle_settings_non_text_input(message: Message) -> None:
    """Reject stickers, photos, voice notes, etc. during settings text input."""
    await message.answer(
        SETTINGS_NON_TEXT_INPUT_UA,
        reply_markup=build_settings_cancel_keyboard(),
    )


__all__ = [
    "MAX_EXERCISES_PER_SET",
    "MAX_REMINDER_HOUR",
    "MIN_EXERCISES_PER_SET",
    "MIN_REMINDER_HOUR",
    "SETTINGS_CANCELLED_UA",
    "SETTINGS_CUSTOM_EXERCISES_PROMPT_UA",
    "SETTINGS_CUSTOM_REMINDER_PROMPT_UA",
    "SETTINGS_CUSTOM_TIMEZONE_PROMPT_UA",
    "SETTINGS_INVALID_EXERCISES_UA",
    "SETTINGS_INVALID_LEVEL_UA",
    "SETTINGS_INVALID_REMINDER_UA",
    "SETTINGS_INVALID_TIMEZONE_UA",
    "SETTINGS_NON_TEXT_INPUT_UA",
    "SettingsStates",
    "format_exercises_section_text",
    "format_level_section_text",
    "format_reminder_section_text",
    "format_settings_overview",
    "format_timezone_section_text",
    "handle_settings_back_to_menu",
    "handle_settings_cancel",
    "handle_settings_exercises_custom_start",
    "handle_settings_exercises_select",
    "handle_settings_exercises_text_input",
    "handle_settings_level_select",
    "handle_settings_menu",
    "handle_settings_non_text_input",
    "handle_settings_open_exercises",
    "handle_settings_open_level",
    "handle_settings_open_reminder",
    "handle_settings_open_timezone",
    "handle_settings_reminder_custom_start",
    "handle_settings_reminder_select",
    "handle_settings_reminder_text_input",
    "handle_settings_timezone_custom_start",
    "handle_settings_timezone_select",
    "handle_settings_timezone_text_input",
    "router",
]
