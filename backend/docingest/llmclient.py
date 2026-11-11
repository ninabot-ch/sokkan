"""llmclient.py — the ONE outbound call of the ingestion: a local OpenAI-compatible endpoint.

The documents of a customer are sent to the model. So the endpoint must be on the machine or
on its private network (the Anchor's own Ship model): `check_local` refuses a public address
unless the operator wrote SOKKAN_DOC_INGEST_ALLOW_REMOTE_LLM=1 — an explicit, logged choice.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
from urllib.parse import urlparse

import httpx

_CGNAT = ipaddress.ip_network("100.64.0.0/10")      # Tailscale / carrier-grade NAT


class LLMError(RuntimeError):
    pass


class NotLocal(LLMError):
    pass


class LLMUnavailable(LLMError):
    """The endpoint does not answer (restart, overload) even after the retries: the pass
    stops and resumes later — the documents are not marked as failed."""


# waits between attempts when the model is down or restarting (a local model reloads in a
# few minutes): ~6 minutes in all
RETRY_WAITS = (5, 15, 30, 60, 90, 150)


def endpoint() -> dict:
    url = (os.environ.get("SOKKAN_DOC_INGEST_LLM_URL")
           or os.environ.get("SOKKAN_ASSISTANT_LLM_URL") or "").strip().rstrip("/")
    model = (os.environ.get("SOKKAN_DOC_INGEST_LLM_MODEL")
             or os.environ.get("SOKKAN_ASSISTANT_LLM_MODEL") or "").strip()
    key = (os.environ.get("SOKKAN_DOC_INGEST_LLM_KEY") or "").strip()
    return {"url": url, "model": model, "key": key}


def is_local_address(host: str) -> bool:
    """True when EVERY address `host` resolves to is loopback, private, link-local or CGNAT."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addrs = {i[4][0] for i in infos}
    if not addrs:
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not (ip.is_loopback or ip.is_private or ip.is_link_local or
                (ip.version == 4 and ip in _CGNAT)):
            return False
    return True


def check_local(url: str) -> None:
    if (os.environ.get("SOKKAN_DOC_INGEST_ALLOW_REMOTE_LLM") or "").strip() == "1":
        return
    host = urlparse(url).hostname or ""
    if not host or not is_local_address(host):
        raise NotLocal(
            f"the model endpoint {host or url!r} is not on this machine or its private network: "
            "documents would leave the site. Point SOKKAN_DOC_INGEST_LLM_URL at the local "
            "model, or set SOKKAN_DOC_INGEST_ALLOW_REMOTE_LLM=1 to accept it explicitly.")


def ready() -> str | None:
    """None when a pass can call the model, else why not."""
    ep = endpoint()
    if not ep["url"] or not ep["model"]:
        return "no model endpoint (SOKKAN_DOC_INGEST_LLM_URL / SOKKAN_DOC_INGEST_LLM_MODEL)"
    try:
        check_local(ep["url"])
    except NotLocal as e:
        return str(e)
    return None


class Client:
    """`complete(system, user, schema)` -> (parsed JSON object, usage). Thread-safe."""

    def __init__(self, url: str | None = None, model: str | None = None, key: str | None = None,
                 timeout: float = 600.0):
        ep = endpoint()
        self.url = (url or ep["url"]).rstrip("/")
        self.model = model or ep["model"]
        self.key = ep["key"] if key is None else key
        if not self.url or not self.model:
            raise LLMError("no model endpoint configured")
        check_local(self.url)
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0),
                                  trust_env=False)   # no proxy: the call stays on the LAN
        self.retry_waits = RETRY_WAITS
        self.sleep = time.sleep
        self.should_stop = lambda: False

    def close(self) -> None:
        self._http.close()

    def complete(self, system: str, user: str, schema: dict, max_tokens: int = 4096,
                 name: str = "doc_notes") -> tuple[dict, dict]:
        body = {
            "model": self.model, "temperature": 0.1, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": name, "strict": True, "schema": schema}},
            # reasoning off: the answer IS the JSON (Qwen chat template; ignored elsewhere)
            "chat_template_kwargs": {"enable_thinking": False},
        }
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        r = None
        last = ""
        for wait in (*self.retry_waits, None):
            if self.should_stop():
                raise LLMUnavailable("stopped while waiting for the model")
            try:
                r = self._http.post(f"{self.url}/chat/completions", json=body, headers=headers)
                if r.status_code not in (502, 503, 504, 529):
                    break
                last, r = f"HTTP {r.status_code}", None
            except httpx.HTTPError as e:
                last = type(e).__name__
            if wait is not None:
                self.sleep(wait)
        if r is None:
            raise LLMUnavailable(f"model unreachable after {len(self.retry_waits) + 1} "
                                 f"attempts: {last}")
        if r.status_code != 200:
            raise LLMError(f"model answered HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
            choice = data["choices"][0]
            content = choice["message"]["content"] or ""
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("unreadable model answer") from None
        use = {"tokens_in": int(usage.get("prompt_tokens") or 0),
               "tokens_out": int(usage.get("completion_tokens") or 0)}
        if choice.get("finish_reason") == "length":
            raise LLMTruncated("the model's answer was cut (max_tokens)", use)
        try:
            obj = json.loads(content)
        except ValueError:
            raise LLMInvalid("the model did not answer valid JSON", use) from None
        if not isinstance(obj, dict):
            raise LLMInvalid("the model did not answer a JSON object", use)
        return obj, use


class LLMInvalid(LLMError):
    def __init__(self, msg: str, usage: dict):
        super().__init__(msg)
        self.usage = usage


class LLMTruncated(LLMInvalid):
    pass
