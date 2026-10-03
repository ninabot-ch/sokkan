#!/bin/sh
# Runs INSIDE the test machine (a Docker-in-Docker container started by run.sh): what a
# self-hosted user does, step by step, then the checks. Output in /work.
#
#   SRC=2.3.0 MODE=installer|manual ACCEPT=1|0|'' sh /harness/e2e_upgrade/inner.sh
#
# /dists holds sokkan-<version>.tar.gz (git archive of the release, like sokkan.ch/dist),
# install-<version>.sh (the installer of that time) and install-3.sh (the 3.0 one).
set -u
W=/work
H=/harness
U=/home/user
BASE=http://127.0.0.1:8088
MEMDIR=/data/claude/projects/-workspace/memory
Q="who delivers our flour and when?"
mkdir -p "$W" "$U" /srv/dist
phase() { echo "$(date +%s.%N) $1" >> "$W/phases.log"; printf '\n== [%s] %s\n' "$(date +%T)" "$1"; }
fail() { echo "FATAL: $*" | tee -a "$W/fatal.txt"; phase fatal; exit 1; }
dc() { (cd "$U/sokkan" && docker compose "$@"); }
api() { dc ps -q api | head -n 1; }
set_dist() { cp "/dists/sokkan-$1.tar.gz" /srv/dist/sokkan-latest.tar.gz; echo "$1" > /srv/dist/VERSION; }
wait_for() {  # wait_for <seconds> <what> <shell test>
  n=0
  until sh -c "$3" >/dev/null 2>&1; do
    n=$((n + 3)); [ "$n" -ge "$1" ] && { echo "timeout ($1 s): $2"; return 1; }; sleep 3
  done
  echo "ok after ~${n}s: $2"
}
healthy() { wait_for "$1" "api health on :3009" "curl -sf -m 5 http://127.0.0.1:3009/api/health"; }
probe() {  # probe <args…> — e2e_migration/probe.py in the api container
  c="$(api)"; docker cp "$H/e2e_migration/probe.py" "$c:/tmp/probe.py" >/dev/null 2>&1
  docker exec -i -u "$PUSER" "$c" python /tmp/probe.py "$@" 2>>"$W/probe.err" | tail -n 1
}
token() { sed -n 's/^SOKKAN_LOCAL_TOKEN=//p' "$U/sokkan/.env" | tail -n 1; }

cp /dists/sokkan-*.tar.gz /srv/dist/          # every release, as on sokkan.ch/dist
(cd /srv && python3 -m http.server 8088 >/dev/null 2>&1 &)
sleep 1
docker compose version > "$W/compose-version.txt"; docker version --format '{{.Server.Version}}' > "$W/engine-version.txt"
cat "$W/compose-version.txt" "$W/engine-version.txt"

# ---------------------------------------------------------------- 1. the source version
phase "install $SRC"
set_dist "$SRC"
cd "$U" && SOKKAN_BASE="$BASE" sh "/dists/install-$SRC.sh" > "$W/install-src.log" 2>&1 \
  || { cat "$W/install-src.log"; fail "installer of $SRC"; }
tail -n 4 "$W/install-src.log"
# quiet the daily update check (it calls sokkan.ch); a real user has it on
echo "SOKKAN_UPDATE_CHECK=0" >> "$U/sokkan/.env"
phase "build+start $SRC"
dc up -d --build > "$W/up-src.log" 2>&1 || { tail -n 30 "$W/up-src.log"; fail "up $SRC"; }
healthy 600 || { dc logs --tail 50 api; fail "$SRC does not start"; }
PUSER=sokkan
docker exec "$(api)" id sokkan >/dev/null 2>&1 || PUSER=root      # 0.1.0 runs as root

phase "seed data"
python3 "$H/e2e_migration/gen_corpus.py" "$W/corpus" "$W/questions.json" > /dev/null
VOL="$(docker volume ls -q | grep '_sokkan-data$' | head -n 1)"
OWN=1000:1000; [ "$PUSER" = root ] && OWN=0:0
docker run --rm -v "$VOL:/data" -v "$W/corpus:/src:ro" python:3.12-slim \
  sh -c "mkdir -p $MEMDIR && cp -p /src/*.md $MEMDIR/ && chown -R $OWN /data/claude"
docker cp "$H/e2e_upgrade/seed.py" "$(api):/tmp/seed.py"
docker exec -u "$PUSER" "$(api)" python /tmp/seed.py > "$W/seed.json" || fail "seed"
dc restart api >/dev/null 2>&1          # index at boot (every version)
healthy 300 || fail "restart after seed"
wait_for 600 "2.x index has the 52 notes" \
  "docker exec -u $PUSER $(api) python -c \"import sqlite3; c=sqlite3.connect('file:/data/memory.db?mode=ro', uri=True); assert c.execute('select count(*) from notes').fetchone()[0] >= 52\"" \
  || fail "the source version did not index the notes"
probe notes > "$W/notes-before.json"
probe bench < "$W/questions.json" > "$W/bench-before.json"
python3 "$H/e2e_upgrade/client.py" snapshot "$(token)" "$W/snap-before.json" > /dev/null
docker run --rm -v "$VOL:/data" python:3.12-slim sha256sum /data/memory.db > "$W/memorydb.before.sha256"
cat "$W/bench-before.json"

# ---------------------------------------------------------------- 2. the update
phase "upgrade ($MODE)"
python3 "$H/e2e_upgrade/client.py" watch "$(token)" 3000 "$Q" > "$W/watch-http.jsonl" 2>/dev/null &
WATCH_HTTP=$!
set_dist 3
T0=$(date +%s)
if [ "$MODE" = installer ]; then
  cd "$U" && SOKKAN_BASE="$BASE" SOKKAN_ACCEPT_GEMMA_TERMS="${ACCEPT:-}" sh /dists/install-3.sh \
    > "$W/upgrade.log" 2>&1 || { tail -n 40 "$W/upgrade.log"; fail "installer upgrade"; }
else
  # docs/UPGRADE.md, "Equivalent manual steps" — memory-setup.sh is NOT run
  ( cd "$U/sokkan" && curl -fsSL "$BASE/dist/sokkan-latest.tar.gz" | tar xz --strip-components=1 \
    && docker compose up -d --build ) > "$W/upgrade.log" 2>&1 \
    || { tail -n 40 "$W/upgrade.log"; fail "manual upgrade"; }
fi
tail -n 8 "$W/upgrade.log"
cp "$U/sokkan/.env" "$W/env-after-upgrade.txt"
healthy 600 || { dc logs --tail 80 api; fail "3.0 does not start"; }
echo "$(( $(date +%s) - T0 ))" > "$W/upgrade-seconds.txt"
PUSER=sokkan
phase "migration"
c="$(api)"; docker cp "$H/e2e_migration/probe.py" "$c:/tmp/probe.py"
docker exec -u sokkan "$c" python /tmp/probe.py watch 1500 "$Q" > "$W/watch.jsonl" 2>/dev/null &
WATCH=$!
probe recall "$Q" > "$W/recall-during.json"
wait_for 1500 "migration finished" \
  "docker exec $c grep -Eq '\"status\": \"(done|blocked|failed)\"' /data/memory-migration/state.json" \
  || true
sleep 5
kill "$WATCH" 2>/dev/null
phase "checks after"
docker exec "$c" cat /data/memory-migration/state.json > "$W/state.json" 2>/dev/null
for f in manifest.json normalize-applied.json normalize-plan.txt; do
  docker exec "$c" cat "/data/memory-migration/$f" > "$W/$f" 2>/dev/null
done
probe recall "$Q" > "$W/recall-after.json"
probe notes > "$W/notes-after.json"
probe bench < "$W/questions.json" > "$W/bench-after.json"
cat "$W/bench-after.json"
docker exec -u sokkan "$c" sh -c "printf -- '---\nname: upg-after-switch\ndescription: Kombucha fermentation tank cleaned every Thursday\nmetadata:\n  type: project\n---\n\nThe kombucha tank is cleaned every Thursday.\n' > $MEMDIR/upg_after_switch.md"
if wait_for 120 "new note indexed after the switch" \
  "docker exec -u sokkan $c python /tmp/probe.py search 'when is the kombucha tank cleaned?' | grep -q upg-after-switch"; then
  echo '{"indexed": true}' > "$W/indexrunner.json"
else echo '{"indexed": false}' > "$W/indexrunner.json"; fi
docker exec -u sokkan "$c" rm -f "$MEMDIR/upg_after_switch.md"
python3 "$H/e2e_upgrade/client.py" snapshot "$(token)" "$W/snap-after.json" > /dev/null
dc ps --format '{{.Service}} {{.State}} {{.Health}}' > "$W/ps-after.txt" 2>/dev/null || dc ps > "$W/ps-after.txt"
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' > "$W/ram-after.txt"
kill "$WATCH_HTTP" 2>/dev/null
dc logs --no-color api > "$W/api-3.log" 2>&1
# 3.0 never writes the 2.x index (the rollback below re-indexes the repaired notes)
docker run --rm -v "$VOL:/data" python:3.12-slim sha256sum /data/memory.db > "$W/memorydb.after.sha256"

# ---------------------------------------------------------------- 3. roll back
phase "rollback to $SRC"
set_dist "$SRC"
python3 "$H/e2e_upgrade/client.py" watch "$(token)" 1200 "$Q" > "$W/watch-rollback.jsonl" 2>/dev/null &
WR=$!
# docs/UPGRADE.md, "Roll back": ./scripts/rollback.sh <release> (shipped by 3.0)
( cd "$U/sokkan" && SOKKAN_BASE="$BASE" sh scripts/rollback.sh "$SRC" ) > "$W/rollback.log" 2>&1 \
  || { tail -n 30 "$W/rollback.log"; fail "rollback"; }
healthy 600 || { dc logs --tail 50 api; fail "$SRC does not start after the rollback"; }
PUSER=sokkan; docker exec "$(api)" id sokkan >/dev/null 2>&1 || PUSER=root
sleep 20
probe bench < "$W/questions.json" > "$W/bench-rollback.json"
python3 "$H/e2e_upgrade/client.py" snapshot "$(token)" "$W/snap-rollback.json" > /dev/null
kill "$WR" 2>/dev/null
cat "$W/bench-rollback.json"
phase done
