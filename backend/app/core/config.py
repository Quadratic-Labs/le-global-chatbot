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
            f"Invalid floating-point environment variable: {name}"
        ) from error

    return parsed_value


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
        openai_api_key=os.getenv(
            "OPENAI_API_KEY"
        ),
        openai_model=os.getenv(
            "OPENAI_MODEL",
            "gpt-5",
        ),
        openai_timeout_seconds=env_float(
            "OPENAI_TIMEOUT_SECONDS",
            60.0,
        ),
    )