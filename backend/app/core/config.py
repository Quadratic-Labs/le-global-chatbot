import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def required_env(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def env_bool(
    name: str,
    default: bool = False,
) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def env_float(
    name: str,
    default: float,
) -> float:
    value = os.getenv(name)

    if value is None:
        return default

    try:
        return float(
            value
        )

    except ValueError as error:
        raise RuntimeError(
            "Invalid floating-point environment "
            f"variable: {name}"
        ) from error


def env_int(
    name: str,
    default: int,
    minimum: int = 1,
) -> int:
    value = os.getenv(name)

    if value is None:
        return default

    try:
        parsed_value = int(
            value
        )

    except ValueError as error:
        raise RuntimeError(
            f"Invalid integer environment variable: {name}"
        ) from error

    if parsed_value < minimum:
        raise RuntimeError(
            f"{name} must be greater than or equal "
            f"to {minimum}."
        )

    return parsed_value


def env_csv(
    name: str,
) -> tuple[str, ...]:
    raw_value = os.getenv(
        name,
        "",
    )

    values: list[str] = []
    seen_values: set[str] = set()

    for value in raw_value.split(","):
        normalized_value = value.strip()

        if not normalized_value:
            continue

        if normalized_value in seen_values:
            continue

        seen_values.add(
            normalized_value
        )

        values.append(
            normalized_value
        )

    return tuple(
        values
    )


def optional_secret(
    name: str,
) -> str | None:
    """Read an optional secret without retaining whitespace."""

    value = os.getenv(
        name
    )

    if value is None:
        return None

    return value.strip() or None


@dataclass(frozen=True)
class Settings:
    app_env: str

    opensearch_url: str
    opensearch_username: str
    opensearch_password: str
    opensearch_verify_certs: bool

    redis_url: str

    document_source_dir: Path
    document_processed_dir: Path
    document_upload_max_bytes: int

    openai_api_key: str | None
    openai_model: str
    openai_timeout_seconds: float

    api_access_key: str | None
    admin_api_key: str | None

    cors_allowed_origins: tuple[str, ...]
    rate_limit_requests: int
    rate_limit_window_seconds: int

    rerank_enabled: bool
    rerank_pool_multiplier: int


@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_env=os.getenv(
            "APP_ENV",
            "development",
        ),
        opensearch_url=required_env(
            "OPENSEARCH_URL"
        ),
        opensearch_username=os.getenv(
            "OPENSEARCH_USERNAME",
            "admin",
        ),
        opensearch_password=required_env(
            "OPENSEARCH_PASSWORD"
        ),
        opensearch_verify_certs=env_bool(
            "OPENSEARCH_VERIFY_CERTS",
            False,
        ),
        redis_url=required_env(
            "REDIS_URL"
        ),
        document_source_dir=Path(
            required_env(
                "DOCUMENT_SOURCE_DIR"
            )
        ),
        document_processed_dir=Path(
            required_env(
                "DOCUMENT_PROCESSED_DIR"
            )
        ),
        document_upload_max_bytes=env_int(
            "DOCUMENT_UPLOAD_MAX_BYTES",
            25 * 1024 * 1024,
        ),
        openai_api_key=optional_secret(
            "OPENAI_API_KEY"
        ),
        openai_model=os.getenv(
            "OPENAI_MODEL",
            "gpt-5-mini",
        ),
        openai_timeout_seconds=env_float(
            "OPENAI_TIMEOUT_SECONDS",
            60.0,
        ),
        api_access_key=optional_secret(
            "API_ACCESS_KEY"
        ),
        admin_api_key=optional_secret(
            "ADMIN_API_KEY"
        ),
        cors_allowed_origins=env_csv(
            "CORS_ALLOWED_ORIGINS"
        ),
        rate_limit_requests=env_int(
            "RATE_LIMIT_REQUESTS",
            60,
        ),
        rate_limit_window_seconds=env_int(
            "RATE_LIMIT_WINDOW_SECONDS",
            60,
        ),
        rerank_enabled=env_bool(
            "RERANK_ENABLED",
            False,
        ),
        rerank_pool_multiplier=env_int(
            "RERANK_POOL_MULTIPLIER",
            3,
        ),
    )