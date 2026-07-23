#!/usr/bin/env bash
# Seed the fastapi-notes example: install its memory notes into the running
# SOKKAN instance and create three board cards to ▶ spawn from.
# Run from your SOKKAN checkout root:  ./examples/fastapi-notes/seed.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../.."   # repo root (where docker-compose.yml and .env live)

[ -f .env ] && set -a && . ./.env && set +a
PORT="${SOKKAN_PORT:-3009}"
BASE="http://localhost:$PORT"
MEM_DIR=/data/claude/projects/-workspace/memory

echo "→ installing memory notes into the api container"
docker compose exec -T api mkdir -p "$MEM_DIR"
for f in "$HERE"/memory/*.md; do
  docker compose cp "$f" "api:$MEM_DIR/$(basename "$f")"
done
echo "  $(ls "$HERE"/memory/*.md | wc -l) notes copied — indexed within ~2 minutes"

JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT
if [ -n "${SOKKAN_LOCAL_TOKEN:-}" ]; then
  curl -sf -c "$JAR" -X POST "$BASE/api/auth/local" \
    -H 'Content-Type: application/json' \
    -d "{\"token\": \"$SOKKAN_LOCAL_TOKEN\"}" >/dev/null
fi

card() {  # title, description, tag
  curl -sf -b "$JAR" -X POST "$BASE/api/board/card" \
    -H 'Content-Type: application/json' \
    -d "$(python3 - "$1" "$2" "$3" <<'PY'
import json, sys
print(json.dumps({"title": sys.argv[1], "description": sys.argv[2],
                  "tag": sys.argv[3], "bucket": "Backlog", "priority": 2}))
PY
)" >/dev/null && echo "  card: $1"
}

echo "→ creating board cards"
card "Add DELETE /notes/{id}" \
  "Add a DELETE /notes/{id} endpoint to the fastapi-notes API. Follow the project's conventions for auth, error responses and tests." \
  "backend"
card "Add a /stats endpoint" \
  "Add GET /stats returning the note count and the timestamp of the latest note. Follow the project's endpoint conventions." \
  "backend"
card "Add pagination to GET /notes" \
  "GET /notes currently returns every note. Add limit/offset query params with sane defaults, plus tests." \
  "backend"

echo "done — open $BASE, go to Board, and ▶ spawn a card."
