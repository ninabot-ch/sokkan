"""The local Ship → Deep router of the appliance (backend/shiprouter.py) against two fake
upstreams (real sockets): what leaves the box and why, fallback when the local model is down,
and 402 / 429 / unreachable as text a person can read (backend/llmerrors.py)."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

import llmerrors
import shiprouter

SSE = (b'event: message_start\ndata: {"type":"message_start","message":{"model":"%s"}}\n\n'
       b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
       b'event: message_stop\ndata: {"type":"message_stop"}\n\n')


class Fake:
    """An upstream that answers /v1/messages with `status` and records what it received."""

    def __init__(self, name):
        self.name, self.status, self.body, self.seen = name, 200, None, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                outer.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                   "body": json.loads(raw or b"{}")})
                if self.path.endswith("count_tokens"):
                    out, ctype = b'{"input_tokens": 42}', "application/json"
                    self.send_response(200)
                elif outer.status == 200:
                    out, ctype = SSE % outer.name.encode(), "text/event-stream"
                    self.send_response(200)
                else:
                    out = outer.body or json.dumps({"error": {"message": "upstream says no"}}).encode()
                    ctype = "application/json"
                    self.send_response(outer.status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _dead_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


@pytest.fixture()
def box(monkeypatch):
    ship, deep = Fake("ship"), Fake("deep")
    for k in list(__import__("os").environ):
        if k.startswith(("SOKKAN_SHIP", "SOKKAN_DEEP", "SOKKAN_ESCALATION", "SOKKAN_ERROR_LANG")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("SOKKAN_SHIP_URL", ship.url)
    monkeypatch.setenv("SOKKAN_SHIP_MODEL", "qwen3.8-27b")
    monkeypatch.setenv("SOKKAN_DEEP_URL", deep.url)
    monkeypatch.setenv("SOKKAN_DEEP_TOKEN", "sik_instance_token")
    monkeypatch.setenv("SOKKAN_SHIP_CONNECT_TIMEOUT_S", "1")
    shiprouter._stats.clear()
    yield TestClient(shiprouter.app), ship, deep, monkeypatch
    ship.close()
    deep.close()


def msg(text="bonjour", **kw):
    return {"model": "sokkan-ship", "max_tokens": 64, "stream": True,
            "messages": [{"role": "user", "content": text}], **kw}


def tool_round(error: str | None, i: int = 0) -> list[dict]:
    return [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                          "is_error": error is not None, "content": error or "ok"}]}]


# ---------------------------------------------------------------- what leaves the box

def test_an_ordinary_request_stays_on_the_local_model(box):
    c, ship, deep, _ = box
    r = c.post("/v1/messages", json=msg(), headers={"x-api-key": "whatever", "x-sokkan-user": "ana@pilot.example"})
    assert r.status_code == 200 and r.headers["x-sokkan-tier"] == "ship"
    assert b'"model":"ship"' in r.content and b"message_stop" in r.content       # stream relayed whole
    assert len(ship.seen) == 1 and deep.seen == []
    assert ship.seen[0]["body"]["model"] == "qwen3.8-27b"                         # the served name
    assert ship.seen[0]["body"]["stream"] is True


@pytest.mark.parametrize("payload,headers,reason", [
    (msg(model="sokkan-deep"), {}, "requested"),
    (msg(), {"x-sokkan-tier": "deep"}, "requested"),
    (msg("analyse ce contrat /deep"), {}, "keyword"),
    ({**msg(), "messages": [{"role": "user", "content": "fais-le"}, *tool_round("boom A", 1),
                            *tool_round("boom B", 2), *tool_round("boom C", 3)]}, {}, "fails"),
    ({**msg(), "messages": [{"role": "user", "content": "fais-le"}, *tool_round("same error", 1),
                            *tool_round("same error", 2)]}, {}, "repeat"),
])
def test_escalation_signals_send_the_request_to_deep(box, payload, headers, reason):
    c, ship, deep, _ = box
    r = c.post("/v1/messages", json=payload, headers=headers)
    assert r.status_code == 200
    assert (r.headers["x-sokkan-tier"], r.headers["x-sokkan-reason"]) == ("deep", reason)
    assert ship.seen == [] and len(deep.seen) == 1
    sent = deep.seen[0]
    assert sent["body"]["model"] == "sokkan-deep"
    assert sent["headers"]["x-api-key"] == "sik_instance_token"
    assert sent["headers"]["authorization"] == "Bearer sik_instance_token"


def test_the_decision_is_taken_again_at_every_request(box):
    """Not sticky: once the person writes again, the loop of failures no longer counts."""
    c, ship, deep, _ = box
    failing = [{"role": "user", "content": "fais-le"}, *tool_round("e1", 1), *tool_round("e2", 2),
               *tool_round("e3", 3)]
    assert c.post("/v1/messages", json={**msg(), "messages": failing}).headers["x-sokkan-tier"] == "deep"
    after = failing + [{"role": "assistant", "content": "…"}, {"role": "user", "content": "essaie autrement"}]
    assert c.post("/v1/messages", json={**msg(), "messages": after}).headers["x-sokkan-tier"] == "ship"


def test_a_keyword_inside_a_tool_result_is_not_the_person_asking(box):
    c, ship, deep, _ = box
    m = [{"role": "user", "content": "lis le fichier"}, *tool_round(None, 1)]
    m[-1]["content"][0]["content"] = "contenu du fichier avec /deep dedans"
    r = c.post("/v1/messages", json={**msg(), "messages": m})
    assert r.headers["x-sokkan-tier"] == "ship"


def test_escalation_off_and_no_gateway_keep_everything_local(box):
    c, ship, deep, mp = box
    mp.setenv("SOKKAN_ESCALATION", "0")
    assert c.post("/v1/messages", json=msg("x /deep")).headers["x-sokkan-tier"] == "ship"
    assert c.post("/v1/messages", json=msg(model="sokkan-deep")).headers["x-sokkan-tier"] == "deep"  # explicit
    mp.delenv("SOKKAN_ESCALATION")
    mp.setenv("SOKKAN_DEEP_TOKEN", "")
    r = c.post("/v1/messages", json=msg("x /deep", model="sokkan-deep"))
    assert (r.headers["x-sokkan-tier"], r.headers["x-sokkan-reason"]) == ("ship", "deep_not_configured")
    assert len(deep.seen) == 1                                 # only the explicit one above


def test_who_typed_stays_on_the_box_unless_the_operator_forwards_it(box):
    c, ship, deep, mp = box
    h = {"x-sokkan-tier": "deep", "x-sokkan-user": "ana@pilot.example", "cookie": "sokkan_session=abc"}
    c.post("/v1/messages", json=msg(), headers=h)
    assert "x-sokkan-user" not in deep.seen[0]["headers"] and "cookie" not in deep.seen[0]["headers"]
    assert "ana@pilot.example" not in json.dumps(deep.seen[0]["headers"])
    mp.setenv("SOKKAN_DEEP_FORWARD_USER", "1")
    c.post("/v1/messages", json=msg(), headers=h)
    assert deep.seen[1]["headers"]["x-sokkan-user"] == "ana@pilot.example"


def test_the_caller_token_never_reaches_an_upstream(box):
    c, ship, deep, _ = box
    c.post("/v1/messages", json=msg(), headers={"x-api-key": "caller-secret", "authorization": "Bearer caller-secret"})
    c.post("/v1/messages", json=msg(), headers={"x-api-key": "caller-secret", "x-sokkan-tier": "deep"})
    assert "caller-secret" not in json.dumps(ship.seen) + json.dumps(deep.seen)


# ---------------------------------------------------------------- local model down

def test_local_model_down_falls_back_to_deep(box):
    c, ship, deep, mp = box
    mp.setenv("SOKKAN_SHIP_URL", _dead_url())
    r = c.post("/v1/messages", json=msg())
    assert r.status_code == 200 and b'"model":"deep"' in r.content
    assert (r.headers["x-sokkan-tier"], r.headers["x-sokkan-reason"]) == ("deep", "ship_down")
    ship.status = 503
    mp.setenv("SOKKAN_SHIP_URL", ship.url)
    assert c.post("/v1/messages", json=msg()).headers["x-sokkan-reason"] == "ship_down"


@pytest.mark.parametrize("lang,needle", [("en", "cannot be reached"), ("fr", "injoignable")])
def test_local_model_down_without_fallback_is_a_readable_503(box, lang, needle):
    c, ship, deep, mp = box
    mp.setenv("SOKKAN_SHIP_URL", _dead_url())
    mp.setenv("SOKKAN_SHIP_FALLBACK_DEEP", "0")
    mp.setenv("SOKKAN_ERROR_LANG", lang)
    r = c.post("/v1/messages", json=msg())
    assert r.status_code == 503 and r.headers["x-sokkan-error"] == "unreachable"
    assert r.headers["x-should-retry"] == "false"
    e = r.json()
    assert e["type"] == "error" and e["error"]["type"] == "api_error"
    assert needle in e["error"]["message"] and "[sokkan:unreachable]" in e["error"]["message"]
    assert deep.seen == []
    assert "Traceback" not in r.text and "ConnectError" not in e["error"]["message"].split("[sokkan")[0]


def test_a_busy_gateway_is_never_sent_as_a_529_overloaded(box):
    """The CLI retries 529 / `overloaded_error` on its own, header or not."""
    c, ship, deep, mp = box
    mp.setenv("SOKKAN_SHIP_URL", _dead_url())
    deep.status = 503
    r = c.post("/v1/messages", json=msg())
    assert r.status_code == 503 and r.headers["x-sokkan-error"] == "overloaded"
    assert r.headers["x-should-retry"] == "false" and r.json()["error"]["type"] == "api_error"
    assert "overloaded_error" not in r.text and llmerrors.from_text("API Error: 503 " + r.text)["code"] == "overloaded"
    assert 529 not in llmerrors.HTTP_STATUS.values()


def test_both_down_is_readable_too(box):
    c, ship, deep, mp = box
    mp.setenv("SOKKAN_SHIP_URL", _dead_url())
    mp.setenv("SOKKAN_DEEP_URL", _dead_url())
    r = c.post("/v1/messages", json=msg())
    assert r.status_code == 503 and "[sokkan:unreachable]" in r.text


# ---------------------------------------------------------------- 402 / 429 readable

@pytest.mark.parametrize("status,code,etype,fr", [
    (402, "quota_exhausted", "billing_error", "crédit d'inférence"),
    (429, "rate_limited", "rate_limit_error", "limite les requêtes"),
    (401, "auth", "authentication_error", "refusé les identifiants"),
])
def test_gateway_refusals_come_back_as_text_a_person_can_read(box, status, code, etype, fr):
    c, ship, deep, mp = box
    mp.setenv("SOKKAN_ERROR_LANG", "fr")
    deep.status = status
    deep.body = b'{"detail":"wallet empty tenant=ten_a769 token=sik_SECRET"}'
    r = c.post("/v1/messages", json=msg(model="sokkan-deep"))
    assert r.status_code == status and r.headers["x-sokkan-error"] == code
    # the CLI must not retry for minutes behind the person's back (it does on 429 / 5xx)
    assert r.headers["x-should-retry"] == "false"
    e = r.json()["error"]
    assert e["type"] == etype and fr in e["message"].lower()
    assert "Rien n'est perdu" in e["message"] or code == "auth"
    assert "sik_SECRET" not in r.text and "ten_a769" not in r.text      # upstream detail not echoed
    # and the session side finds the same code again in what the CLI prints
    line = f"API Error: {status} {r.text}"
    assert llmerrors.from_text(line, "fr")["code"] == code


def test_a_caller_error_is_handed_back_untouched(box):
    c, ship, deep, _ = box
    ship.status, ship.body = 400, b'{"type":"error","error":{"type":"invalid_request_error","message":"max_tokens required"}}'
    r = c.post("/v1/messages", json=msg())
    assert r.status_code == 400 and "max_tokens required" in r.text


def test_context_overflow_says_so(box):
    c, ship, deep, _ = box
    ship.status, ship.body = 400, b'{"error":{"message":"This model maximum context length is 131072 tokens"}}'
    r = c.post("/v1/messages", json=msg())
    assert r.status_code == 400 and r.headers["x-sokkan-error"] == "too_long"


# ---------------------------------------------------------------- llmerrors alone

@pytest.mark.parametrize("line,code", [
    ('API Error: 402 {"type":"error","error":{"type":"billing_error","message":"x"}}', "quota_exhausted"),
    ("API Error: 429 Too Many Requests", "rate_limited"),
    ("API Error: Connection error.", "unreachable"),
    ("API Error: 503 upstream", "overloaded"),
    ("API Error: 502 Bad Gateway", "unreachable"),
    ("Here is the report you asked for", None),
    ("the API Error: handling code is in app.py", None),
    ("", None),
])
def test_from_text(line, code):
    got = llmerrors.from_text(line)
    assert (got or {}).get("code") == code


def test_every_code_has_both_languages_and_says_what_to_do():
    for code in llmerrors.CODES:
        for lg in ("en", "fr"):
            h = llmerrors.humanize(code, lg)
            assert len(h["message"]) > 60 and h["title"]
            assert "{" not in h["message"] and "Error" not in h["title"]
    assert llmerrors.lang("de") == "en"


# ---------------------------------------------------------------- misc

def test_stats_and_health_carry_no_content(box):
    c, ship, deep, _ = box
    c.post("/v1/messages", json=msg("texte très confidentiel du client"))
    c.post("/v1/messages", json=msg("autre secret /deep"))
    s = c.get("/stats").json()
    assert s["counts"]["ship:default"] == 1 and s["counts"]["deep:keyword"] == 1
    assert "confidentiel" not in json.dumps(s) and "secret" not in json.dumps(s)
    h = c.get("/health").json()
    assert h == {"ok": True, "ship": True, "deep": True}


def test_count_tokens_never_fails_a_session(box):
    c, ship, deep, mp = box
    assert c.post("/v1/messages/count_tokens", json=msg()).json() == {"input_tokens": 42}
    mp.setenv("SOKKAN_SHIP_URL", _dead_url())
    assert c.post("/v1/messages/count_tokens", json=msg("abcd" * 100)).json()["input_tokens"] == 100


def test_nothing_configured_is_a_readable_error(box):
    c, ship, deep, mp = box
    mp.setenv("SOKKAN_SHIP_URL", "")
    mp.setenv("SOKKAN_DEEP_URL", "")
    r = c.post("/v1/messages", json=msg())
    assert r.status_code == 503 and "[sokkan:unreachable]" in r.text


# ---------------------------------------------------------------- the api side (llm.py)

def test_a_local_model_is_the_instance_engine_without_any_click(tmp_path, monkeypatch):
    import llm
    monkeypatch.setattr(llm, "CONFIG", tmp_path / "llm.json")
    for k in ("SOKKAN_INFER_BASE_URL", "SOKKAN_INFER_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
              "SOKKAN_SHIP_URL", "SOKKAN_SHIP_ROUTER_URL"):
        monkeypatch.delenv(k, raising=False)
    assert llm.load() == {} and not llm.configured()
    monkeypatch.setenv("SOKKAN_SHIP_URL", "http://host.docker.internal:8004")
    assert llm.configured() and llm.unattended_credentials() == "custom"
    env = llm.session_env("ana@pilot.example")
    assert env["ANTHROPIC_BASE_URL"] == "http://router:8096"          # the router, not the model
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "sokkan-ship" and llm.session_model() == "sokkan-ship"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    st = llm.status()
    assert st["mode"] == "custom" and st["engine"] == "appliance" and st["configured"]
    # a model chosen in the cockpit still wins
    llm.save({"mode": "byok", "anthropic_api_key": "sk-ant-api03-xxxxxxxx"})
    assert llm.load()["mode"] == "byok"


def test_session_stream_turns_a_cli_error_line_into_words():
    """agentchat emits an `error` event with the readable text next to the raw result."""
    import inspect

    import agentchat
    src = inspect.getsource(agentchat)
    assert "llmerrors.from_text" in src and '"code": h["code"]' in src
