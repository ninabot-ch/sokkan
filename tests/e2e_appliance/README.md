# e2e_appliance — the test plan of the on-prem appliance (T1-T14)

What must be true before a SOKKAN box is left at a site with no engineer. Every test runs on
a **blank machine** and ends with **PASS**, **FAIL** or **NOT PLAYED** (with the reason — a
test that could not run is never counted as a success).

```sh
tests/e2e_appliance/run.sh            # everything (≈ 60-90 min, first run: image builds)
tests/e2e_appliance/run.sh appliance  # T1-T7, T11-T14
tests/e2e_appliance/run.sh upgrade    # T8-T10
```

The « machine » is a Docker-in-Docker container: its own Docker engine, network and volumes,
6 GB of memory, nothing published on the host. No cloud resource, no GPU, no real model, no
real gateway: the local model and the Deep gateway are `mock_llm.py` (their answers say who
served: `[ship] ok` / `[deep] ok`). The release is signed with a **throw-away** cosign key
made for the run; images are built from the release tarball and pushed to a registry inside
the machine. Settings: see the head of `run.sh` (`E2E_WORK` must not be under `/tmp`).

Results: `$E2E_WORK/work/results.json`, one record per test with its measures; logs next to it
(`install-sokkan.log`, `upgrade-A.log`, `mem.tsv` = memory of the machine every 2 s).

## The tests and their PASS criteria

| | Test | PASS when (all of) | Measured |
|---|---|---|---|
| T0 | Publisher side | tarball signed; api and web images built **from the tarball**, pushed, signed by digest | build + sign time |
| T1 | Install on a blank machine | the same release **without signature is refused and leaves no file**; `install.sh --profile appliance --version <v>` exits 0 without any question; `/api/version.dist` = the pinned version and its commit; `.env` is 0600 with no placeholder; api, web and router run the **signed digests** (nothing built); listeners: `127.0.0.1:3009` and `:443` only — api, router, Postgres, embedding are not published; the 3 personal codes and the owner code are printed once | install time, RAM after install |
| T2 | First launch | `https://<name>` answers 200 with the exported CA and not without it; plain HTTP does not answer on the network address; owner signs in; engine = the local model, ready, no click; local model and gateway reachable; Terminal, Preview, Magnitude, Missions, Alerts, Incidents, Infra are **off**; a wrong code is refused; each of the 3 people is themselves (`instance_role: none`) and lists only their own project; a first message is answered by the local model | time to first answer |
| T2b | Support tunnel | closed after install; a non-admin gets 403; the admin's request opens it for 2 h (state `open`, remaining ≤ 7200 s); past the deadline the timer's command closes it; journal has `support.tunnel.request`, `.opened` (by the admin), `.expired` | — |
| T3 | 3 simultaneous sessions | 3 people each start a session at the same moment and all 3 are answered; nobody reads another project (404); the journal shows 3 `session.spawn` and 3 `auth.login` under 3 names | wall time, RAM peak with 3 sessions |
| T4 | Forced escalation | a message with `/deep` is served by the gateway (model `sokkan-deep`, the instance's token); the next ordinary message is served locally again (not sticky); router counters show `deep:keyword` | latency of each |
| T5 | Quota reached | gateway answers 402 then 429: the session shows an `error` event with code `quota_exhausted` / `rate_limited`, the French sentence, « Rien n'est perdu », and **no upstream detail**; once the gateway answers again, so does the session | time to the message |
| T6 | Local model down | with the model stopped an ordinary message is answered from Deep (fallback); with both down the session shows `unreachable`/`overloaded` in words; the cockpit still answers; `GET /api/appliance/inference` says `ship: false`; after the model returns, sessions work **without restarting SOKKAN** | fallback latency, recovery |
| T7 | Network cut | with all outbound traffic dropped: a local session is answered; a `/deep` message shows « injoignable » (code `unreachable`); sessions list and memory search still answer; when the network returns, `/deep` works again | time to the refusal (connect timeout) |
| T8 | Upgrade 3.4.6 → new, entry A (installer) | a 3.4.6 installed with the installer of its time, with data; `install.sh` on it exits 0; `/api/version.dist` = new; token, board card and journal kept; `.sokkan-previous` = 3.4.6; `SOKKAN_COMMIT` written | upgrade time, **longest interruption** of `/api/health`, RAM peak |
| T9 | Upgrade 3.4.6 → new, entry B (`scripts/upgrade.sh <v>`) | exits 0, new version answers, data kept, and the tree is **identical** to entry A's (same files, same sizes) | upgrade time, interruption |
| T10 | Rollback | `scripts/rollback.sh` without argument goes back to 3.4.6 (version answered, no file of the newer release left, data kept) and records the way forward | rollback time, interruption |
| T11 | Backup / restore on a new machine | `backup-offsite.sh init` generates the key on the machine (0600) and prints it once; `run` exits 0, the cockpit sees the backup age; the bucket cannot be opened with a wrong key and holds no readable client text; then **volumes and install are destroyed**, a fresh install does not know the people; with only the bucket and the client's copy of the key, `restore latest --yes` brings back: the 3 people (same codes), sessions (same count), journal, board card, memory search | backup time and size, reinstall, restore time |
| T12 | Memory with documents | 3 notes written to the memory folder are in the top 3 of their question (French) within 7 min; a session about one of them shows the note's text to the model (recall) | time to index, search latency |
| T13 | Reboot | after the machine's engine restarts, with **no command typed**: healthy as the same version; owner and the 3 people sign in; HTTPS answers with the **same CA**; api, web, db, router, tls running; a session is answered; **a work started before the reboot still shows its conversation** to its author (their message and the agent's answer, simple profile); memory search still finds the note | reboot → healthy |
| T14 | Every `.env` line has an effect | for each variable of `deploy/appliance/env.appliance.example` one observable check passes (API answer, listener, certificate, cookie flag, what an endpoint received, feature reason naming the variable…); `SOKKAN_ESCALATION` and `SOKKAN_SHIP_FALLBACK_DEEP` are proven by **flipping** them; the heartbeat body equals the documented fields and holds nothing about people; a template variable with no check fails the test | variables checked |

## What this bench cannot prove (needs a real machine — scripts ready)

| Not played here | Why | How to play it |
|---|---|---|
| systemd units: model at boot (`sokkan-ship.service`), tunnel closed at boot, backup timer, request watcher | no systemd in the container | on the box or a VM: `sh scripts/appliance/host-setup.sh`, reboot, `systemctl status sokkan-*` |
| the real model (vLLM, 4× Arc Pro B60), its restart after a GPU fault, tokens/s under 3 sessions | the reference machine serves production; no GPU in the container | `deploy/appliance/ship` on the delivered box, then T3-T6 with `SOKKAN_SHIP_URL` pointing at it |
| GPU memory profile (EmbeddingGemma + reranker on the Arc cards) | same | `SOKKAN_ACCEPT_GEMMA_TERMS=1`, profile `gpu`, then T12 |
| a real Tailscale tunnel | no tailnet in the container: the tunnel command is a stub | on the box: `support-tunnel.sh open 0.1`, `tailscale status`, wait 6 min |
| the real Deep gateway (escalation quality, real 402/429 bodies) | would write a tenant on the production gateway | with a test tenant: T4-T5 against `https://infer.sokkan.ch` |
| disk encryption, UPS shutdown | hardware | see `docs/appliance/README.md` § Open points |
| sizing under real load (RAM of the api with 3 long sessions, Postgres growth) | mock answers are 1 line | measure during the pilot's first week (heartbeat: disk; `docker stats`) |
