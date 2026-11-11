"""The refusals of the inference gateway, as the gateway ANSWERS them — and what a person reads.

Inputs come from tests/fixtures/gateway_contract/contract.json: the bodies the gateway of
sokkan-cloud `study-quotas` (f260ada) answered when its app was played on a throw-away
database (see `_source` in the file; tests/test_pilot_integration.py compares the file with a
checkout when one is at hand). Nothing here is written from the instance's code.

What must hold for the four refusals about the study — 410 `study_expired`, 412
`study_mode_required` (its text changed on f260ada: it no longer names the study nor the
header), 429 `user_daily_cap` (a person's own ceiling), 429 `user_limit_reached` (too many
distinct people today, `max_users_day`):

* the local router hands them back as they are, once, with `x-should-retry: false` — the
  gateway sends no such header and the CLI retries a bare 429 for minutes;
* the session says ONE sentence, with the gateway's code, and not the generic « wait a
  minute and send again » of a 429 on top of it;
* the simple profile shows the notice of that code in the person's language (French and
  English dictionaries), never « a technical problem, you can send your request again »;
* what the person reads holds no status code, no variable, no header name.
"""
from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = json.loads((ROOT / "tests" / "fixtures" / "gateway_contract" / "contract.json").read_text(encoding="utf-8"))
REFUSALS = {r["code"]: r for r in CONTRACT["refusals"]}
CODES = ("study_expired", "study_mode_required", "user_daily_cap", "user_limit_reached")
# nothing of this in a sentence meant for a person who is not technical
TECHNICAL = re.compile(r"SOKKAN_[A-Z]|x-sokkan|\b4\d\d\b|\bHTTP\b|\bAPI\b|header|en-tête|token|jeton|"
                       r"max_users|tenant|gateway|passerelle", re.I)


def shown_by_the_cli(r: dict) -> str:
    """Claude Code prints a refusal as « API Error: <status> <message> »."""
    return f'API Error: {r["status"]} {r["message"]}'


def test_the_contract_holds_the_four_study_refusals_and_the_new_412():
    assert tuple(REFUSALS) == CODES
    assert (REFUSALS["user_limit_reached"]["status"], REFUSALS["user_limit_reached"]["type"]) == (429, "rate_limit_error")
    assert REFUSALS["user_limit_reached"]["body"]["error"]["code"] == "user_limit_reached"
    assert "too many distinct users today" in REFUSALS["user_limit_reached"]["message"]
    m412 = REFUSALS["study_mode_required"]["message"]
    assert "tied to a study" in m412 and "x-sokkan-study" not in m412 and "heg-eskills" not in m412
    assert "code" not in CONTRACT["tenant_cap"]["body"]["error"]        # the tenant's own 429 has none


# ---- backend/study.py: the code and the sentence ------------------------------------------
@pytest.mark.parametrize("code", CODES)
def test_a_refusal_is_recognised_by_its_words_and_by_its_code(code):
    import study
    r = REFUSALS[code]
    assert study.refusal_code(shown_by_the_cli(r)) == code            # what the CLI prints
    assert study.refusal_code(json.dumps(r["body"])) == code          # the JSON body
    assert study.refusal_code(code) == code                           # the header x-sokkan-error
    said = study.explain_refusal(shown_by_the_cli(r))
    assert said and said == study.explain_refusal(json.dumps(r["body"]))
    en, fr = said.split(" — ", 1)
    for part in (en, fr):
        assert len(part) > 60 and not TECHNICAL.search(part), (code, part)
    assert re.search(r"[éèàç]", fr) and not re.search(r"[éèàç]", en)


def test_the_four_sentences_are_four_different_things_to_do():
    import study
    said = {c: study.explain_refusal(shown_by_the_cli(REFUSALS[c])) for c in CODES}
    assert len(set(said.values())) == 4
    # too many people today is NOT « your own ceiling » — and neither says « try again »
    for code in ("user_limit_reached", "study_mode_required"):
        assert "will not help" in said[code] and "ne changera rien" in said[code]
        assert "administrat" in said[code]
    assert "nombre de personnes" in said["user_limit_reached"] and "number of people" in said["user_limit_reached"]
    assert "plafond personnel" in said["user_daily_cap"] and "plafond personnel" not in said["user_limit_reached"]


def test_other_errors_are_not_study_refusals():
    import study
    tenant = CONTRACT["tenant_cap"]
    for other in ("", "API Error: 500 upstream", "API Error: 402 inference credits exhausted",
                  f'API Error: 429 {tenant["body"]["error"]["message"]}', json.dumps(tenant["body"]),
                  "Failed to authenticate. API Error: 401", "RuntimeError: boom"):
        assert study.refusal_code(other) is None and study.explain_refusal(other) is None, other


# ---- the local router (appliance): handed back once, never retried --------------------------
class Gateway:
    """A socket that answers /v1/messages like the gateway does when it refuses: the status,
    the JSON body, the header `x-sokkan-error` when there is a code — and nothing else."""

    def __init__(self):
        self.status, self.body, self.code, self.hits = 200, {}, "", 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length") or 0))
                outer.hits += 1
                out = json.dumps(outer.body).encode()
                self.send_response(outer.status)
                self.send_header("content-type", "application/json")
                if outer.code:
                    self.send_header("x-sokkan-error", outer.code)
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def refuse(self, r: dict):
        self.status, self.body, self.code, self.hits = r["status"], r["body"], r["body"]["error"].get("code", ""), 0


@pytest.fixture()
def router(monkeypatch):
    import shiprouter
    gw = Gateway()
    for k in list(os.environ):
        if k.startswith(("SOKKAN_SHIP", "SOKKAN_DEEP", "SOKKAN_ESCALATION", "SOKKAN_ERROR_LANG")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("SOKKAN_DEEP_URL", gw.url)
    monkeypatch.setenv("SOKKAN_DEEP_TOKEN", "sik_instance")
    try:
        yield TestClient(shiprouter.app), gw
    finally:
        gw.srv.shutdown()


ASK = {"model": "sokkan-ship", "max_tokens": 16, "stream": True, "messages": [{"role": "user", "content": "bonjour"}]}
STUDY_HEADERS = {"x-sokkan-study": "heg-eskills-2027", "x-sokkan-user": "P6"}


@pytest.mark.parametrize("code", CODES)
def test_the_router_hands_a_study_refusal_back_once_and_forbids_the_retry(router, code):
    c, gw = router
    r = REFUSALS[code]
    gw.refuse(r)
    out = c.post("/v1/messages", json=ASK, headers=STUDY_HEADERS)
    assert out.status_code == r["status"]
    assert out.json() == r["body"]                          # the gateway's own words, untouched
    assert out.headers["x-sokkan-error"] == code
    assert out.headers["x-should-retry"] == "false"         # the gateway sends none: the router must
    assert gw.hits == 1                                     # … and the router itself asked once


def test_the_tenant_wide_429_keeps_the_routers_own_message(router):
    c, gw = router
    gw.refuse(CONTRACT["tenant_cap"])
    out = c.post("/v1/messages", json=ASK, headers=STUDY_HEADERS)
    assert out.status_code == 429 and out.headers["x-sokkan-error"] == "rate_limited"
    assert out.headers["x-should-retry"] == "false" and gw.hits == 1


# ---- the session: one sentence, with the code ------------------------------------------------
def _session():
    import agentchat
    return agentchat.AgentSession("s-refusal", cwd="/tmp", user="camille@eskills.example")


def _result(text: str):
    from claude_agent_sdk import ResultMessage
    return ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1, is_error=True,
                         num_turns=1, session_id="cs", total_cost_usd=0.0, usage={}, result=text)


def _assistant(text: str):
    from claude_agent_sdk import AssistantMessage, TextBlock
    return AssistantMessage(content=[TextBlock(text=text)], model="sokkan-ship")


@pytest.mark.parametrize("code", CODES)
def test_the_session_says_one_sentence_with_the_gateways_code(code):
    import study
    sess = _session()
    shown = shown_by_the_cli(REFUSALS[code])
    sess._translate(_assistant(shown))         # the CLI's own line, as an assistant sentence …
    sess._translate(_result(shown))            # … then the result of the turn, in error
    errors = [e for e in sess.events if e["type"] == "error"]
    assert [(e.get("code"), e["message"]) for e in errors] == [(code, study.explain_refusal(shown))]
    # the generic sentence of a 429 (« wait a minute and send your message again ») is not added
    assert not any("Wait a minute" in e["message"] or "Too many requests" in e["message"] for e in errors)


def test_a_429_that_is_not_about_the_study_keeps_the_generic_message():
    sess = _session()
    sess._translate(_result(f'API Error: 429 {CONTRACT["tenant_cap"]["body"]["error"]["message"]}'))
    errors = [e for e in sess.events if e["type"] == "error"]
    assert [e.get("code") for e in errors] == ["rate_limited"]


# ---- the simple profile: the notice of the code, in the person's language ---------------------
@pytest.mark.parametrize("code", CODES)
def test_the_simple_thread_shows_one_notice_of_that_code_per_turn(code):
    import plain
    sess = _session()
    shown = shown_by_the_cli(REFUSALS[code])
    sess._translate(_assistant(shown))
    sess._translate(_result(shown))
    v = plain.View()
    out = [x for ev in sess.events for x in v.feed(ev)]
    notices = [x for x in out if x["type"] == "notice"]
    assert [n["code"] for n in notices] == [code]            # not « engine », and only once
    assert not any(x["type"] == "text" for x in out)         # the raw line is not a sentence of the agent
    assert REFUSALS[code]["message"][:40] in notices[0]["detail"]   # the raw text stays in the folded detail
    # the person sends again (a new turn): the refusal is said again, once
    again = v.feed({"type": "user_echo", "text": "encore"}) + v.feed({"type": "text", "text": shown}) \
        + v.feed({"type": "error", "code": code, "message": "…"})
    assert [x["code"] for x in again if x["type"] == "notice"] == [code]


def _dict(lang: str) -> dict:
    src = (ROOT / "frontend" / "lib" / "simple" / "dict.ts").read_text(encoding="utf-8")
    start = src.index("export const fr") if lang == "fr" else src.index("export const en")
    end = src.index("export const en") if lang == "fr" else len(src)
    return dict(re.findall(r'^\s*"([\w.]+)":\s*"((?:[^"\\]|\\.)*)",?\s*$', src[start:end], re.M))


@pytest.mark.parametrize("code", CODES)
def test_the_simple_profile_has_the_sentence_in_french_and_in_english(code):
    import plain
    assert code in plain.STUDY_NOTICES
    fr, en = _dict("fr")[f"no.{code}"], _dict("en")[f"no.{code}"]
    assert fr != en and re.search(r"[éèàç]", fr) and not re.search(r"[éèàç]", en)
    for s in (fr, en):
        assert len(s) > 60 and not TECHNICAL.search(s), (code, s)
    # nothing invites to send the same request again (the generic notice does)
    assert "relancer" not in fr and "send your request again" not in en
    if code in ("user_limit_reached", "study_mode_required"):
        assert "ne changera rien" in fr and "will not help" in en
    # the thread renders the code with its own sentence instead of falling back on « engine »
    thread = (ROOT / "frontend" / "components" / "simple" / "Thread.tsx").read_text(encoding="utf-8")
    listed = re.search(r"t\(\(`no\.\$\{\[(.*?)\]\.includes\(it\.code\)", thread, re.S).group(1)
    assert f'"{code}"' in listed
