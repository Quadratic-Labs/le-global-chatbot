"""Build the public configuration used by the chatbot frontend."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from app.models.catalog import LegalCatalogResponse
from app.models.frontend import (
    FrontendConfigResponse,
    FrontendLimits,
)
from app.services.legal_catalog import (
    LegalCatalogError,
    get_legal_catalog,
)


API_VERSION: Final[str] = "0.5.0"

DEFAULT_LANGUAGE: Final[str] = "en"
SUPPORTED_LANGUAGES: Final[tuple[str, ...]] = (
    "en",
)

QUESTION_MIN_LENGTH: Final[int] = 2
QUESTION_MAX_LENGTH: Final[int] = 2000

MAX_SOURCES_DEFAULT: Final[int] = 6
MAX_SOURCES_MIN: Final[int] = 1
MAX_SOURCES_MAX: Final[int] = 10


CatalogProvider = Callable[
    [],
    LegalCatalogResponse,
]


class FrontendConfigError(RuntimeError):
    """Raised when frontend configuration cannot be generated."""


def get_frontend_config(
    catalog_provider: CatalogProvider = get_legal_catalog,
) -> FrontendConfigResponse:
    """Return the current chatbot frontend configuration."""

    try:
        catalog = catalog_provider()

    except LegalCatalogError as error:
        raise FrontendConfigError(
            "The indexed legal catalog could not be loaded."
        ) from error

    return FrontendConfigResponse(
        api_version=API_VERSION,
        default_language=DEFAULT_LANGUAGE,
        supported_languages=list(
            SUPPORTED_LANGUAGES
        ),
        limits=FrontendLimits(
            question_min_length=QUESTION_MIN_LENGTH,
            question_max_length=QUESTION_MAX_LENGTH,
            max_sources_default=MAX_SOURCES_DEFAULT,
            max_sources_min=MAX_SOURCES_MIN,
            max_sources_max=MAX_SOURCES_MAX,
        ),
        catalog=catalog,
    )