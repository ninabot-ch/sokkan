# Kubernetes — SOKKAN Enterprise on a cluster

Status: **experimental** (feature `kubernetes_runner`, 3.2 cycle). One app, two packagings:

| Packaging | For | What runs where |
|---|---|---|
| `docker compose` (unchanged) | community, one machine | `api`, `web`, `db`, `corthexis-embed` on one host; sessions inside `api` (`SOKKAN_RUNNER=local`, default) — or one container per session with the opt-in docker runner (§7) |
| Helm chart [`deploy/helm/sokkan`](../../deploy/helm/sokkan) | enterprise, a cluster (Exoscale SKS, any vanilla Kubernetes ≥ 1.25, OpenShift) | `api`, `web`, egress gateway, optional embeddings / vLLM; **one Pod per agent session or agent run**; Postgres outside (DBaaS) |

Same images, same code, same feature switches ([FEATURES.md](FEATURES.md)): nothing is
forked. The only difference is *where the Claude Code CLI of a session executes*, chosen by
`SOKKAN_RUNNER`.

## 1. Architecture

```
                 Ingress (TLS, controller-agnostic)
          /  ─────────────►  web (Next.js, 2 replicas, stateless)
          /api, /term ────►  api (FastAPI + Agent SDK, 1 replica)  ──►  Postgres + pgvector (DBaaS)
                               │  ▲          │                        ──►  embeddings (optional)
          K8s API (Role:       │  │ MCP relay │ stream-json over TCP
          pods, secrets) ◄─────┘  │ :8098     ▼ :7070
                              ┌───┴──────────────────────────┐
                              │ session Pod (one per session │──► egress gateway :3128 ──► model API,
                              │ / run): supervisor + CLI     │      (CONNECT allowlist)       allowed forges
                              └──────────────────────────────┘
                                 mounts: its workspace + its transcript dir (subPath of the data PVC)
```

* **The cockpit still drives every session with the Agent SDK** — permissions (Allow / Deny),
  questions, memory-recall hooks, budgets, event translation do not change. `agentchat` asks
  `runner.client_args()` for the SDK client's options and transport: `local` → no transport,
  the SDK spawns the CLI in the api as before; `kubernetes` / `docker` → a `RunnerTransport`
  that carries the same stream-json protocol to the session's container.
* **Session pod** = the session image ([`docker/session.Dockerfile`](../../docker/session.Dockerfile):
  the CLI, git, ripgrep, jq, python3; ~700 MB uncompressed, of which the CLI ~210 MB) running
  `sokkan-session-supervisor` as PID 1. The supervisor authenticates the api (token derived
  from the instance key), starts the CLI on the first connection, and keeps it alive when the
  api goes away: output is buffered and replayed, and the new SDK client's `initialize` is
  answered from the cached first answer. That is how an api restart **reattaches** to live
  sessions instead of losing them.
* **SOKKAN MCP servers stay in the api** (memory, board, agents, observability need its data).
  In a pod the CLI runs `sokkan-mcp-relay <server>`, which connects to the api's relay
  (`:8098`) with the session's relay token; the api starts the real server with the identity
  *it* registered for that session (user, project, agent run). A pod cannot pick another
  identity or another session's servers.
* **Network**: session pods reach the api relay, DNS and the egress gateway — nothing else
  (NetworkPolicy). The gateway relays HTTPS `CONNECT` to `runner.egress.allow` only (model
  endpoint, your forges), every decision logged. Only the api reaches a pod's supervisor port.
* **Secrets**: a session's environment (model access, the vault values *named* for it, its
  tokens) lives in a per-session Secret owned by the pod (garbage-collected with it), never in
  the pod spec. The api's own secrets (DB URL, OIDC, vault key) never reach a session pod:
  `runner.session_env` forwards what the session adds plus `ANTHROPIC_*` / `CLAUDE_CODE_*`.
* **Lifecycle**: closing a session or finishing an agent run deletes its pod and Secret;
  `activeDeadlineSeconds` caps a pod's life (`runner.maxSeconds`, 24 h); the supervisor exits
  after `runner.idleSeconds` (15 min) without the api; at its first remote session the api
  deletes finished session pods (`reconcile`) and adopts the live ones on demand.

### Security properties of a session pod (tested: `tests/test_runner.py`, `tests/test_helm_chart.py`)

`runAsNonRoot`, no fixed uid on OpenShift, `allowPrivilegeEscalation: false`, all capabilities
dropped, `RuntimeDefault` seccomp, read-only root filesystem (HOME and /tmp are emptyDirs),
`automountServiceAccountToken: false` (dedicated ServiceAccount `<release>-session` without
any binding), no host namespaces, no hostPath, CPU / memory requests and limits, only two
subPath mounts of the data volume (its working directory, its transcript directory).

**git push in the person's name** (GitLab projects, lot 5): the session's git credential
helper (`sokkan-git-credential`, in the session image) cannot reach the api's loopback from
a pod; it asks through the **same authenticated relay as MCP** (`SOKKAN_RELAY_ADDR`,
`SOKKAN_RELAY_TOKEN`, port 8098 already allowed by the NetworkPolicy). The api knows which
session a relay token belongs to; the HMAC ticket in the session env must name that same
session and its live person, or nothing is answered. The token is returned to git on stdout
only — never in the pod's env, its Secret, a file, or the transcript. The git traffic itself
goes through the egress gateway: put the GitLab host in `runner.egress.allow` (and its CA in
`SOKKAN_GITLAB_CA_BUNDLE`, on the data volume, for an internal CA).

What it does **not** do yet: a session of project A and a session of project B on the same
node share the kernel (no gVisor / Kata — use a `runtimeClassName` policy if you need it);
the api's ServiceAccount can read Secrets in the release namespace (it creates one per
session) — the api is the trust root and already holds every secret of the instance.

## 2. Prerequisites (Exoscale SKS)

| Item | Requirement |
|---|---|
| Cluster | SKS ≥ 1.29, nodepool of ≥ 2 × `standard.large` (4 vCPU / 8 GB) for a team of 10 (§6); addons **Exoscale CCM** (LoadBalancer = NLB) and **Exoscale CSI** (block storage, `ReadWriteOnce`) |
| Ingress | ingress-nginx (or any controller) exposed by an Exoscale NLB; cert-manager for TLS (or terminate on your edge) |
| Postgres | Exoscale DBaaS PostgreSQL ≥ 15 with the `vector` extension; ip-filter = the nodes' egress IPs; URL in a Secret |
| Metrics | metrics-server (SKS default) — `kubectl top`, session usage in the cockpit |
| Images | `sokkan-api`, `sokkan-web`, `sokkan-session` in a registry the nodes can pull (build: §8) |
| Optional | GPU nodepool (vLLM, `vllm.enabled`), External Secrets Operator (`externalSecret.enabled`) |

## 3. Install, step by step

```bash
# 0. images (until they are published): build and push the three images
docker build -f docker/api.Dockerfile     -t $REG/sokkan-api:3.2.0 .
docker build -f docker/web.Dockerfile     -t $REG/sokkan-web:3.2.0 .
docker build -f docker/session.Dockerfile -t $REG/sokkan-session:3.2.0 .
docker push $REG/sokkan-api:3.2.0 && docker push $REG/sokkan-web:3.2.0 && docker push $REG/sokkan-session:3.2.0

# 1. namespace + database Secret (URI from `exo dbaas show sokkan-pg --uri`)
kubectl create namespace sokkan
kubectl -n sokkan create secret generic sokkan-db \
  --from-literal=DATABASE_URL='postgresql://avnadmin:…@sokkan-pg-….a.aivencloud.com:21699/defaultdb?sslmode=require'

# 2. the api's sensitive environment (or externalSecret.*)
kubectl -n sokkan create secret generic sokkan-env \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-… \
  --from-literal=SOKKAN_OIDC_CLIENT_SECRET=…

# 3. your values (host, SSO, images)
cat > my.yaml <<'EOF'
api:
  image: {repository: $REG/sokkan-api, tag: 3.2.0}
  publicUrl: https://sokkan.acme.ch
  ownerEmail: ops@acme.ch
  env:
    SOKKAN_AUTH_MODE: oidc
    SOKKAN_OIDC_ISSUER: https://login.acme.ch/realms/acme
    SOKKAN_OIDC_CLIENT_ID: sokkan
web: {image: {repository: $REG/sokkan-web, tag: 3.2.0}}
runner:
  image: {repository: $REG/sokkan-session, tag: 3.2.0}
  egress: {allow: [api.anthropic.com, gitlab.acme.ch]}
ingress: {host: sokkan.acme.ch}
secrets: {existingSecret: sokkan-env}
EOF

# 4. install
helm install sokkan deploy/helm/sokkan -n sokkan -f deploy/helm/sokkan/values-sks.yaml -f my.yaml --wait

# 5. check
kubectl -n sokkan get pods
curl -fsS https://sokkan.acme.ch/api/health
# open a session in the cockpit, then:
kubectl -n sokkan get pods -l app.kubernetes.io/component=session
```

Every value is commented in [`values.yaml`](../../deploy/helm/sokkan/values.yaml). The chart
refuses what it cannot run: more than one api replica / the HPA (§9), no Postgres.

Upgrade: `helm upgrade sokkan … --wait` (the api is `Recreate`: ~1 min of cockpit downtime;
live session pods survive and are reattached). Roll back: `helm rollback sokkan <rev>`.
Uninstall keeps the data PVC and the runner key (`helm.sh/resource-policy: keep`).

## 4. OpenShift

`-f values-openshift.yaml`: no `runAsUser` / `runAsGroup` / `fsGroup` anywhere (restricted-v2
assigns them from the namespace range), the session pods are created by the api without uid
either (`SOKKAN_K8S_RUN_AS_USER` empty). Every SOKKAN image runs under an arbitrary uid: the
session image's writable paths are group-0 with the owner's rights and HOME is an emptyDir;
the api writes only to its volumes. No privileged pod, no host namespaces, no user namespaces,
no extra SCC. Exposure is a standard `Ingress` that the OpenShift router turns into a `Route`
(edge TLS with the router's certificate, or your `tls.secretName`). NetworkPolicies select the
router by `network.openshift.io/policy-group: ingress`.

Not tested on a real OpenShift cluster yet: the chart is rendered and checked against the
restricted rules in CI (`tests/test_helm_chart.py::test_openshift_overlay_sets_no_uid_or_fsgroup`);
the session image was run under an arbitrary uid (`docker run --user 12345:0 --read-only`).

## 5. Storage and state

| Data | Where | Note |
|---|---|---|
| SQLite (board, agents, projects, vault, audit), workspaces, transcripts, memory notes (`.md` = source of truth) | data PVC `/data` | `ReadWriteOnce` (SKS CSI): session pods are pinned to the api's node (podAffinity, set by the chart). `ReadWriteMany` (NFS, CephFS…) lifts that |
| Memory index, note history, recall log | Postgres (DBaaS) | backed up by the DBaaS |
| Embedding models | `models` PVC (only with `embeddings.enabled`) | re-downloadable |

**Object storage is not supported by the app today**: memory and files are on the data volume.
A Redis queue for agent runs does not exist either — the scheduler is in the api process.
These are the reasons the api is a single replica (§9).

## 6. Sizing

Measured on gmk1, 07.10.2026 — the real Claude Code CLI 2.1.292 in the session image, driven
through the runner by the Agent SDK, model = the scripted mock (so: the CLI's own cost, not a
long conversation):

| What | Measured |
|---|---|
| session container start → CLI ready (docker runner) | 1.9 s |
| session pod scheduled → supervisor serving (k3s, image present) | 1.7–2.2 s |
| memory of one session after a turn (docker stats) | **≈ 118 MiB** |
| same on Kubernetes (k3s, cgroup of the session pod) | **116 MiB current, 141 MiB peak**; `kubectl top`: 75m / 112Mi |
| CPU of an idle session between turns | 8–20 millicores |
| supervisor alone (no CLI yet / scripted CLI) | ≈ 24 MiB, ~1 m |
| session image | 695 MB uncompressed (CLI 207 MB) |

What is **not** measured yet: a long real conversation with tools (Bash builds, test runs) —
the CLI's memory grows with its context and the commands it runs live in the same cgroup. The
3.1 incident (4 heavy sessions saturating a 4 GB VM, note `sokkan-cockpit-vm-memory`) is the
reason for the defaults: **request 250m / 512Mi, limit 1 CPU / 2Gi** per session pod. A
session that crosses its limit is OOM-killed alone (the CLI exit is reported in the cockpit),
instead of taking the node and its neighbours down.

Rule of thumb until the POC measures real load (estimate, not a measurement):

| Team | Concurrent sessions | Nodes (`standard.large`, 4 vCPU / 8 GB) |
|---|---|---|
| 5 | ≤ 10 | 2 (api + web + sessions) |
| 10 | ≤ 25 | 3 |
| 25 | ≤ 60 | 5–6, or `standard.extra-large` |

The api pod: request 500m / 1 Gi, limit 2 CPU / 3 Gi (in-process ONNX embeddings with the
`leger` memory profile). With `ReadWriteOnce` storage every session runs on the api's node:
size **that** node for the concurrent sessions, or use `ReadWriteMany` to spread them.

## 7. docker runner (compose, one container per session)

Same isolation on a single host, opt-in:

```bash
docker build -f docker/session.Dockerfile -t sokkan-session:local .
echo 'COMPOSE_FILE=docker-compose.yml:docker/runner/compose.docker-runner.yml' >> .env
echo "SOKKAN_WORKSPACE=$PWD/workspace" >> .env        # absolute: it is a bind source
docker compose up -d
```

Session containers: uid 1000, read-only root fs, all capabilities dropped, no-new-privileges,
CPU / memory (no swap) / pids limits, an **internal** network (`sokkan-sessions`, no route out)
shared with the api (relay) and `session-egress` (the allowlist gateway). The api reaches the
Docker Engine API through `docker-socket-proxy` restricted to containers. **Trade-off**: whoever
controls the api can create containers on the host (the proxy cannot forbid a privileged one);
use it on a host dedicated to SOKKAN. Docker ≥ 26 (volume subpath mounts).

## 8. Migration: compose → chart

1. **Backup** on the compose host (OPERATIONS.md §5):
   ```bash
   docker compose stop api
   docker run --rm -v sokkan_sokkan-data:/data -v $PWD:/out alpine tar czf /out/sokkan-data.tgz -C /data .
   docker run --rm -v ${SOKKAN_WORKSPACE:-$PWD/workspace}:/ws -v $PWD:/out alpine tar czf /out/workspace.tgz -C /ws .
   docker compose exec db pg_dump -U sokkan -Fc sokkan > sokkan-pg.dump
   ```
2. **Postgres**: restore into the DBaaS (`pg_restore --no-owner -d "$DATABASE_URL" sokkan-pg.dump`),
   after `CREATE EXTENSION vector;`.
3. **Install the chart** with `api.replicas: 1`, then scale the api to 0
   (`kubectl -n sokkan scale deploy/sokkan-api --replicas=0`).
4. **Restore the data volume** through a helper pod that mounts the PVC:
   ```bash
   kubectl -n sokkan run restore --image=alpine --restart=Never --overrides='{"spec":{"securityContext":{"runAsUser":1000,"runAsNonRoot":true,"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"restore","image":"alpine","command":["sleep","3600"],"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}},"volumeMounts":[{"name":"d","mountPath":"/data"}]}],"volumes":[{"name":"d","persistentVolumeClaim":{"claimName":"sokkan-data"}}]}}'
   kubectl -n sokkan cp sokkan-data.tgz restore:/tmp/ && kubectl -n sokkan exec restore -- tar xzf /tmp/sokkan-data.tgz -C /data
   kubectl -n sokkan cp workspace.tgz restore:/tmp/ && kubectl -n sokkan exec restore -- sh -c 'mkdir -p /data/workspace && tar xzf /tmp/workspace.tgz -C /data/workspace'
   kubectl -n sokkan delete pod restore
   ```
   The chart's default project workspace is `/data/workspace` (compose: `/workspace`). Claude
   Code keys transcripts by path: move `/data/claude/projects/-workspace` to
   `/data/claude/projects/-data-workspace` so History and the auto-memory follow.
5. `kubectl -n sokkan scale deploy/sokkan-api --replicas=1`, check `/api/health`, open History.

Back to compose: the same in reverse (the data volume layout is the same apart from the
workspace path).

## 9. Limits (read before promising anything)

* **One api replica.** SQLite + live sessions in memory. `api.replicas > 1` and the HPA are
  refused unless `api.allowMultipleReplicas=true` — and then unsupported. Session pods scale;
  the api does not, yet.
* **No object storage, no Redis**: not implemented by the app (§5).
* **Raw terminal (`tmux`) is off** in the chart: it would run inside the api pod.
* A session image built against another SDK version than the api's: keep
  `CLAUDE_AGENT_SDK_VERSION` aligned (session.Dockerfile build arg).
* Model access: a remote session needs explicit credentials (API key, inference token,
  cockpit model settings). A `claude login` of the api's user does not reach the pods.
* Not yet validated on SKS or OpenShift clusters (k3d only, §10).

## 10. Tests

| Test | What it proves |
|---|---|
| `tests/test_runner.py` (29) | selection, env filtering, mounts, supervisor (relay, buffer + reattach, exit, idle), the SDK over a runner with Allow / Deny, MCP relay identity, egress allowlist, docker and kubernetes runners against fake APIs (adopt, stop, reconcile, restricted pod) |
| `tests/test_forge_push_runners.py` | git push through the relay: simulated pod (always) and a real docker container on its own bridge network (api loopback unreachable, relay + fake GitLab on the gateway; SKIPPED without a daemon or `SOKKAN_TEST_GIT_IMAGE`); a ticket replayed on another session's relay channel, without a relay token, forged, or after the session ended → refused; token absent from env, output and disk |
| `tests/test_runner_docker_integration.py` | the real CLI in a real session container (mock model): a turn, MCP relay `connected`, transcript in the api's dir, reattach after a dropped api connection, cleanup — SKIPPED without a Docker daemon or the session image |
| `tests/test_helm_chart.py` (15) | `helm lint --strict` and `helm template` for default / SKS / OpenShift / k3d / everything-on; no root, no privileged, limits everywhere, dedicated SAs, minimal Role, session NetworkPolicy, OpenShift overlay without uid — SKIPPED without `helm` |
| [`deploy/helm/sokkan/ci/k3d-test.sh`](../../deploy/helm/sokkan/ci/k3d-test.sh) | real cluster (k3s in Docker): install, api health, a cockpit session through `agentchat` starts a pod, survives the api process, is reattached, then pod and Secret are deleted; measures the pod |

Last real runs (gmk1, 07.10.2026, k3s v1.30 in k3d, clusters deleted afterwards):

* `k3d-test.sh` — chart installed with the real `sokkan-api`, `sokkan-web`, `sokkan-session`
  images and `devPostgres`: all pods Running, `/api/health` ok; a session through
  `agentchat` inside the api pod created `sokkan-s-…` (restricted securityContext, limits),
  the real CLI answered (mock model reached through `SOKKAN_SESSION_NO_PROXY`), the api
  process was killed without closing, a new one logged `kept 1 live session` then
  `reattached session … to sokkan-s-…` and ran a second turn (`adopted: true, resumed: true`);
  closing the session left no pod and no Secret.
* `k3d-runner-smoke.sh` — a namespace that ENFORCES Pod Security `restricted`: the session
  pod is admitted, a privileged control pod is refused; from the session pod the MCP relay
  answers as the identity set by the api (`relay-as-alice:ping`), a direct connection to the
  internet fails, the egress gateway answers `403` for a host outside the allowlist, no
  ServiceAccount token is mounted.

Two bugs found by the real cluster and fixed: a session memory limit below the default
request was refused by the API server (requests are now clamped to the limits); an `http://`
model endpoint went through the egress proxy (new `SOKKAN_SESSION_NO_PROXY`).

Host note: Docker's default address pools were exhausted on gmk1 — the scripts create their
network with an explicit subnet (`K3D_SUBNET`, default 10.251.0.0/24), and relax the kubelet
disk-eviction thresholds (the host disk was > 90 % full).
