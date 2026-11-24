"""Entrypoint for running the standalone Telegram bot in long-polling mode."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import MenuButtonDefault, MenuButtonWebApp, WebAppInfo

from bot_app.api_client import BackendAPIClient
from bot_app.config import BotSettings, get_settings, normalize_https_miniapp_url
from bot_app.handlers import (
    settings_router,
    start_router,
    training_router,
    vocabulary_router,
)
from bot_app.keyboards import MENU_BTN_MINIAPP

logger = logging.getLogger(__name__)


def configure_logging(log_level: str) -> None:
    """Configure root logging for the bot service."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def create_bot(settings: BotSettings) -> Bot:
    """Create an ``aiogram.Bot`` instance after validating ``BOT_TOKEN``."""
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN must be configured before starting the Telegram bot.")
    return Bot(token=settings.bot_token)


def _attach_router(dispatcher: Dispatcher, router: Router) -> None:
    """Attach a module-level router, detaching any prior parent dispatcher cleanly."""
    if router.parent_router is not None:
        router._parent_router = None
    dispatcher.include_router(router)


def create_dispatcher(api_client: BackendAPIClient, settings: BotSettings) -> Dispatcher:
    """Create the ``aiogram.Dispatcher`` with shared dependencies and routers."""
    dispatcher = Dispatcher()
    dispatcher["api_client"] = api_client
    dispatcher["settings"] = settings
    _attach_router(dispatcher, start_router)
    _attach_router(dispatcher, training_router)
    _attach_router(dispatcher, vocabulary_router)
    _attach_router(dispatcher, settings_router)
    return dispatcher


async def configure_menu_button(bot: Bot, miniapp_url: str | None) -> None:
    """Configure Telegram's native chat menu button for the Mini App, or reset to default."""
    safe_url = normalize_https_miniapp_url(miniapp_url)
    menu_button: MenuButtonWebApp | MenuButtonDefault
    if safe_url is not None:
        menu_button = MenuButtonWebApp(
            text=MENU_BTN_MINIAPP,
            web_app=WebAppInfo(url=safe_url),
        )
    else:
        menu_button = MenuButtonDefault()

    try:
        await bot.set_chat_menu_button(menu_button=menu_button)
    except TelegramAPIError as exc:
        error_message = exc.message
        bot_token = getattr(bot, "token", None)
        if isinstance(bot_token, str) and bot_token:
            error_message = error_message.replace(bot_token, "[REDACTED]")
        logger.warning(
            "Failed to configure Telegram chat menu button (miniapp_enabled=%s, error=%s): %s",
            safe_url is not None,
            type(exc).__name__,
            error_message,
        )


async def run_polling(settings: BotSettings | None = None) -> None:
    """Run the Telegram bot in long-polling mode with clean HTTP client shutdown."""
    resolved_settings = settings if settings is not None else get_settings()
    configure_logging(resolved_settings.log_level)

    if not resolved_settings.internal_api_key:
        raise RuntimeError(
            "INTERNAL_API_KEY must be configured before starting the Telegram bot.",
        )

    bot = create_bot(resolved_settings)
    api_client = BackendAPIClient(
        base_url=resolved_settings.backend_api_url,
        internal_api_key=resolved_settings.internal_api_key,
        timeout=resolved_settings.backend_api_timeout_seconds,
    )
    dispatcher = create_dispatcher(api_client=api_client, settings=resolved_settings)

    logger.info(
        "Starting Telegram bot polling (backend_api_url=%s, miniapp_enabled=%s)",
        resolved_settings.backend_api_url,
        resolved_settings.https_miniapp_url is not None,
    )
    try:
        await configure_menu_button(bot, resolved_settings.miniapp_url)
        await dispatcher.start_polling(bot)
    finally:
        await api_client.aclose()
        await bot.session.close()


def main() -> None:
    """Synchronous CLI entrypoint for ``python -m bot_app.main``."""
    asyncio.run(run_polling())


if __name__ == "__main__":
    main()
