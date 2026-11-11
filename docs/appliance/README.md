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

On the box (Ubuntu 24.04, Docker, the model unit prepared — § 6), as root:

```sh
sh install.sh --profile appliance --version <version>+<commit>
```

Non-interactive when these are in the environment (otherwise it asks): `SOKKAN_ORG_NAME`,
`SOKKAN_OWNER_EMAIL`, `SOKKAN_OWNER_NAME`, `SOKKAN_BOX_NAME` (the name people type),
`SOKKAN_BOX_IP`, `SOKKAN_DEEP_TOKEN`, `SOKKAN_INSTANCE_ID`, `SOKKAN_HEARTBEAT_URL`,
`SOKKAN_ACCEPT_GEMMA_TERMS` (the client's answer), `SOKKAN_ACCOUNTS="a@x:Ana:projet-ana,…"`.

What it does: downloads **that** release, verifies its signature (refused without one — § 7),
writes `.env` from `deploy/appliance/env.appliance.example`, pulls the signed images, starts,
waits until `/api/version` answers the pinned version, installs the host units
(`scripts/appliance/host-setup.sh`), creates the people and prints each access code **once**.

Then, once: `sh scripts/appliance/export-ca.sh` → install `sokkan-local-ca.crt` on each
workstation (read the fingerprint out loud when handing it over).

## 3. People: one code each (decision D2)

Local auth used to know one identity. Chosen path — the simplest that puts **distinct names in
the journal**: one instance, one personal access code per person, one project per person.

```sh
sh scripts/appliance/accounts.sh add ana@client.ch --name "Ana Example" --project projet-ana
sh scripts/appliance/accounts.sh code ana@client.ch      # lost code: a new one, the old one dies
sh scripts/appliance/accounts.sh revoke ana@client.ch    # leaves: open cockpit closed at once
sh scripts/appliance/accounts.sh list
```

The login screen is unchanged (one field). The instance stores a salted hash, never the code.
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

`SOKKAN_TLS_SITES` lists every name/IP people type; a name that is not listed gets no certificate.
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
