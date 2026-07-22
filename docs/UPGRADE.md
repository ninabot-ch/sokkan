# Upgrading & rolling back SOKKAN

SOKKAN checks `https://sokkan.ch/dist/VERSION` once a day and tells you in
**Profile** when a newer release is out. How you apply it depends on how you run
SOKKAN.

Your data — the SQLite state, the memory index, transcripts — lives in the
`sokkan-data` Docker volume and in your `SOKKAN_WORKSPACE`. **Upgrades and
rollbacks never touch it**; only the application code is replaced.

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

## Managed cloud

Nothing to run — updates are handled for you:

- **Automatic**: each new release is rolled out to the managed fleet.
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
