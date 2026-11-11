#!/usr/bin/env python3
"""study.py — SOKKAN feature `study_mode`: an instance used under an audited research protocol.

An organisation's volunteers use SOKKAN under a study (a school observes how agents are
supervised at work). The instance then:

* tags the work: study, participant, scenario (S1…S6), reference time typed by the person;
* PSEUDONYMISES AT THE SOURCE: one local table maps a person to a pseudonym (P1, P2…).
  It is the only place of this module that holds an e-mail; every other study table, every
  export and the identity sent to the inference gateway carry the pseudonym only;
* asks two short questions when a task ends (was it useful, how long did checking take);
* shows a permanent banner and a plain-language page « What is recorded », and asks each
  person for an explicit consent before their first session in study mode — withdrawable;
* builds the researcher's package (study_export.py): metadata only by default.

Whether the study is on is not decided here: the state (active, study, end date, ceilings,
consumption) is READ from the inference gateway through the `GET /usage` call the instance
already makes (llm.usage). The gateway serves the free tier only to an audited instance, so
the condition is held technically. Contract (every field optional — an older gateway omits
them): ``{"study": {"active", "study_id", "expires_at", "audit_required"}, "limits":
{"tokens_day", "tokens_month", "deep_per_user_day"}, "used": {"tokens_day",
"tokens_month"}}``. An instance that is not on the gateway (or whose gateway does not serve
the block yet) can declare a study locally: SOKKAN_STUDY_ID (+ SOKKAN_STUDY_EXPIRES_AT).
The gateway as delivered (sokkan-cloud `infer/pilote-etude.md`) also serves `study.expired`,
`study.days_left`, `study.notice` (the end of the free period is near), `free`,
`limits.user_tokens_day` and, for the person named by `x-sokkan-user` on the call,
`used.user_tokens_day` / `used.deep_user_day`; a limit of 0 means no ceiling. It refuses with
410 `study_expired`, 412 `study_mode_required`, 429 `user_daily_cap` (a person's ceiling), 429
`user_limit_reached` (too many distinct people today) — `explain_refusal`.

Storage: ``$SOKKAN_DATA_DIR/study.db`` (SQLite, survives restarts, in the instance backup).
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import sqlite3
import threading
import time
from pathlib import Path

import features

DB = Path(os.environ.get("SOKKAN_STUDY_DB") or os.path.join(
    os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")), "study.db"))

SCENARIOS = ("S1", "S2", "S3", "S4", "S5", "S6")
# version of the consent text shown in the cockpit (frontend/lib/studyText.ts): a new
# version of the text asks everyone again
CONSENT_VERSION = "2026-11"
STATE_TTL_S = 60          # the gateway is asked at most once a minute
TOKEN_PREFIX = "skx_"
_STUDY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_SCHEMA = """
-- THE correspondence table: the only study table that holds an e-mail. Never exported.
CREATE TABLE IF NOT EXISTS participants (
    email TEXT PRIMARY KEY,
    pseudonym TEXT NOT NULL UNIQUE,           -- P1, P2… never reused
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
    pseudonym TEXT NOT NULL,
    study_id TEXT NOT NULL,
    version TEXT NOT NULL,                    -- version of the text the person accepted
    given_at REAL NOT NULL,
    withdrawn_at REAL,
    content_scenarios TEXT NOT NULL DEFAULT '[]',   -- scenarios whose CONTENT may be exported
    PRIMARY KEY (pseudonym, study_id)
);
CREATE TABLE IF NOT EXISTS tags (
    session_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL,
    pseudonym TEXT NOT NULL,                  -- the participant the work belongs to
    scenario TEXT NOT NULL DEFAULT '',        -- '' until chosen, then S1…S6
    reference_minutes REAL,                   -- time the task takes without the agent (typed)
    card_id INTEGER,
    auto INTEGER NOT NULL DEFAULT 0,          -- 1 = tagged by the first turn, not by hand
    tagged_at REAL NOT NULL,
    closed_at REAL                            -- the person said the task is done
);
CREATE INDEX IF NOT EXISTS ix_tags_study ON tags(study_id, pseudonym);
CREATE TABLE IF NOT EXISTS card_tags (
    card_id INTEGER PRIMARY KEY,
    study_id TEXT NOT NULL,
    scenario TEXT NOT NULL DEFAULT '',
    reference_minutes REAL,
    tagged_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    study_id TEXT NOT NULL,
    pseudonym TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    duration_ms INTEGER,
    model TEXT NOT NULL DEFAULT '',           -- tier / model that served the turn
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    is_error INTEGER NOT NULL DEFAULT 0,
    prompt_chars INTEGER NOT NULL DEFAULT 0,  -- size of the request, never its text
    prompt_hmac TEXT NOT NULL DEFAULT ''      -- same request typed twice = same value
);
CREATE INDEX IF NOT EXISTS ix_turns_session ON turns(session_id, started_at);
CREATE TABLE IF NOT EXISTS feedback (
    session_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL,
    pseudonym TEXT NOT NULL,
    scenario TEXT NOT NULL DEFAULT '',
    useful INTEGER NOT NULL,                  -- 1 (not at all) … 5 (very)
    verify_minutes REAL NOT NULL,             -- time spent checking the result, declared
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL DEFAULT '',
    token_sha256 TEXT NOT NULL UNIQUE,        -- the token itself is shown once, never stored
    hint TEXT NOT NULL DEFAULT '',            -- its last 4 characters
    study_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,                 -- who created it: the action journal says
    expires_at REAL,
    revoked_at REAL,
    last_used_at REAL,
    uses INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    by TEXT NOT NULL DEFAULT '',              -- admin | token:<id> (who: the action journal)
    study_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    counts TEXT NOT NULL DEFAULT '{}',
    sha256 TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    at REAL NOT NULL
);
"""

RUNNING_TURN_MAX_S = 2 * 3600        # an open turn older than this belongs to a dead process
_lock = threading.RLock()
_state_cache: tuple[float, dict] | None = None


class StudyError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def enabled() -> bool:
    return features.enabled("study_mode")


def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    try:
        os.chmod(DB, 0o600)  # holds the correspondence table
    except OSError:
        pass
    return con


def _norm(email: str) -> str:
    return (email or "").lower().strip()


# ---- key/value (last known gateway state, export salt) ----------------------------------
def _kv_get(key: str):
    con = _con()
    r = con.execute("SELECT value, at FROM kv WHERE key = ?", (key,)).fetchone()
    con.close()
    if r is None:
        return None, 0.0
    try:
        return json.loads(r["value"]), float(r["at"])
    except ValueError:
        return None, 0.0


def _kv_set(key: str, value) -> None:
    con = _con()
    con.execute("INSERT INTO kv(key, value, at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET"
                " value = excluded.value, at = excluded.at", (key, json.dumps(value), time.time()))
    con.commit()
    con.close()


def salt() -> bytes:
    """Secret of this instance for the keyed hashes of the exports (a hash of a short text
    cannot be reversed by trying candidates). Never exported."""
    with _lock:
        v, _ = _kv_get("salt")
        if not v:
            v = _secrets.token_hex(32)
            _kv_set("salt", v)
        return bytes.fromhex(v)


def keyed(value: str, length: int = 16) -> str:
    return hmac.new(salt(), (value or "").encode("utf-8", "replace"),
                    hashlib.sha256).hexdigest()[:length]


# ---- state: read from the inference gateway ---------------------------------------------
def _parse_iso(v) -> float | None:
    if not v:
        return None
    try:
        d = datetime.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def _int(v) -> int | None:
    try:
        return None if v is None or v == "" else int(v)
    except (TypeError, ValueError):
        return None


def parse_usage(u: dict | None) -> dict | None:
    """The study block of a gateway `/usage` answer, None when the gateway does not serve
    one. Every field is optional; ceilings and consumption fall back on the fields the
    gateway has always served (daily_quota_tokens, used_today…)."""
    if not isinstance(u, dict):
        return None
    limits = u.get("limits") if isinstance(u.get("limits"), dict) else {}
    used = u.get("used") if isinstance(u.get("used"), dict) else {}
    out_limits = {
        "tokens_day": _int(limits.get("tokens_day")) or _int(u.get("daily_quota_tokens")),
        "tokens_month": _int(limits.get("tokens_month")) or _int(u.get("monthly_quota_tokens")),
        "deep_per_user_day": _int(limits.get("deep_per_user_day")) or None,   # 0 = no ceiling
        "user_tokens_day": _int(limits.get("user_tokens_day")) or None,
    }
    mine = {"tokens_day": _int(used.get("user_tokens_day")),
            "deep_day": _int(used.get("deep_user_day"))}
    out_used = {
        "tokens_day": _int(used.get("tokens_day")) if used.get("tokens_day") is not None
        else _int(u.get("used_today")),
        "tokens_month": _int(used.get("tokens_month")) if used.get("tokens_month") is not None
        else _int(u.get("used_month")),
    }
    s = u.get("study")
    if not isinstance(s, dict):
        return {"study": None, "limits": out_limits, "used": out_used, "mine": mine}
    sid = str(s.get("study_id") or "").strip()
    return {"study": {"active": bool(s.get("active")) and bool(sid) and s.get("expired") is not True,
                      "notice": s.get("notice") is True,
                      "study_id": sid if _STUDY_ID_RE.match(sid) else "",
                      "expires_at": str(s.get("expires_at") or ""),
                      # audited use is the condition of the free tier: required unless the
                      # gateway explicitly says otherwise
                      "audit_required": s.get("audit_required") is not False},
            "limits": out_limits, "used": out_used, "mine": mine}


def _local_declaration() -> dict | None:
    sid = (os.environ.get("SOKKAN_STUDY_ID") or "").strip()
    if not sid or not _STUDY_ID_RE.match(sid):
        return None
    req = (os.environ.get("SOKKAN_STUDY_AUDIT_REQUIRED") or "1").strip().lower()
    return {"active": True, "study_id": sid,
            "expires_at": (os.environ.get("SOKKAN_STUDY_EXPIRES_AT") or "").strip(),
            "audit_required": req not in ("0", "false", "no", "off")}


def _finish(study: dict | None, limits: dict, used: dict, source: str, fetched_at: float,
            stale: bool = False) -> dict:
    now = time.time()
    exp = _parse_iso((study or {}).get("expires_at"))
    expired = exp is not None and now >= exp
    active = bool(study and study.get("active") and study.get("study_id") and not expired)
    return {"enabled": True, "active": active,
            "study_id": (study or {}).get("study_id") or "",
            "expires_at": (study or {}).get("expires_at") or "",
            "expired": bool(study and expired),
            "days_left": None if exp is None else max(0, int((exp - now) // 86400)),
            "audit_required": bool(study and study.get("audit_required")) if study else False,
            "notice": bool(active and (study or {}).get("notice")),
            "limits": limits, "used": used, "source": source, "stale": stale,
            "fetched_at": fetched_at}


_OFF = {"enabled": False, "active": False, "study_id": "", "expires_at": "", "expired": False,
        "days_left": None, "audit_required": False, "notice": False,
        "limits": {"tokens_day": None, "tokens_month": None, "deep_per_user_day": None,
                   "user_tokens_day": None},
        "used": {"tokens_day": None, "tokens_month": None}, "source": "none", "stale": False,
        "fetched_at": 0.0}


def state(refresh: bool = False) -> dict:
    """The study state of this instance. Off while the feature is off. Asked to the gateway
    at most every STATE_TTL_S; when the gateway does not answer, the last known state holds
    (its end date still applies) — an outage never switches the study off silently."""
    global _state_cache
    if not enabled():
        return dict(_OFF)
    now = time.time()
    with _lock:
        if not refresh and _state_cache and now - _state_cache[0] < STATE_TTL_S:
            return _finish(*_state_cache[1])
    import llm
    try:
        parsed = parse_usage(llm.usage())
    except Exception:  # noqa: BLE001 — a gateway error is an absence of answer
        parsed = None
    empty_l, empty_u = dict(_OFF["limits"]), dict(_OFF["used"])
    if parsed and parsed["study"] is not None:
        args = (parsed["study"], parsed["limits"], parsed["used"], "gateway", now, False)
        _kv_set("gateway_state", {"study": parsed["study"], "limits": parsed["limits"],
                                  "used": parsed["used"]})
    else:
        local = _local_declaration()
        last, at = _kv_get("gateway_state")
        if parsed is None and last and not local:
            # managed inference that does not answer right now: the last answer holds
            args = (last.get("study"), last.get("limits") or empty_l,
                    last.get("used") or empty_u, "gateway", at, True)
        elif local:
            args = (local, (parsed or {}).get("limits") or empty_l,
                    (parsed or {}).get("used") or empty_u, "local", now, False)
        else:
            args = (None, (parsed or {}).get("limits") or empty_l,
                    (parsed or {}).get("used") or empty_u, "none", now, False)
    with _lock:
        _state_cache = (now, args)
    return _finish(*args)


def reset_cache() -> None:
    global _state_cache
    with _lock:
        _state_cache = None
        _mine_cache.clear()


_mine_cache: dict[str, tuple[float, dict | None]] = {}


def my_usage(email: str) -> dict | None:
    """The person's own consumption today as the gateway counts it under their pseudonym
    ({tokens_day, deep_day}); None when there is no active study or the gateway does not
    say. Asked at most once a minute per person."""
    if not email or not active():
        return None
    p = pseudonym_of(email)
    now = time.time()
    with _lock:
        hit = _mine_cache.get(p)
        if hit and now - hit[0] < STATE_TTL_S:
            return hit[1]
    import llm
    try:
        parsed = parse_usage(llm.usage(headers=gateway_headers(email)))
    except Exception:  # noqa: BLE001
        parsed = None
    mine = (parsed or {}).get("mine")
    if mine and mine["tokens_day"] is None and mine["deep_day"] is None:
        mine = None
    with _lock:
        _mine_cache[p] = (now, mine)
    return mine


# what the gateway answers when it refuses (infer/gateway.py `_refuse`): code, then the
# words of its message — Claude Code shows « API Error: <status> <message> », not the code.
# The sentences are for a person who is not technical: what happened, that nothing is lost,
# who can act. No variable name, no status code. (The simple profile says the same in the
# person's own language: `no.<code>` of frontend/lib/simple/dict.ts.)
_REFUSALS = (
    (("study_expired", "free inference period for this environment ended"),
     "The free period of the study has ended: nothing was sent to a model. Ask your SOKKAN "
     "administrator to extend it or to switch to a paid plan. — La période gratuite de l'étude "
     "est terminée : rien n'a été envoyé à un modèle. Demandez à votre administrateur SOKKAN "
     "de la prolonger ou de passer à une offre payante."),
    (("study_mode_required", "only served while the instance runs in study mode"),
     "The free use of the assistant is tied to a study, and this workspace is not in study mode "
     "at the moment: nothing was sent to a model and nothing is lost. Only your SOKKAN "
     "administrator can turn study mode back on: please tell them; sending the request again "
     "will not help. — L'usage gratuit de l'assistant est lié à une étude, et cet espace n'est "
     "pas en mode étude pour l'instant : rien n'a été envoyé à un modèle et rien n'est perdu. "
     "Seul votre administrateur SOKKAN peut réactiver le mode étude : prévenez-le ; renvoyer la "
     "demande ne changera rien."),
    (("user_daily_cap", "personal daily inference cap reached"),
     "You have reached your personal ceiling for today; it resets at midnight UTC (your "
     "colleagues are not affected). — Vous avez atteint votre plafond personnel du jour ; il "
     "repart à minuit UTC (vos collègues ne sont pas concernés)."),
    (("user_limit_reached", "too many distinct users today"),
     "The number of people who can use the assistant today in this workspace is reached, and "
     "you are not one of them yet: nothing was sent to a model and nothing is lost. The people "
     "who already used it today are not affected. Ask your SOKKAN administrator to raise the "
     "limit; sending the request again today will not help. — Le nombre de personnes qui "
     "peuvent utiliser l'assistant aujourd'hui dans cet espace est atteint, et vous n'en faites "
     "pas encore partie : rien n'a été envoyé à un modèle et rien n'est perdu. Les personnes qui "
     "l'ont déjà utilisé aujourd'hui ne sont pas concernées. Demandez à votre administrateur "
     "SOKKAN de relever la limite ; renvoyer la demande aujourd'hui ne changera rien."),
)
REFUSAL_CODES = tuple(marks[0] for marks, _ in _REFUSALS)
# On an appliance only the requests sent to the Deep gateway are metered there: a person at
# their ceiling has lost the stronger model for the day, NOT the model of the box (the notice
# said « tomorrow » to somebody who could go on at once — journey of 11.11.2026).
_DAILY_CAP_LOCAL = (
    "You have reached your personal ceiling of the stronger model for today (your colleagues "
    "are not affected). Nothing is lost and you can carry on right away with the usual model: "
    "send your request without asking for the stronger one, which opens again tomorrow "
    "(midnight UTC). — Vous avez atteint votre plafond personnel du modèle renforcé pour "
    "aujourd'hui (vos collègues ne sont pas concernés). Rien n'est perdu et vous pouvez "
    "continuer tout de suite avec le modèle habituel : renvoyez votre demande sans demander "
    "le modèle renforcé, qui vous sera rouvert demain (minuit UTC).")


def local_model_remains() -> bool:
    """Is this instance an appliance — sessions on the model of the box through the local
    router, the gateway asked for Deep only (llm.appliance_config, unless a model was chosen
    in the cockpit)? Then a person's ceiling on the gateway leaves the local model usable."""
    try:
        import llm
        return llm.load().get("engine") == "appliance"
    except Exception:  # noqa: BLE001 — a sentence never fails on a lookup
        return False


def refusal_code(text: str) -> str | None:
    """The gateway's own code of a study refusal (`error.code`, header `x-sokkan-error`),
    found in what the session saw of it — the JSON body, or the CLI's « API Error: <status>
    <message> » line; None for any other error."""
    low = (text or "").lower()
    if not low:
        return None
    for marks, _ in _REFUSALS:
        if any(m in low for m in marks):
            return marks[0]
    return None


def explain_refusal(text: str) -> str | None:
    """A sentence a person can read, in English and in French, when the inference gateway
    refused the call for a study reason; None for any other error. Works with the feature
    off too (412 is exactly what an instance without study mode gets)."""
    code = refusal_code(text)
    if code == "user_daily_cap" and local_model_remains():
        return _DAILY_CAP_LOCAL
    return next(s for marks, s in _REFUSALS if marks[0] == code) if code else None


def single_identity() -> bool:
    """Would every volunteer share ONE pseudonym? True for the local sign-in while nobody
    has a personal access code (localaccounts.py, 3.6): the owner's token is then the only
    identity. With named accounts — or any identity provider — each person is their own."""
    try:
        import auth
        if auth.MODE != "local":
            return False
        import localaccounts
        return not localaccounts.any_account()
    except Exception:  # noqa: BLE001 — unknown = warn
        return True


def active() -> bool:
    return enabled() and state()["active"]


# ---- pseudonyms -------------------------------------------------------------------------
def pseudonym_of(email: str, create: bool = True) -> str:
    """The pseudonym of a person; allocated on first use (P1, P2… in order, never reused)."""
    email = _norm(email)
    if not email:
        return ""
    with _lock:
        con = _con()
        try:
            r = con.execute("SELECT pseudonym FROM participants WHERE email = ?",
                            (email,)).fetchone()
            if r is not None or not create:
                return r["pseudonym"] if r else ""
            n = con.execute("SELECT coalesce(max(CAST(substr(pseudonym, 2) AS INTEGER)), 0)"
                            " FROM participants").fetchone()[0]
            p = f"P{int(n) + 1}"
            con.execute("INSERT INTO participants(email, pseudonym, created_at) VALUES(?,?,?)",
                        (email, p, time.time()))
            con.commit()
            return p
        finally:
            con.close()


def correspondence() -> list[dict]:
    """The local correspondence table — for the instance's admins only (study_api)."""
    con = _con()
    rows = [dict(r) for r in con.execute(
        "SELECT email, pseudonym, created_at FROM participants"
        " ORDER BY CAST(substr(pseudonym, 2) AS INTEGER)")]
    con.close()
    return rows


def known_emails() -> list[str]:
    return [r["email"] for r in correspondence()]


# ---- consent ----------------------------------------------------------------------------
def _clean_scenarios(scenarios) -> list[str]:
    return sorted({str(s).upper() for s in (scenarios or []) if str(s).upper() in SCENARIOS})


def consent_of(email: str, study_id: str | None = None) -> dict:
    """{given, at, version, current, withdrawn_at, content_scenarios} for the active study."""
    sid = study_id if study_id is not None else state()["study_id"]
    p = pseudonym_of(email, create=False)
    none = {"given": False, "at": None, "version": None, "current": False,
            "withdrawn_at": None, "content_scenarios": [], "study_id": sid}
    if not p or not sid:
        return none
    con = _con()
    r = con.execute("SELECT * FROM consents WHERE pseudonym = ? AND study_id = ?",
                    (p, sid)).fetchone()
    con.close()
    if r is None:
        return none
    given = r["withdrawn_at"] is None
    return {"given": given, "at": r["given_at"], "version": r["version"],
            "current": given and r["version"] == CONSENT_VERSION,
            "withdrawn_at": r["withdrawn_at"],
            "content_scenarios": json.loads(r["content_scenarios"] or "[]") if given else [],
            "study_id": sid}


def give_consent(email: str, content_scenarios=None) -> dict:
    st = state()
    if not st["study_id"]:
        raise StudyError(409, "no study on this instance")
    p = pseudonym_of(email)
    now = time.time()
    scen = json.dumps(_clean_scenarios(content_scenarios))
    con = _con()
    con.execute(
        "INSERT INTO consents(pseudonym, study_id, version, given_at, content_scenarios)"
        " VALUES(?,?,?,?,?) ON CONFLICT(pseudonym, study_id) DO UPDATE SET"
        " version = excluded.version, content_scenarios = excluded.content_scenarios,"
        # a consent given again after a withdrawal starts a new period (what was collected
        # before the withdrawal stays out of the exports)
        " given_at = CASE WHEN consents.withdrawn_at IS NULL THEN consents.given_at"
        "            ELSE excluded.given_at END, withdrawn_at = NULL",
        (p, st["study_id"], CONSENT_VERSION, now, scen))
    con.commit()
    con.close()
    return consent_of(email, st["study_id"])


def set_content_scenarios(email: str, content_scenarios) -> dict:
    c = consent_of(email)
    if not c["given"]:
        raise StudyError(409, "consent first")
    con = _con()
    con.execute("UPDATE consents SET content_scenarios = ? WHERE pseudonym = ? AND study_id = ?",
                (json.dumps(_clean_scenarios(content_scenarios)),
                 pseudonym_of(email), c["study_id"]))
    con.commit()
    con.close()
    return consent_of(email, c["study_id"])


def withdraw(email: str) -> dict:
    """Withdrawal: collection stops now and NOTHING collected under this consent is
    exported any more (the export keeps only the sessions of a consent that still holds)."""
    c = consent_of(email)
    if not c["given"]:
        return c
    con = _con()
    con.execute("UPDATE consents SET withdrawn_at = ?, content_scenarios = '[]'"
                " WHERE pseudonym = ? AND study_id = ?",
                (time.time(), pseudonym_of(email), c["study_id"]))
    con.commit()
    con.close()
    return consent_of(email, c["study_id"])


def consents(study_id: str) -> list[dict]:
    """Consents of a study, by pseudonym (no e-mail)."""
    con = _con()
    rows = []
    for r in con.execute("SELECT * FROM consents WHERE study_id = ? ORDER BY given_at",
                         (study_id,)):
        d = dict(r)
        d["content_scenarios"] = json.loads(d["content_scenarios"] or "[]")
        rows.append(d)
    con.close()
    return rows


def gate(email: str) -> str | None:
    """Why this person cannot start a turn right now, None when they can. While a study
    that requires the audit is active, a person works only once they have consented to the
    current text — the free tier is served for audited use only."""
    if not email or not enabled():
        return None
    st = state()
    if not st["active"] or not st["audit_required"]:
        return None
    if consent_of(email, st["study_id"])["current"]:
        return None
    return ("This instance takes part in a study: before your first session, read what is "
            "recorded and give (or refuse) your consent in Setup › Study. — Cette instance "
            "participe à une étude : avant votre première session, lisez ce qui est "
            "enregistré et donnez (ou refusez) votre accord dans Setup › Étude.")


# ---- what the instance sends to the gateway ---------------------------------------------
def gateway_headers(email: str, prefix: str = "") -> dict[str, str]:
    """Headers of an inference call. Outside a study: the person's e-mail (as before). In
    study mode: the pseudonym, never the e-mail, and the study."""
    email = _norm(email)
    if not enabled():
        return {"x-sokkan-user": f"{prefix}{email}"} if email else {}
    st = state()
    if not st["active"]:
        return {"x-sokkan-user": f"{prefix}{email}"} if email else {}
    out = {"x-sokkan-study": st["study_id"]}
    if email:
        out["x-sokkan-user"] = f"{prefix}{pseudonym_of(email)}"
    return out


# ---- tags -------------------------------------------------------------------------------
def _card_of_session(session_id: str) -> int | None:
    try:
        import board
        con = board._con()
        r = con.execute("SELECT id FROM cards WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                        (session_id,)).fetchone()
        con.close()
        return int(r["id"]) if r else None
    except Exception:  # noqa: BLE001
        return None


def tag_of(session_id: str) -> dict | None:
    con = _con()
    r = con.execute("SELECT * FROM tags WHERE session_id = ?", (session_id,)).fetchone()
    con.close()
    return dict(r) if r else None


def _check_tag(scenario: str | None, reference_minutes) -> tuple[str | None, float | None]:
    if scenario is not None:
        scenario = str(scenario).upper().strip()
        if scenario and scenario not in SCENARIOS:
            raise StudyError(400, f"scenario: one of {', '.join(SCENARIOS)}")
    if reference_minutes is not None:
        try:
            reference_minutes = float(reference_minutes)
        except (TypeError, ValueError):
            raise StudyError(400, "reference_minutes: a number of minutes") from None
        if not 0 <= reference_minutes <= 100000:
            raise StudyError(400, "reference_minutes: between 0 and 100000")
    return scenario, reference_minutes


def tag_session(session_id: str, owner_email: str, *, scenario: str | None = None,
                reference_minutes=None, auto: bool = False) -> dict:
    """Tag a session for the active study (participant = the person the session belongs
    to). `None` leaves a field as it is; a card's tag is inherited when the session has none."""
    st = state()
    if not st["active"]:
        raise StudyError(409, "no active study on this instance")
    scenario, reference_minutes = _check_tag(scenario, reference_minutes)
    p = pseudonym_of(owner_email)
    if not p:
        raise StudyError(400, "this session has no owner to attribute the work to")
    cur = tag_of(session_id)
    card_id = _card_of_session(session_id)
    inherited = card_tag_of(card_id) if (cur is None and card_id is not None) else None
    if cur is None:
        con = _con()
        con.execute(
            "INSERT OR IGNORE INTO tags(session_id, study_id, pseudonym, scenario,"
            " reference_minutes, card_id, auto, tagged_at) VALUES(?,?,?,?,?,?,?,?)",
            (session_id, st["study_id"], p,
             scenario if scenario is not None else ((inherited or {}).get("scenario") or ""),
             reference_minutes if reference_minutes is not None
             else (inherited or {}).get("reference_minutes"),
             card_id, 1 if auto else 0, time.time()))
        con.commit()
        con.close()
    elif not auto:
        sets, args = ["auto = 0"], []
        if scenario is not None:
            sets.append("scenario = ?")
            args.append(scenario)
        if reference_minutes is not None:
            sets.append("reference_minutes = ?")
            args.append(reference_minutes)
        con = _con()
        con.execute(f"UPDATE tags SET {', '.join(sets)} WHERE session_id = ?",
                    (*args, session_id))
        con.commit()
        con.close()
    return tag_of(session_id) or {}


def card_tag_of(card_id: int | None) -> dict | None:
    if card_id is None:
        return None
    con = _con()
    r = con.execute("SELECT * FROM card_tags WHERE card_id = ?", (int(card_id),)).fetchone()
    con.close()
    return dict(r) if r else None


def tag_card(card_id: int, *, scenario: str | None = None, reference_minutes=None) -> dict:
    """Tag a card: the sessions opened from it start with its scenario and reference time."""
    st = state()
    if not st["active"]:
        raise StudyError(409, "no active study on this instance")
    scenario, reference_minutes = _check_tag(scenario, reference_minutes)
    cur = card_tag_of(card_id) or {}
    con = _con()
    con.execute(
        "INSERT INTO card_tags(card_id, study_id, scenario, reference_minutes, tagged_at)"
        " VALUES(?,?,?,?,?) ON CONFLICT(card_id) DO UPDATE SET study_id = excluded.study_id,"
        " scenario = excluded.scenario, reference_minutes = excluded.reference_minutes",
        (int(card_id), st["study_id"],
         scenario if scenario is not None else (cur.get("scenario") or ""),
         reference_minutes if reference_minutes is not None else cur.get("reference_minutes"),
         time.time()))
    con.commit()
    con.close()
    return card_tag_of(card_id) or {}


def my_sessions(email: str, limit: int = 30) -> list[dict]:
    """The person's own study sessions, newest first, with their answers when given."""
    p = pseudonym_of(email, create=False)
    if not p:
        return []
    con = _con()
    rows = [dict(r) for r in con.execute(
        "SELECT t.*, f.useful, f.verify_minutes, f.created_at AS feedback_at,"
        " (SELECT count(*) FROM turns u WHERE u.session_id = t.session_id) AS turns,"
        " (SELECT count(*) FROM turns u WHERE u.session_id = t.session_id AND u.ended_at IS NULL"
        "   AND u.started_at > ?) AS running"
        " FROM tags t LEFT JOIN feedback f ON f.session_id = t.session_id"
        " WHERE t.pseudonym = ? ORDER BY t.tagged_at DESC LIMIT ?",
        (time.time() - RUNNING_TURN_MAX_S, p, max(1, min(limit, 200))))]
    con.close()
    return rows


# ---- turns (durations, tier served — never the text) ------------------------------------
def turn_start(session_id: str, email: str, prompt: str) -> int | None:
    """A consenting participant starts a turn in an active study: the session is tagged if
    it was not, and the turn's metadata is opened. None = not recorded."""
    try:
        if not email or not active():
            return None
        st = state()
        if not consent_of(email, st["study_id"])["given"]:
            return None
        tag = tag_of(session_id) or tag_session(session_id, email, auto=True)
        if not tag or tag.get("study_id") != st["study_id"]:
            return None
        con = _con()
        cur = con.execute(
            "INSERT INTO turns(session_id, study_id, pseudonym, started_at, prompt_chars,"
            " prompt_hmac) VALUES(?,?,?,?,?,?)",
            (session_id, st["study_id"], tag["pseudonym"], time.time(), len(prompt or ""),
             keyed(prompt or "")))
        con.commit()
        tid = cur.lastrowid
        con.close()
        return tid
    except Exception:  # noqa: BLE001 — the study never breaks a turn
        return None


def turn_end(turn_id: int | None, *, model: str = "", tokens_in: int = 0, tokens_out: int = 0,
             is_error: bool = False) -> None:
    if not turn_id:
        return
    try:
        now = time.time()
        con = _con()
        con.execute(
            "UPDATE turns SET ended_at = ?, duration_ms = CAST((? - started_at) * 1000 AS"
            " INTEGER), model = ?, tokens_in = ?, tokens_out = ?, is_error = ? WHERE id = ?",
            (now, now, model or "", max(0, int(tokens_in)), max(0, int(tokens_out)),
             1 if is_error else 0, turn_id))
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass


# ---- feedback: two short questions at the end of a task ----------------------------------
def give_feedback(session_id: str, email: str, useful, verify_minutes) -> dict:
    tag = tag_of(session_id)
    if tag is None:
        raise StudyError(404, "this session is not part of the study")
    if tag["pseudonym"] != pseudonym_of(email, create=False):
        raise StudyError(403, "only the participant answers for their own task")
    try:
        useful = int(useful)
        verify_minutes = float(verify_minutes)
    except (TypeError, ValueError):
        raise StudyError(400, "useful: 1 to 5; verify_minutes: a number of minutes") from None
    if not 1 <= useful <= 5:
        raise StudyError(400, "useful: 1 (not at all) to 5 (very)")
    if not 0 <= verify_minutes <= 100000:
        raise StudyError(400, "verify_minutes: between 0 and 100000")
    now = time.time()
    con = _con()
    con.execute(
        "INSERT INTO feedback(session_id, study_id, pseudonym, scenario, useful, verify_minutes,"
        " created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
        " useful = excluded.useful, verify_minutes = excluded.verify_minutes,"
        " scenario = excluded.scenario, created_at = excluded.created_at",
        (session_id, tag["study_id"], tag["pseudonym"], tag["scenario"], useful, verify_minutes,
         now))
    con.execute("UPDATE tags SET closed_at = coalesce(closed_at, ?) WHERE session_id = ?",
                (now, session_id))
    con.commit()
    r = dict(con.execute("SELECT * FROM feedback WHERE session_id = ?", (session_id,)).fetchone())
    con.close()
    return r


def pending_feedback(email: str) -> int:
    """Study sessions of the person that have turns, no answers yet, and no turn running:
    the agent is still working (or waits for a validation) = the task is not over, nobody is
    asked « two answers » about it yet. A turn left open by a process that died stops
    counting as running after RUNNING_TURN_MAX_S."""
    return sum(1 for s in my_sessions(email, 200)
               if s["turns"] and s["feedback_at"] is None and not s["running"])


# ---- export tokens: read-only, limited to the researcher's package -----------------------
def create_token(label: str, days: int | None = 30) -> dict:
    st = state()
    tok = TOKEN_PREFIX + _secrets.token_urlsafe(32)
    now = time.time()
    exp = None if not days else now + max(1, min(int(days), 400)) * 86400
    con = _con()
    cur = con.execute(
        "INSERT INTO tokens(label, token_sha256, hint, study_id, created_at, expires_at)"
        " VALUES(?,?,?,?,?,?)",
        ((label or "").strip()[:80], hashlib.sha256(tok.encode()).hexdigest(), tok[-4:],
         st["study_id"], now, exp))
    con.commit()
    tid = cur.lastrowid
    con.close()
    return {"id": tid, "token": tok, "label": (label or "").strip()[:80], "expires_at": exp,
            "study_id": st["study_id"]}


def list_tokens() -> list[dict]:
    con = _con()
    rows = [dict(r) for r in con.execute(
        "SELECT id, label, hint, study_id, created_at, expires_at, revoked_at,"
        " last_used_at, uses FROM tokens ORDER BY id DESC")]
    con.close()
    return rows


def revoke_token(token_id: int) -> bool:
    con = _con()
    cur = con.execute("UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                      (time.time(), int(token_id)))
    con.commit()
    n = cur.rowcount
    con.close()
    return bool(n)


def check_token(token: str) -> dict | None:
    """The token row when `token` is a live export token (and count the use), else None."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    h = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    con = _con()
    r = con.execute("SELECT * FROM tokens WHERE token_sha256 = ?", (h,)).fetchone()
    ok = (r is not None and hmac.compare_digest(r["token_sha256"], h)
          and r["revoked_at"] is None and (r["expires_at"] is None or r["expires_at"] > now))
    if ok:
        con.execute("UPDATE tokens SET last_used_at = ?, uses = uses + 1 WHERE id = ?",
                    (now, r["id"]))
        con.commit()
    out = dict(r) if ok else None
    con.close()
    return out


def log_export(by: str, study_id: str, mode: str, counts: dict, digest: str) -> None:
    con = _con()
    con.execute("INSERT INTO exports(at, by, study_id, mode, counts, sha256) VALUES(?,?,?,?,?,?)",
                (time.time(), by if by.startswith("token:") else "admin", study_id, mode,
                 json.dumps(counts), digest))
    con.commit()
    con.close()


def exports(limit: int = 20) -> list[dict]:
    con = _con()
    rows = []
    for r in con.execute("SELECT * FROM exports ORDER BY id DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["counts"] = json.loads(d["counts"] or "{}")
        rows.append(d)
    con.close()
    return rows
