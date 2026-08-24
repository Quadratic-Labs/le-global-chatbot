# Real OpenSearch 3.7 integration harness

Mission "ORDER 3", section 29. Reproducible instructions for standing
up an isolated stack (real OpenSearch 3.7.0, real Redis, the real
backend) and exercising the admin document lifecycle end-to-end,
without ever touching production and without any unit test
automatically depending on Docker being available.

Nothing under `tests/` imports anything from this directory, and
nothing here runs as part of `python -m unittest discover`. This is
deliberate - the ordinary unit test suite must keep passing on a
machine with no Docker at all.

## Prerequisites

- Docker.
- The backend's own runtime image, already built and tagged locally
  (any tag works; examples below use
  `le-global-backend:candidate-v0.4.9-0a8bc46`, the tag used while
  building this harness - substitute your own).

## 1. Create an isolated network

```sh
docker network create admin-e2e
```

Never reuse production's own network name/containers. Nothing in this
harness should be able to reach a production service.

## 2. Start real OpenSearch 3.7.0 and Redis

```sh
docker run -d --name admin-e2e-opensearch --network admin-e2e \
  -e discovery.type=single-node \
  -e OPENSEARCH_INITIAL_ADMIN_PASSWORD='TestOnly-E2E-Pass-9f3k2L!' \
  opensearchproject/opensearch:3.7.0

docker run -d --name admin-e2e-redis --network admin-e2e redis:7-alpine
```

`plugins.security.disabled=true` does NOT work on this image version -
the entrypoint still refuses to start without
`OPENSEARCH_INITIAL_ADMIN_PASSWORD` set. Use a real password (never a
production one) and `OPENSEARCH_VERIFY_CERTS=false` on the backend side
below instead of disabling security.

## 3. Start the backend against the isolated stack

```sh
docker run -d --name admin-e2e-backend --network admin-e2e \
  -e OPENAI_API_KEY=sk-test-not-a-real-key \
  -e API_ACCESS_KEY=test-api-access-key \
  -e ADMIN_API_KEY=test-admin-key \
  -e LOG_LEVEL=INFO \
  -e APP_ENV=test \
  -e OPENSEARCH_URL=https://admin-e2e-opensearch:9200 \
  -e OPENSEARCH_USERNAME=admin \
  -e OPENSEARCH_PASSWORD='TestOnly-E2E-Pass-9f3k2L!' \
  -e OPENSEARCH_VERIFY_CERTS=false \
  -e REDIS_URL=redis://admin-e2e-redis:6379/0 \
  -e DOCUMENT_SOURCE_DIR=/data/documents/source \
  -e DOCUMENT_PROCESSED_DIR=/data/documents/processed \
  -e DOCUMENT_UPLOAD_MAX_BYTES=26214400 \
  le-global-backend:candidate-v0.4.9-0a8bc46
```

To test against code changes that have not been rebuilt into an image
yet, bind-mount the changed files read-only over the image's own copy
instead of rebuilding every iteration, e.g.:

```sh
  -v "$(pwd)/backend/app/services/country_lock.py:/app/app/services/country_lock.py:ro"
```

## 4. Confirm health

```sh
docker run --rm --network admin-e2e curlimages/curl:latest -s \
  http://admin-e2e-backend:8000/health
# {"status":"ok","service":"le-global-backend","dependencies":{"opensearch":"ok","redis":"ok"}}
```

## 5. Exercise the admin API

Every request goes through a throwaway `curlimages/curl` container on
the same network - never install curl on the host, never touch
production credentials:

```sh
BASE=http://admin-e2e-backend:8000/api/v1/admin/documents
AUTH=(-H "X-API-Key: test-api-access-key" -H "X-Admin-Key: test-admin-key")

# Upload
docker run --rm --network admin-e2e -v /path/to/corpus:/corpus:ro \
  curlimages/curl:latest -s -X POST "${AUTH[@]}" \
  -F "file=@/corpus/Chile.docx;type=application/vnd.openxmlformats-officedocument.wordprocessingml.document" \
  -F "replace_existing=0" -F "confirm_warnings=false" "$BASE"

# Catalog / stats / download / reindex / delete
docker run --rm --network admin-e2e curlimages/curl:latest -s "${AUTH[@]}" "$BASE"
docker run --rm --network admin-e2e curlimages/curl:latest -s "${AUTH[@]}" "$BASE/stats"
docker run --rm --network admin-e2e curlimages/curl:latest -s "${AUTH[@]}" "$BASE/<document_id>/download" -o out.docx
docker run --rm --network admin-e2e curlimages/curl:latest -s -X POST "${AUTH[@]}" "$BASE/<document_id>/reindex"
docker run --rm --network admin-e2e curlimages/curl:latest -s -X DELETE "${AUTH[@]}" "$BASE/<document_id>"
```

Query OpenSearch directly (read-only checks, e.g. to cross-verify
chunk counts against the catalog's own report):

```sh
docker run --rm --network admin-e2e curlimages/curl:latest -sk \
  -u admin:'TestOnly-E2E-Pass-9f3k2L!' \
  "https://admin-e2e-opensearch:9200/legal-documents-v1/_count"
```

## 6. Reset to a clean slate between scenarios

```sh
docker run --rm --network admin-e2e curlimages/curl:latest -sk \
  -u admin:'TestOnly-E2E-Pass-9f3k2L!' -X DELETE \
  "https://admin-e2e-opensearch:9200/legal-documents-v1"

docker exec -u root admin-e2e-backend sh -c \
  "rm -rf /data/documents/source/* /data/documents/.admin-locks"
```

## 7. Tear down

```sh
docker rm -f admin-e2e-backend admin-e2e-opensearch admin-e2e-redis
docker network rm admin-e2e
```

## Why real OpenSearch, not just the unit-test fakes

The unit test suite's FakeOpenSearch/FakeOpenSearchClient doubles
(see `tests/test_admin_documents_router_integration.py`,
`tests/test_admin_document_lifecycle.py`, `tests/test_admin_documents.py`)
are deliberately shaped to match real OpenSearch 3.7 response
structures for every field the production code actually reads
(verified directly against this harness while building it - e.g. the
real `_search` response's `hits.total.value`/`hits.hits[]._source`
shape, and the real `_delete_by_query` response's `deleted` count).
They are fast and sufficient for logic and failure-injection testing.
This harness exists for the things a fake cannot tell you: genuine
network behavior, real OpenSearch response latency/behavior at scale,
and end-to-end multipart upload through the real FastAPI stack.

## Canonical contact-table DOCX mutation matrix

`docx_contact_mutation_matrix.py` drives the exact same isolated stack
above (steps 1-3) through a real-HTTP differential matrix over the
Admin Contact API: rebuild-only, add (with/without photo), update text,
replace photo, delete photo, a second consecutive update, and the
add-photo-then-delete sequence, each starting from a clean baseline and
each validated for ZIP/XML/relationship/docPr/sectPr integrity plus a
python-docx reopen. Written 2026-08-24 investigating a report that a
real Admin Contact mutation produced a DOCX Microsoft Word refused to
open - the exact real document and operation sequence were identified
from live WordPress/backend access logs (never assumed), reproduced
here, and validated clean across this matrix, an 8-cycle repeated
add/photo/delete stress run, and large PNG/JPEG photo variants; no
defect was found. Kept as permanent regression coverage per that
investigation's own conclusion (documents currently-verified-correct
behavior, not a fix for a confirmed defect).

Run it against step 3's backend, using a read-write snapshot (prepare
one with `scripts/prepare_release_compatibility_snapshot.py`, then
`chmod -R a+rwX` it - this script mutates it repeatedly by design) and
clean backups of the DOCX/ContactState/photo to reset to between
stages:

```sh
docker cp docx_contact_mutation_matrix.py admin-e2e-backend:/tmp/matrix.py
docker exec admin-e2e-backend python /tmp/matrix.py \
  --base-url http://localhost:8000 \
  --api-key test-api-access-key --admin-key test-admin-key \
  --document-id <doc_id> --original-contact-id <hex> \
  --original-member-firm "..." --original-contact-person "..." \
  --original-email "..." --original-phone "..." \
  --original-address "..." --original-website "..." \
  --snapshot-source-dir /data/documents/source \
  --docx-filename <COUNTRY>.docx \
  --original-docx-backup /tmp/clean.docx \
  --original-state-backup /tmp/clean-state.json \
  --original-photo-name <contact_id>--<sha256>.jpg
```

Cross-check any output separately with LibreOffice headless
(`libreoffice --headless --convert-to pdf ...`) - not scripted here,
since it requires LibreOffice on the runner rather than inside the
backend image. Neither this script nor LibreOffice succeeding is proof
Microsoft Word itself accepts the file; treat a clean matrix run as
strong evidence, not a substitute for opening a real canary in Word.
