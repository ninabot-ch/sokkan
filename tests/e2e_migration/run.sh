#!/bin/sh
# End-to-end test of the SOKKAN 2.x -> 3.0 memory migration, on real containers.
#
#   IMG23=<SOKKAN 2.3 api image> IMG30=<3.0 api image> tests/e2e_migration/run.sh
#
# Build the images first, e.g.:
#   git archive v2.3.0 | tar x -C /tmp/v23 && docker build -t sokkan-e2e-mig-api:2.3 -f /tmp/v23/docker/api.Dockerfile /tmp/v23
#   docker build -t sokkan-e2e-mig-api:3.0 -f docker/api.Dockerfile .
#
# What it does (every container on the default bridge network, memory-capped; nothing is
# published on the host):
#   1. a fictional 2.x memory (gen_corpus.py: 52 files, dates, links, defects) is put in a
#      data volume; the 2.3 api indexes it (memory.db, MiniLM) -> bench "before"
#   2. the 2.3 api is stopped; Postgres + the llama.cpp embedding server (waiting for its
#      model) + the 3.0 api start on the SAME volume: the migration runs by itself
#   3. a probe searches every second through the whole migration (who serves, which step)
#   4. the model is set up (Gemma terms answered by $ACCEPT, default 1), the import runs,
#      the checks pass, the store takes over -> bench "after", notes and dates compared
#   5. rollback: the 2.3 api on the same volume still serves memory.db as before
# Results in $WORK (default: a new temp dir). KEEP=1 keeps the containers and volumes.
set -eu

P="${P:-sokkan-e2e-mig}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="${WORK:-$(mktemp -d)}"
IMG23="${IMG23:-$P-api:2.3}"
IMG30="${IMG30:-$P-api:3.0}"
LLAMA="${LLAMA:-ghcr.io/ggml-org/llama.cpp:server-b11347}"
ACCEPT="${ACCEPT:-1}"
MEMDIR=/data/claude/projects/-workspace/memory
mkdir -p "$WORK"
say() { printf '\n== %s\n' "$*"; }
cleanup() {
  [ "${KEEP:-0}" = 1 ] && return 0
  docker rm -f "$P-api23" "$P-api30" "$P-db" "$P-embed" "$P-watch" >/dev/null 2>&1 || true
  docker volume rm "$P-data" "$P-models" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup
probe() {  # probe <container> <args…>
  c="$1"; shift
  docker exec -i -u sokkan "$c" python /tmp/probe.py "$@" 2>>"$WORK/probe.err" | tail -n 1
}
wait_for() {  # wait_for <seconds> <description> <shell test>
  n=0
  until sh -c "$3" >/dev/null 2>&1; do
    n=$((n + 2)); [ "$n" -ge "$1" ] && { echo "timeout: $2" >&2; return 1; }; sleep 2
  done
}

say "1. fictional 2.x memory in the data volume"
python3 "$HERE/gen_corpus.py" "$WORK/corpus" "$WORK/questions.json"
docker volume create "$P-data" >/dev/null
docker run --rm --network bridge -v "$P-data:/data" -v "$WORK/corpus:/src:ro" python:3.12-slim \
  sh -c "mkdir -p $MEMDIR && cp -p /src/*.md $MEMDIR/ && chown -R 1000:1000 /data"

say "2. SOKKAN 2.3 indexes it"
docker run -d --name "$P-api23" --network bridge --memory 2g -v "$P-data:/data" "$IMG23" >/dev/null
docker cp "$HERE/probe.py" "$P-api23:/tmp/probe.py"
wait_for 300 "2.3 index" "docker exec -u sokkan $P-api23 python -c \"import sqlite3; c=sqlite3.connect('file:/data/memory.db?mode=ro', uri=True); assert c.execute('select count(*) from notes').fetchone()[0] >= 52\""
probe "$P-api23" notes > "$WORK/notes-2x.json"
probe "$P-api23" bench < "$WORK/questions.json" > "$WORK/bench-2x.json"
cat "$WORK/bench-2x.json"
docker rm -f "$P-api23" >/dev/null
docker run --rm -v "$P-data:/data" python:3.12-slim sha256sum /data/memory.db > "$WORK/memorydb.sha256"

say "3. 3.0 stack on the same volume: Postgres, embedding server (no model yet), api"
docker run -d --name "$P-db" --network bridge --memory 1g --shm-size 640m \
  -e POSTGRES_USER=sokkan -e POSTGRES_PASSWORD=e2e -e POSTGRES_DB=sokkan \
  pgvector/pgvector:pg16 -c shared_buffers=256MB -c maintenance_work_mem=512MB >/dev/null
docker volume create "$P-models" >/dev/null
docker run -d --name "$P-embed" --network bridge --memory 2g -v "$P-models:/models:ro" \
  -v "$ROOT/docker/embed/run.sh:/opt/corthexis/run.sh:ro" --entrypoint /bin/sh \
  -e CORTHEXIS_MEMORY_PROFILE=leger -e CORTHEXIS_EMBED_DEVICE=cpu "$LLAMA" \
  /opt/corthexis/run.sh embed >/dev/null
wait_for 60 "postgres" "docker exec $P-db pg_isready -U sokkan -d sokkan"
docker run -d --name "$P-api30" --network bridge --memory 2g \
  --link "$P-db:db" --link "$P-embed:corthexis-embed" \
  -v "$P-data:/data" -v "$P-models:/models:ro" \
  -e CORTHEXIS_DATABASE_URL=postgresql://sokkan:e2e@db:5432/sokkan \
  -e CORTHEXIS_MEMORY_BACKEND=auto -e CORTHEXIS_MODELS_DIR=/models \
  -e SOKKAN_MIGRATION_RETRY_S=10 -e SOKKAN_REINDEX_S=20 "$IMG30" >/dev/null
docker cp "$HERE/probe.py" "$P-api30:/tmp/probe.py"
# continuity probe: one search per second for the whole migration
docker exec -u sokkan "$P-api30" python /tmp/probe.py watch 900 "who delivers our flour and when?" \
  > "$WORK/watch.jsonl" 2>/dev/null &
WATCH=$!
wait_for 120 "migration waiting for the model" \
  "docker exec $P-api30 grep -q '\"step\": \"index\"' /data/memory-migration/state.json"
sleep 15
probe "$P-api30" search "who delivers our flour and when?" > "$WORK/search-during.json"
echo "search while the model is missing: $(cat "$WORK/search-during.json")"
probe "$P-api30" recall "who delivers our flour and when?" > "$WORK/recall-during.json"
echo "recall hook while the model is missing: $(cut -c1-200 "$WORK/recall-during.json")"

say "4. model setup (Gemma terms: ACCEPT=$ACCEPT), import, checks, switch"
docker run --rm --network bridge -v "$P-models:/models" \
  -v "$ROOT/memory/core/models.py:/opt/corthexis/models.py:ro" \
  -e CORTHEXIS_MODELS_DIR=/models -e CORTHEXIS_MEMORY_PROFILE=leger \
  -e CORTHEXIS_ACCEPT_GEMMA_TERMS="$ACCEPT" -e CORTHEXIS_OWNER=e2e-test \
  python:3.12-slim python /opt/corthexis/models.py setup | tail -n 3
wait_for 600 "migration done" \
  "docker exec $P-api30 grep -Eq '\"status\": \"(done|blocked|failed)\"' /data/memory-migration/state.json"
sleep 5
kill "$WATCH" 2>/dev/null || true
docker exec "$P-api30" cat /data/memory-migration/state.json > "$WORK/state.json"
docker exec "$P-api30" python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8097/api/memory/migration').read().decode())" > "$WORK/api-migration.json"
docker exec "$P-api30" ls -la /data/memory-migration > "$WORK/migration-dir.txt"
for f in manifest.json normalize-applied.json normalize-plan.txt; do
  docker exec "$P-api30" cat "/data/memory-migration/$f" > "$WORK/$f"
done
probe "$P-api30" recall "who delivers our flour and when?" > "$WORK/recall-after.json"
echo "recall hook after the switch: $(cut -c1-200 "$WORK/recall-after.json")"
# the store indexer (IndexRunner) took over: a note written now is searchable in seconds
docker exec -u sokkan "$P-api30" sh -c "printf -- '---\nname: e2e-after-switch\ndescription: Kombucha fermentation tank cleaned every Thursday\nmetadata:\n  type: project\n---\n\nThe kombucha tank is cleaned every Thursday.\n' > $MEMDIR/e2e_after_switch.md"
if wait_for 90 "new note indexed by the IndexRunner" \
  "docker exec -u sokkan $P-api30 python /tmp/probe.py search 'when is the kombucha tank cleaned?' | grep -q '\"e2e-after-switch\"'"; then
  echo '{"indexed": true}' > "$WORK/indexrunner.json"
else
  echo '{"indexed": false}' > "$WORK/indexrunner.json"
fi
echo "IndexRunner after the switch: $(cat "$WORK/indexrunner.json")"
docker exec -u sokkan "$P-api30" rm -f "$MEMDIR/e2e_after_switch.md"
probe "$P-api30" notes > "$WORK/notes-3x.json"
probe "$P-api30" bench < "$WORK/questions.json" > "$WORK/bench-3x.json"
cat "$WORK/bench-3x.json"
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' "$P-api30" "$P-db" "$P-embed" > "$WORK/ram.txt"
cat "$WORK/ram.txt"

say "5. rollback: the 2.3 image on the same volume"
docker rm -f "$P-api30" >/dev/null
docker run --rm -v "$P-data:/data" python:3.12-slim sha256sum /data/memory.db > "$WORK/memorydb.after.sha256"
docker run -d --name "$P-api23" --network bridge --memory 2g -v "$P-data:/data" "$IMG23" >/dev/null
docker cp "$HERE/probe.py" "$P-api23:/tmp/probe.py"
sleep 40
probe "$P-api23" bench < "$WORK/questions.json" > "$WORK/bench-rollback.json"
cat "$WORK/bench-rollback.json"

say "report"
python3 "$HERE/report.py" "$WORK"
echo "results: $WORK"
