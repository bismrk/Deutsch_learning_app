"""Entrypoint for running the standalone Telegram bot in long-polling mode."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from bot_app.api_client import BackendAPIClient
from bot_app.config import BotSettings, get_settings
from bot_app.handlers import start_router

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


def create_dispatcher(api_client: BackendAPIClient, settings: BotSettings) -> Dispatcher:
    """Create the ``aiogram.Dispatcher`` with shared dependencies and routers."""
    dispatcher = Dispatcher()
    dispatcher["api_client"] = api_client
    dispatcher["settings"] = settings
    dispatcher.include_router(start_router)
    return dispatcher


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
        await dispatcher.start_polling(bot)
    finally:
        await api_client.aclose()
        await bot.session.close()


def main() -> None:
    """Synchronous CLI entrypoint for ``python -m bot_app.main``."""
    asyncio.run(run_polling())


if __name__ == "__main__":
    main()
