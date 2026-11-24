"""Telegram bot routers package."""

from bot_app.handlers.start import router as start_router
from bot_app.handlers.training import router as training_router

__all__ = ["start_router", "training_router"]
