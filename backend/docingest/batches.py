"""batches.py — quarantine BY BATCH: what the agent produced waits for a human, by lots.

The unit quarantine of 3.1 (memory/quarantine.py: one note, one approval) does not scale to
an archive. A batch = one source x one pass. Its notes are files under
`<quarantine dir>/batches/<id>/` — outside the memory directory, indexed by nothing, recalled
by nothing — until someone approves them:

* `sample` shows a reviewer the flagged notes first, then a random draw; what was shown is
  recorded;
* `approve_batch` moves every UNFLAGGED pending note into the memory, and is refused until
  the reviewer has actually opened a minimum sample; a flagged note (contradiction, proof
  not found, suspected injection…) is only ever approved one by one;
* `reject_batch` / `approve_note` / `reject_note` for the rest;
* a source becomes TRUSTED after N consecutive human-approved batches without a rejection:
  its next batches approve themselves (unflagged notes, at or below `trust_max_level`), and
  one rejection takes the trust away;
* every decision — human or policy — is a row in `decisions` and a line in the audit journal.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path

import audit
import quarantine
from core import levels, notes as corenotes

from . import config, state

SAMPLE_DEFAULT = 10


class ReviewError(RuntimeError):
    """A refusal a reviewer can act on (409)."""


def review_min_sample() -> int:
    return max(0, config._num("SOKKAN_DOC_INGEST_REVIEW_MIN_SAMPLE", 5))


def batch_dir(project: str, batch_id: int) -> Path:
    return quarantine.qdir(project) / "batches" / f"{batch_id:06d}"


# ---- writing (the runner) --------------------------------------------------------------------
def open_batch(source: str, project: str, pass_id: int) -> int:
    c = state.con()
    try:
        cur = c.execute("INSERT INTO batches(source, project, pass_id, status, created_at) "
                        "VALUES(?,?,?,?,?)", (source, project, pass_id, "open", time.time()))
        c.commit()
        return int(cur.lastrowid)
    finally:
        c.close()


def is_ours(text: str) -> bool:
    head = text.split("\n---", 2)[0] if text.startswith("---") else ""
    return re.search(r"^\s*ingested_by:\s*[\"']?doc-ingest", head, re.M) is not None


def name_free(project: str, name: str, source: str, rel: str) -> bool:
    """May the agent use `name` for a note of (source, rel)? Yes if nobody has it, or if it
    already is this document's note (a new version replaces it). A note written by a human,
    or derived from another document, is never overwritten."""
    row = state.index_get(project, name)
    if row is not None:
        return (row["source"], row["rel"]) == (source, rel)
    f = quarantine.memory_dir(project) / corenotes.filename_for(name)
    legacy = quarantine.memory_dir(project) / f"{name}.md"
    return not f.exists() and not legacy.exists() and not (
        quarantine.qdir(project) / f"{name}.md").exists()


def unique_name(project: str, base: str, source: str, rel: str, taken: set[str]) -> str:
    base = base[:56].strip("-") or "note"
    if not quarantine.NAME_RE.match(base):
        base = "note-" + re.sub(r"[^a-z0-9-]", "", base)[:50]
    cand, i = base, 1
    while cand in taken or not name_free(project, cand, source, rel):
        i += 1
        cand = f"{base[:56]}-{i}"
    return cand


def put_note(batch_id: int, project: str, source: str, rel: str, *, name: str, kind: str,
             level: int, title: str, description: str, text: str, body: str,
             flags: list[str]) -> None:
    d = batch_dir(project, batch_id)
    d.mkdir(parents=True, exist_ok=True)
    f = d / corenotes.filename_for(name)
    tmp = f.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(f)
    c = state.con()
    try:
        c.execute(
            "INSERT INTO batch_notes(batch_id, name, type, level, rel, file, title, description,"
            " flags, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(batch_id, name) DO UPDATE SET type=excluded.type, level=excluded.level,"
            " rel=excluded.rel, file=excluded.file, title=excluded.title,"
            " description=excluded.description, flags=excluded.flags, status='pending',"
            " decided_by='', decided_at=NULL, reason=''",
            (batch_id, name, kind, level, rel, str(f), title, description,
             json.dumps(sorted(set(flags))), "pending", time.time()))
        c.commit()
    finally:
        c.close()
    state.index_note(name=name, project=project, source=source, rel=rel, type=kind, level=level,
                     status="pending", batch_id=batch_id, title=title, description=description,
                     body=body)


def drop_pending_of(batch_id: int, project: str, source: str, rel: str) -> int:
    """A document read again in the same batch (resume, reconciliation): its previous
    pending notes of THIS batch are replaced, not duplicated."""
    c = state.con()
    try:
        rows = c.execute("SELECT name, file FROM batch_notes WHERE batch_id=? AND rel=? AND "
                         "status='pending'", (batch_id, rel)).fetchall()
        for r in rows:
            try:
                os.unlink(r["file"])
            except OSError:
                pass
        c.execute("DELETE FROM batch_notes WHERE batch_id=? AND rel=? AND status='pending'",
                  (batch_id, rel))
        c.commit()
    finally:
        c.close()
    for r in rows:
        row = state.index_get(project, r["name"])
        if row and row["status"] == "pending" and row["batch_id"] == batch_id:
            state.index_set_status(project, r["name"], "rejected")
    return len(rows)


def add_flag(batch_id: int, name: str, flag: str) -> None:
    c = state.con()
    try:
        r = c.execute("SELECT flags FROM batch_notes WHERE batch_id=? AND name=?",
                      (batch_id, name)).fetchone()
        if r:
            fl = sorted(set(json.loads(r["flags"]) + [flag]))
            c.execute("UPDATE batch_notes SET flags=? WHERE batch_id=? AND name=?",
                      (json.dumps(fl), batch_id, name))
            c.commit()
    finally:
        c.close()


def close_batch(batch_id: int) -> dict:
    """End of pass: the batch is offered for review (or closed empty)."""
    c = state.con()
    try:
        n = c.execute("SELECT COUNT(*) FROM batch_notes WHERE batch_id=?", (batch_id,)).fetchone()[0]
        c.execute("UPDATE batches SET closed_at=?, status=? WHERE id=? AND status='open'",
                  (time.time(), "pending" if n else "empty", batch_id))
        c.commit()
    finally:
        c.close()
    return get(batch_id) or {}


# ---- reading ---------------------------------------------------------------------------------
def _row(r) -> dict:
    d = dict(r)
    if "flags" in d:
        d["flags"] = json.loads(d["flags"] or "[]")
    if "level" in d:
        d["level_id"] = levels.ident(d["level"])
    return d


def get(batch_id: int, project: str | None = None) -> dict | None:
    c = state.con()
    try:
        r = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    finally:
        c.close()
    if r is None or (project is not None and r["project"] != project):
        return None
    return dict(r)


def list_batches(project: str, status: str | None = None, source: str | None = None,
                 limit: int = 100) -> list[dict]:
    c = state.con()
    try:
        q, args = "SELECT * FROM batches WHERE project=?", [project]
        if status:
            q += " AND status=?"
            args.append(status)
        if source:
            q += " AND source=?"
            args.append(source)
        rows = [dict(r) for r in c.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit))]
        for b in rows:
            b["counts"] = {r[0]: r[1] for r in c.execute(
                "SELECT status, COUNT(*) FROM batch_notes WHERE batch_id=? GROUP BY status",
                (b["id"],))}
        return rows
    finally:
        c.close()


def notes_of(batch_id: int, status: str | None = None, flag: str | None = None,
             max_level: int | None = None, offset: int = 0, limit: int = 200) -> list[dict]:
    c = state.con()
    try:
        q, args = "SELECT * FROM batch_notes WHERE batch_id=?", [batch_id]
        if status:
            q += " AND status=?"
            args.append(status)
        if max_level is not None:
            q += " AND level<=?"
            args.append(max_level)
        rows = [_row(r) for r in c.execute(q + " ORDER BY rel, type DESC, name", args)]
    finally:
        c.close()
    if flag:
        rows = [r for r in rows if (flag == "any" and r["flags"]) or flag in r["flags"]]
    return rows[offset:offset + limit]


def note(batch_id: int, name: str, max_level: int | None = None, viewer: str = "") -> dict | None:
    c = state.con()
    try:
        r = c.execute("SELECT * FROM batch_notes WHERE batch_id=? AND name=?",
                      (batch_id, name)).fetchone()
        if r is None or (max_level is not None and r["level"] > max_level):
            return None          # above the reader's clearance = does not exist
        d = _row(r)
        try:
            d["text"] = Path(d["file"]).read_text(encoding="utf-8")
        except OSError:
            d["text"] = ""
        if viewer:
            c.execute("INSERT OR IGNORE INTO views(batch_id, name, user, ts) VALUES(?,?,?,?)",
                      (batch_id, name, viewer, time.time()))
            c.commit()
        return d
    finally:
        c.close()


def viewed(batch_id: int, user: str) -> int:
    c = state.con()
    try:
        return c.execute("SELECT COUNT(*) FROM views WHERE batch_id=? AND user=?",
                         (batch_id, user)).fetchone()[0]
    finally:
        c.close()


def sample(batch_id: int, user: str, n: int = SAMPLE_DEFAULT, max_level: int | None = None,
           seed: int | None = None) -> list[dict]:
    """What a reviewer should read: flagged notes first (up to half), then a random draw of
    the rest. Opening the sample is recorded (it is what `approve_batch` checks)."""
    pend = notes_of(batch_id, status="pending", max_level=max_level, limit=10**9)
    flagged = [p for p in pend if p["flags"]]
    plain = [p for p in pend if not p["flags"]]
    rng = random.Random(seed if seed is not None else f"{batch_id}:{user}:{int(time.time()) // 60}")
    rng.shuffle(flagged)
    rng.shuffle(plain)
    k = min(len(flagged), max(1, n // 2))
    take = flagged[:k] + plain[: n - k]
    if len(take) < n:                      # few plain notes: fill with more flagged ones
        take += flagged[k: k + n - len(take)]
    out = []
    for t in take[:n]:
        full = note(batch_id, t["name"], max_level, viewer=user)
        if full:
            out.append(full)
    return out


def stats(batch_id: int, max_level: int | None = None) -> dict:
    b = get(batch_id)
    if b is None:
        raise KeyError(batch_id)
    rows = notes_of(batch_id, limit=10**9)
    visible = [r for r in rows if max_level is None or r["level"] <= max_level]
    by = lambda key: _count(visible, key)  # noqa: E731
    flags: dict[str, int] = {}
    for r in visible:
        for f in r["flags"]:
            flags[f] = flags.get(f, 0) + 1
    p = state.pass_get(b["pass_id"]) if b.get("pass_id") else None
    out = {"batch": b, "notes": len(visible), "hidden_above_clearance": len(rows) - len(visible),
           "by_status": by("status"), "by_type": by("type"),
           "by_level": _count(visible, "level_id"), "flags": flags,
           "flagged": sum(1 for r in visible if r["flags"]),
           "documents": len({r["rel"] for r in visible}),
           "pass": None, "abstained": [], "duplicates": [], "errors": []}
    if p:
        out["pass"] = {k: p[k] for k in ("id", "kind", "status", "started_at", "ended_at",
                                         "seconds", "counters", "error")}
        for key, outcome in (("abstained", "abstained"), ("duplicates", "duplicate"),
                             ("errors", "error")):
            out[key] = [{"path": e["rel"], "detail": e["detail"]}
                        for e in state.events(p["id"], outcome, limit=200)]
    return out


def _count(rows: list[dict], key: str) -> dict:
    out: dict = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return out


# ---- decisions ---------------------------------------------------------------------------------
def journal(user: str, b: dict, action: str, note_name: str = "", reason: str = "",
            detail: dict | None = None) -> None:
    c = state.con()
    try:
        c.execute("INSERT INTO decisions(ts, user, source, project, batch_id, note, action, reason,"
                  " detail) VALUES(?,?,?,?,?,?,?,?,?)",
                  (time.time(), user, b["source"], b["project"], b["id"], note_name, action,
                   reason[:500], json.dumps(detail or {}, ensure_ascii=False)))
        c.commit()
    finally:
        c.close()
    audit.log(user, f"doc_ingest.{action}", f"batch #{b['id']}" + (f" {note_name}" if note_name
              else ""), reason[:200], project=b["project"])


def decisions(project: str | None = None, batch_id: int | None = None, limit: int = 5000
              ) -> list[dict]:
    c = state.con()
    try:
        q, args, where = "SELECT * FROM decisions", [], []
        if project is not None:
            where.append("project=?")
            args.append(project)
        if batch_id is not None:
            where.append("batch_id=?")
            args.append(batch_id)
        if where:
            q += " WHERE " + " AND ".join(where)
        out = []
        for r in c.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)):
            d = dict(r)
            d["detail"] = json.loads(d["detail"] or "{}")
            out.append(d)
        return out
    finally:
        c.close()


def _refresh_status(batch_id: int) -> str:
    c = state.con()
    try:
        counts = {r[0]: r[1] for r in c.execute(
            "SELECT status, COUNT(*) FROM batch_notes WHERE batch_id=? GROUP BY status",
            (batch_id,))}
        cur = c.execute("SELECT status FROM batches WHERE id=?", (batch_id,)).fetchone()["status"]
        if cur == "open":
            return cur
        if counts.get("pending"):
            st = "pending"
        elif counts.get("approved") and counts.get("rejected"):
            st = "partial"
        elif counts.get("approved"):
            st = "approved"
        elif counts.get("rejected"):
            st = "rejected"
        else:
            st = "empty"
        c.execute("UPDATE batches SET status=?, decided_at=? WHERE id=?",
                  (st, None if st == "pending" else time.time(), batch_id))
        c.commit()
        return st
    finally:
        c.close()


def _set(batch_id: int, name: str, status: str, user: str, reason: str = "") -> None:
    c = state.con()
    try:
        c.execute("UPDATE batch_notes SET status=?, decided_by=?, decided_at=?, reason=? "
                  "WHERE batch_id=? AND name=?", (status, user, time.time(), reason[:500],
                                                  batch_id, name))
        c.commit()
    finally:
        c.close()


def _into_memory(b: dict, n: dict, user: str) -> Path:
    """THE only write of this package into the memory directory."""
    import store_backend
    project = b["project"]
    # 3.4.2: a note of another project or above the default level needs the 3.0 store —
    # refused before anything is written, never a note nothing indexes
    store_backend.require_store(project, n["level"])
    text = Path(n["file"]).read_text(encoding="utf-8")
    md = quarantine.memory_dir(project)
    dest = md / corenotes.filename_for(n["name"])
    if dest.exists() and not is_ours(dest.read_text(encoding="utf-8", errors="replace")):
        raise ReviewError(f"a note named {n['name']} was written in the memory meanwhile and "
                          "does not come from the ingestion: reject this one or rename that one")
    text = text.replace("\n---\n", f"\n  approved_by: {json.dumps(user)}\n---\n", 1)
    md.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)
    try:
        os.unlink(n["file"])
    except OSError:
        pass
    if n["level"] > levels.DEFAULT and store_backend.enabled():
        try:      # the floor: an edit of the file cannot take the note below its level
            store_backend.get_store().set_level(n["name"], n["level"], project=project,
                                                by=user, reason="doc-ingest source floor")
        except Exception as e:  # noqa: BLE001
            print(f"[doc-ingest] level floor of {n['name']} not recorded: {e!r}", flush=True)
    return dest


def _check_open(b: dict | None) -> dict:
    if b is None:
        raise KeyError("batch")
    if b["status"] == "open":
        raise ReviewError("this batch is still being written by a running pass")
    return b


def approve_note(batch_id: int, name: str, user: str, max_level: int | None = None,
                 project: str | None = None, _action: str = "approve") -> dict:
    b = _check_open(get(batch_id, project))
    n = note(batch_id, name, max_level)
    if n is None:
        raise KeyError(name)
    if n["status"] != "pending":
        raise ReviewError(f"{name} is already {n['status']}")
    dest = _into_memory(b, n, user)
    _set(batch_id, name, "approved", user)
    state.index_set_status(b["project"], name, "approved")
    journal(user, b, _action, name, "", {"type": n["type"], "level": n["level_id"],
                                         "flags": n["flags"], "path": n["rel"]})
    return {"ok": True, "note": name, "path": str(dest), "batch_status": _refresh_status(batch_id)}


def reject_note(batch_id: int, name: str, user: str, reason: str = "",
                max_level: int | None = None, project: str | None = None,
                _action: str = "reject") -> dict:
    b = _check_open(get(batch_id, project))
    n = note(batch_id, name, max_level)
    if n is None:
        raise KeyError(name)
    if n["status"] != "pending":
        raise ReviewError(f"{name} is already {n['status']}")
    rej = batch_dir(b["project"], batch_id) / "rejected"
    rej.mkdir(parents=True, exist_ok=True)
    try:
        Path(n["file"]).replace(rej / Path(n["file"]).name)
    except OSError:
        pass
    _set(batch_id, name, "rejected", user, reason)
    state.index_set_status(b["project"], name, "rejected")
    journal(user, b, _action, name, reason, {"type": n["type"], "level": n["level_id"],
                                             "flags": n["flags"], "path": n["rel"]})
    return {"ok": True, "note": name, "batch_status": _refresh_status(batch_id)}


def approve_batch(batch_id: int, user: str, max_level: int | None = None,
                  project: str | None = None, _policy: bool = False,
                  _level_cap: int | None = None) -> dict:
    """Every unflagged pending note of the batch -> memory. Flagged notes stay pending."""
    b = _check_open(get(batch_id, project))
    pend = notes_of(batch_id, status="pending", limit=10**9)
    reachable = [p for p in pend if max_level is None or p["level"] <= max_level]
    if not _policy:
        need = min(review_min_sample(), len(reachable))
        seen = viewed(batch_id, user)
        if seen < need:
            # the guard against rubber-stamping: you approve what you have looked at
            raise ReviewError(f"open at least {need} notes of this batch before approving it "
                              f"(you opened {seen}) — GET …/sample")
    ok, kept, failed = [], [], []
    for p in reachable:
        if p["flags"] or (_level_cap is not None and p["level"] > _level_cap):
            kept.append(p["name"])
            continue
        try:
            _into_memory(b, p, user)
        except Exception as e:  # noqa: BLE001 — one note that cannot land never blocks the lot
            failed.append({"note": p["name"], "error": str(e)[:200]})
            continue
        _set(batch_id, p["name"], "approved", user)
        state.index_set_status(b["project"], p["name"], "approved")
        ok.append(p["name"])
    status = _refresh_status(batch_id)
    journal(user, b, "auto_approve_batch" if _policy else "approve_batch", "", "",
            {"approved": len(ok), "left_pending": len(kept), "failed": failed,
             "hidden_above_clearance": len(pend) - len(reachable),
             "viewed": viewed(batch_id, user) if not _policy else 0,
             "seconds_since_closed": round(time.time() - (b.get("closed_at") or time.time()))})
    return {"ok": True, "approved": len(ok), "left_pending": kept, "failed": failed,
            "batch_status": status}


def reject_batch(batch_id: int, user: str, reason: str = "", max_level: int | None = None,
                 project: str | None = None) -> dict:
    b = _check_open(get(batch_id, project))
    pend = notes_of(batch_id, status="pending", max_level=max_level, limit=10**9)
    rej = batch_dir(b["project"], batch_id) / "rejected"
    rej.mkdir(parents=True, exist_ok=True)
    for p in pend:
        try:
            Path(p["file"]).replace(rej / Path(p["file"]).name)
        except OSError:
            pass
        _set(batch_id, p["name"], "rejected", user, reason)
        state.index_set_status(b["project"], p["name"], "rejected")
    status = _refresh_status(batch_id)
    journal(user, b, "reject_batch", "", reason, {"rejected": len(pend)})
    return {"ok": True, "rejected": len(pend), "batch_status": status}


# ---- trust policy ------------------------------------------------------------------------------
POLICY_USER = "policy:trust"


def trust(src) -> dict:
    """Is this source trusted? Yes when its last `trust_after` batches DECIDED BY A HUMAN were
    all approved without a single rejected note. Batches the policy approved itself do not
    count (trust never feeds on itself); any human rejection since takes it away."""
    n = int(src.trust_after or 0)
    if n <= 0:
        return {"trusted": False, "need": 0, "have": 0, "reason": "trust policy off"}
    c = state.con()
    try:
        rows = c.execute(
            "SELECT b.id, b.status, "
            " (SELECT COUNT(*) FROM decisions d WHERE d.batch_id=b.id AND d.user<>? AND "
            "  d.action IN ('approve_batch','approve','reject','reject_batch')) AS human, "
            " (SELECT COUNT(*) FROM batch_notes bn WHERE bn.batch_id=b.id AND "
            "  bn.status='rejected') AS rejected "
            "FROM batches b WHERE b.source=? AND b.status IN ('approved','rejected','partial') "
            "ORDER BY b.id DESC", (POLICY_USER, src.id)).fetchall()
    finally:
        c.close()
    human = [r for r in rows if r["human"]]
    have = 0
    for r in human:
        if r["status"] == "approved" and not r["rejected"]:
            have += 1
        else:
            break
    return {"trusted": have >= n, "need": n, "have": have,
            "reason": "" if have >= n else f"{have}/{n} consecutive batches approved by a person"}


def apply_trust(batch_id: int, src) -> dict | None:
    """End of a pass: a trusted source's batch approves itself (unflagged notes, at or below
    `trust_max_level`). Journaled as the policy's decision — never silent."""
    t = trust(src)
    if not t["trusted"]:
        return None
    b = get(batch_id)
    if b is None or b["status"] != "pending":
        return None
    return approve_batch(batch_id, POLICY_USER, _policy=True, _level_cap=src.trust_max_level)
