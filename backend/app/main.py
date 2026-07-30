import logging
import os
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import (
    CORSMiddleware,
)

from app.core.config import env_csv
from app.middleware.api_protection import (
    ApiProtectionMiddleware,
)
from app.routers.admin_document_lifecycle import (
    router as admin_document_lifecycle_router,
)
from app.routers.admin_documents import (
    router as admin_documents_router,
)
from app.routers.chat import (
    router as chat_router,
)
from app.routers.frontend_config import (
    router as frontend_config_router,
)
from app.routers.health import (
    router as health_router,
)
from app.routers.legal_catalog import (
    router as legal_catalog_router,
)
from app.routers.legal_search import (
    router as legal_search_router,
)


def configure_application_logging() -> None:
    """Configure application logs for container stdout."""

    level_name = os.getenv(
        "LOG_LEVEL",
        "INFO",
    ).strip().upper()

    level = getattr(
        logging,
        level_name,
        logging.INFO,
    )

    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    app_logger.propagate = False

    if not app_logger.handlers:
        handler = logging.StreamHandler(
            sys.stdout
        )

        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter("%(message)s")
        )

        app_logger.addHandler(handler)


configure_application_logging()


app = FastAPI(
    title="L&E Global Chatbot API",
    version="0.7.0",
    description=(
        "Backend API for the L&E Global legal chatbot."
    ),
)

app.add_middleware(
    ApiProtectionMiddleware
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(
        env_csv(
            "CORS_ALLOWED_ORIGINS"
        )
    ),
    allow_credentials=False,
    allow_methods=[
        "GET",
        "POST",
        "DELETE",
        "OPTIONS",
    ],
    allow_headers=[
        "Content-Type",
        "X-API-Key",
        "X-Admin-Key",
    ],
)

app.include_router(
    health_router
)

app.include_router(
    frontend_config_router
)

app.include_router(
    legal_search_router
)

app.include_router(
    legal_catalog_router
)

app.include_router(
    chat_router
)

app.include_router(
    admin_documents_router
)

app.include_router(
    admin_document_lifecycle_router
)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "le-global-backend",
        "version": "0.7.0",
        "documentation": "/docs",
    }