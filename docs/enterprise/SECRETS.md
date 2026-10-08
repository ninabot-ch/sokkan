# Secrets — providers, OpenBao, migration, rotation, backups (SOKKAN 3.3)

Feature `secrets_provider` (beta; enterprise default **on**, community default off). Code:
`backend/secrets_provider/`. Operations: `scripts/secrets-migrate.py`, `scripts/secrets-rotate.py`.
Screen: **Setup › Secrets** (instance admin) — provider, why it was selected, its configuration,
**Test connection**.

## 1. What is a secret here

| What | Who reads it | Stored as |
|---|---|---|
| **Project secrets** (vault: `DB_PASSWORD`, `STRIPE_KEY`…) | injected by NAME into a project's sessions and agent runs, never shown | the provider's store, per `(project, NAME)` |
| **Opaque values SOKKAN encrypts itself**: BYOK model keys (`modelkeys.json`), forge OAuth tokens (`projects.db`), Teams app tokens (`teams.db`) | the api, in memory | Fernet ciphertext on the data volume, under a **data key** |
| **Data keys** — one per *context*: `vault` (vault values in file mode, model keys), `forge` (forge tokens, git credential tickets), `teams` (Teams tokens, approval signatures) | the api, in memory | depends on the provider (§ 2) |

The provider decides where the **project secrets** and the **data keys** live. Ciphertexts on the
data volume never change format between providers: a migration moves keys and secrets, not
`projects.db` or `teams.db`.

## 2. The three providers

| | `file` (3.2 behaviour) | `openbao` (reference) | `kubernetes` |
|---|---|---|---|
| Project secrets | `vault.json` (Fernet, `vault` key) | **KV v2** `<kv>/sokkan/<instance>/<project>/<NAME>` = `{"value": …}` | Secret `<prefix>-vault-<project>`, one entry per NAME |
| Data keys | `vault.key`, `forge.key`, `teams.key` (0600) **in clear** on the volume | `<key>.wrapped` = transit ciphertexts only; the **transit key never leaves OpenBao**; unwrapped in memory | Secret `<prefix>-data-keys` |
| At-rest protection | file permissions | OpenBao barrier + transit (envelope encryption) | the cluster (etcd encryption / KMS provider) |
| A copy of the data volume reveals | everything | nothing (needs OpenBao) | nothing (needs the cluster Secrets) |
| For | community, one VM | enterprise, our cloud, the customer's Vault | clusters without OpenBao |

Selection (`secrets_provider.selected()`, logged at startup as `[secrets] provider …`):

1. feature off → **file**, always (a `SOKKAN_SECRETS_PROVIDER` other than `file` is ignored, and said);
2. `SOKKAN_SECRETS_PROVIDER=file|openbao|kubernetes`;
3. unset → **openbao** when `SOKKAN_OPENBAO_ADDR` is set, else **file** — on an enterprise
   instance Setup › Secrets then shows a warning with the next step.

Fail-closed rules:
* openbao/kubernetes selected while a clear `vault.key` (etc.) is still on disk and not migrated →
  the api refuses to mint a new key (secret reads answer **503** with the reason) — run the migration;
* file selected while `*.key.wrapped` files exist (feature switched off on an OpenBao instance) →
  refused the same way: a fresh key would make every stored value unreadable;
* OpenBao unreachable / sealed → secret reads and session starts that need secrets answer 503;
  nothing falls back to files; the cockpit itself keeps running.

```
           ┌────────────── api pod / container ──────────────┐
 session ←─┤ vault.session_env(names)  ──► KV v2  get        │──► OpenBao
           │ forge/teams/BYOK encrypt  ──► Fernet(data key)  │      kv:  sokkan/<inst>/<project>/<NAME>
           │                 data key  ◄── transit decrypt ◄─┤      transit: sokkan-<inst> (never exported)
           │ /data/forge.key.wrapped = "vault:v1:…" (no key) │
           └─────────────────────────────────────────────────┘
```

## 3. OpenBao configuration

| Variable | Meaning |
|---|---|
| `SOKKAN_OPENBAO_ADDR` | `https://bao.example:8200` (plain `http://` refused except loopback or `SOKKAN_OPENBAO_ALLOW_HTTP=1`) |
| `SOKKAN_OPENBAO_CACERT` | PEM bundle to verify the server |
| `SOKKAN_OPENBAO_NAMESPACE` | namespace (`X-Vault-Namespace`) — OpenBao 2.4 (tested) / Vault Enterprise |
| `SOKKAN_OPENBAO_AUTH` | `kubernetes` \| `approle` \| `token` (default: approle if a role id is set, kubernetes if a k8s role is set, else token) |
| `SOKKAN_OPENBAO_K8S_ROLE`, `_K8S_MOUNT`, `_K8S_JWT_FILE` | kubernetes auth (default mount `kubernetes`, JWT `/var/run/secrets/kubernetes.io/serviceaccount/token`) |
| `SOKKAN_OPENBAO_ROLE_ID(_FILE)`, `SOKKAN_OPENBAO_SECRET_ID(_FILE)`, `_APPROLE_MOUNT` | AppRole (prefer the `_FILE` forms: not in the environment) |
| `SOKKAN_OPENBAO_TOKEN(_FILE)` | a token — break-glass and the operator's rotation only |
| `SOKKAN_INSTANCE_ID` | instance id (default `default`; Helm: the release name) → KV prefix `sokkan/<id>` and transit key `sokkan-<id>` |
| `SOKKAN_OPENBAO_KV_MOUNT` / `_TRANSIT_MOUNT` / `_TRANSIT_KEY` / `_PREFIX` | defaults `secret`, `transit`, `sokkan-<id>`, `sokkan/<id>` |

Tokens are renewed at 2/3 of their TTL (`renew-self`); a token that cannot be renewed or is
refused (403) is replaced by a new login, once per request.

**AppRole or Kubernetes?** Kubernetes auth when SOKKAN runs in a cluster that OpenBao can call
(TokenReview): no secret to distribute, the identity is the api's ServiceAccount, revoked with it.
AppRole for compose / VM installs: the `secret-id` is a credential — mount it as a file
(`SOKKAN_OPENBAO_SECRET_ID_FILE`), give it a TTL / CIDR binding per your policy, rotate it like a
password (a new `secret-id`, restart the api).

**Least-privilege policy** (written by `deploy/helm/sokkan/files/openbao-configure.sh`, which also
writes `sokkan-<inst>-rotate` = the same + `transit/keys/sokkan-<inst>/rotate` for the operator):

```hcl
path "secret/data/sokkan/<inst>/*"     { capabilities = ["create", "read", "update", "delete"] }
path "secret/metadata/sokkan/<inst>/*" { capabilities = ["list", "read", "delete"] }
path "transit/encrypt/sokkan-<inst>" { capabilities = ["update"] }
path "transit/decrypt/sokkan-<inst>" { capabilities = ["update"] }
path "transit/rewrap/sokkan-<inst>"  { capabilities = ["update"] }
path "transit/keys/sokkan-<inst>"    { capabilities = ["read"] }
```

The api cannot rotate, export or delete the transit key, nor read another instance's prefix
(both tested against a real OpenBao).

**A customer with its own Vault / OpenBao**: they create the mounts, the transit key, the policy
above and a role (kubernetes or AppRole) — or run `openbao-configure.sh` themselves with an
operator token (`INST=… AUTH=kubernetes SA=<release>-sokkan-api NS=<namespace> sh openbao-configure.sh`;
works with the `vault` CLI too). Helm: `openbao.address`, `openbao.caSecret`, `openbao.namespace`,
`openbao.auth.*`. We never need their root token.

## 4. Migration, file → OpenBao (hot)

The api keeps running on files during the copy; one restart switches it.

```bash
# 0. a backup (§ 8) — with a passphrase
# 1. the OpenBao side is ready (§ 3/§ 6); the api's env gets SOKKAN_OPENBAO_* AND, for now,
#    SOKKAN_SECRETS_PROVIDER=file (without it an enterprise api picks openbao as soon as
#    SOKKAN_OPENBAO_ADDR is set — and refuses to serve secrets until they are migrated)
docker compose exec -w /app/backend api python3 -m secrets_provider.cli migrate --from file --to openbao --dry-run
docker compose exec -w /app/backend api python3 -m secrets_provider.cli migrate --from file --to openbao
# 2. SOKKAN_SECRETS_PROVIDER=openbao in .env, docker compose up -d api; Setup › Secrets → Test connection
# 3. compare once more (writes nothing; exit 1 = a value differs), then shred the clear keys
docker compose exec -w /app/backend api python3 -m secrets_provider.cli migrate --from file --to openbao --check
docker compose exec -w /app/backend api python3 -m secrets_provider.cli migrate --from file --to openbao --purge
```

(Outside a container: `scripts/secrets-migrate.py --from file --to openbao …` with the same env.)

What it does, in order: data keys first (`vault`, `forge`, `teams` wrapped by transit into
`<key>.wrapped`, read back and compared), then every project secret (written to KV, read back).
**Idempotent**: what is already there with the same value is skipped; a value that differs is
**reported, never overwritten** unless `--overwrite`. `--purge` re-verifies, then overwrites and
unlinks `vault.key`, `forge.key`, `teams.key` (best effort: copy-on-write filesystems and SSDs may
keep old blocks — encrypted volumes recommended) and renames `vault.json` to
`vault.json.pre-openbao` (ciphertext whose key is now wrapped). Marker: `secrets-provider.json`.
Journal: `$SOKKAN_DATA_DIR/secrets-ops.log` (JSON lines, never a value) + the audit journal
(`secrets.migrate`).

**Rollback**
* before `--purge`: unset `SOKKAN_SECRETS_PROVIDER`, restart — the files were never touched
  (secrets written in OpenBao after the switch: `migrate --from openbao --to file`);
* after `--purge`: `migrate --from openbao --to file` writes the same data keys back as files and
  the KV secrets into `vault.json`; set `SOKKAN_SECRETS_PROVIDER=file`, restart, then delete the
  `*.key.wrapped` files.

**Export** (an escrow copy, or leaving OpenBao without reaching it from the target):
`… cli export --out sokkan-secrets.enc` with `SOKKAN_SECRETS_EXPORT_PASSPHRASE(_FILE)` (≥ 12
characters; scrypt + Fernet; data keys **and** secrets — treat it like the keys themselves), then
`… cli import --in sokkan-secrets.enc --to file|openbao|kubernetes` (same idempotence rules).

`--to kubernetes` works the same way (Secrets of the namespace instead of KV + transit).

## 5. Rotation

```bash
# master key (OpenBao): new transit key version, every wrapped data key rewrapped. Values do not
# change; run with the OPERATOR's token (policy sokkan-<inst>-rotate) — the api's role cannot.
SOKKAN_OPENBAO_AUTH=token SOKKAN_OPENBAO_TOKEN_FILE=/root/.bao-operator \
  docker compose exec … python3 -m secrets_provider.cli rotate --master
# data keys (any provider): a new key per context, every value re-encrypted, the old key dropped
… python3 -m secrets_provider.cli rotate --data-keys            # vault forge teams
… python3 -m secrets_provider.cli rotate --data-keys forge --dry-run
```

`--data-keys`: (1) a new key becomes primary while the old one still decrypts (`<key>.next` /
a second entry in `.wrapped` / `<ctx>.next` in the Secret) — the running api keeps working;
(2) `vault.json` (file mode), `modelkeys.json`, forge tokens, Teams token cache re-encrypted;
(3) the old key is dropped. An interrupted rotation resumes with the same new key. Effects: git
credential tickets of live sessions are signed with the forge key → after a `forge` rotation,
sessions opened before it must be reopened to push; pending Teams approval cards (`teams`) must
be re-requested. Run data-key rotations when no admin is editing secrets (the files are rewritten).
Old transit key versions stay decryptable (backups taken before a `--master` rotation still
restore) unless you raise `min_decryption_version` yourself — do it only after the retention
window of your backups.

Journal: `secrets-ops.log` + audit `secrets.rotate` (contexts, counts, transit version).

## 6. OpenBao in the Helm chart (one node)

`openbao.enabled=true` + `secrets.provider=openbao` (`values-sks.yaml` does it): a StatefulSet
`<release>-sokkan-openbao` (file storage on a 1 Gi PVC kept on uninstall, non-root, read-only
root filesystem, `disable_mlock`, NetworkPolicy: only the api and the configure Job reach it),
kubernetes auth (ClusterRoleBinding `system:auth-delegator` for its ServiceAccount — the chart's
only cluster-scoped object; `openbao.authDelegator=false` if your platform team binds it).

Minimal operating procedure:

1. **Init** (once): `kubectl exec -it <rel>-sokkan-openbao-0 -- bao operator init -key-shares=5 -key-threshold=3`.
   The 5 unseal keys go to 5 different people / safes (Vaultwarden collections, paper); the root
   token is used in step 3 then revoked. Nobody stores all keys together.
2. **Unseal** — after every pod restart: `bao operator unseal` ×3 (three key holders). Until then
   the pod is not Ready and secret reads in SOKKAN answer 503; the cockpit runs.
3. **Configure**: create an operator token (`bao token create -policy=root -ttl=1h` or a narrower
   admin policy), `kubectl create secret generic sokkan-openbao-operator --from-literal=token=…`,
   `helm upgrade … --set openbao.configure.enabled=true` (the post-upgrade Job runs
   `openbao-configure.sh`: mounts, transit key, policies, kubernetes role bound to the api's
   ServiceAccount), then **delete the Secret** and revoke the token.
4. **Backups of OpenBao itself**: the file backend → snapshot the PVC (CSI snapshot) while sealed
   or stopped, or move to the raft backend and use `bao operator raft snapshot save`. Without
   OpenBao (or its snapshot) and the unseal keys, a SOKKAN backup in openbao mode is unreadable —
   by design.
5. **Monitoring**: readiness = unsealed; alert on the pod not Ready > 5 min; `GET /v1/sys/health`.
6. **Upgrade**: change `openbao.image.tag`, the pod restarts → unseal again.

Out of scope here: auto-unseal (KMS / HSM / transit seal), HA (raft, 3 nodes), audit device —
for production with SLAs, use the customer's OpenBao/Vault cluster (§ 3).

## 7. Kubernetes provider

`secrets.provider=kubernetes`: the api gets a Role `get/list/create/update/patch` on `secrets` in
its namespace (RBAC cannot restrict create/list by name — **install SOKKAN in its own
namespace**). Secrets: `<prefix>-vault-<project>` (labels `sokkan.ch/vault=true`,
`sokkan.ch/instance=<prefix>`), `<prefix>-data-keys`. Protection = the cluster's etcd encryption;
ask the platform team whether a KMS provider is configured. SOKKAN writes these Secrets itself
(do not let an External Secrets Operator manage the same names).

## 8. Backups

| Provider | `backup.sh` writes | Restore needs |
|---|---|---|
| file | `data.tgz` **without any key file** (3.3 also keeps `forge.key` / `teams.key` out — they were in clear in `data.tgz` in 3.2) + `<key>.enc` per key file with the passphrase (or `VAULT_KEY_NOT_INCLUDED.txt`) | the passphrase, or `--vault-key` / `--keys-dir` |
| openbao (detected: `*.key.wrapped` on the volume) | `data.tgz` (wrapped keys = ciphertexts), `secrets.openbao.json` (every KV secret encrypted by transit), `OPENBAO_REQUIRED.txt`; **no key at all**, `--include-plain-key` refused | the same OpenBao (or its snapshot) with the transit key, and an identity allowed to decrypt |
| kubernetes | `data.tgz` + `KUBERNETES_SECRETS_NOT_INCLUDED.txt` | the cluster's backup of the Secrets |

`restore.sh` in openbao mode first unwraps every wrapped key of the set with the configured
OpenBao — **nothing is touched if one fails** — then restores; `--import-secrets` puts the KV
values of `secrets.openbao.json` back (missing names; `--overwrite-secrets` for all). Tested end
to end against a real OpenBao: backup → data volume and KV wiped → restore with another
instance's AppRole refused untouched → restore → every secret, model key, forge token readable.

## 9. Tests

`tests/test_secrets_provider.py`: one contract for the 3 providers (CRUD, contexts isolated,
data-key rotation, idempotent import, health, no key on disk); the Kubernetes API is a fake
(`tests/fake_k8s.py`, Secrets + TokenReview). With a real OpenBao:

```bash
docker run -d --name sokkan-bao-test -p 127.0.0.1:58200:8200 openbao/openbao:2.4.1 \
  server -dev -dev-root-token-id=root -dev-listen-address=0.0.0.0:8200
SOKKAN_TEST_OPENBAO_ADDR=http://127.0.0.1:58200 SOKKAN_TEST_OPENBAO_TOKEN=root \
SOKKAN_TEST_OPENBAO_CONTAINER=sokkan-bao-test pytest tests/test_secrets_provider.py
docker rm -f sokkan-bao-test
```

(KV layout and policy isolation, AppRole renewal and re-login, Kubernetes auth through
TokenReview, namespaces, migration both ways + export/import, master rotation, backup → restore,
the chart's configure script.) Chart: `tests/test_helm_chart.py` (OpenBao on/off, external
Vault, kubernetes provider, refusals, no root anywhere).
