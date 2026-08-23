#!/usr/bin/env bash
#
# Run the LIVE release/rollback compatibility smoke gate
# (scripts/release_compatibility_smoke.py) against one candidate
# backend image, using a real, isolated OpenSearch + Redis + backend
# stack - never production's own network, containers, or dependencies.
#
# This is the automated, repository-owned version of the manual
# recipe documented in backend/integration_tests/README.md, extended
# with the bootstrap and smoke-check steps this gate needs. See
# docs/RELEASE_COMPATIBILITY.md for the full write-up, including why
# each step exists and what it proved against candidate-ed292d7.
#
# Usage:
#   scripts/run_release_compatibility_smoke.sh <image-tag> <label> [host-port]
#
# Example:
#   scripts/run_release_compatibility_smoke.sh \
#       le-global-backend:candidate-v0.4.12-abc1234 v0.4.12-abc1234 18001
#
# Prerequisites:
#   - Docker.
#   - A read-only snapshot already prepared at
#     $SNAPSHOT_DIR/documents/{source,processed}, e.g. via:
#       sudo python3 scripts/prepare_release_compatibility_snapshot.py \
#         --production-source-dir /var/lib/le-global-chatbot/documents/source \
#         --manifest scripts/fixtures/release_compatibility_manifest.json \
#         --output-dir "$SNAPSHOT_DIR/documents/source"
#       sudo mkdir -p "$SNAPSHOT_DIR/documents/processed"
#       sudo chown -R 10001:10001 "$SNAPSHOT_DIR"
#
# This script NEVER touches production: it creates its own isolated
# Docker network and its own throwaway OpenSearch/Redis, and mounts
# the snapshot directory read-only into the candidate container.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <image-tag> <label> [host-port]" >&2
  exit 2
fi

IMAGE_TAG="$1"
LABEL="$2"
HOST_PORT="${3:-18000}"

SNAPSHOT_DIR="${RELEASE_SMOKE_SNAPSHOT_DIR:-/var/tmp/le-global-smoke-fixture}"
MANIFEST="${RELEASE_SMOKE_MANIFEST:-$(dirname "$0")/fixtures/release_compatibility_manifest.json}"
OPENSEARCH_TEST_PASSWORD="${RELEASE_SMOKE_OPENSEARCH_PASSWORD:-TestOnly-Smoke-Gate-$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
API_KEY="${RELEASE_SMOKE_API_KEY:-smoke-test-api-key}"
ADMIN_KEY="${RELEASE_SMOKE_ADMIN_KEY:-smoke-test-admin-key}"
NET="admin-smoke-${LABEL}"

if [ ! -d "${SNAPSHOT_DIR}/documents/source" ]; then
  echo "[ERROR] no snapshot found at ${SNAPSHOT_DIR}/documents/source" >&2
  echo "        prepare one first with scripts/prepare_release_compatibility_snapshot.py" >&2
  exit 2
fi

cleanup() {
  docker rm -f "admin-smoke-${LABEL}-backend" "admin-smoke-${LABEL}-opensearch" "admin-smoke-${LABEL}-redis" >/dev/null 2>&1 || true
  docker network rm "${NET}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "${NET}" >/dev/null

docker run -d --name "admin-smoke-${LABEL}-opensearch" --network "${NET}" \
  -e discovery.type=single-node \
  -e OPENSEARCH_INITIAL_ADMIN_PASSWORD="${OPENSEARCH_TEST_PASSWORD}" \
  opensearchproject/opensearch:3.7.0 >/dev/null

docker run -d --name "admin-smoke-${LABEL}-redis" --network "${NET}" \
  redis:7-alpine >/dev/null

echo "[1/4] waiting for isolated OpenSearch..." >&2
for _ in $(seq 1 60); do
  if docker run --rm --network "${NET}" curlimages/curl:latest -sk -o /dev/null -w '%{http_code}' \
      -u "admin:${OPENSEARCH_TEST_PASSWORD}" "https://admin-smoke-${LABEL}-opensearch:9200/" 2>/dev/null | grep -q '^200$'; then
    break
  fi
  sleep 2
done

docker run -d --name "admin-smoke-${LABEL}-backend" --network "${NET}" \
  -p "127.0.0.1:${HOST_PORT}:8000" \
  -e OPENAI_API_KEY=sk-test-not-a-real-key \
  -e API_ACCESS_KEY="${API_KEY}" \
  -e ADMIN_API_KEY="${ADMIN_KEY}" \
  -e LOG_LEVEL=INFO \
  -e APP_ENV=test \
  -e OPENSEARCH_URL="https://admin-smoke-${LABEL}-opensearch:9200" \
  -e OPENSEARCH_USERNAME=admin \
  -e OPENSEARCH_PASSWORD="${OPENSEARCH_TEST_PASSWORD}" \
  -e OPENSEARCH_VERIFY_CERTS=false \
  -e REDIS_URL="redis://admin-smoke-${LABEL}-redis:6379/0" \
  -e DOCUMENT_SOURCE_DIR=/data/documents/source \
  -e DOCUMENT_PROCESSED_DIR=/data/documents/processed \
  -e DOCUMENT_UPLOAD_MAX_BYTES=26214400 \
  -v "${SNAPSHOT_DIR}/documents/source:/data/documents/source:ro" \
  -v "${SNAPSHOT_DIR}/documents/processed:/data/documents/processed:ro" \
  "${IMAGE_TAG}" >/dev/null

echo "[2/4] waiting for candidate backend..." >&2
for _ in $(seq 1 60); do
  if docker exec "admin-smoke-${LABEL}-backend" python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "[3/4] bootstrapping isolated OpenSearch metadata from the snapshot (this candidate's own scripts/index_docx_corpus.py, against ITS OWN isolated OpenSearch only)..." >&2
docker exec -e PYTHONPATH=/app -w /app "admin-smoke-${LABEL}-backend" \
  python scripts/index_docx_corpus.py --source-dir /data/documents/source

echo "[4/4] running the smoke gate..." >&2
python3 "$(dirname "$0")/release_compatibility_smoke.py" \
  --base-url "http://127.0.0.1:${HOST_PORT}" \
  --manifest "${MANIFEST}" \
  --api-key "${API_KEY}" \
  --admin-key "${ADMIN_KEY}"
