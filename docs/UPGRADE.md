# Upgrading & rolling back SOKKAN

SOKKAN checks `https://sokkan.ch/dist/VERSION` once a day and tells you in
**Profile** when a newer release is out. How you apply it depends on how you run
SOKKAN.

Your data — the SQLite state, the memory index, transcripts — lives in the
`sokkan-data` Docker volume and in your `SOKKAN_WORKSPACE`. **Upgrades and
rollbacks never touch it**; only the application code is replaced. (One
exception, announced: the update from 2.x to 3.0 migrates the memory — see
below; the notes are archived first and the 2.x index is kept.)

---

## Self-hosted

### Upgrade

Re-run the installer from the directory that contains your `sokkan/` folder:

```bash
curl -fsSL https://sokkan.ch/install.sh | sh
```

It detects the existing install, pulls the current release over it, keeps your
`.env` and data volumes, and rebuilds (short interruption while the containers
restart). Equivalent manual steps, run inside the `sokkan/` folder:

```bash
curl -fsSL https://sokkan.ch/dist/sokkan-latest.tar.gz | tar xz --strip-components=1
docker compose up -d --build
```

### Roll back to a specific version

Every release is kept as an immutable tarball named by its build hash. Read the
version you're on in `.env` (`SOKKAN_VERSION`) or the footer of sokkan.ch, pick
an earlier hash, and pull that tarball instead of `-latest`:

```bash
cd sokkan
curl -fsSL https://sokkan.ch/dist/sokkan-<hash>.tar.gz | tar xz --strip-components=1
# pin the version string so the update banner reflects reality
sed -i '/^SOKKAN_VERSION=/d' .env && echo 'SOKKAN_VERSION=<hash>' >> .env
docker compose up -d --build
```

The full changelog (with hashes) is at
[`CHANGELOG.md`](../CHANGELOG.md).

> **Note on old data volumes:** installs from v0.1.0 created the `/data` volume
> owned by `root`. Newer images run as an unprivileged user and will refuse to
> start on such a volume with a clear message. Fix it once:
> `docker compose run --rm --user root api chown -R 1000:1000 /data`.

---

## From 2.x to 3.0 — the memory moves to the new engine

3.0 replaces the memory index (`memory.db`, SQLite) by the CortHeXis engine: a
Postgres + pgvector store (`db` service) and a local embedding model
(`corthexis-embed`, llama.cpp). **Your notes — the `.md` files — stay the source
of truth.** The migration is automatic and runs at the first start of 3.0; you
do not have to do anything, and searches keep working while it runs.

### Before you update

- **Docker Compose 2.20 or newer** (the compose file uses `include:`); the
  installer checks it.
- **RAM**: the Light profile adds Postgres and the embedding server to the
  stack — about **250 MB** more than 2.x at rest on a small memory (measured: Postgres
  65 MB, embedding server 160 MB, plus the 330 MB model file in the page cache;
  budget up to ~1.2 GB for the model server while importing a large memory);
  4 GB is the minimum, `scripts/doctor.sh` tells you where you stand.
- **Disk**: the migration archives the notes before touching them (a few MB
  for a few hundred notes), and the model is downloaded once (≈ 330 MB for
  EmbeddingGemma, ≈ 300 MB for the MIT model).

### Update

Same command as any update — the installer notices a 2.x install and sets up
the memory engine first:

```bash
curl -fsSL https://sokkan.ch/install.sh | sh
```

It runs `./scripts/memory-setup.sh`, which picks the **memory profile** for
this machine (`leger`, `standard` or `gpu`, recommended by Magnitude from your
cores, RAM and GPU), asks whether you accept the licence of the recommended
model (EmbeddingGemma, Gemma Terms of Use — decline and an MIT model is used),
writes `SOKKAN_MEMORY_PROFILE` to `.env`, then rebuilds. Manual equivalent:

```bash
cd sokkan
curl -fsSL https://sokkan.ch/dist/sokkan-latest.tar.gz | tar xz --strip-components=1
./scripts/memory-setup.sh            # --accept | --decline to answer without a prompt
docker compose up -d --build
```

Your 2.x embedding settings are kept rather than replaced:

| 2.x `.env` | 3.0 profile | Meaning |
|---|---|---|
| nothing (default MiniLM) | `leger` (or what Magnitude recommends) | the 3.0 model, CPU |
| `ML_SERVICE_URL=…` | `remote` | your remote embedding service, same vectors as before |
| `SOKKAN_EMBED_MODEL=…` (another fastembed model) | `legacy` | that model, in process |

To move from `remote` or `legacy` to the 3.0 model later:
`./scripts/memory-setup.sh --profile leger && docker compose up -d` — the notes
are re-indexed in the background while the current index keeps serving.

### What happens at the first start

The api container runs these steps in the background. Each one can be
interrupted (restart, power cut, model not downloaded yet) and resumes where it
stopped.

| Step | What it does |
|---|---|
| archive | `memory-migration/corpus-2x-<date>.tar.gz` in the data volume: every file of the memory directory, plus a manifest (SHA-256, date, declared date of each note). Nothing is changed before this archive exists and has been read back. |
| plan | the repairs the notes need (one naming convention: file = name; orphan updates merged into their note; broken frontmatter rewritten; dead `[[links]]` re-attached), computed without writing — `memory-migration/normalize-plan.txt` |
| normalize | the plan applied. A rewritten file keeps the date it had: a repair is not an update. |
| dates (files) | **date test**: no declared date changed, no note made younger |
| dates import | each note's date: `metadata.modified` from the frontmatter when present, else the 2.x file date, labelled `migrated-mtime` (shown as a reconstructed date) |
| index | every note embedded into generation 1 of the store, with the configured profile |
| verify | every note of the archive and every note of the 2.x index is in the store; **date test** in the store: same dates as before, none younger than the start of the migration |
| switch | searches now read the store |

**Searches and the per-turn recall during the migration** are served by the 2.x
`memory.db`, read-only,
with the 2.x model — the same results as before the update. The switch happens
only once generation 1 is complete and checked. Measured on a 52-note test
memory, Light profile: archive, repairs and date import in 1 s, import of the
51 notes in 27 s once the model was up; 92 searches (one per second) during the
migration, none failed, 87 served by `memory.db`, then by the store.

Follow it in the **CortHeXis** tab, or `GET /api/memory/migration` (steps, the
repair plan, progress, the two date tests, the log, and which index serves).

**If a check fails** (a note missing, a date that moved), the migration stops
before the switch with status `blocked`, the 2.x index keeps serving, and the
reason is shown. After reading it, an admin can go on anyway
(`POST /api/memory/migration/approve {"what": "override"}`), or fix the notes
and restart the api.

Options (`.env`):

- `SOKKAN_MIGRATION_NORMALIZE=ask` — wait for an explicit go before repairing
  the notes (`POST /api/memory/migration/approve {"what": "normalize"}`);
  `off` — index the notes as they are (a note shadowed by a duplicate name then
  blocks the switch, as it would be lost).
- `CORTHEXIS_MEMORY_BACKEND=sqlite` — stay on the 2.x index for now.

### Roll back

`memory.db` is never written by 3.0. To go back to 2.3:

```bash
cd sokkan
curl -fsSL https://sokkan.ch/dist/sokkan-<2.3 hash>.tar.gz | tar xz --strip-components=1
sed -i '/^SOKKAN_VERSION=/d' .env && echo 'SOKKAN_VERSION=<2.3 hash>' >> .env
docker compose up -d --build --remove-orphans
```

2.3 serves `memory.db` as it was. If the repairs renamed notes and you want the
files exactly as before the update, restore the archive (the newest
`corpus-2x-*.tar.gz`):

```bash
docker compose run --rm --user sokkan --entrypoint sh api -c \
  'cd /data/memory-migration && f=$(ls -t corpus-2x-*.tar.gz | head -1) && \
   tar xzf "$f" -C /tmp && cp -p /tmp/memory/* "$SOKKAN_MEMORY_DIR"/'
```

(Notes written after the update are kept; restoring copies the archived files
over their current versions.) The Postgres volume `sokkan-pg` can stay — 2.3
ignores it — or be removed with `docker volume rm <project>_sokkan-pg` to
start the migration over at the next 3.0 start (delete
`/data/memory-migration/state.json` too).

---

## Managed cloud

Nothing to run — updates are handled for you:

- **Automatic**: each new release is rolled out to the managed fleet. From 2.x
  to 3.0 the memory is migrated the same way as on a self-hosted install (above),
  in the background, without interrupting searches.
- **On demand**: an admin can click **⬆ update** in **Infra → My fleet** when the
  update banner appears (short interruption while the cockpit rebuilds).

Rollback on a managed instance is an operator action (support pins your instance
to a previous release) — reach us at **hello@sokkan.ch**.

---

## What "a version" means

Releases are identified by a semver line plus the build hash, e.g.
`0.9.0+c47d997`. The semver part is what you see on the site and in the update
banner; the hash is what names the downloadable tarball and pins an exact build
for rollback.
