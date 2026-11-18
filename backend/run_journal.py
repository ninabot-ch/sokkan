"""The journal of a run's events, one row per event, written by the cockpit (4.0).

The contract says every event of a run is journalled by the cockpit, never by the
adapter: whatever the engine, the same trace of what a run did. This is that trace, kept
in `run_events.db` (data directory), and only its SHAPE — never what a person or a model
wrote:

| Event | Kept |
|---|---|
| run.started / run.ended | engine, model, resumed or not / status, resumable |
| run.info | the model that answered, the model asked for, the engine's session reference |
| turn.started / turn.ended | prompt reference (a hash) / stop reason, error, result or not, duration |
| tool.called / tool.result | call id, tool name, classification / ok, size of the output |
| gate.requested / gate.resolved | gate id, kind, decisions offered / decision, by, via |
| context.injected | path, channel, note names, size |
| error | code, recoverable (not the message: it may quote a secret) |

Not kept: the text and reasoning of the model, tool inputs and outputs (the transcript,
the journal of validations and the audit log hold what must be kept of them), `usage`
(its own ledger, usage_events.db). Rows are buffered by the session and written at the
end of a turn, when a gate opens and when the run ends — one transaction each. Kept
`SOKKAN_RUN_EVENTS_DAYS` days (default 90; 0 = forever). Best-effort: a journal that
cannot be written never breaks a run.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

_PRUNED: dict[str, float] = {}


def db_path() -> Path:
    d = Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")))
    return d / "run_events.db"


def _con() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=2)
    con.row_factory = sqlite3.Row
    con.executescript(
        "CREATE TABLE IF NOT EXISTS run_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL,"
        " project TEXT NOT NULL DEFAULT 'default', engine TEXT NOT NULL DEFAULT '',"
        " type TEXT NOT NULL, turn_id TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}');"
        "CREATE INDEX IF NOT EXISTS ix_run_events_session ON run_events(session_id, seq);"
        "CREATE INDEX IF NOT EXISTS ix_run_events_ts ON run_events(ts);")
    return con


def _shape(ev) -> dict | None:
    """What is kept of one contract event; None = not journalled."""
    t = ev.type
    g = lambda k: getattr(ev, k, None)  # noqa: E731
    if t == "run.started":
        return {"harness": g("harness"), "model_engine": g("engine"), "model": g("model"),
                "resumed": bool(g("resumed_from"))}
    if t == "run.info":
        return {k: g(k) for k in ("model", "requested_model", "engine_session") if g(k)}
    if t == "turn.started":
        return {"prompt_ref": g("prompt_ref")}
    if t == "turn.ended":
        return {"stop_reason": g("stop_reason"), "is_error": bool(g("is_error")),
                "reported": bool(g("reported")), "num_turns": g("num_turns"),
                "duration_ms": g("duration_ms")}
    if t == "tool.called":
        return {"call_id": g("call_id"), "name": g("name"), "classification": g("classification")}
    if t == "tool.result":
        return {"call_id": g("call_id"), "ok": bool(g("ok")), "chars": len(g("output") or "")}
    if t == "gate.requested":
        gate = g("gate")
        return {"gate_id": gate.gate_id, "kind": gate.kind, "options": list(gate.options)}
    if t == "gate.resolved":
        return {"gate_id": g("gate_id"), "decision": g("decision"), "by": g("by"), "via": g("via")}
    if t == "context.injected":
        return {"source": g("source"), "channel": g("channel"),
                "notes": list(g("note_names") or ()), "chars": g("chars")}
    if t == "error":
        return {"code": g("code"), "recoverable": bool(g("recoverable"))}
    if t == "run.ended":
        return {"status": g("status"), "resumable": bool(g("resumable"))}
    return None          # assistant.text / assistant.think / usage


class Journal:
    """The buffer of one session. `add` keeps the shape of an event; `flush` writes."""

    FLUSH_ON = {"turn.ended", "gate.requested", "run.ended"}

    def __init__(self, session_id: str, project: str = "default", engine: str = "") -> None:
        self.session_id, self.project, self.engine = session_id, project or "default", engine
        self._rows: list[tuple] = []
        self._seq = None

    def add(self, ev) -> None:
        try:
            data = _shape(ev)
        except Exception:  # noqa: BLE001
            return
        if data is None:
            return
        turn = getattr(ev, "turn_id", None) or ""
        if ev.type == "gate.requested":
            turn = getattr(ev.gate, "turn_id", None) or ""
        self._rows.append((ev.type, turn, json.dumps(data, default=str), time.time()))
        if ev.type in self.FLUSH_ON:
            self.flush()

    def flush(self) -> None:
        rows, self._rows = self._rows, []
        if not rows:
            return
        try:
            con = _con()
            try:
                if self._seq is None:
                    r = con.execute("SELECT MAX(seq) FROM run_events WHERE session_id = ?",
                                    (self.session_id,)).fetchone()
                    self._seq = int(r[0] or 0)
                out = []
                for typ, turn, data, ts in rows:
                    self._seq += 1
                    out.append((self.session_id, self._seq, ts, self.project, self.engine,
                                typ, turn, data))
                con.executemany(
                    "INSERT INTO run_events(session_id, seq, ts, project, engine, type, turn_id,"
                    " data) VALUES(?,?,?,?,?,?,?,?)", out)
                _prune(con)
                con.commit()
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001
            import sys
            print(f"[sokkan] run journal of {self.session_id} not written ({e!r})",
                  file=sys.stderr)


def _prune(con: sqlite3.Connection) -> None:
    days = float(os.environ.get("SOKKAN_RUN_EVENTS_DAYS") or 90)
    key, now = str(db_path()), time.time()
    if days <= 0 or now - _PRUNED.get(key, 0) < 86400:
        return
    _PRUNED[key] = now
    con.execute("DELETE FROM run_events WHERE ts < ?", (now - days * 86400,))


def events(session_id: str) -> list[dict]:
    """The journal of one session, in order."""
    if not db_path().exists():
        return []
    con = _con()
    try:
        rows = con.execute("SELECT * FROM run_events WHERE session_id = ? ORDER BY seq",
                           (session_id,)).fetchall()
    finally:
        con.close()
    return [{"seq": r["seq"], "ts": r["ts"], "type": r["type"], "turn_id": r["turn_id"],
             "project": r["project"], "engine": r["engine"], **json.loads(r["data"])}
            for r in rows]


def engine_of(session_id: str) -> str:
    """The engine of a session's last run ('' when the journal does not know it)."""
    if not db_path().exists():
        return ""
    con = _con()
    try:
        r = con.execute("SELECT engine FROM run_events WHERE session_id = ? AND engine != ''"
                        " ORDER BY seq DESC LIMIT 1", (session_id,)).fetchone()
    finally:
        con.close()
    return r["engine"] if r else ""
