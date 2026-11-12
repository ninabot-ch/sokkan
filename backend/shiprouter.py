#!/usr/bin/env python3
"""shiprouter.py — SOKKAN appliance: the local Ship → Deep router.

On an on-prem box the everyday model (« Ship ») runs ON the machine (vLLM, which serves the
Anthropic Messages API at /v1/messages next to the OpenAI one). Sessions talk to THIS small
service instead of the model, so that:

* a request that needs the stronger model (« Deep ») leaves for the operator gateway
  (`SOKKAN_DEEP_URL`, with the instance's token) — and only those requests leave the box;
* when the local model is down, the session still gets an answer (Deep, if allowed) or a
  message a person can read — never a spinner;
* 402 / 429 / unreachable come back as readable text (backend/llmerrors.py).

When does a request go to Deep (`decide`) — same principle as the hosted gateway, decided
again at every request, never sticky:
  requested   the caller asked for the Deep model by name, or sent `x-sokkan-tier: deep`
  keyword     the last human message contains one of SOKKAN_ESCALATION_KEYWORDS
  fails       the tool loop ends with >= SOKKAN_ESCALATION_MAX_FAILS failed tool results
  repeat      the same tool error came back >= SOKKAN_ESCALATION_REPEAT_FAILS times in a row
  size        the prompt is larger than SOKKAN_ESCALATION_MIN_PROMPT_TOKENS (0 = never)
  ship_down   Ship did not answer (connection, timeout, 5xx) and SOKKAN_SHIP_FALLBACK_DEEP=1
Nothing goes to Deep when SOKKAN_DEEP_URL / SOKKAN_DEEP_TOKEN are not set, or when
SOKKAN_ESCALATION=0 (then only `requested` and `ship_down` remain).

Side calls of the CLI (`side_call`: sent by the CLI, no tool declared — the title of the
session, a summary…) are not the person asking: they stay on the local model whatever they
carry (reason `side_call`), and are not sent to Deep when it is down either. The title call
holds the person's first message: with « /deep » in it, one question used to count two Deep
escalations of the person's day on the gateway.

« Write with Claude » (3.6.5, tier `claude`, backend/claudetier.py): a request goes to the
Anthropic API ONLY when its session carries `x-sokkan-tier: claude` — the header the instance
puts on the session of a work a person started with « Write with Claude ». Never by keyword,
size, failure, or when the local model is down (`ship_down` goes to Deep or fails, never to
Claude); side calls stay on the local model. Before each request the router asks the api
(`GET /api/internal/claude-grant`, `SOKKAN_ROUTER_SECRET`) whether THIS project may still use
Claude and with which models, and gets the organisation's key, kept in memory a few seconds.
No key, project classified too high, model outside the list, api unreachable → a refusal a
person can read (`llmerrors`, `x-should-retry: false`), and nothing is sent anywhere else.
Towards Anthropic: the key as `x-api-key` with `anthropic-version`, no `authorization`, and no
`x-sokkan-*` header (who typed the request, the project, the study stay on the box).

Run: `uvicorn shiprouter:app --host 0.0.0.0 --port 8096 --app-dir /app/backend` (the `router`
service of the appliance profile). It keeps no content: `/stats` and the log carry tier,
reason, status and duration only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import llmerrors
from features import env_num

log = logging.getLogger("sokkan.shiprouter")
app = FastAPI(title="SOKKAN ship router", docs_url=None, redoc_url=None, openapi_url=None)

_stats: Counter = Counter()
_last: dict = {"ts": None, "tier": None, "reason": None, "status": None}
_BOOT = time.time()


SECRET_FILE = "/run/sokkan-router/secret"   # the api's entrypoint writes it (3.6.6)


def _secret_file() -> str:
    try:
        with open(SECRET_FILE, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return ""


def cfg() -> dict:
    """Read at every request: an operator edits .env and recreates the container, a test
    patches the environment."""
    e = os.environ.get
    kw = e("SOKKAN_ESCALATION_KEYWORDS")
    return {
        "ship_url": (e("SOKKAN_SHIP_URL") or "").rstrip("/"),
        "ship_model": e("SOKKAN_SHIP_MODEL") or "",
        "ship_token": e("SOKKAN_SHIP_TOKEN") or "",
        "ship_timeout": env_num("SOKKAN_SHIP_TIMEOUT_S", 600.0, float),
        "ship_connect": env_num("SOKKAN_SHIP_CONNECT_TIMEOUT_S", 5.0, float),
        "deep_url": (e("SOKKAN_DEEP_URL") or "").rstrip("/"),
        "deep_token": e("SOKKAN_DEEP_TOKEN") or "",
        "deep_model": e("SOKKAN_DEEP_MODEL") or "sokkan-deep",
        "escalation": (e("SOKKAN_ESCALATION") or "1").strip().lower() not in ("0", "false", "no", "off"),
        "keywords": tuple(k.strip().lower() for k in (kw if kw is not None and kw.strip() else "/deep,#deep").split(",")
                          if k.strip()),
        "max_fails": env_num("SOKKAN_ESCALATION_MAX_FAILS", 3),
        "repeat_fails": env_num("SOKKAN_ESCALATION_REPEAT_FAILS", 2),
        "min_prompt_tokens": env_num("SOKKAN_ESCALATION_MIN_PROMPT_TOKENS", 0),
        "fallback": (e("SOKKAN_SHIP_FALLBACK_DEEP") or "1").strip().lower() not in ("0", "false", "no", "off"),
        "forward_user": (e("SOKKAN_DEEP_FORWARD_USER") or "0").strip() == "1",
        "claude_url": (e("SOKKAN_CLAUDE_URL") or "https://api.anthropic.com").rstrip("/"),
        "router_secret": (e("SOKKAN_ROUTER_SECRET") or "").strip() or _secret_file(),
        "api_url": (e("SOKKAN_ROUTER_API_URL") or "http://api:8097").rstrip("/"),
    }


def deep_ready(c: dict) -> bool:
    return bool(c["deep_url"] and c["deep_token"])


# ---- reading the conversation (never stored, never logged) ----------------------------
def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(str(b.get("text") or ""))
            elif b.get("type") == "tool_result":
                out.append(_flatten(b.get("content")))
    return "\n".join(out)


def last_human_text(payload: dict) -> str:
    """Text typed by the person in the LAST user message (tool results are not human text)."""
    for m in reversed(payload.get("messages") or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        texts = [str(b.get("text") or "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text"]
        if texts:
            return "\n".join(texts)
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c or []):
            continue        # a tool round: keep looking for what the person last wrote
        return ""
    return ""


def loop_stats(payload: dict) -> dict:
    """Trailing tool loop: failed tool results since the last human text, and how many times
    the SAME error came back in a row at the end."""
    fails, errors = 0, []
    for m in reversed(payload.get("messages") or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        blocks = [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if not results:
            break                                    # the person spoke: the loop starts after
        for b in results:
            if b.get("is_error"):
                fails += 1
                errors.append(_flatten(b.get("content"))[:300])
            else:
                errors.append(None)
    repeat = 0
    if errors and errors[0] is not None:
        for e in errors:
            if e is not None and e == errors[0]:
                repeat += 1
            else:
                break
    return {"fails": fails, "repeat": repeat}


def approx_tokens(payload: dict) -> int:
    n = len(_flatten(payload.get("system")) if not isinstance(payload.get("system"), str)
            else payload.get("system") or "")
    for m in payload.get("messages") or []:
        n += len(_flatten(m.get("content")))
    return n // 4


def side_call(payload: dict, headers) -> bool:
    """A call the CLI makes for itself next to the conversation: it comes from the CLI
    (`x-app: cli`, user agent `claude-cli/…`) and declares NO tool, where every request of
    the conversation loop declares them (28 on the capture of 11.11.2026, sub-agents too).
    Seen: the title of the session (`tools: []`, the person's first message between
    <session> tags — tests/fixtures/cli_calls/). Same shape, not captured here: the summary
    of a fetched page, of a tool run, the check of a shell command. A request without tools
    from anything else (the cockpit's assistant, a script) is somebody asking: not one."""
    if payload.get("tools"):
        return False
    ua = (headers.get("user-agent") or "").strip().lower()
    return (headers.get("x-app") or "").strip().lower() == "cli" or ua.startswith("claude-cli/")


def decide(payload: dict, headers, c: dict | None = None) -> tuple[str, str]:
    """(tier, reason) for this request. tier = "ship" | "deep" | "claude"."""
    c = c or cfg()
    if (headers.get("x-sokkan-tier") or "").strip().lower() == "claude":
        # the explicit gesture « Write with Claude » — and nothing else ever leads there. The
        # CLI's own side calls (title of the session…) stay on the box when it has a model.
        if side_call(payload, headers) and c["ship_url"]:
            return "ship", "side_call"
        return "claude", "requested"
    if not deep_ready(c):
        return "ship", "deep_not_configured"
    if side_call(payload, headers):
        # never Deep, by name, by keyword, by size or by failure: not an escalation of the
        # person's day. A box WITHOUT local model has nowhere else to send it.
        return ("ship", "side_call") if c["ship_url"] else ("deep", "no_local_model")
    asked = (headers.get("x-sokkan-tier") or "").strip().lower()
    model = str(payload.get("model") or "")
    if asked == "deep" or model in (c["deep_model"], "sokkan-deep"):
        return "deep", "requested"
    if asked == "ship" or not c["escalation"]:
        return "ship", "default"
    text = last_human_text(payload).lower()
    if text and any(k in text for k in c["keywords"]):
        return "deep", "keyword"
    if c["min_prompt_tokens"] and approx_tokens(payload) >= c["min_prompt_tokens"]:
        return "deep", "size"
    st = loop_stats(payload)
    if c["max_fails"] and st["fails"] >= c["max_fails"]:
        return "deep", "fails"
    if c["repeat_fails"] and st["repeat"] >= c["repeat_fails"]:
        return "deep", "repeat"
    return "ship", "default"


# ---- forwarding ----------------------------------------------------------------------
_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "upgrade",
        "proxy-authorization", "proxy-authenticate", "trailer", "authorization", "x-api-key",
        "x-sokkan-tier", "accept-encoding", "cookie", "x-sokkan-project"}


_PSEUDONYM_RE = re.compile(r"^(?:[a-z][a-z0-9_-]{0,23}:)?P[0-9]{1,6}$")
# refusals of the gateway that are about the STUDY (infer/pilote-etude.md § Refus): their own
# words are the readable message (backend/study.explain_refusal recognises them)
STUDY_REFUSALS = ("study_expired", "study_mode_required", "user_daily_cap", "user_limit_reached")


def _pseudonymous(headers: dict) -> bool:
    """Study mode: `x-sokkan-study` names the study and `x-sokkan-user` is a pseudonym
    (never an address). Anything else is treated as a person's identity."""
    low = {k.lower(): v for k, v in headers.items()}
    return bool((low.get("x-sokkan-study") or "").strip()) and bool(
        _PSEUDONYM_RE.match((low.get("x-sokkan-user") or "").strip()))


def study_refusal(status: int, raw: str, header: str = "") -> str:
    """The study code of a gateway refusal ('' when it is not one)."""
    if status not in (410, 412, 429):
        return ""
    code = (header or "").strip()
    if code not in STUDY_REFUSALS:
        try:
            code = str(((json.loads(raw) or {}).get("error") or {}).get("code") or "")
        except (ValueError, AttributeError):
            code = ""
    return code if code in STUDY_REFUSALS else ""


def _upstream(tier: str, c: dict, inbound) -> tuple[str, dict]:
    h = {k: v for k, v in inbound.items() if k.lower() not in _HOP}
    if tier == "deep":
        h["x-api-key"] = c["deep_token"]
        h["authorization"] = f"Bearer {c['deep_token']}"
        if not c["forward_user"] and not _pseudonymous(h):
            # who typed the request stays on the box unless the operator says otherwise —
            # in study mode the instance sends a PSEUDONYM (P1, P2…) with the study: that one
            # goes to the gateway, which meters per person and caps Deep per person
            h = {k: v for k, v in h.items() if k.lower() not in ("x-sokkan-user", "anthropic-custom-headers")}
        return c["deep_url"] + "/v1/messages", h
    h = {k: v for k, v in h.items() if k.lower() != "x-sokkan-user"}
    tok = c["ship_token"] or "local"
    h["x-api-key"] = tok
    h["authorization"] = f"Bearer {tok}"
    return c["ship_url"] + "/v1/messages", h


def _body(tier: str, payload: dict, c: dict) -> bytes:
    p = dict(payload)
    p["model"] = c["deep_model"] if tier == "deep" else (c["ship_model"] or p.get("model") or "")
    return json.dumps(p, ensure_ascii=False).encode()


def _error(code: str, tier: str, reason: str, detail: str = "") -> Response:
    _stats[f"error:{code}"] += 1
    # `x-should-retry: false` — the CLI (Anthropic SDK) retries 429 and 5xx by itself, up to ten
    # times with a growing pause: the person watched a spinner for minutes and never read the
    # message (seen on the test bench: 150 s without a word on a 429). The router has already
    # done what can be done (fallback to Deep); what is left is for a person to read and decide.
    return Response(llmerrors.dumps(code, detail=detail), status_code=llmerrors.HTTP_STATUS[code],
                    media_type="application/json",
                    headers={"x-sokkan-tier": tier, "x-sokkan-reason": reason, "x-sokkan-error": code,
                             "x-should-retry": "false"})


def _client(c: dict) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(c["ship_timeout"], connect=c["ship_connect"]))


async def _open(client: httpx.AsyncClient, tier: str, payload: dict, c: dict, inbound):
    """Send to one tier; returns (response | None, status | None, error text)."""
    url, headers = _upstream(tier, c, inbound)
    headers["content-type"] = "application/json"
    try:
        req = client.build_request("POST", url, content=_body(tier, payload, c), headers=headers)
        r = await client.send(req, stream=True)
    except (httpx.HTTPError, OSError) as e:
        return None, None, type(e).__name__
    return r, r.status_code, ""


# ---- « Write with Claude » ----------------------------------------------------------------
_grants: dict[tuple[str, str], tuple[float, dict]] = {}
GRANT_TTL_S = 10.0


async def claude_grant(c: dict, project: str, model: str) -> dict | None:
    """The api's decision for (project, model), with the key when allowed. None = the api
    could not be asked (no secret, unreachable, not the router's api). A few seconds of cache
    so that a tool loop does not ask at every request; a refusal is never cached."""
    if not c["router_secret"] or not project:
        return None
    now = time.time()
    hit = _grants.get((project, model))
    if hit and now - hit[0] < GRANT_TTL_S:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as cl:
            r = await cl.get(c["api_url"] + "/api/internal/claude-grant",
                             params={"project": project, "model": model},
                             headers={"x-sokkan-router-secret": c["router_secret"]})
    except (httpx.HTTPError, OSError):
        return None
    if r.status_code != 200:
        return None
    try:
        g = r.json()
    except ValueError:
        return None
    if not isinstance(g, dict):
        return None
    if g.get("allowed"):
        _grants[(project, model)] = (now, g)
    else:
        _grants.pop((project, model), None)
    return g


_CLAUDE_REFUSAL = {"no_key": "claude_no_key", "model": "claude_model"}   # else claude_forbidden


async def _claude(payload: dict, inbound, c: dict, t0: float) -> Response:
    project = (inbound.get("x-sokkan-project") or "").strip()
    model = str(payload.get("model") or "")
    g = await claude_grant(c, project, model)
    reason = "requested"
    if g is None:
        code = "claude_unavailable"
    elif not g.get("allowed"):
        code = _CLAUDE_REFUSAL.get(str(g.get("code") or ""), "claude_forbidden")
    elif model not in (g.get("models") or []) or not g.get("key"):
        code = "claude_model" if g.get("key") else "claude_no_key"
    else:
        code = ""
    if code:
        _last.update(ts=int(time.time()), tier="claude", reason=code, status=None)
        _stats["claude:refused"] += 1
        log.info("tier=claude reason=%s status=refused ms=%d", code, (time.time() - t0) * 1000)
        return _error(code, "claude", "refused")
    h = {k: v for k, v in inbound.items()
         if k.lower() not in _HOP and not k.lower().startswith("x-sokkan-")
         and k.lower() != "anthropic-custom-headers"}
    h["x-api-key"] = g["key"]
    h.setdefault("anthropic-version", "2023-06-01")    # the CLI sends its own; a script may not
    # the stream is relayed byte for byte without its content-encoding: ask for it plain —
    # httpx asks gzip by default, Anthropic honours it, and the CLI read « Unrecognized token
    # '\x1f' » (the gzip magic) on its first answer (seen 12.11.2026)
    h["accept-encoding"] = "identity"
    h["content-type"] = "application/json"
    client = _client(c)
    try:
        req = client.build_request("POST", c["claude_url"] + "/v1/messages",
                                   content=json.dumps(payload, ensure_ascii=False).encode(), headers=h)
        r = await client.send(req, stream=True)
    except (httpx.HTTPError, OSError):
        await client.aclose()
        _stats["claude:unreachable"] += 1
        return _error("claude_unavailable", "claude", reason)
    status = r.status_code
    _last.update(ts=int(time.time()), tier="claude", reason=reason, status=status)
    _stats[f"claude:{reason}"] += 1
    log.info("tier=claude reason=%s status=%s ms=%d", reason, status, (time.time() - t0) * 1000)
    if status >= 400:
        raw = (await r.aread())[:4000].decode("utf-8", "replace")
        await r.aclose()
        await client.aclose()
        if status in (401, 403):
            return _error("claude_auth", "claude", reason)
        code = llmerrors.classify(status, raw)
        if code:
            return _error(code, "claude", reason)
        # the caller's own error (a bad request): handed back, never retried by the CLI
        return Response(raw, status_code=status, media_type=r.headers.get("content-type", "application/json"),
                        headers={"x-sokkan-tier": "claude", "x-sokkan-reason": reason,
                                 "x-should-retry": "false"})

    async def relay():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "cache-control", "request-id")}
    keep.update({"x-sokkan-tier": "claude", "x-sokkan-reason": reason})
    return StreamingResponse(relay(), status_code=status, headers=keep)


@app.post("/v1/messages")
async def messages(request: Request):
    t0 = time.time()
    c = cfg()
    try:
        payload = json.loads(await request.body() or b"{}")
        assert isinstance(payload, dict)
    except (ValueError, AssertionError):
        return JSONResponse({"type": "error", "error": {"type": "invalid_request_error",
                                                        "message": "body must be a JSON object"}}, 400)
    if not c["ship_url"] and not deep_ready(c) \
            and (request.headers.get("x-sokkan-tier") or "").strip().lower() != "claude":
        return _error("unreachable", "none", "not_configured", "no SOKKAN_SHIP_URL, no Deep gateway")
    tier, reason = decide(payload, request.headers, c)
    if tier == "claude":
        return await _claude(payload, request.headers, c, t0)
    if tier == "ship" and not c["ship_url"]:
        tier, reason = "deep", "no_local_model"
    client = _client(c)
    r, status, err = await _open(client, tier, payload, c, request.headers)
    ship_failed = tier == "ship" and (r is None or status >= 500)
    if ship_failed and c["fallback"] and deep_ready(c) and reason != "side_call":
        if r is not None:
            await r.aclose()
        _stats["ship_down"] += 1
        tier, reason = "deep", "ship_down"
        r, status, err = await _open(client, "deep", payload, c, request.headers)
    _last.update(ts=int(time.time()), tier=tier, reason=reason, status=status)
    _stats[f"{tier}:{reason}"] += 1
    log.info("tier=%s reason=%s status=%s ms=%d", tier, reason, status, (time.time() - t0) * 1000)
    if r is None:
        await client.aclose()
        return _error("unreachable", tier, reason, err)
    if status >= 400:
        raw = (await r.aread())[:4000].decode("utf-8", "replace")
        await r.aclose()
        await client.aclose()
        refusal = study_refusal(status, raw, r.headers.get("x-sokkan-error") or "")
        if refusal:
            # 410 / 412 / 429 (a person's ceiling, too many people today) of the study: handed
            # back as the gateway said it
            # (status, code, its sentence), and never retried by the CLI — the gateway sends
            # `x-should-retry: false` itself since sokkan-cloud `study-no-retry`; one deployed
            # before does not, and the CLI retries a bare 429 for minutes
            return Response(raw, status_code=status,
                            media_type=r.headers.get("content-type", "application/json"),
                            headers={"x-sokkan-tier": tier, "x-sokkan-reason": reason,
                                     "x-sokkan-error": refusal, "x-should-retry": "false"})
        code = llmerrors.classify(status, raw)
        if code:
            return _error(code, tier, reason)
        # an error that is the caller's (bad request…): hand it back untouched
        return Response(raw, status_code=status, media_type=r.headers.get("content-type", "application/json"),
                        headers={"x-sokkan-tier": tier, "x-sokkan-reason": reason})

    async def relay():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    keep = {k: v for k, v in r.headers.items()
            if k.lower() in ("content-type", "cache-control", "request-id", "anthropic-ratelimit-requests-remaining")}
    keep.update({"x-sokkan-tier": tier, "x-sokkan-reason": reason})
    return StreamingResponse(relay(), status_code=status, headers=keep)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Asked by the CLI before long prompts: the local model answers; an estimate otherwise
    (never a reason to fail a session)."""
    c = cfg()
    body = await request.body()
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        payload = {}
    if c["ship_url"]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=c["ship_connect"])) as cl:
                r = await cl.post(c["ship_url"] + "/v1/messages/count_tokens",
                                  content=_body("ship", payload, c),
                                  headers={"content-type": "application/json",
                                           "authorization": f"Bearer {c['ship_token'] or 'local'}"})
                if r.status_code == 200:
                    return Response(r.content, media_type="application/json")
        except (httpx.HTTPError, OSError):
            pass
    return {"input_tokens": approx_tokens(payload)}


@app.get("/v1/models")
def models():
    c = cfg()
    data = [{"id": c["ship_model"] or "sokkan-ship", "type": "model", "display_name": "Ship (local)"}]
    if deep_ready(c):
        data.append({"id": c["deep_model"], "type": "model", "display_name": "Deep (gateway)"})
    return {"data": data, "has_more": False}


async def probe() -> dict:
    """Is each tier reachable now? (no inference: /health of the local model, /health of the gateway)"""
    c = cfg()
    out = {"ship": None, "deep": None}
    async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=2.0)) as cl:
        for tier, base in (("ship", c["ship_url"]), ("deep", c["deep_url"] if deep_ready(c) else "")):
            if not base:
                continue
            try:
                out[tier] = (await cl.get(base + "/health")).status_code < 500
            except (httpx.HTTPError, OSError):
                out[tier] = False
    return out


@app.get("/health")
async def health():
    """200 as long as the router runs; `ship` / `deep` say what it can reach."""
    return {"ok": True, **(await probe())}


@app.get("/stats")
def stats():
    """Counters only — tier:reason → requests. No content, no user, no model output."""
    c = cfg()
    return {"uptime_s": int(time.time() - _BOOT), "counts": dict(_stats), "last": dict(_last),
            "ship_configured": bool(c["ship_url"]), "deep_configured": deep_ready(c),
            "escalation": c["escalation"], "fallback": c["fallback"],
            "claude_configured": bool(c["router_secret"])}
