#!/usr/bin/env python3
"""shim.py — SOKKAN Magnitude : proxy Anthropic Messages ⇄ OpenAI chat/completions.

Les sessions Claude Code du cockpit parlent l'API Anthropic Messages ; llama-server
expose l'API OpenAI `/v1/chat/completions`. Ce shim traduit dans les deux sens,
streaming SSE compris, pour que `ANTHROPIC_BASE_URL=http://host.docker.internal:8790`
suffise à faire tourner les sessions sur le modèle local.

- Stdlib uniquement (`http.server.ThreadingHTTPServer` + `urllib.request`) : tourne
  sur la machine du client sans pip install.
- Bind 0.0.0.0:8790 (le container api doit l'atteindre), protégé par le serve_token
  généré par l'agent (`x-api-key` OU `authorization: Bearer`, compare constant-time).
- llama-server reste sur 127.0.0.1:8791 — seul le shim est exposé.

Standalone : `python3 -m magnitude.shim --upstream http://127.0.0.1:8791 \
              --port 8790 --token <serve_token> --model qwen3-8b`
"""
from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_UPSTREAM = "http://127.0.0.1:8791"
DEFAULT_PORT = 8790
UPSTREAM_TIMEOUT_S = 600  # un 70B qui prefill un gros contexte peut être long

_FINISH_MAP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}


def _map_finish(reason: str | None) -> str:
    return _FINISH_MAP.get(reason or "", "end_turn")


def _estimate_tokens(text_len: int) -> int:
    """Estimation grossière ~4 chars/token (suffisant pour count_tokens et l'usage)."""
    return max(1, text_len // 4)


# ---------------------------------------------------------------- traduction requête

def _blocks_to_text(blocks) -> str:
    """Aplatie une liste de blocks (text/image/autre) en texte."""
    parts: list[str] = []
    for b in blocks or []:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict):
            t = b.get("type")
            if t == "text":
                parts.append(b.get("text") or "")
            elif t == "image":
                parts.append("[image omitted]")
            else:  # block inconnu : sérialisé plutôt que perdu
                parts.append(json.dumps(b, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def _stringify_tool_result(content) -> str:
    """Contenu d'un tool_result Anthropic (string OU liste de blocks) → string OpenAI."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _blocks_to_text(content)
    return json.dumps(content, ensure_ascii=False)


def anthropic_to_openai(req: dict, model_id: str) -> dict:
    """Requête Anthropic Messages → payload OpenAI /v1/chat/completions.

    Champs Anthropic inconnus (metadata, thinking, …) ignorés silencieusement.
    Le model du client est ignoré : upstream = model_id du shim.
    """
    messages: list[dict] = []

    system = req.get("system")
    if system:
        sys_text = system if isinstance(system, str) else _blocks_to_text(system)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    for m in req.get("messages") or []:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        blocks = content or []
        if role == "assistant":
            texts: list[str] = []
            tool_calls: list[dict] = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    texts.append(b.get("text") or "")
                elif t == "tool_use":
                    tool_calls.append({
                        "id": b.get("id") or f"toolu_{secrets.token_hex(8)}",
                        "type": "function",
                        "function": {"name": b.get("name") or "",
                                     "arguments": json.dumps(b.get("input") or {},
                                                             ensure_ascii=False)},
                    })
                elif t == "image":
                    texts.append("[image omitted]")
            msg: dict = {"role": "assistant",
                         "content": "\n".join(t for t in texts if t) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            elif msg["content"] is None:
                msg["content"] = ""
            messages.append(msg)
        else:  # user
            tool_msgs: list[dict] = []
            rest: list = []
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    # role tool AVANT le reste du contenu user du même tour ;
                    # is_error n'a pas d'équivalent OpenAI → marqueur dans le content
                    body = _stringify_tool_result(b.get("content"))
                    if b.get("is_error"):
                        body = "[tool_error] " + body
                    tool_msgs.append({"role": "tool",
                                      "tool_call_id": b.get("tool_use_id") or "",
                                      "content": body})
                else:
                    rest.append(b)
            messages.extend(tool_msgs)
            text = _blocks_to_text(rest)
            if text or not tool_msgs:
                messages.append({"role": "user", "content": text})

    out: dict = {"model": model_id, "messages": messages}
    if req.get("max_tokens") is not None:
        out["max_tokens"] = req["max_tokens"]
    for k in ("temperature", "top_p"):
        if req.get(k) is not None:
            out[k] = req[k]
    if req.get("stop_sequences"):
        out["stop"] = req["stop_sequences"]
    if req.get("stream"):
        out["stream"] = True
        # llama-server sait renvoyer l'usage dans le chunk final
        out["stream_options"] = {"include_usage": True}

    tools = req.get("tools")
    if tools:
        out["tools"] = [{"type": "function",
                         "function": {"name": t.get("name") or "",
                                      "description": t.get("description") or "",
                                      "parameters": t.get("input_schema")
                                      or {"type": "object", "properties": {}}}}
                        for t in tools]
    tc = req.get("tool_choice")
    if isinstance(tc, dict):
        tt = tc.get("type")
        if tt == "auto":
            out["tool_choice"] = "auto"
        elif tt == "any":
            out["tool_choice"] = "required"
        elif tt == "none":
            out["tool_choice"] = "none"
        elif tt == "tool":
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": tc.get("name") or ""}}
    return out


# --------------------------------------------------------------- traduction réponse

def openai_to_anthropic(resp: dict, model_id: str) -> dict:
    """Réponse OpenAI non-stream → enveloppe message Anthropic."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            inp = {}
        if not isinstance(inp, dict):
            inp = {}
        content.append({"type": "tool_use",
                        "id": tc.get("id") or f"toolu_{secrets.token_hex(8)}",
                        "name": fn.get("name") or "", "input": inp})
    usage = resp.get("usage") or {}
    return {"id": resp.get("id") or f"msg_{secrets.token_hex(12)}",
            "type": "message",
            "role": "assistant",
            "model": model_id,
            "content": content,
            "stop_reason": _map_finish(choice.get("finish_reason")),
            "stop_sequence": None,
            "usage": {"input_tokens": usage.get("prompt_tokens") or 0,
                      "output_tokens": usage.get("completion_tokens") or 0}}


class StreamTranslator:
    """Chunks OpenAI streaming → séquence d'events SSE Anthropic.

    Séquence garantie : message_start → content_block_start/delta/stop (par block,
    text puis tool_use au fil des tool_calls) → message_delta → message_stop.
    Feeder les chunks décodés via `feed()` ; chaque appel rend une liste de tuples
    (event_name, payload) à émettre dans l'ordre.
    """

    def __init__(self, model_id: str, input_tokens: int = 0):
        self.model_id = model_id
        self.input_tokens = input_tokens
        self.msg_id = f"msg_{secrets.token_hex(12)}"
        self._block_index = -1        # index Anthropic du block courant
        self._block_type: str | None = None   # "text" | "tool_use"
        self._tool_index: int | None = None   # index OpenAI du tool_call courant
        self._tool_id: str | None = None      # id du tool_call courant (fallback sans index)
        self._out_chars = 0           # fallback estimation output_tokens
        self._usage_out: int | None = None
        self._finish_reason: str | None = None
        self.finished = False

    def start_events(self) -> list[tuple[str, dict]]:
        return [("message_start", {"type": "message_start", "message": {
            "id": self.msg_id, "type": "message", "role": "assistant",
            "model": self.model_id, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": 0}}})]

    def _close_block(self, events: list) -> None:
        if self._block_type is not None:
            events.append(("content_block_stop",
                           {"type": "content_block_stop", "index": self._block_index}))
            self._block_type = None

    def feed(self, chunk: dict) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        usage = chunk.get("usage")
        if usage and usage.get("completion_tokens") is not None:
            self._usage_out = usage["completion_tokens"]
        choices = chunk.get("choices") or []
        if not choices or self.finished:
            return events
        choice = choices[0]
        delta = choice.get("delta") or {}

        text = delta.get("content")
        if text:
            if self._block_type != "text":
                self._close_block(events)
                self._block_index += 1
                self._block_type = "text"
                events.append(("content_block_start",
                               {"type": "content_block_start", "index": self._block_index,
                                "content_block": {"type": "text", "text": ""}}))
            self._out_chars += len(text)
            events.append(("content_block_delta",
                           {"type": "content_block_delta", "index": self._block_index,
                            "delta": {"type": "text_delta", "text": text}}))

        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index")
            tc_id = tc.get("id")
            fn = tc.get("function") or {}
            # nouveau tool call si : pas de block tool_use ouvert, index OpenAI qui
            # change, ou (upstream sans index) un id différent — l'id n'apparaît que
            # sur le 1er fragment d'un call
            new_call = (self._block_type != "tool_use"
                        or (idx is not None and idx != self._tool_index)
                        or (idx is None and tc_id and tc_id != self._tool_id))
            if new_call:
                self._close_block(events)
                self._block_index += 1
                self._block_type = "tool_use"
                self._tool_index = idx
                self._tool_id = tc_id or f"toolu_{secrets.token_hex(8)}"
                events.append(("content_block_start",
                               {"type": "content_block_start", "index": self._block_index,
                                "content_block": {"type": "tool_use",
                                                  "id": self._tool_id,
                                                  "name": fn.get("name") or "",
                                                  "input": {}}}))
            args = fn.get("arguments")
            if args:
                self._out_chars += len(args)
                events.append(("content_block_delta",
                               {"type": "content_block_delta", "index": self._block_index,
                                "delta": {"type": "input_json_delta",
                                          "partial_json": args}}))

        if choice.get("finish_reason"):
            # ne PAS émettre message_delta/stop ici : llama-server envoie encore un
            # chunk usage-only APRÈS le finish_reason (stream_options.include_usage).
            # On mémorise et on laisse end_events() clore avec l'usage réel.
            self._finish_reason = choice["finish_reason"]
            self._close_block(events)
        return events

    def end_events(self) -> list[tuple[str, dict]]:
        """Clôture du stream ([DONE], EOF ou panne upstream) — idempotent."""
        if self.finished:
            return []
        self.finished = True
        events: list[tuple[str, dict]] = []
        self._close_block(events)
        out = self._usage_out if self._usage_out is not None \
            else _estimate_tokens(self._out_chars)
        events.append(("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": _map_finish(self._finish_reason),
                                 "stop_sequence": None},
                       "usage": {"output_tokens": out}}))
        events.append(("message_stop", {"type": "message_stop"}))
        return events


# ------------------------------------------------------------------------- serveur

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sokkan-magnitude-shim"

    # -- helpers ------------------------------------------------------------

    @property
    def shim(self) -> "Shim":
        return self.server.shim  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # pas de bruit stderr par requête
        pass

    def _authed(self) -> bool:
        tok = self.headers.get("x-api-key") or ""
        if not tok:
            auth = self.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                tok = auth[7:].strip()
        return bool(tok) and secrets.compare_digest(tok, self.shim.token)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        if self.close_connection:  # annoncer la fermeture au pool client
            self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, err_type: str, message: str) -> None:
        if getattr(self, "_streaming", False):
            return  # headers SSE déjà partis : impossible d'émettre une réponse HTTP
        # fermer la connexion : un body non lu (401 avant _read_body) resterait dans
        # le socket et empoisonnerait la requête keep-alive suivante du pool client
        self.close_connection = True
        self._send_json(status, {"type": "error",
                                 "error": {"type": err_type, "message": message}})

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    # -- routes -------------------------------------------------------------

    def do_GET(self):  # noqa: N802 (convention http.server)
        self._send_error_json(404, "not_found_error", f"Not found: {self.path}")

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        try:
            if path.endswith("/v1/messages/count_tokens"):
                self._handle_count_tokens()
            elif path.endswith("/v1/messages"):
                self._handle_messages()
            else:
                self._send_error_json(404, "not_found_error", f"Not found: {self.path}")
        except (BrokenPipeError, ConnectionResetError):
            pass  # client parti — silencieux
        except Exception as e:  # jamais de traceback brut vers le client
            try:
                self._send_error_json(500, "api_error", f"shim error: {e}")
            except OSError:
                pass

    def _handle_count_tokens(self) -> None:
        if not self._authed():
            self._send_error_json(401, "authentication_error", "invalid x-api-key")
            return
        raw = self._read_body()
        self._send_json(200, {"input_tokens": _estimate_tokens(len(raw))})

    def _handle_messages(self) -> None:
        if not self._authed():
            self._send_error_json(401, "authentication_error", "invalid x-api-key")
            return
        raw = self._read_body()
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            self._send_error_json(400, "invalid_request_error", "invalid JSON body")
            return
        if not isinstance(req, dict):
            self._send_error_json(400, "invalid_request_error", "body must be an object")
            return

        payload = anthropic_to_openai(req, self.shim.model_id)
        stream = bool(payload.get("stream"))
        upstream_req = urllib.request.Request(
            self.shim.upstream + "/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST")
        try:
            resp = urllib.request.urlopen(upstream_req, timeout=UPSTREAM_TIMEOUT_S)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:2000]
            except OSError:
                pass
            self._send_error_json(e.code, "api_error",
                                  f"upstream error {e.code}: {detail or e.reason}")
            return
        except (urllib.error.URLError, OSError) as e:
            self._send_error_json(502, "api_error", f"upstream unreachable: {e}")
            return

        with resp:
            if stream:
                self._relay_stream(resp, input_tokens=_estimate_tokens(len(raw)))
            else:
                try:
                    data = json.loads(resp.read())
                except ValueError:
                    self._send_error_json(502, "api_error",
                                          "upstream returned invalid JSON")
                    return
                self._send_json(200, openai_to_anthropic(data, self.shim.model_id))

    # -- streaming ----------------------------------------------------------

    def _write_event(self, name: str, payload: dict) -> None:
        self.wfile.write(f"event: {name}\ndata: "
                         f"{json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _relay_stream(self, resp, input_tokens: int) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.close_connection = True
        self.end_headers()
        self._streaming = True  # plus jamais de réponse HTTP sur cette connexion

        tr = StreamTranslator(self.shim.model_id, input_tokens=input_tokens)
        it = iter(resp)
        try:
            for name, payload in tr.start_events():
                self._write_event(name, payload)
            while True:
                # la lecture upstream est isolée du write-path : une panne de
                # llama-server (RST, timeout, IncompleteRead) ne doit pas être
                # confondue avec un client parti — on clôt proprement la séquence
                # Anthropic via end_events() au lieu de tronquer le stream
                try:
                    raw_line = next(it)
                except StopIteration:
                    break
                except Exception:  # noqa: BLE001 — upstream mort en plein stream
                    break
                line = raw_line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue  # chunk malformé : on n'abat pas le stream
                for name, payload in tr.feed(chunk):
                    self._write_event(name, payload)
            for name, payload in tr.end_events():
                self._write_event(name, payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # le client a raccroché — rien à faire


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Shim:
    """Serveur shim Anthropic-compatible devant llama-server.

    `start()` est non-bloquant (thread daemon) ; `stop()` arrête proprement.
    """

    def __init__(self, upstream: str = DEFAULT_UPSTREAM, port: int = DEFAULT_PORT,
                 token: str = "", model_id: str = "local-model"):
        self.upstream = upstream.rstrip("/")
        self.port = port
        self.token = token
        self.model_id = model_id
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._httpd is not None:
            return
        self._httpd = _Server(("0.0.0.0", self.port), _Handler)
        self._httpd.shim = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="magnitude-shim", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


# ---------------------------------------------------------------------- standalone

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python3 -m magnitude.shim",
        description="SOKKAN Magnitude — Anthropic-compatible shim for llama-server")
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM,
                    help=f"llama-server base URL (default {DEFAULT_UPSTREAM})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"shim listen port (default {DEFAULT_PORT})")
    ap.add_argument("--token", required=True, help="serve token (auth of callers)")
    ap.add_argument("--model", default="local-model", dest="model",
                    help="model id reported to clients and sent upstream")
    args = ap.parse_args(argv)

    shim = Shim(upstream=args.upstream, port=args.port,
                token=args.token, model_id=args.model)
    shim.start()
    print(f"magnitude shim: 0.0.0.0:{args.port} -> {shim.upstream} "
          f"(model {args.model})", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        shim.stop()


if __name__ == "__main__":
    main()
