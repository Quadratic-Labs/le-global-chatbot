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
        parsed_value = float(
            value
        )

    except ValueError as error:
        raise RuntimeError(
            "Invalid floating-point environment "
            f"variable: {name}"
        ) from error

    return parsed_value


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
    """Read a comma-separated environment variable."""

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

    openai_api_key: str | None
    openai_model: str
    openai_timeout_seconds: float

    api_access_key: str | None
    cors_allowed_origins: tuple[str, ...]
    rate_limit_requests: int
    rate_limit_window_seconds: int


@lru_cache
def get_settings() -> Settings:
    api_access_key = os.getenv(
        "API_ACCESS_KEY"
    )

    if api_access_key is not None:
        api_access_key = (
            api_access_key.strip()
            or None
        )

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
        openai_api_key=os.getenv(
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
        api_access_key=api_access_key,
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
    )