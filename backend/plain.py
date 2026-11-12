#!/usr/bin/env python3
"""plain.py — the plain-language view of a session's events (feature `simple_profile`).

A session emits developer events: ``tool_use mcp__sokkan-memory__memory_search``, a
``permission`` for ``Write /workspace/deliverables/…/plan_v2.md``, a first user message that
carries the whole internal brief. The simple profile's thread never shows those. `View`
turns the same stream into what a person who never saw a terminal can read:

    tool_use / tool_result   → activity {kind, subject} / activity_done {ok}
                               (kind = a word of the UI's dictionary: read_document,
                                search_memory, write_draft… — the sentence is the UI's, in
                                the person's language; the raw tool, title and input stay in
                                `detail`, shown only in the folded « technical detail »)
    permission               → approval {kind, title, version, sources[], draft, detail}
                               (« the agent wants to save "Training plan – v2" », the person's
                                documents it used with their date, the draft to read first)
    user (replayed)          → user, the internal brief stripped
    user_echo                → user (what the person typed, as sent)
    a saved deliverable      → deliverable {name, title, version}
    error / sandbox          → notice {code, detail}
    thinking, model, session, perm_mode, result → nothing

One `View` per WebSocket connection, fed in order (it remembers which tool call wrote what).
Pure functions of the events plus two lookups (the session's documents, its deliverables
directory): unit-tested without a model.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

BRIEF_RX = re.compile(r"<sokkan-brief>.*?</sokkan-brief>\s*", re.S)
_REMINDER_RX = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)
# the study refusals of the inference gateway (sokkan-cloud infer/pilote-etude.md § Refus):
# each has its own sentence in the simple profile's dictionary (`no.<code>`)
STUDY_NOTICES = ("study_expired", "study_mode_required", "user_daily_cap", "user_limit_reached")


def _local_model_remains() -> bool:
    try:
        import study
        return study.local_model_remains()
    except Exception:  # noqa: BLE001 — the view never fails on a lookup
        return False


def _study_code(text: str) -> str | None:
    try:
        import study
        code = study.refusal_code(text)
    except Exception:  # noqa: BLE001 — the view never fails on a lookup
        return None
    return code if code in STUDY_NOTICES else None


_CLAUDE_REFUSAL = re.compile(r"\[sokkan:(claude_[a-z_]+)\]")
_ENGINE_ERROR = re.compile(r"\s*(API Error\b|Credit balance is too low|Invalid API key)", re.I)
_EDIT = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_DETAIL_MAX = 4000
DRAFT_MAX = 120_000


def strip_brief(text: str) -> str:
    """What the person typed, without what SOKKAN added around it for the agent."""
    return _REMINDER_RX.sub("", BRIEF_RX.sub("", text or "")).strip()


def human_title(name: str) -> str:
    """« plan-de-formation_excel.md » → « Plan de formation excel »."""
    stem = Path(name or "").name
    stem = re.sub(r"\.(md|markdown|txt|docx|html?)$", "", stem, flags=re.I)
    stem = re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", stem)).strip()
    stem = re.sub(r"\s+v(?:ersion)?\s*\d+$", "", stem, flags=re.I).strip() or stem
    return (stem[:1].upper() + stem[1:]) if stem else "Document"


def title_from_markdown(text: str) -> str:
    for line in (text or "").splitlines()[:40]:
        m = re.match(r"\s{0,3}#{1,2}\s+(.+?)\s*#*\s*$", line)
        if m:
            t = re.sub(r"[*_`]", "", m.group(1)).strip()
            if t:
                return t[:90]
    return ""


def _short(v, n: int = _DETAIL_MAX):
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + "…"
    if isinstance(v, dict):
        return {k: _short(x, n) for k, x in list(v.items())[:30]}
    if isinstance(v, list):
        return [_short(x, n) for x in v[:30]]
    return v


def _host(url: str) -> str:
    m = re.match(r"^[a-z]+://([^/:?#]+)", url or "", flags=re.I)
    return m.group(1) if m else ""


def _engine(model, lang: str) -> dict | None:
    try:
        import claudetier
        return claudetier.describe_model(model, lang)
    except Exception:  # noqa: BLE001 — a badge never breaks the thread
        return None


class View:
    """Stateful translator for ONE connection. ``docs_dir`` / ``deliv_dir`` are the session's
    documents and deliverables directories; ``doc_lookup(id)`` returns a document dict (or
    None); ``on_write(name, tool_id)`` registers a saved deliverable and returns its version."""

    def __init__(self, *, docs_dir: Path | str | None = None, deliv_dir: Path | str | None = None,
                 doc_lookup=None, on_write=None, attached: int = 0, lang: str = "fr"):
        self.docs_dir = Path(docs_dir).resolve() if docs_dir else None
        self.deliv_dir = Path(deliv_dir).resolve() if deliv_dir else None
        self.doc_lookup = doc_lookup or (lambda _id: None)
        self.on_write = on_write or (lambda name, tool_id: 1)
        self.attached = attached
        self.lang = lang if lang in ("fr", "en") else "fr"
        self._tools: dict[str, dict] = {}       # tool_use id → {kind, deliverable}
        self._used: dict[str, dict] = {}        # doc id → source (documents the agent opened)
        self._writes: dict[str, int] = {}       # deliverable name → version seen in this view
        self._refused: str | None = None        # study refusal already said in this turn

    # ---- paths ---------------------------------------------------------------------------
    def _resolve(self, p: str) -> Path | None:
        if not p or not isinstance(p, str):
            return None
        try:
            return Path(os.path.abspath(os.path.expanduser(p)))
        except (OSError, ValueError):
            return None

    def _doc_of(self, p: str) -> dict | None:
        """The dropped document a path belongs to (the file or its text copy)."""
        rp = self._resolve(p)
        if rp is None or self.docs_dir is None:
            return None
        try:
            rel = rp.relative_to(self.docs_dir)
        except ValueError:
            return None
        if len(rel.parts) < 2:
            return None
        return self.doc_lookup(rel.parts[0])

    def _under(self, p: str, base: Path | None) -> bool:
        rp = self._resolve(p)
        if rp is None or base is None:
            return False
        return rp == base or base in rp.parents

    def _deliverable_of(self, p: str) -> str | None:
        rp = self._resolve(p)
        if rp is None or self.deliv_dir is None:
            return None
        return rp.name if rp.parent == self.deliv_dir else None

    def _source(self, doc: dict) -> dict:
        return {"id": doc["id"], "name": doc["name"], "ext": doc["ext"],
                "modified_at": doc.get("modified_at") or doc.get("created_at")}

    # ---- classification of a tool call ---------------------------------------------------
    def classify(self, tool: str, inp: dict) -> dict:
        """→ {kind, subject?, deliverable?} — never a tool name, a path or a command."""
        inp = inp if isinstance(inp, dict) else {}
        path = str(inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "")
        if tool == "Read":
            doc = self._doc_of(path)
            if doc:
                self._used[doc["id"]] = self._source(doc)
                return {"kind": "read_document", "subject": doc["name"]}
            name = self._deliverable_of(path)
            if name:
                return {"kind": "read_draft", "subject": human_title(name)}
            return {"kind": "read_file", "subject": Path(path).name if path else ""}
        if tool in ("Glob", "Grep"):
            where = path or str(inp.get("pattern") or "")
            if self.docs_dir is not None and (self._under(path, self.docs_dir)
                                              or str(self.docs_dir) in where):
                doc = self._doc_of(path)
                if doc:
                    self._used[doc["id"]] = self._source(doc)
                return {"kind": "search_documents"}
            return {"kind": "search_files"}
        if tool in _EDIT:
            name = self._deliverable_of(path)
            if name:
                return {"kind": "write_draft", "subject": human_title(name), "deliverable": name}
            return {"kind": "write_file", "subject": Path(path).name if path else ""}
        if tool == "mcp__sokkan-memory__memory_search":
            return {"kind": "search_memory", "subject": str(inp.get("query") or "")[:120]}
        if tool in ("mcp__sokkan-memory__memory_get", "mcp__sokkan-memory__memory_links"):
            return {"kind": "read_memory"}
        if tool == "mcp__sokkan-memory__memory_write":
            # the note's own title when its text opens with one (« # Procédure de devis »), else
            # one out of its description (3.6.4): the name is a slug, without accents
            # (« Procedure de devis ») — the last resort only. Same rule as the Memory page.
            import notetitle
            return {"kind": "save_memory", "subject": notetitle.display_title(
                str(inp.get("name") or ""), str(inp.get("description") or ""),
                None, str(inp.get("body") or ""))[:90]}
        if tool == "WebSearch":
            return {"kind": "web_search", "subject": str(inp.get("query") or "")[:120]}
        if tool == "WebFetch":
            return {"kind": "web_read", "subject": _host(str(inp.get("url") or ""))}
        if tool == "ToolSearch":
            return {"kind": "other"}
        if tool == "Bash":
            return {"kind": "run_command"}
        if tool in ("Task", "Agent"):
            return {"kind": "delegate", "subject": str(inp.get("description") or "")[:120]}
        if tool == "TodoWrite":
            return {"kind": "plan_steps"}
        if tool.startswith("mcp__sokkan-board__"):
            return {"kind": "board"}
        if tool.startswith("mcp__sokkan-agents__"):
            return {"kind": "agents"}
        if tool.startswith("mcp__sokkan-observability__"):
            return {"kind": "monitoring"}
        return {"kind": "other"}

    def _detail(self, ev: dict) -> dict:
        return {"tool": ev.get("tool") or "", "title": _short(ev.get("title") or "", 400),
                "input": _short(ev.get("input") or {})}

    def _draft(self, tool: str, inp: dict) -> dict | None:
        if tool == "Write" and isinstance(inp.get("content"), str):
            text = inp["content"]
        elif tool in ("Edit", "MultiEdit"):
            edits = inp.get("edits") if tool == "MultiEdit" else [inp]
            parts = [str(e.get("new_string") or "") for e in (edits or []) if isinstance(e, dict)]
            text = "\n\n…\n\n".join(p for p in parts if p)
            if not text:
                return None
            return {"text": text[:DRAFT_MAX], "partial": True, "truncated": len(text) > DRAFT_MAX}
        else:
            return None
        return {"text": text[:DRAFT_MAX], "partial": False, "truncated": len(text) > DRAFT_MAX}

    # ---- the translation ------------------------------------------------------------------
    def feed(self, ev: dict) -> list[dict]:
        t = ev.get("type")
        if t in ("status", "question", "question_resolved"):
            return [ev]
        if t == "text":
            text = ev.get("text") or ""
            cm = _CLAUDE_REFUSAL.search(text) if "[sokkan:claude_" in text else None
            if cm:
                # 3.6.5: the router refused a « Write with Claude » request — its own sentence,
                # in the person's language, once per turn (the CLI prefixes a 403 with « Failed
                # to authenticate. », and its error result would add « a technical problem »)
                import llmerrors
                if cm.group(1) not in llmerrors.CODES or self._refused == "claude":
                    return []
                self._refused = "claude"
                h = llmerrors.humanize(cm.group(1), self.lang)
                return [{"type": "notice", "code": "claude", "title": h["title"], "detail": h["message"]}]
            if _ENGINE_ERROR.match(text):
                # the CLI reports an unreachable / failing engine as an assistant sentence
                code = _study_code(text)
                if code:
                    # … or a refusal of the gateway about the study: said once, as what it is
                    # (« the agent ran into a technical problem, send it again » would be false)
                    return self._refusal(code, text)
                return [{"type": "notice", "code": "engine", "detail": _short(text, 1000)}]
            out = {"type": "text", "text": text}
            eng = _engine(ev.get("model"), self.lang)
            if eng:
                out["engine"] = eng     # 3.6.5: the model that answered, on every answer
            return [out]
        if t in ("user_echo", "user"):
            self._refused = None                # a new turn: a refusal is said again
        if t == "user_echo":
            return [{"type": "user", "text": ev.get("text") or "",
                     "documents": ev.get("documents") or []}]
        if t == "user":
            text = strip_brief(ev.get("text") or "")
            return [{"type": "user", "text": text, "documents": []}] if text else []
        if t == "tool_use":
            tool, inp = ev.get("tool") or "", ev.get("input") or {}
            c = self.classify(tool, inp)
            tid = ev.get("id") or ""
            if tid:
                self._tools[tid] = {**c, "model": ev.get("model") or ""}
            return [{"type": "activity", "id": tid, "kind": c["kind"],
                     "subject": c.get("subject") or "", "detail": self._detail(ev)}]
        if t == "tool_result":
            tid = ev.get("tool_use_id") or ""
            c = self._tools.get(tid) or {}
            ok = not ev.get("is_error")
            out = [{"type": "activity_done", "id": tid, "ok": ok,
                    "detail": {"result": _short(ev.get("text") or "", 2000)}}]
            if ok and c.get("deliverable"):
                name = c["deliverable"]
                model = c.get("model") or ""
                try:
                    version = int(self.on_write(name, tid, model) or 1)
                except TypeError:           # a two-argument callback (before 3.6.5)
                    version = int(self.on_write(name, tid) or 1)
                self._writes[name] = version
                card = {"type": "deliverable", "name": name,
                        "title": human_title(name), "version": version}
                eng = _engine(model, self.lang)
                if eng:
                    card["engine"] = eng
                out.append(card)
            return out
        if t == "permission":
            tool, inp = ev.get("tool") or "", ev.get("input") or {}
            c = self.classify(tool, inp)
            draft = self._draft(tool, inp) if c["kind"] in ("write_draft", "write_file") else None
            title = c.get("subject") or ""
            if c["kind"] == "write_draft" and draft and not draft["partial"]:
                title = title_from_markdown(draft["text"]) or title
            version = 0
            if c.get("deliverable"):
                version = self._writes.get(c["deliverable"], 0) + 1
            return [{"type": "approval", "id": ev.get("id") or "", "kind": c["kind"],
                     "title": title, "version": version,
                     "sources": sorted(self._used.values(), key=lambda s: s["name"].lower()),
                     "attached": self.attached,
                     # a save in the person's own space sends nothing outside the instance
                     "stays_inside": c["kind"] in ("write_draft", "write_file", "save_memory"),
                     "draft": draft, "detail": self._detail(ev)}]
        if t == "permission_resolved":
            return [{"type": "approval_resolved", "id": ev.get("id") or ""}]
        if t == "error":
            msg = str(ev.get("message") or "")
            if str(ev.get("code") or "").startswith("claude_") or \
                    (self._refused == "claude" and not ev.get("code")):
                if self._refused == "claude":
                    return []      # the error result of the same refusal: already said
                return self.feed({"type": "text", "text": f"[sokkan:{ev['code']}]"})
            low = msg.lower()
            if ev.get("code") in STUDY_NOTICES:
                # a refusal of the inference gateway about the study (backend/study.py): its
                # own notice, in the person's language — never « you can send it again »
                return self._refusal(ev["code"], msg)
            code = ("busy" if "already running" in low else
                    "budget" if "budget" in low else
                    "read_only" if "lecture seule" in low or "read-only" in low else
                    "mode_locked" if "approval mode" in low else "engine")
            return [{"type": "notice", "code": code, "detail": _short(msg, 1000)}]
        if t == "sandbox":
            return [{"type": "notice", "code": "sandbox",
                     "detail": _short(str(ev.get("message") or ""), 1000)}] if ev.get("denied") else []
        return []      # thinking, model, session, perm_mode, result: not for this thread

    def _refusal(self, code: str, detail: str) -> list[dict]:
        """One notice per turn for a study refusal: the session reports it twice (the CLI's
        « API Error » line, then the sentence of backend/study.py)."""
        if self._refused == code:
            return []
        self._refused = code
        if code == "user_daily_cap" and _local_model_remains():
            # an appliance: the ceiling closes the stronger model only, the person can go on
            # at once with the model of the box — its own sentence (`no.user_daily_cap_local`)
            code = "user_daily_cap_local"
        return [{"type": "notice", "code": code, "detail": _short(detail, 1000)}]

    def sources(self) -> list[dict]:
        return sorted(self._used.values(), key=lambda s: s["name"].lower())


def readable_line(line: str, lang: str = "fr") -> str:
    """A preview that would show a router's code (« [sokkan:claude_forbidden] », or the same
    after markdown cleaning: « [sokkan:claude forbidden] ») → the readable title, else ''.
    Any other line is returned as is."""
    m = re.search(r"sokkan:([a-z]+(?:[ _][a-z]+)*)", line or "")
    if not m:
        return line
    code = m.group(1).replace(" ", "_")
    try:
        import llmerrors
        return llmerrors.humanize(code, lang)["title"] if code in llmerrors.CODES else ""
    except Exception:  # noqa: BLE001
        return ""


def last_line(events: list[dict], limit: int = 140, lang: str = "fr") -> str:
    """The last sentence the agent wrote — the preview under a recent work. A refusal of the
    router is previewed as its readable title (3.6.5), never as its code."""
    for ev in reversed(events or []):
        if ev.get("type") == "text" and "[sokkan:" in (ev.get("text") or ""):
            said = readable_line(ev["text"], lang)
            if said:
                return said
            continue
        if ev.get("type") == "text" and (ev.get("text") or "").strip() \
                and not _ENGINE_ERROR.match(ev["text"]):
            text = re.sub(r"```.*?```", " ", ev["text"], flags=re.S)
            text = re.sub(r"[#*_`>|]+", " ", text)
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if not lines:
                continue
            last = lines[-1]
            sentences = [s for s in re.split(r"(?<=[.!?…])\s+", last) if s.strip()]
            out = (sentences[-1] if sentences else last).strip()
            out = re.sub(r"\s+", " ", out)
            return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"
    return ""
