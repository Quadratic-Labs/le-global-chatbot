from functools import lru_cache
from urllib.parse import urlparse

from opensearchpy import OpenSearch

from app.core.config import get_settings


@lru_cache
def get_opensearch_client() -> OpenSearch:
    settings = get_settings()
    parsed_url = urlparse(settings.opensearch_url)

    if not parsed_url.hostname:
        raise RuntimeError("Invalid OPENSEARCH_URL")

    use_ssl = parsed_url.scheme == "https"

    return OpenSearch(
        hosts=[
            {
                "host": parsed_url.hostname,
                "port": parsed_url.port or (
                    443 if use_ssl else 9200
                ),
            }
        ],
        http_auth=(
            settings.opensearch_username,
            settings.opensearch_password,
        ),
        use_ssl=use_ssl,
        verify_certs=settings.opensearch_verify_certs,
        ssl_assert_hostname=settings.opensearch_verify_certs,
        ssl_show_warn=False,
    )
