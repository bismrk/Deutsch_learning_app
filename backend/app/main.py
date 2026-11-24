from fastapi import FastAPI

from app.api import api_router
from app.api.users import router as users_router
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
    application.include_router(users_router, prefix="/api")
    return application


app = create_app()
