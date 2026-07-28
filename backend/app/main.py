from fastapi import FastAPI

from app.routers.chat import (
    router as chat_router,
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


app = FastAPI(
    title="L&E Global Chatbot API",
    version="0.3.0",
    description=(
        "Backend API for the L&E Global legal chatbot."
    ),
)

app.include_router(
    health_router
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


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "le-global-backend",
        "version": "0.3.0",
        "documentation": "/docs",
    }