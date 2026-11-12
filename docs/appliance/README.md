# SOKKAN on-prem appliance — operations

One machine at the client's site serves the cockpit, the memory (CortHeXis) and the everyday
model. Only the requests that need the stronger model leave it. Nobody on site is an engineer:
everything below is either decided at install or one command.

Related pages: [SUPPORT-TUNNEL.md](SUPPORT-TUNNEL.md) (contractual, French) ·
[HEARTBEAT.md](HEARTBEAT.md) (French) · [../ENVIRONMENT.md](../ENVIRONMENT.md) (every variable) ·
[../../tests/e2e_appliance/README.md](../../tests/e2e_appliance/README.md) (the test plan T1-T14).

## 1. What runs where

```
                 workstations (browser, HTTPS, the box's CA installed once)
                                   │ 443
┌──────────────────────────────── the box ────────────────────────────────────┐
│ docker compose (profile appliance)                                          │
│   tls (Caddy, local CA) → web → api ── db (Postgres + pgvector)             │
│                                 │  └── corthexis-embed / -rerank (GPU)       │
│                                 └→ router ──→ Ship: model on this machine    │
│                                        │      (vLLM, systemd unit, :8004)    │
│ host: systemd — model, support-tunnel agent, nightly backup                  │
└────────────────────────────────────────┼─────────────────────────────────────┘
        outbound only:   Deep gateway (hard requests) · backup bucket (encrypted)
                         heartbeat (optional) · support tunnel (when the client opens it)
```

Published on the network: **443 only**. The plain-HTTP port answers on `127.0.0.1`; the api,
the router, Postgres, the embedding servers and the model are not reachable from the LAN.

## 2. Install

On the box (Ubuntu 24.04, Docker, the model unit prepared — § 6), as root.

**First, once: the verification tool and the public key.** The installer does not fetch them —
a release is only verified with a tool and a key that were on the machine before it. Without
both, an appliance install stops with « signature required but cosign or the public key is
missing — REFUSED » and changes nothing.

```sh
# cosign, the build pinned in deploy/appliance/versions.env (copy the three values from there)
COSIGN_VERSION=v2.4.1
COSIGN_SHA256=8b24b946dd5809c6bd93de08033bcf6bc0ed7d336b7785787c080f574b89249b
COSIGN_URL=https://github.com/sigstore/cosign/releases/download/$COSIGN_VERSION/cosign-linux-amd64
curl -fsSL "$COSIGN_URL" -o /usr/local/bin/cosign
echo "$COSIGN_SHA256  /usr/local/bin/cosign" | sha256sum -c - && chmod 0755 /usr/local/bin/cosign

# the public key of the SOKKAN releases
install -d -m 0755 /etc/sokkan
curl -fsSL https://sokkan.ch/cosign.pub -o /etc/sokkan/cosign.pub
sha256sum /etc/sokkan/cosign.pub
```

The key served on `https://sokkan.ch/cosign.pub` is the file `deploy/appliance/cosign.pub` of
this repository. Compare the printed SHA-256 with the one of that file **obtained another way**
(the public repository, or read to you by the publisher): a key and a release downloaded from
the same site only prove that the site agrees with itself. Other places the installer accepts:
`SOKKAN_COSIGN=<path to the binary>`, `SOKKAN_COSIGN_PUB=<path to the key>`.

**Then the install:**

```sh
curl -fsSL https://sokkan.ch/install.sh -o install.sh
sh install.sh --profile appliance --version <version>+<commit>
```

Non-interactive when these are in the environment (otherwise it asks, with the default shown):

| Variable | What it is | Default |
|---|---|---|
| `SOKKAN_ORG_NAME` | the organisation's name, shown in the cockpit | `Organisation` |
| `SOKKAN_OWNER_EMAIL`, `SOKKAN_OWNER_NAME` | the administrator | `admin@localhost`, `Admin` |
| `SOKKAN_BOX_NAME` | the name people type to reach the box (§ 5) | `sokkan.lan` |
| `SOKKAN_BOX_IP` | the box's address on the local network | first address of the machine |
| `SOKKAN_SHIP_URL` | the local model's endpoint, **as the containers reach it**. Empty = no local model (the assistant and document ingestion stay off) | `http://host.docker.internal:8004` |
| `SOKKAN_SHIP_MODEL` | the name the local model is served under (what its `/v1/models` lists) | `qwen3.8-27b` |
| `SOKKAN_DEEP_TOKEN` | this instance's token for the Deep gateway; empty = no Deep | — |
| `SOKKAN_DEEP_URL` | another Deep gateway than the template's (not asked) | template value |
| `SOKKAN_INSTANCE_ID` | opaque name for supervision | `sokkan-box` |
| `SOKKAN_HEARTBEAT_URL`, `SOKKAN_HEARTBEAT_TOKEN` | supervision heartbeat; empty = nothing is sent | — |
| `SOKKAN_ACCEPT_GEMMA_TERMS` | the client's answer to the embedding model's terms (`1` / `0`) | — |
| `SOKKAN_ACCOUNTS` | the people: `"a@x:Ana:projet-ana,b@x:Ben:projet-ben"` | none |

`SOKKAN_SHIP_URL` — the default points at a model served **on the box itself** (§ 6: it listens
on the host loopback and the Docker bridge, port 8004). A model on another machine: its address
on the network, e.g. `http://192.168.1.60:8004`; `localhost` would be the container, not the box.
`SOKKAN_SHIP_MODEL` must be the served name exactly, or every request is refused by the model
server. Both can be changed later in `.env`, followed by `docker compose up -d`.

What it does: downloads **that** release, verifies its signature (refused without one — § 7),
writes `.env` from `deploy/appliance/env.appliance.example`, pulls the signed images — or, when
the release publishes none, says so and **builds them on the machine** from the verified
tarball (5-10 min) —, starts, waits until `/api/version` answers the pinned version, installs
the host units (`scripts/appliance/host-setup.sh`), creates the people and prints each access
code **once**.

Then, once: `sh scripts/appliance/export-ca.sh` → install `sokkan-local-ca.crt` on each
workstation (read the fingerprint out loud when handing it over). People reach the box by its
**name** (§ 5).

## 3. People: one code each (decision D2)

Local auth used to know one identity. Chosen path — the simplest that puts **distinct names in
the journal**: one instance, one personal access code per person, one project per person.

```sh
sh scripts/appliance/accounts.sh add ana@client.ch --name "Ana Example" --project projet-ana
sh scripts/appliance/accounts.sh code ana@client.ch      # lost code: a new one, the old one dies
sh scripts/appliance/accounts.sh revoke ana@client.ch    # leaves: open cockpit closed at once
sh scripts/appliance/accounts.sh list
```

The sign-in screen has one field, « Access code », in the browser's language (3.6.3: French or
English, light theme, no variable name). The instance stores a salted hash, never the code.
With `SOKKAN_DEFAULT_ROLE=none` (set by the profile) a person sees only their project; nobody
but the owner reads the whole journal. The owner's code is `SOKKAN_LOCAL_TOKEN`.

Not chosen, and why: **OIDC** needs an identity provider — the client has none we can use, and
running Keycloak/Authentik on the box for three people is one more service to patch and back
up; it stays the right answer when the client already has Entra ID (`SOKKAN_AUTH_MODE=oidc`,
nothing else changes). **Three instances** would triple the model routing, backups and upgrades.

## 4. Inference: Ship here, Deep outside

`SOKKAN_SHIP_URL` set = the local model is the instance's engine (no click). Sessions talk to
the `router` service, which decides at every request:

| Goes to Deep when | Setting |
|---|---|
| the message contains a keyword (`/deep`) | `SOKKAN_ESCALATION_KEYWORDS` |
| the tool loop failed 3 times, or twice with the same error | `SOKKAN_ESCALATION_MAX_FAILS`, `_REPEAT_FAILS` |
| the caller asked for the Deep model | — |
| the local model does not answer | `SOKKAN_SHIP_FALLBACK_DEEP=1` (uses credit) |

`SOKKAN_ESCALATION=0` keeps everything local unless asked. Who typed a request is **not** sent
to the gateway (`SOKKAN_DEEP_FORWARD_USER=0`) — except in study mode, where the instance sends a
**pseudonym** (P1, P2…) with the study: the router hands both to the gateway, which serves the
free tier on that condition and meters per person; an address is never forwarded. The study
itself (end date, ceilings) is read by the api from the gateway (`GET /usage` with
`SOKKAN_DEEP_TOKEN`); its refusals (410 / 412 / 429 per person) reach the person as the gateway
worded them. What a person reads when it fails: a sentence
(402 credit, 429 quota, unreachable), in `SOKKAN_ERROR_LANG`. State: `GET /api/appliance/inference`.

Not yet there (decision needed, § 9): the hosted gateway's « sentinel » escalation (the small
model asking for help by itself) and its judge are not in the box's router.

### « Write with Claude » (3.6.5, feature `claude_tier`)

A third destination, reached **only** by a person's explicit gesture: in the simple profile,
when sending a piece of work, « Write with Claude » (with a model selector and a card that says
where the request goes: Anthropic, United States — outside Switzerland and Europe). Meant for
writing content that carries nothing of the organisation (course material); never chosen by a
keyword, a size, a failure, or when the local model is down (`ship_down` goes to Deep or fails).

| Who | What |
|---|---|
| the administrator | enters the organisation's Anthropic API key in **Settings › Claude** (encrypted in the model-key store, never in `.env`), picks the default model. The key is tested against `GET /v1/models`; the selector offers the models of `SOKKAN_CLAUDE_MODELS` that the key sees. On an appliance this key **never** becomes the engine of every session. |
| a project admin | classifies the project: `PUT /api/projects/<slug>/level` (`public`, `team`, `project` — the default —, `confidential`, `restricted`; lowering takes a reason, both directions journaled). |
| the instance | allows Claude in a project classified at most `SOKKAN_CLAUDE_MAX_LEVEL` (default `team`; `project`, `confidential` and `restricted` stay on the box — `confidential` and above whatever the variable says). Otherwise the choice is greyed out with the reason, and a start is refused (409). A work of Claude recalls no memory note and takes no document above that level. |
| the router | checks again at EVERY request: `GET /api/internal/claude-grant` on the api with a shared secret (3.6.6: written once by the api's entrypoint in the `router-secret` volume, read-only for the router — so whatever script did the upgrade; `SOKKAN_ROUTER_SECRET` in `.env` wins when set; the route answers 404 to anything else, and to a request that came through the reverse proxy). It sends `x-api-key` + `anthropic-version` to `https://api.anthropic.com/v1/messages`, no `authorization`, no `x-sokkan-*` header (who typed it, the project, the study stay on the box). No key, project too high, model outside the list, api unreachable: a sentence the person can read (`claude_*`, `x-should-retry: false`) and nothing sent anywhere else. |

What is visible: every answer carries a badge with the model **that answered** (the `model` of
the API's response, recorded in the transcript — « Local model · on your machine » or « Claude
Opus 5.5 · Anthropic »); every deliverable card and the stored deliverable name the model of each
save; the journal of validations has an `engine` row for the gesture and a `model` column on
every approval; « Export our work » gives `model` and `engine` per deliverable (`index.json`).

| Variable | Default | |
|---|---|---|
| `SOKKAN_FEATURE_CLAUDE_TIER` | off (`1` in the appliance profile) | needs `simple_profile` |
| `SOKKAN_ROUTER_SECRET` | empty (the file of the `router-secret` volume) | api AND router; an explicit value wins over the file |
| `SOKKAN_CLAUDE_MODELS` | `claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5,claude-fable-5-2` | the list a person chooses from; the router refuses any other |
| `SOKKAN_CLAUDE_MODEL` | `claude-opus-5-5` | default when the administrator chose none |
| `SOKKAN_CLAUDE_MAX_LEVEL` | `team` | capped at `project` |

### What the people of the site get (3.6 pilot)

The profile turns on, with what each needs:

| Feature | Needs | Setting of the profile |
|---|---|---|
| `documents` (document drop) | — | `SOKKAN_FEATURE_DOCUMENTS=1` |
| `simple_profile` | `documents` | `SOKKAN_FEATURE_SIMPLE_PROFILE=1`, `SOKKAN_PROFILE_DEFAULT=simple` |
| `study_mode` | — (inert until a study is active) | `SOKKAN_FEATURE_STUDY_MODE=1` |
| `doc_ingest` | `agents`, `memory_quarantine`, a LOCAL model | `SOKKAN_FEATURE_DOC_INGEST=1`, `SOKKAN_DOC_INGEST_LLM_URL` / `_MODEL` = the Ship model, 4 streams |

Sessions on the local model run with `CLAUDE_CODE_EFFORT_LEVEL=medium` (vLLM refuses the CLI's
default with a 400). Without a local model the installer leaves `doc_ingest` off: documents are
never read by the Deep gateway. The release images carry the document extractors. Details:
`docs/enterprise/PILOT-RELEASE-NOTES.md`.

## 5. HTTPS (decision D12)

Chosen: the `tls` service — Caddy with **its own certificate authority**, listening on 443, the
plain port bound to `127.0.0.1` (`SOKKAN_BIND`). No public DNS name, no Let's Encrypt, works
offline; the price is installing one root certificate per workstation. The existing `edge`
service is something else (public custom domains with on-demand Let's Encrypt, for managed
fleets) and cannot issue for a LAN name.

`SOKKAN_TLS_SITES` lists every name people type; a name that is not listed gets no certificate.

**People reach the box by its name, not by its address.** `https://<box name>` answers once the
name resolves to the box on the workstation — a record in the site's DNS, or one line in the
workstation's hosts file (`192.168.1.50  sokkan.lan`). `https://<box IP>` does **not** answer,
although the installer lists the address in `SOKKAN_TLS_SITES`: a browser that connects to a bare
address sends no server name, the `tls` container cannot tell which certificate is asked for,
and the TLS handshake fails (measured on an appliance install: « tlsv1 alert internal error »). The installer's
last message says so since 3.6.3; before, it announced « also https://<box IP> ».
The CA lives in the `caddy-data` volume: **it is not in the SOKKAN backup** — after a restore on
a new machine a new CA is created and must be installed again (or copy the volume).

## 6. The local model

`deploy/appliance/ship/`: compose file, `ship.env.example`, the image layer with the
« one visible device per worker » fix (without it a tensor-parallel worker mirrors every GB of
VRAM in host RAM), and `sokkan-ship.service`: started after Docker at boot, restarted with a
45 s delay, **given up after 5 failures in 30 min** (a GPU that no longer answers needs a
person). `systemctl status sokkan-ship` says so; the cockpit says « the model cannot be reached ».
The model listens on the host loopback and the Docker bridge only.

## 7. Updates: signed, pinned, one path

```sh
sh install.sh --version <new>            # or: ./scripts/upgrade.sh <new>   (the same script)
./scripts/rollback.sh                    # back to the version that ran before
```

A release = tarball + `.sha256` + `.sig` (cosign key pair, verified **offline** with
`/etc/sokkan/cosign.pub`, the key already on the machine) + `.images` (the digests of the
prebuilt images, each signed). With `SOKKAN_REQUIRE_SIGNATURE=1`: no signature, no key, no
cosign, or a wrong signature = **refused, nothing changed**. After the start the script waits
until `/api/version` answers the new version; otherwise it exits 1 and prints the rollback.
Publisher side: `scripts/release-artifacts.sh`. cosign is pinned (`deploy/appliance/versions.env`).

## 8. Backup, restore, supervision, support

- **Backup** — `/etc/sokkan/backup.env` (S3 endpoint in Switzerland, a key limited to the
  bucket), then `backup-offsite.sh init` (generates `/etc/sokkan/backup.key` on the machine and
  prints it once — the client keeps a copy elsewhere), nightly timer 01:30-02:30.
  `backup-offsite.sh status`; the cockpit and the heartbeat see the age of the last success.
- **Restore on a new machine** — install the same version, put `backup.env` and the client's
  copy of `backup.key` in `/etc/sokkan/`, `backup-offsite.sh restore latest --yes`. People keep
  their codes; the owner code is the new machine's.
- **Heartbeat** — off unless `SOKKAN_HEARTBEAT_URL`; the exact payload: `GET /api/appliance/heartbeat`.
- **Support tunnel** — off; `support-tunnel.sh open <hours>` or the cockpit (admin). Journal:
  `support.tunnel.*`.

## 9. Open points (not solved by this profile)

| Point | State |
|---|---|
| Disk encryption (LUKS + TPM, or a passphrase at boot) | not done — an installer of ours cannot retrofit it; to decide before the box is imaged |
| The release signing key (who holds it, where, rotation) | to create; until then nothing can be installed with `SOKKAN_REQUIRE_SIGNATURE=1` outside tests |
| Registry for the prebuilt images (reachable from the client's network) | to choose |
| Sentinel escalation + judge on the box | not ported from the hosted gateway |
| Cockpit buttons (support tunnel, personal codes, backup age) | API ready, UI is another lot |
| UPS / clean shutdown | not covered here (see our NUT runbook) |
| Sizing | measured only with mock answers in the test bench; real figures come from the pilot |
