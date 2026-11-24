"""Telegram bot routers package."""

from bot_app.handlers.settings import router as settings_router
from bot_app.handlers.start import router as start_router
from bot_app.handlers.training import router as training_router
from bot_app.handlers.vocabulary import router as vocabulary_router

__all__ = ["settings_router", "start_router", "training_router", "vocabulary_router"]
