"""teams.proactive — pending approvals pushed to the project's Teams channel (3.4.0).

What waits for a human in a project — an agent proposal or a pending change
(``agent.activate``), a tool call of an agent run waiting for its approval (``run.tool``) —
is posted as an Adaptive Card in every channel mapped to the project with ``approvals`` on.
Not only on demand (``@Nina approvals``): the moment it starts waiting.

* **Where**: ``POST {serviceUrl}/v3/conversations/{channel id}/activities`` (a new thread in
  the channel). The serviceUrl is the one Microsoft sent with the channel's last VERIFIED
  activity (``remember``), else ``SOKKAN_TEAMS_SERVICE_URL`` (default the global Teams
  endpoint) — always a Microsoft host (``botauth.service_url_ok``).
* **Idempotent**: one card per (approval, channel), recorded in ``teams.db`` (``proactive``);
  a sync twice posts nothing new.
* **Closed elsewhere**: when the approval stops waiting (decided in the cockpit, in Teams,
  the run ended…) the card is REPLACED (``PUT …/activities/{id}``) by its outcome and its
  signed token is spent — a late click on an old card does nothing.
* **Level**: an approval whose object is above the channel's level (a run whose session read
  confidential notes, in a « project » channel) is announced without its content: « an
  approval waits in <project> (level X) — open SOKKAN », no buttons.
* **Never blocking**: called after the fact (``poke`` from ``agents``, a periodic job), in a
  thread; a failure is logged and retried at the next sync.
"""
from __future__ import annotations

import json
import threading
import time

import teams
from teams import botauth, cards, connector, signing, store

DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/teams/"
_lock = threading.Lock()
_timer: threading.Timer | None = None
_timer_lock = threading.Lock()
# seconds between a change and the sync it triggers (coalesces bursts); 0 = inline (tests),
# None = hooks off (the periodic job still syncs)
DEBOUNCE_S: float | None = 2.0
_last: dict = {"at": None, "posted": 0, "closed": 0, "errors": []}


def interval_s() -> int:
    """SOKKAN_TEAMS_PROACTIVE_S: period of the safety-net sync (default 60, 0 = proactive off)."""
    import features
    return max(0, int(features.env_num("SOKKAN_TEAMS_PROACTIVE_S", 60)))


def active() -> bool:
    return teams.enabled() and not teams.configured() and interval_s() > 0


def state() -> dict:
    return {"on": active(), "interval_s": interval_s(), "last_sync": _last["at"],
            "posted": _last["posted"], "closed": _last["closed"], "errors": _last["errors"][-5:]}


# ---- where a channel is reached ------------------------------------------------------------
def remember(activity: dict) -> None:
    """Record the serviceUrl of a verified activity of a channel (botauth already checked it
    is the token's and a Microsoft host)."""
    from teams import bot
    cd = activity.get("channelData") or {}
    ch = (cd.get("channel") or {}).get("id")
    surl = activity.get("serviceUrl") or ""
    if not ch or not botauth.service_url_ok(surl):
        return
    c = store.con()
    with c:
        c.execute("INSERT OR REPLACE INTO conversations(channel_id, service_url, conversation_id,"
                  " updated_at) VALUES(?,?,?,?)", (bot.channel_key(activity), surl, ch, time.time()))
    c.close()


def _reach(channel_id: str) -> tuple[str, str]:
    c = store.con()
    r = c.execute("SELECT service_url, conversation_id FROM conversations WHERE channel_id=?",
                  (channel_id,)).fetchone()
    c.close()
    if r:
        return r["service_url"], r["conversation_id"]
    return teams.cfg("SERVICE_URL", DEFAULT_SERVICE_URL), channel_id


# ---- what waits ----------------------------------------------------------------------------
def _open_url(path: str) -> str:
    base = teams.public_url()
    return f"{base}{path}" if base.startswith("https://") else ""


def collect() -> dict[str, dict]:
    """Every approval waiting now, keyed by a stable item key."""
    import agents
    out: dict[str, dict] = {}
    con = agents._con()
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM agents WHERE status != 'archived'")]
        runs = con.execute("SELECT r.id, r.session_id, r.agent_id FROM runs r WHERE "
                           "r.status='running' AND r.waiting_approval=1").fetchall()
    finally:
        con.close()
    for aid in ids:
        a = agents.get(aid)
        if not a or not (a["status"] == "pending" or a.get("pending_change")):
            continue
        change = bool(a.get("pending_change"))
        trig = a.get("trigger")
        out[f"agent.activate:{aid}"] = {
            "kind": "agent.activate", "ref": str(aid), "project": a.get("project") or "default",
            "level": 2,
            "requested_by": (a.get("pending_change_by") if change else a.get("proposed_by"))
            or a.get("owner") or "",
            "title": (f"Apply the change to the agent {a['name']}?" if change
                      else f"Activate the agent {a['name']}?"),
            "facts": [("Project", a.get("project") or "default"), ("Owner", a.get("owner") or ""),
                      ("Proposed by", (a.get("pending_change_by") if change
                                       else a.get("proposed_by")) or ""),
                      ("Trigger", trig if isinstance(trig, str) else json.dumps(trig)),
                      ("Purpose", (a.get("purpose") or "")[:200])],
            "open": _open_url(f"/?plane=build&tab=crew&agent={aid}"),
        }
    if runs:
        import sharing
        from agents_runtime import _run_level
        for r in runs:
            a = agents.get(r["agent_id"]) or {}
            if not r["session_id"]:
                continue
            for p in sharing.pending_approvals(r["session_id"]):
                out[f"run.tool:{r['id']}:{p['id']}"] = {
                    "kind": "run.tool", "ref": f"{r['id']}:{p['id']}",
                    "project": a.get("project") or "default",
                    "level": _run_level(r["session_id"]), "requested_by": f"agent:{a.get('name', '')}",
                    "title": f"Let the agent {a.get('name', r['agent_id'])} use {p['tool']}?",
                    "facts": [("Project", a.get("project") or "default"),
                              ("Run", f"#{r['id']}"), ("Tool", p["tool"]),
                              ("Action", (p.get("title") or "")[:300])],
                    "open": _open_url(f"/?plane=build&tab=crew&agent={r['agent_id']}"),
                }
    return out


# ---- the sync --------------------------------------------------------------------------------
def _outcome(item_key: str, row: dict) -> tuple[str, str, str]:
    """(decision, by, detail) of an approval that no longer waits."""
    import agents
    if row["nonce"]:
        s = signing.row(row["nonce"])
        if s and s["used_at"] and s["decision"] in ("approve", "refuse"):
            return s["decision"], s["used_by"], "decided in Teams"
    if row["kind"] == "agent.activate":
        a = agents.get(int(row["ref"])) or {}
        if a.get("status") == "active" and a.get("approved_at") and \
                a["approved_at"] >= row["posted_at"] - 1:
            return "approve", a.get("approved_by") or "", "decided in SOKKAN"
        if a.get("status") == "draft":
            return "refuse", "", "sent back to draft in SOKKAN"
        return "closed", "", f"no longer waiting (agent {a.get('status', 'removed')})"
    return "closed", "", "decided in SOKKAN, or the run moved on"


def sync() -> dict:
    """Post what started waiting, replace what stopped. Returns counters."""
    if not active():
        return {"posted": 0, "closed": 0, "skipped": "inactive"}
    if not _lock.acquire(blocking=False):
        return {"posted": 0, "closed": 0, "skipped": "busy"}
    try:
        return _sync()
    finally:
        _lock.release()


def _sync() -> dict:
    from core import levels
    pending = collect()
    chans: dict[str, list[dict]] = {}
    for ch in store.channels():
        if ch.get("approvals", 1):
            chans.setdefault(ch["project"], []).append(ch)
    c = store.con()
    rows = {r["key"]: dict(r) for r in c.execute("SELECT * FROM proactive")}
    c.close()
    posted = closed = 0
    errors: list[str] = []
    for item_key, it in pending.items():
        for ch in chans.get(it["project"], []):
            key = f"{item_key}@{ch['channel_id']}"
            if key in rows and rows[key]["state"] == "open":
                continue
            surl, conv = _reach(ch["channel_id"])
            nonce = ""
            if it["level"] > int(ch["level"]):
                payload = connector.card(cards.notice(
                    "An approval waits", f"In « {it['project']} », at the level "
                    f"« {levels.label(it['level'])} » — above this channel's. Open SOKKAN to see "
                    "and decide it.", it["open"]), "An approval waits")
            else:
                spec = {"title": it["title"], "facts": it["facts"], "open_url": it["open"],
                        "note": "Decide here or in SOKKAN. You act as yourself, within your role."}
                tok = signing.issue(it["kind"], it["ref"], it["project"], it["requested_by"],
                                    card=spec)
                nonce = signing.peek(tok)["n"]
                payload = connector.card(cards.approval(it["title"], it["facts"], tok, spec["note"],
                                                        it["open"]), it["title"])
            try:
                res = connector.send(surl, conv, payload)
            except Exception as e:  # noqa: BLE001 — retried at the next sync
                if nonce:
                    signing.close(nonce, "SOKKAN", "unsent")
                errors.append(f"{ch['channel_id'][:40]}: {e}")
                print(f"[teams] proactive post failed ({item_key}): {e!r}", flush=True)
                continue
            c = store.con()
            with c:
                c.execute("INSERT OR REPLACE INTO proactive(key, item, kind, ref, project, "
                          "channel_id, service_url, conversation_id, activity_id, nonce, title, "
                          "level, state, posted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open',?)",
                          (key, item_key, it["kind"], it["ref"], it["project"], ch["channel_id"],
                           surl, conv, str(res.get("id") or ""), nonce, it["title"],
                           it["level"], time.time()))
            c.close()
            posted += 1
            import audit
            audit.log("teams", "teams.approval.post", item_key, ch["channel_id"][:120],
                      project=it["project"])
    for key, row in rows.items():
        if row["state"] != "open" or row["item"] in pending:
            continue
        decision, by, detail = _outcome(row["item"], row)
        if row["nonce"]:
            signing.close(row["nonce"], by or "SOKKAN")
        try:
            if row["activity_id"]:
                connector.update(row["service_url"], row["conversation_id"], row["activity_id"],
                                 connector.card(cards.decided(row["title"] or "Approval", decision,
                                                              by, detail), row["title"]))
        except Exception as e:  # noqa: BLE001 — the card stays as it was; its token is spent
            errors.append(f"update {key[:60]}: {e}")
            print(f"[teams] proactive update failed ({key}): {e!r}", flush=True)
        c = store.con()
        with c:
            c.execute("UPDATE proactive SET state='closed', closed_at=?, closed_note=? WHERE key=?",
                      (time.time(), f"{decision} {by} {detail}".strip(), key))
        c.close()
        closed += 1
    _last.update(at=time.time(), posted=posted, closed=closed, errors=(_last["errors"] + errors)[-20:])
    return {"posted": posted, "closed": closed, "errors": errors}


def poke() -> None:
    """Something that may wait for an approval changed: sync soon, in the background."""
    global _timer
    if DEBOUNCE_S is None:
        return
    try:
        if not active():
            return
    except Exception:  # noqa: BLE001
        return
    if DEBOUNCE_S == 0:
        _safe_sync()
        return
    with _timer_lock:
        if _timer is not None and _timer.is_alive():
            return
        _timer = threading.Timer(DEBOUNCE_S, _safe_sync)
        _timer.daemon = True
        _timer.start()


def _safe_sync() -> None:
    try:
        sync()
    except Exception as e:  # noqa: BLE001
        print(f"[teams] proactive sync failed: {e!r}", flush=True)


async def loop(stop) -> None:
    """Safety net of the hooks (an approval that started waiting in another process, a post
    that failed): started in the API lifespan when the feature is on."""
    import asyncio
    while not stop.is_set():
        if active():
            await asyncio.to_thread(_safe_sync)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(10, interval_s() or 60))
        except asyncio.TimeoutError:
            pass

