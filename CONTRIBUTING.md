# Contributing to SOKKAN

Thanks for considering a contribution — SOKKAN is young and feedback from
people who actually run parallel agent sessions is the most valuable input
we can get.

## Dev setup

```bash
git clone https://github.com/ninabot-ch/sokkan && cd sokkan

# backend + memory (Python 3.12)
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt -r memory/requirements.txt -r requirements-dev.txt

# frontend
cd frontend && npm ci && cd ..
```

Run the whole stack the same way users do:

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN
docker compose up -d --build
```

Or run the backend alone for fast iteration:

```bash
SOKKAN_DATA_DIR=/tmp/sokkan-dev uvicorn app:app --app-dir backend --port 8097 --reload
```

## Before you open a PR

```bash
ruff check backend memory tests   # lint
pytest -q                         # unit tests
cd frontend && npx tsc --noEmit   # typecheck
```

CI runs the same three + a docker smoke (`compose up` + `/api/health`).

- Keep commits atomic, one concern per commit, conventional messages
  (`fix:`, `feat:`, `chore:`, `docs:`, `ci:`, `test:`).
- Don't break `docker compose up --build` — it is the product.
- User-facing API messages are in English.
- New behavior that touches the security model (auth, roles, feature flags,
  the preview SSRF policy) needs a test.

## Where help is welcome

- Provider adapters (the session protocol is provider-neutral; Codex is next)
- i18n of the web UI (currently French-first — an English pass is planned)
- Memory pipeline: chunking, ranking, note curation tooling
- Docs and examples of real memory-note workflows

## Reporting security issues

Please do NOT open a public issue for vulnerabilities — email
security@ninabot.ch and we'll respond quickly.
