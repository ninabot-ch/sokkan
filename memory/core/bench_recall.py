"""Bench "ignored facts" for the recall at every turn (P0-3) — no model call needed.

A fictional corpus (a small company, *Orchard Works*) where each scenario's answer is ONLY
in a note, plus look-alike notes that must not win. Each question is posed:

* at the 1st turn of a fresh session,
* at the 5th turn, after four unrelated messages (generic coding chatter),
* as the prompt of a sub-agent (``PreToolUse`` on Task/Agent),

and the bench checks what the recall INJECTS (the expected note is in the block or not),
not what a model answers: it measures the mechanism without spending model credit. The
generic messages double as negatives: a recall on "run the tests again" is noise in the
context, so their injection rate is the false-positive rate of the threshold.

    python -m core.bench_recall --dsn postgresql://… [--load] [--profile gpu] [--json]

``--load`` indexes the fictional corpus into the store first (use a throw-away database).
Embedding / reranker servers come from the ``CORTHEXIS_*`` configuration.
"""
from __future__ import annotations

import statistics
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- corpus

def _note(name: str, description: str, body: str, type_: str = "project") -> str:
    return (f"---\nname: {name}\ndescription: {description}\nmetadata:\n  type: {type_}\n"
            f"  modified: '2026-03-{(sum(map(ord, name)) % 27) + 1:02d}T10:00:00+00:00'\n"
            f"---\n\n{body}\n")


FACTS = {
    "mailbox-access-clara": (
        "Mail access granted to the assistant — which mailboxes it may read",
        "The assistant HAS read-only access to Clara's personal mailbox (clara.home), through "
        "an IMAP delegation set up on 2026-02-14. Credentials are in the vault folder "
        "ops/clara-imap. Sending from that box is not allowed."),
    "billing-api-staging": (
        "Staging environment of the billing service",
        "The staging billing API listens on port 8443 behind the internal proxy; production "
        "uses 443. Staging data is reset every Monday at 06:00."),
    "ads-credit-voucher": (
        "Advertising budget situation",
        "We still hold 1 800 EUR of free search-advertising credit from the startup "
        "programme. It expires on 2026-06-30; campaigns paused in July 2025 because the card "
        "on file had expired, not because of the credit."),
    "release-gate-owner": (
        "Who signs off production deployments",
        "Only Mira can approve a production release: she runs the promote script after "
        "reading the staging checklist. Nobody else promotes, even in an emergency — call her."),
    "backup-sunday-quota": (
        "Weekly offsite copy — known failure",
        "The night backup to the Basel bucket fails every Sunday because the weekly full "
        "copy exceeds the 500 GB quota; the incremental copies of the other days succeed. "
        "Fix pending: ask the provider for 1 TB."),
    "render-node-gpu": (
        "Hardware of the render node",
        "Since 2026-03-10 the render node runs two Intel Arc cards; the old NVIDIA card was "
        "removed. nvidia-smi failing there is normal, use clinfo to list the devices."),
    "newsletter-sending": (
        "How the monthly newsletter is delivered",
        "The newsletter is sent through the Postwave provider, not our own SMTP server, "
        "from news@orchard.example, with a dedicated sending domain and DKIM."),
    "decision-async-standup": (
        "Team ritual change decided in March",
        "Decision of 2026-03-03: the Tuesday standup meeting is replaced by a written "
        "async update in the team channel before 10:00; the Friday demo stays live."),
    "browser-handoff-vnc": (
        "Shared automated browser — what to do when a site blocks it",
        "When a captcha or a bot check appears in the shared automated browser, call the "
        "handoff tool: the owner gets a phone notification and takes over through VNC on "
        "port 5901, then the agent resumes. Never retry in a loop."),
    "packaging-supplier": (
        "Where the shop's boxes come from",
        "Cardboard packaging is ordered from Kartonage Weiss in Biel, minimum order 500 "
        "boxes, delivery in 6 working days; the account number is on the last invoice."),
    "incident-payment-terminal-may": (
        "Card reader outage at the counter, May 2025",
        "On 2025-05-17 the payment terminal at the counter stopped accepting cards for 3 "
        "hours: an expired TLS certificate on the acquirer side. Workaround used: QR "
        "payments. Lesson: keep the QR stand ready."),
    "promo-code-first-month": (
        "Discounts available for new subscribers",
        "The code ORCHARD-START gives 50 % off the first month of any plan; it is single "
        "use per customer and valid until 2026-12-31."),
}

DISTRACTORS = {
    "mailbox-shared-support": ("Shared support inbox rota",
                               "support@ is read by whoever is on rota; replies within 24 h."),
    "billing-api-production": ("Production billing service runbook",
                               "Restart order: database first, then the API, then workers."),
    "ads-campaign-design": ("Visual guidelines of the search ads",
                            "Headlines under 30 characters, no exclamation marks."),
    "release-notes-style": ("How we write release notes",
                            "One line per change, user-facing words, no ticket numbers."),
    "backup-database-restore": ("Restoring the database from a dump",
                                "pg_restore into a scratch database, then compare row counts."),
    "render-queue-priorities": ("Render queue priorities",
                                "Client jobs before internal previews; max 4 jobs in parallel."),
    "newsletter-editorial-calendar": ("Newsletter topics per month",
                                      "January recap, April product, September events."),
    "meeting-room-booking": ("Booking the meeting room",
                             "Book through the shared calendar, 2 h maximum per slot."),
    "browser-profile-cookies": ("Browser profiles of the automation",
                                "One profile per customer account, never mixed."),
    "shop-opening-hours": ("Shop opening hours",
                           "Tuesday to Saturday, 9:00-18:30, closed on Mondays."),
    "incident-website-downtime-2024": ("Website outage of 2024",
                                       "DNS misconfiguration during a registrar change, 2 h."),
    "pricing-plans-2026": ("Subscription plans and prices 2026",
                           "Starter 19 EUR, Team 49 EUR, Studio 99 EUR per month."),
    "coding-style-python": ("Python coding conventions",
                            "ruff, line length 100, type hints on public functions, pytest."),
    "ci-pipeline-overview": ("How the CI pipeline runs",
                             "Lint, unit tests, build image, deploy to staging on main."),
    "laptop-setup-new-hire": ("Laptop setup for a new hire",
                              "Disk encryption, password manager, VPN profile, then git."),
    "vendor-coffee-machine": ("Coffee machine maintenance",
                              "Descale every 2 weeks; the technician comes in October."),
}

# (question, expected note) — phrased without the note's key words where possible
SCENARIOS = [
    ("Can you read my wife Clara's private emails, or do you not have access to that?",
     "mailbox-access-clara"),
    ("On which port should I point the client to test invoices before they go live?",
     "billing-api-staging"),
    ("Do we still have free money for Google search ads, and until when can we use it?",
     "ads-credit-voucher"),
    ("I want to push this fix to prod tonight myself, is that ok?", "release-gate-owner"),
    ("The offsite copy job is red again this morning, it's Monday, any idea why?",
     "backup-sunday-quota"),
    ("nvidia-smi says no devices found on the render machine, is the GPU dead?",
     "render-node-gpu"),
    ("Should I configure our postfix relay for the monthly mailing?", "newsletter-sending"),
    ("Are we still meeting tomorrow morning for the weekly sync call?",
     "decision-async-standup"),
    ("The scraper hit a 'verify you are human' page, what should the agent do?",
     "browser-handoff-vnc"),
    ("We are running out of shipping boxes, who do I reorder from?", "packaging-supplier"),
    ("Has the card machine at the till ever broken down before, what did we do then?",
     "incident-payment-terminal-may"),
    ("A new customer asks if there is any discount for starting, what can I offer?",
     "promo-code-first-month"),
]

# generic messages of a coding session: nothing to recall (negatives, and the filler turns)
FILLER = [
    "Rename the variable tmp to buffer in utils.py please",
    "Run the whole test suite again and tell me what fails",
    "thanks, that looks good to me",
    "Explain this stack trace: KeyError 'id' in handlers.py line 42",
    "Can you add type hints to the parse function?",
    "Refactor this loop into a list comprehension",
    "What does the --force flag of git push do exactly?",
    "Write a docstring for the class below",
    "Merci, tu peux continuer avec la suite",
    "Mach bitte die Tests grün und committe danach",
    "Make the button blue and center the title",
    "Why is this regex not matching the trailing slash?",
]


def write_corpus(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, (desc, body) in {**FACTS, **DISTRACTORS}.items():
        (directory / (name.replace("-", "_") + ".md")).write_text(
            _note(name, desc, body), encoding="utf-8")
    return directory


# --------------------------------------------------------------------------- bench

@dataclass
class Outcome:
    mode: str
    question: str
    expected: str
    injected: list[str]
    latency_ms: int

    @property
    def hit(self) -> bool:
        return self.expected in self.injected


def run(recaller, *, filler_turns: int = 4) -> dict:
    """Pose every scenario at turn 1, at turn ``filler_turns + 1`` and in a sub-agent."""
    outcomes: list[Outcome] = []
    noise: list[tuple[str, list[str], int]] = []
    for i, (q, expected) in enumerate(SCENARIOS):
        sid = f"bench-{uuid.uuid4().hex[:8]}"
        r = recaller.recall(q, session_id=sid, channel="prompt")
        outcomes.append(Outcome("turn-1", q, expected, r.notes, r.latency_ms))

        sid = f"bench-{uuid.uuid4().hex[:8]}"
        for j in range(filler_turns):
            f = FILLER[(i + j) % len(FILLER)]
            rf = recaller.recall(f, session_id=sid, channel="prompt")
            noise.append((f, rf.notes, rf.latency_ms))
        r = recaller.recall(q, session_id=sid, channel="prompt")
        outcomes.append(Outcome(f"turn-{filler_turns + 1}", q, expected, r.notes,
                                r.latency_ms))

        r = recaller.recall(q, session_id=f"bench-{uuid.uuid4().hex[:8]}", channel="subagent",
                            agent_id="toolu_bench")
        outcomes.append(Outcome("subagent", q, expected, r.notes, r.latency_ms))

    # the same note is not injected twice in one session
    sid = f"bench-{uuid.uuid4().hex[:8]}"
    q, expected = SCENARIOS[0]
    first = recaller.recall(q, session_id=sid, channel="prompt").notes
    again = recaller.recall(q + " (asking again)", session_id=sid, channel="prompt").notes

    by_mode: dict[str, dict] = {}
    for o in outcomes:
        m = by_mode.setdefault(o.mode, {"n": 0, "hits": 0, "injected": 0, "lat": []})
        m["n"] += 1
        m["hits"] += o.hit
        m["injected"] += len(o.injected)
        m["lat"].append(o.latency_ms)
    lat_all = [o.latency_ms for o in outcomes] + [n[2] for n in noise]
    return {
        "scenarios": len(SCENARIOS),
        "by_mode": {k: {"hit_rate": round(v["hits"] / v["n"], 3), "hits": v["hits"],
                        "n": v["n"], "notes_per_turn": round(v["injected"] / v["n"], 2),
                        "p50_ms": int(statistics.median(v["lat"]))}
                    for k, v in by_mode.items()},
        "filler": {"turns": len(noise),
                   "with_recall": sum(1 for n in noise if n[1]),
                   "false_positive_rate": round(sum(1 for n in noise if n[1]) / len(noise), 3)
                   if noise else None},
        "dedup": {"first": first, "again": again,
                  "ok": expected in first and expected not in again},
        "latency_ms": {"p50": int(statistics.median(lat_all)),
                       "p95": int(sorted(lat_all)[int(0.95 * (len(lat_all) - 1))]),
                       "max": max(lat_all)},
        "misses": [{"mode": o.mode, "q": o.question, "expected": o.expected,
                    "injected": o.injected} for o in outcomes if not o.hit],
        "filler_recalls": [{"q": n[0], "injected": n[1]} for n in noise if n[1]],
    }


def load(store, embedder, *, log=None) -> int:
    """Index the fictional corpus into ``store`` (a throw-away database)."""
    from .indexer import IndexConfig, Indexer

    d = write_corpus(Path(tempfile.mkdtemp(prefix="corthexis-bench-recall-")))
    rep = Indexer(store, embedder, IndexConfig(memory_dir=d, write_index=False),
                  log=log or (lambda m: None)).run()
    return rep.notes


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="python -m core.bench_recall")
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--load", action="store_true", help="index the fictional corpus first")
    ap.add_argument("--profile", help="leger | standard | gpu (default: configured)")
    ap.add_argument("--threshold", type=float)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    from . import embed
    from .recall import RecallConfig, Recaller
    from .store import Store

    store = Store(a.dsn)
    e = embed.build(a.profile) if a.profile else embed.get()
    if a.load:
        t = time.monotonic()
        n = load(store, e)
        print(f"indexed {n} notes in {time.monotonic() - t:.1f} s", file=sys.stderr)
    cfg = RecallConfig.from_env(**({"threshold": a.threshold} if a.threshold else {}))
    res = run(Recaller(store, e, cfg, profile=a.profile))
    res["profile"] = a.profile or embed.current_profile()
    res["rerank_inline"] = getattr(e, "rerank_policy", "off") == "interactive"
    print(json.dumps(res, indent=1, ensure_ascii=False) if a.json else
          json.dumps({k: res[k] for k in ("profile", "by_mode", "filler", "dedup",
                                          "latency_ms")}, indent=1))
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
