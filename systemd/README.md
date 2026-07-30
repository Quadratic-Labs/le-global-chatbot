# systemd configuration

This directory contains the operating-system configuration required to run the
L&E Global Legal Chatbot Docker Compose stack automatically.

## Files

- `le-global-chatbot.service` — starts and stops the complete Docker Compose stack.
- `99-le-global-chatbot.conf` — configures the Linux kernel settings required by
  OpenSearch and Redis.
- `verify-rerank.sh` — manual A/B comparison script for `RERANK_ENABLED`; run it
  against a live backend before leaving reranking enabled in production.

## Managed services

The systemd unit manages the following containers:

- FastAPI backend
- OpenSearch
- OpenSearch Dashboards
- Redis

All services are currently managed through a single Docker Compose project located at:

```text
/opt/le-global-chatbot/infra
```

## Post-deployment steps

### Document directory ownership (required before the first deploy of a backend image)

The backend container runs as a non-root user (uid/gid `10001`). The document
directories are bind-mounted from the host (see `infra/compose.yml`), so Docker
does not change their ownership automatically. Before starting the stack for
the first time, or after recreating these directories, run on the host:

```bash
sudo mkdir -p /var/lib/le-global-chatbot/documents/{source,processed}
sudo chown -R 10001:10001 /var/lib/le-global-chatbot/documents
```

Skipping this step causes document upload and reindexing to fail with
permission errors.

### OpenSearch Dashboards credentials (required after changing the default password)

`OPENSEARCH_DASHBOARDS_USERNAME`/`OPENSEARCH_DASHBOARDS_PASSWORD` in
`infra/compose.yml` only configure the Dashboards container — they do not
change the `kibanaserver` account's password inside OpenSearch itself, which
defaults to `kibanaserver` in the security plugin's demo configuration. After
the `opensearch` container is healthy (first deployment, or any time this
password is rotated), align the two by calling the OpenSearch security API
with the admin credentials:

```bash
curl -sk -u "admin:${OPENSEARCH_INITIAL_ADMIN_PASSWORD}" \
  -X PATCH "https://localhost:9200/_plugins/_security/api/internalusers/kibanaserver" \
  -H "Content-Type: application/json" \
  -d '[{"op":"replace","path":"/password","value":"'"${OPENSEARCH_DASHBOARDS_PASSWORD}"'"}]'
```

Skipping this step after changing `OPENSEARCH_DASHBOARDS_PASSWORD` causes
Dashboards to fail authentication and not start.

## RAG / OpenAI tuning variables

Set these in `/etc/le-global-chatbot/le-global-chatbot.env`. All have safe
defaults (shown below) and are optional.

- `OPENAI_ANSWER_REASONING_EFFORT` (default `low`), `OPENAI_ANSWER_MAX_OUTPUT_TOKENS`
  (default `2000`) — budget for the final grounded-answer generation call.
- `OPENAI_RERANK_REASONING_EFFORT` (default `low`), `OPENAI_RERANK_MAX_OUTPUT_TOKENS`
  (default `500`) — separate, smaller budget for the reranking call, since it
  only needs to output a short ordering, not prose. `max_output_tokens`
  includes both the visible output and the model's internal reasoning tokens.
- `RAG_MAX_CONTEXT_CHARACTERS` (default `16000`) — total character budget
  shared across all retrieved sources sent to the model.
- `RAG_MAX_SOURCE_CHARACTERS` (default `4000`) — additional per-source cap,
  whichever of the two budgets is smaller applies. Every retrieved source
  stays represented (truncated, never dropped), so a comparison across
  several countries never silently loses a country because an earlier
  source consumed the whole budget.
- `RERANK_ENABLED` (default `false`), `RERANK_POOL_MULTIPLIER` (default `3`)
  — see `verify-rerank.sh` before enabling this in production.
