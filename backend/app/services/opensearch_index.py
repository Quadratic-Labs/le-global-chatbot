from typing import Any, Final

from opensearchpy import OpenSearch

from app.clients.opensearch import get_opensearch_client


LEGAL_DOCUMENTS_INDEX: Final[str] = (
    "legal-documents-v1"
)

LEGAL_DOCUMENTS_ALIAS: Final[str] = (
    "legal-documents"
)


LEGAL_DOCUMENTS_INDEX_BODY: Final[
    dict[str, Any]
] = {
    "settings": {
        # The current OpenSearch cluster contains one node.
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        # Reject accidental fields instead of silently creating them.
        "dynamic": "strict",
        "properties": {
            "document_id": {
                "type": "keyword",
            },
            "chunk_id": {
                "type": "keyword",
            },
            "country": {
                "type": "keyword",
            },
            "country_code": {
                "type": "keyword",
            },
            "legal_topic": {
                "type": "keyword",
            },
            "document_type": {
                "type": "keyword",
            },
            "language": {
                "type": "keyword",
            },
            "section": {
                "type": "text",
                "fields": {
                    "keyword": {
                        "type": "keyword",
                        "ignore_above": 512,
                    }
                },
            },
            "subsection": {
                "type": "text",
                "fields": {
                    "keyword": {
                        "type": "keyword",
                        "ignore_above": 512,
                    }
                },
            },
            "content": {
                "type": "text",
            },
            "source_filename": {
                "type": "keyword",
            },
            "source_format": {
                "type": "keyword",
            },
            "content_hash": {
                "type": "keyword",
            },
            "reference_year": {
                "type": "integer",
            },
        },
    },
    "aliases": {
        LEGAL_DOCUMENTS_ALIAS: {},
    },
}


def ensure_legal_documents_index(
    client: OpenSearch | None = None,
) -> dict[str, object]:
    """
    Create the legal documents index when it does not exist.

    The function is idempotent:
    - the index is created only once;
    - the search alias is restored when missing;
    - an existing index is never deleted.
    """

    opensearch_client = (
        client
        if client is not None
        else get_opensearch_client()
    )

    index_exists = bool(
        opensearch_client.indices.exists(
            index=LEGAL_DOCUMENTS_INDEX
        )
    )

    if not index_exists:
        response = (
            opensearch_client.indices.create(
                index=LEGAL_DOCUMENTS_INDEX,
                body=LEGAL_DOCUMENTS_INDEX_BODY,
            )
        )

        return {
            "index": LEGAL_DOCUMENTS_INDEX,
            "alias": LEGAL_DOCUMENTS_ALIAS,
            "created": True,
            "alias_created": True,
            "acknowledged": bool(
                response.get(
                    "acknowledged",
                    False,
                )
            ),
        }

    alias_exists = bool(
        opensearch_client.indices.exists_alias(
            name=LEGAL_DOCUMENTS_ALIAS
        )
    )

    if not alias_exists:
        opensearch_client.indices.put_alias(
            index=LEGAL_DOCUMENTS_INDEX,
            name=LEGAL_DOCUMENTS_ALIAS,
        )

    return {
        "index": LEGAL_DOCUMENTS_INDEX,
        "alias": LEGAL_DOCUMENTS_ALIAS,
        "created": False,
        "alias_created": not alias_exists,
        "acknowledged": True,
    }