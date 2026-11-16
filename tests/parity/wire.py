"""The meter on the wire: a pass-through proxy between an engine and the model.

Both engines are measured at the SAME place — the HTTP requests they send to the model —
instead of trusting each engine's own accounting (the CLI and the kernel count the cache
differently: seen on 15.11.2026, the kernel's ledger counted the cache inside the input).

* ``/v1/messages`` (the legacy engine, Anthropic format) and ``/v1/chat/completions`` (the
  kernel, OpenAI format) are forwarded to ``upstream`` unchanged, streamed or not.
* ``thinking=False`` turns the local model's reasoning pass off on BOTH formats, the way the
  product does it (3.6.7 router: ``chat_template_kwargs.enable_thinking=false``, the
  Anthropic ``thinking`` budget dropped) — parity is measured on the setting that ships.
* At most ``max_concurrent`` requests reach the model at once (a shared model is not a
  benchmark's own: requests over the cap wait here, their wait is logged apart).
* One JSON line per request: who (``tag`` set by the runner), when, how long (first byte,
  end), HTTP status, and the model's usage normalised to
  ``prompt_total`` / ``prompt_cached`` / ``prompt_uncached`` / ``output`` / ``reasoning``.

vLLM's ``/v1/messages`` reports ``input_tokens`` INCLUDING the cached part in a one-piece
answer (checked on 16.11.2026: same prompt twice, ``input_tokens`` unchanged,
``cache_read_input_tokens`` up) and EXCLUDING it in a stream (the CLI's requests: 988 + 23 296
written); Anthropic's API always excludes it. ``messages_input_includes_cache=False`` = an
Anthropic upstream.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade", "content-length", "host",
       "accept-encoding"}


def normalise(fmt: str, usage: dict, *, messages_input_includes_cache: bool = True) -> dict:
    """The model's usage block → the bench's five counters (absent = 0).

    ``messages_input_includes_cache`` is about ONE answer: vLLM 0.32's ``/v1/messages``
    counts the cache inside ``input_tokens`` when it answers in one piece, and outside it when
    it streams (``message_start``, Anthropic's way) — measured on 16.11.2026."""
    usage = usage or {}
    if fmt == "messages":
        inp = int(usage.get("input_tokens") or 0)
        cached = int(usage.get("cache_read_input_tokens") or 0)
        written = int(usage.get("cache_creation_input_tokens") or 0)
        total = inp if messages_input_includes_cache else inp + cached + written
        return {"prompt_total": total, "prompt_cached": cached,
                "prompt_uncached": max(0, total - cached),
                "output": int(usage.get("output_tokens") or 0), "reasoning": 0}
    total = int(usage.get("prompt_tokens") or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return {"prompt_total": total, "prompt_cached": cached,
            "prompt_uncached": max(0, total - cached),
            "output": int(usage.get("completion_tokens") or 0),
            "reasoning": int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                             or 0)}


def usage_from_sse(fmt: str, raw: bytes) -> dict:
    """The usage of a streamed answer (Messages: ``message_start`` + ``message_delta``;
    OpenAI: the last chunk that carries ``usage``)."""
    usage: dict = {}
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            continue
        try:
            ev = json.loads(data)
        except ValueError:
            continue
        if fmt == "messages":
            if ev.get("type") == "message_start":
                usage.update((ev.get("message") or {}).get("usage") or {})
            elif ev.get("type") == "message_delta" and ev.get("usage"):
                for k, v in ev["usage"].items():
                    if v is not None:
                        usage[k] = v
        elif ev.get("usage"):
            usage = ev["usage"]
    return usage


def shape(fmt: str, body: dict, *, thinking: bool) -> dict:
    """What leaves for the model: the same body, reasoning off when asked; the OpenAI stream
    asked to carry its usage."""
    body = dict(body)
    if not thinking:
        kw = body.get("chat_template_kwargs")
        body["chat_template_kwargs"] = {**(kw if isinstance(kw, dict) else {}),
                                        "enable_thinking": False}
        body.pop("thinking", None)
    if fmt == "chat" and body.get("stream"):
        so = body.get("stream_options")
        body["stream_options"] = {**(so if isinstance(so, dict) else {}), "include_usage": True}
    return body


class Wire:
    """``with Wire(upstream, log_path, bind=...) as w: w.url`` — ``w.tag = "legacy/a1/p1"``
    labels the requests that follow."""

    def __init__(self, upstream: str, log_path: str, *, bind: str = "127.0.0.1", port: int = 0,
                 thinking: bool = False, max_concurrent: int = 2, model: str | None = None,
                 messages_input_includes_cache: bool = True, timeout: float = 900.0) -> None:
        u = urllib.parse.urlsplit(upstream.rstrip("/"))
        self.up_host, self.up_port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        self.up_https = u.scheme == "https"
        self.up_prefix = u.path.rstrip("/")         # e.g. "" or "/v1" stripped below
        if self.up_prefix.endswith("/v1"):
            self.up_prefix = self.up_prefix[:-3]
        self.log_path, self.thinking, self.model = log_path, thinking, model
        self.mic = messages_input_includes_cache
        self.timeout = timeout
        self.tag = ""
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self._lock = threading.Lock()
        self.records: list[dict] = []
        wire = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/_parity/"):
                    return wire._control(self, None)
                wire._forward(self, None)

            def do_HEAD(self):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                if self.path.startswith("/_parity/"):
                    return wire._control(self, raw)
                wire._forward(self, raw)

        self._httpd = ThreadingHTTPServer((bind, port), H)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.url = f"http://{bind}:{self.port}"

    # ---- the runner's control (the meter may run in a container: tag and records by HTTP) ---
    def _control(self, h: BaseHTTPRequestHandler, raw: bytes | None) -> None:
        path = urllib.parse.urlsplit(h.path)
        if path.path == "/_parity/tag" and raw is not None:
            self.tag = str((json.loads(raw or b"{}") or {}).get("tag") or "")
            out = {"tag": self.tag}
        elif path.path == "/_parity/records":
            tag = (urllib.parse.parse_qs(path.query).get("tag") or [""])[0]
            out = {"records": self.of(tag)}
        else:
            out = {"error": "unknown"}
        data = json.dumps(out).encode()
        h.send_response(200 if "error" not in out else 404)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    # ---- one request ---------------------------------------------------------------------
    def _conn(self):
        cls = http.client.HTTPSConnection if self.up_https else http.client.HTTPConnection
        return cls(self.up_host, self.up_port, timeout=self.timeout)

    def _forward(self, h: BaseHTTPRequestHandler, raw: bytes | None) -> None:
        path = h.path
        bare = path.split("?")[0].rstrip("/")
        fmt = ("messages" if bare.endswith("/v1/messages")
               else "chat" if bare.endswith("/chat/completions") else "")
        body = None
        if raw is not None and fmt:
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = None
        if body is not None:
            if self.model:
                body["model"] = self.model
            body = shape(fmt, body, thinking=self.thinking)
            raw = json.dumps(body, ensure_ascii=False).encode()
        headers = {k: v for k, v in h.headers.items() if k.lower() not in HOP}
        headers["Accept-Encoding"] = "identity"
        if raw is not None:
            headers["Content-Length"] = str(len(raw))
        rec = {"tag": self.tag, "path": path.split("?")[0], "format": fmt,
               "stream": bool(body and body.get("stream")), "t_arrive": time.time()}
        waited = time.monotonic()
        self._sem.acquire()
        try:
            rec["queue_s"] = round(time.monotonic() - waited, 3)
            t0 = time.monotonic()
            conn = self._conn()
            try:
                conn.request("POST" if raw is not None else "GET", self.up_prefix + path,
                             body=raw, headers=headers)
                resp = conn.getresponse()
            except OSError as e:
                rec.update(status=599, error=str(e)[:200], total_s=round(time.monotonic() - t0, 3))
                self._record(rec)
                h.send_response(502)
                h.send_header("Content-Type", "application/json")
                msg = json.dumps({"error": {"type": "upstream_unreachable", "message": str(e)[:200]}})
                h.send_header("Content-Length", str(len(msg)))
                h.end_headers()
                h.wfile.write(msg.encode())
                return
            rec["status"] = resp.status
            h.send_response(resp.status)
            chunked = resp.getheader("Content-Length") is None
            for k, v in resp.getheaders():
                if k.lower() not in HOP:
                    h.send_header(k, v)
            if chunked:
                h.send_header("Transfer-Encoding", "chunked")
            data = bytearray()
            first = None
            if not chunked:
                payload = resp.read()
                first = time.monotonic()
                h.send_header("Content-Length", str(len(payload)))
                h.end_headers()
                h.wfile.write(payload)
                data += payload
            else:
                h.end_headers()
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    if first is None:
                        first = time.monotonic()
                    data += chunk
                    try:
                        h.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        h.wfile.flush()
                    except OSError:
                        break
                try:
                    h.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass
            conn.close()
            rec["first_byte_s"] = round((first or time.monotonic()) - t0, 3)
            rec["total_s"] = round(time.monotonic() - t0, 3)
            if fmt and resp.status == 200:
                if rec["stream"]:
                    usage = usage_from_sse(fmt, bytes(data))
                else:
                    try:
                        usage = json.loads(bytes(data)).get("usage") or {}
                    except ValueError:
                        usage = {}
                rec["usage_raw"] = usage
                rec.update(normalise(fmt, usage,
                                     messages_input_includes_cache=self.mic and not rec["stream"]))
            self._record(rec)
        finally:
            self._sem.release()

    def _record(self, rec: dict) -> None:
        with self._lock:
            self.records.append(rec)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def of(self, tag: str) -> list[dict]:
        with self._lock:
            return [r for r in self.records if r["tag"] == tag and r.get("format")]

    def __enter__(self) -> "Wire":
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def totals(records: list[dict]) -> dict:
    """Sum of a journey's model requests (the wire's view)."""
    out = {"requests": len(records), "prompt_total": 0, "prompt_cached": 0, "prompt_uncached": 0,
           "output": 0, "reasoning": 0, "model_s": 0.0, "queue_s": 0.0,
           "http_errors": sum(1 for r in records if r.get("status") != 200)}
    for r in records:
        for k in ("prompt_total", "prompt_cached", "prompt_uncached", "output", "reasoning"):
            out[k] += int(r.get(k) or 0)
        out["model_s"] += float(r.get("total_s") or 0)
        out["queue_s"] += float(r.get("queue_s") or 0)
    out["model_s"] = round(out["model_s"], 1)
    out["queue_s"] = round(out["queue_s"], 1)
    if records:
        out["prompt_per_request"] = round(out["prompt_total"] / len(records))
    return out


class RemoteWire:
    """The runner's handle on a meter that runs elsewhere (a container of the lab)."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self._tag = ""

    def _call(self, path: str, body: dict | None = None) -> dict:
        import urllib.request
        req = urllib.request.Request(self.url + path, method="POST" if body is not None else "GET",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:     # noqa: S310 — the bench's meter
            return json.loads(r.read())

    @property
    def tag(self) -> str:
        return self._tag

    @tag.setter
    def tag(self, value: str) -> None:
        self._tag = value
        self._call("/_parity/tag", {"tag": value})

    def of(self, tag: str) -> list[dict]:
        return self._call("/_parity/records?" + urllib.parse.urlencode({"tag": tag}))["records"]

    def __enter__(self) -> "RemoteWire":
        return self

    def __exit__(self, *exc) -> None:
        pass


def main() -> None:
    """Serve the meter (the lab's `meter` service): settings from the environment."""
    import os
    e = os.environ.get
    w = Wire(e("PARITY_UPSTREAM", ""), e("PARITY_WIRE_LOG", "/meter/wire.jsonl"),
             bind=e("PARITY_WIRE_BIND", "0.0.0.0"), port=int(e("PARITY_WIRE_PORT", "18790")),  # noqa: S104 — inside its container
             thinking=e("PARITY_THINKING", "0") == "1",
             max_concurrent=int(e("PARITY_MAX_CONCURRENT", "2")), model=e("PARITY_MODEL") or None)
    print(f"meter on {w.url} -> {e('PARITY_UPSTREAM')}", flush=True)
    w._httpd.serve_forever()


if __name__ == "__main__":
    main()
