#!/usr/bin/env python3
"""Regenerate contract.json from a checkout of sokkan-cloud — never by hand.

    SOKKAN_CLOUD_SRC=/path/to/sokkan-cloud python tests/fixtures/gateway_contract/regenerate.py

Plays the gateway's own app (infer/gateway.py) in-process on a throw-away database, upstream
replaced by a canned answer, tenant set as in § 2 of infer/pilote-etude.md (study
heg-eskills-2027, audit_required, expires 2027-04-30, max_users_day 5), and writes what it
ANSWERED: status, body and the headers that matter (`x-sokkan-error`, `x-should-retry`,
`retry-after`) of the four study refusals and of the tenant-wide 429. The `GET /usage` block is
the example of the page, verbatim. Needs the gateway's dependencies (fastapi, httpx)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SOKKAN_CLOUD_SRC") or "")
if not (SRC / "infer" / "gateway.py").exists():
    sys.exit("SOKKAN_CLOUD_SRC must point at a checkout of sokkan-cloud")
os.environ.update(SOKKAN_INFER_UPSTREAM_KEY="up", SOKKAN_INFER_ADMIN_TOKEN="adm", SOKKAN_INFER_JUDGE="",
                  SOKKAN_INFER_DB=tempfile.mktemp(suffix=".db"))
sys.path.insert(0, str(SRC / "infer"))
import coding  # noqa: E402
import gateway  # noqa: E402
import study  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ADM = {"Authorization": "Bearer adm"}
PILOT = {"study_id": "heg-eskills-2027", "study_mode": True, "audit_required": True, "expires_at": "2027-04-30",
         "tokens_day": 6_000_000, "tokens_month": 120_000_000, "deep_per_user_day": 20, "max_users_day": 5}
KEPT = ("x-sokkan-error", "x-should-retry", "retry-after")
# the words of each refusal as they stand in the source (the test looks for them in gateway.py)
WORDS = {
    "study_expired": ["The free inference period for this environment ended on ",
                      ". Nothing was sent to a model. Contact your SOKKAN ",
                      "administrator to extend it or to switch to a paid plan (same token)."],
    "study_mode_required": ["Free inference for this environment is tied to a study and is only served ",
                            "while the instance runs in study mode (audited). Turn study mode on in the ",
                            "instance settings, or contact your SOKKAN administrator. Nothing was sent ",
                            "to a model."],
    "user_daily_cap": ["personal daily inference cap reached for this user in this environment — ",
                       "resets at midnight UTC (other users are not affected)"],
    "user_limit_reached": ["too many distinct users today for this environment (limit: ",
                           " per day) — users already seen today are not affected; ",
                           "ask your SOKKAN administrator to raise the limit. Nothing was sent to a model."],
}


class _Up:
    status_code, text = 200, "{}"

    def json(self):
        return {"id": "x", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 200}}


async def _post(cand, oai, **kw):
    return _Up()


def _fresh(**patch) -> tuple[TestClient, str]:
    gateway.DB = Path(tempfile.mktemp(suffix=".db"))
    gateway._alerted.clear()
    study.now = lambda: datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    c = TestClient(gateway.app)
    tok = c.post("/admin/tenant", json={"client": "eskills", "house": 1}, headers=ADM).json()["token"]
    assert c.patch("/admin/tenant/eskills", json={**PILOT, **patch}, headers=ADM).status_code == 200
    return c, tok


def _ask(c, tok, user="P1", sid="heg-eskills-2027"):
    h = {"x-api-key": tok, "x-sokkan-user": user}
    if sid:
        h["x-sokkan-study"] = sid
    return c.post("/v1/messages", headers=h, json={"model": "sokkan-ship", "max_tokens": 16, "stream": True,
                                                   "messages": [{"role": "user", "content": "bonjour"}]})


def _seen(r) -> dict:
    return {"status": r.status_code, "headers": {k: r.headers[k] for k in KEPT if k in r.headers}, "body": r.json()}


def main() -> None:
    coding.C_KEY, coding.JUDGE, coding.DIFFICULTY_ESCALATION = "k", None, False
    coding.PROVIDERS = [{"name": "p", "url": "u", "key": "k", "models": {t: coding.TIERS[t]["model"] for t in coding.TIERS}}]
    coding._post_json = _post
    played = {}
    c, tok = _fresh()
    study.now = lambda: datetime(2027, 5, 1, tzinfo=timezone.utc)
    played["study_expired"] = _seen(_ask(c, tok))
    c, tok = _fresh()
    played["study_mode_required"] = _seen(_ask(c, tok, sid=""))
    c, tok = _fresh(user_tokens_day=1000)
    assert c.post("/v1/messages", headers={"x-api-key": tok, "x-sokkan-user": "P1", "x-sokkan-study": PILOT["study_id"]},
                  json={"model": "sokkan-ship", "max_tokens": 16,
                        "messages": [{"role": "user", "content": "bonjour"}]}).status_code == 200
    played["user_daily_cap"] = _seen(_ask(c, tok))
    c, tok = _fresh(user_tokens_day=1_000_000)
    for i in range(1, 6):
        assert c.post("/v1/messages", headers={"x-api-key": tok, "x-sokkan-user": f"P{i}", "x-sokkan-study": PILOT["study_id"]},
                      json={"model": "sokkan-ship", "max_tokens": 16,
                            "messages": [{"role": "user", "content": "bonjour"}]}).status_code == 200
    played["user_limit_reached"] = _seen(_ask(c, tok, user="P6"))          # the sixth identity of the day
    c, tok = _fresh(tokens_day=1000)
    assert c.post("/v1/messages", headers={"x-api-key": tok, "x-sokkan-user": "P1", "x-sokkan-study": PILOT["study_id"]},
                  json={"model": "sokkan-ship", "max_tokens": 16,
                        "messages": [{"role": "user", "content": "bonjour"}]}).status_code == 200
    tenant_cap = _seen(_ask(c, tok))

    doc = (SRC / "infer" / "pilote-etude.md").read_text(encoding="utf-8")
    usage = json.loads(re.search(r"### `GET /usage`.*?```json\n(.*?)```", doc, re.S).group(1))
    gw = (SRC / "infer" / "gateway.py").read_text(encoding="utf-8")
    refusals = []
    for code, seen in played.items():
        err = seen["body"]["error"]
        assert err["code"] == code == seen["headers"]["x-sokkan-error"], (code, seen)
        assert all(w in gw for w in WORDS[code]), code
        refusals.append({"status": seen["status"], "type": err["type"], "code": code, "message": err["message"],
                         "headers": seen["headers"], "words_in_source": WORDS[code], "body": seen["body"]})
    assert "code" not in tenant_cap["body"]["error"] and tenant_cap["status"] == 429
    git = lambda *a: subprocess.run(["git", *a], cwd=SRC, capture_output=True, text=True).stdout.strip()  # noqa: E731
    out = {
        "_source": (f"sokkan-cloud branch {git('rev-parse', '--abbrev-ref', 'HEAD')} ({git('rev-parse', '--short', 'HEAD')}"
                    f"{', tree not clean' if git('status', '--porcelain', '--untracked-files=no') else ''}), written by "
                    "tests/fixtures/gateway_contract/regenerate.py: `usage` is the `GET /usage` example of "
                    "infer/pilote-etude.md § 4, verbatim; the four study refusals and the tenant-wide 429 are what "
                    "the gateway's app ANSWERED when played in-process on a throw-away database (tenant set as in "
                    "§ 2 of the page: study heg-eskills-2027, audit_required, expires 2027-04-30, max_users_day 5; "
                    "sixth identity of the day for user_limit_reached; clock set after the end date for the 410). "
                    "`headers` holds, of each answer, x-sokkan-error, x-should-retry and retry-after WHEN SENT: "
                    "since study-no-retry the four study refusals carry `x-should-retry: false`, the tenant-wide "
                    "429 carries none of the three. Regenerate with the script, never by hand; "
                    "tests/test_pilot_integration.py compares this file with the checkout when one is at hand "
                    "(SOKKAN_CLOUD_SRC, else /root/sokkan-cloud-wt-noretry)."),
        "usage": usage,
        "headers_sent": ["x-sokkan-user", "x-sokkan-study"],
        "error_header": "x-sokkan-error",
        "no_retry_header": "x-should-retry",
        "refusals": refusals,
        "tenant_cap": tenant_cap,
    }
    (HERE / "contract.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(out["_source"][:80], "…", [(r["status"], r["code"], r["headers"]) for r in refusals], tenant_cap["headers"])


if __name__ == "__main__":
    main()
