#!/usr/bin/env bash
#
# Manual comparison test for RERANK_ENABLED, run against a live backend.
#
# It sends the same representative questions twice: once with the
# backend's current RERANK_ENABLED setting, then again after you flip
# it in /etc/le-global-chatbot/le-global-chatbot.env and restart the
# backend container. Responses are saved to disk and the cited source
# order is diffed so you can judge whether reranking actually helps
# before leaving it enabled in production.
#
# Usage:
#   ./verify-rerank.sh <api-base-url> <api-access-key> [output-dir]
#
# Example:
#   ./verify-rerank.sh http://localhost:8000 "$API_ACCESS_KEY"

set -euo pipefail

BASE_URL="${1:?Usage: $0 <api-base-url> <api-access-key> [output-dir]}"
API_KEY="${2:?Usage: $0 <api-base-url> <api-access-key> [output-dir]}"
OUT_DIR="${3:-./rerank-comparison-$(date +%Y%m%d-%H%M%S)}"

mkdir -p "$OUT_DIR"

MONO_PAYLOAD='{"question": "What is the statutory notice period for termination?", "country_codes": ["GB"], "max_sources": 6}'
MULTI_PAYLOAD='{"question": "Compare severance pay rules.", "country_codes": ["GB", "ES", "IT"], "max_sources": 6}'
TOPIC_PAYLOAD='{"question": "What are the rules on restrictive covenants and non-compete clauses?", "legal_topics": ["Restrictive Covenants"], "max_sources": 6}'

run_question() {
  local label="$1"
  local payload="$2"
  local outfile="$3"

  curl -s -X POST "$BASE_URL/api/v1/chat" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $API_KEY" \
    -d "$payload" | python3 -m json.tool > "$outfile"

  echo "  -> $label saved to $outfile"
}

run_round() {
  local suffix="$1"

  run_question "mono-country"  "$MONO_PAYLOAD"  "$OUT_DIR/mono-$suffix.json"
  run_question "multi-country" "$MULTI_PAYLOAD" "$OUT_DIR/multi-$suffix.json"
  run_question "topic-filter"  "$TOPIC_PAYLOAD" "$OUT_DIR/topic-$suffix.json"
}

cited_chunk_ids() {
  python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
print([source['chunk_id'] for source in data.get('sources', [])])
" "$1"
}

echo "== Round 1: current RERANK_ENABLED setting =="
run_round "before"

echo
echo "Now flip RERANK_ENABLED in /etc/le-global-chatbot/le-global-chatbot.env and restart the backend:"
echo "  docker compose --env-file /etc/le-global-chatbot/le-global-chatbot.env -f /opt/le-global-chatbot/infra/compose.yml up -d backend"
read -r -p "Press Enter once the backend has restarted with the new setting..." _

echo
echo "== Round 2: other RERANK_ENABLED setting =="
run_round "after"

echo
echo "== Cited source order (chunk_id) diff =="
for name in mono multi topic; do
  echo "--- $name ---"
  diff \
    <(cited_chunk_ids "$OUT_DIR/$name-before.json") \
    <(cited_chunk_ids "$OUT_DIR/$name-after.json") \
    && echo "  (identical order)" || true
done

echo
echo "Full responses are in $OUT_DIR — compare answer wording and source relevance manually before deciding."
