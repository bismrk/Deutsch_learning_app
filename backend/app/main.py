from fastapi import FastAPI

from app.api import api_router
from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    resolved_settings = settings if settings is not None else get_settings()

    application = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
    )
    application.state.settings = resolved_settings
    application.include_router(api_router)
    return application


app = create_app()
