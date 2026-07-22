import os
from dataclasses import dataclass
from functools import lru_cache


def required_env(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    opensearch_url: str
    opensearch_username: str
    opensearch_password: str
    opensearch_verify_certs: bool
    redis_url: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_env=os.getenv("APP_ENV", "development"),
        opensearch_url=required_env("OPENSEARCH_URL"),
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
        redis_url=required_env("REDIS_URL"),
    )
