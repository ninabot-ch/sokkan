"""extract.py — file -> text, through a child process that can be killed.

`Extractor` owns one worker process (extract_worker.py). A request that exceeds the timeout,
or a worker that dies (segfault of a parser, memory limit), is an ERROR OF THAT FILE — it is
recorded and the pass goes on with a fresh worker.
"""
from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import Limits

HERE = Path(__file__).resolve().parent
RECYCLE_AFTER = 200           # documents per worker (parsers leak)


def docling_mode() -> str:
    v = (os.environ.get("SOKKAN_DOC_INGEST_DOCLING") or "auto").strip().lower()
    return v if v in ("auto", "off", "always") else "auto"


def _rss_mb(pid: int) -> int:
    """Resident memory of the child (what it really holds — not its address space)."""
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0


class Extractor:
    def __init__(self, limits: Limits | None = None, python: str | None = None):
        self.limits = limits or Limits.from_env()
        self.python = python or os.environ.get("SOKKAN_DOC_INGEST_PYTHON") or sys.executable
        self._p: subprocess.Popen | None = None
        self._n = 0
        self._lock = threading.Lock()

    def _spawn(self) -> subprocess.Popen:
        env = {**os.environ, "PYTHONPATH": str(HERE.parent), "PYTHONUNBUFFERED": "1",
               # Docling/transformers must not call home during a pass (models are fetched
               # once, at install time — DOC-INGEST.md § 5)
               "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE", "1"),
               "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
               # parsers size their thread pools on the host's cores: a few are enough
               "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS", "4")}
        self._n = 0
        return subprocess.Popen(
            [self.python, "-m", "docingest.extract_worker"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True,
            encoding="utf-8", bufsize=1)

    def close(self) -> None:
        with self._lock:
            self._kill()

    def _kill(self) -> None:
        if self._p is not None:
            try:
                self._p.kill()
                self._p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._p = None

    def extract(self, path: Path) -> dict:
        """{"ok": True, text, title, headings, meta, extractor, chars, truncated, text_sha}
        or {"ok": False, error, kind} — never raises for a bad file."""
        try:
            size = path.stat().st_size
        except OSError as e:
            return {"ok": False, "error": f"cannot stat: {e}", "kind": "unreadable"}
        if size > self.limits.max_file_mb * 1024 * 1024:
            return {"ok": False, "kind": "too_big",
                    "error": f"{size // (1024 * 1024)} MB > limit {self.limits.max_file_mb:g} MB"}
        if size == 0:
            return {"ok": False, "kind": "empty", "error": "empty file"}
        with self._lock:
            if self._p is None or self._p.poll() is not None or self._n >= RECYCLE_AFTER:
                self._kill()
                self._p = self._spawn()
            p = self._p
            self._n += 1
            try:
                assert p.stdin is not None and p.stdout is not None
                p.stdin.write(json.dumps({"path": str(path), "docling": docling_mode(),
                                          "max_chars": self.limits.max_doc_chars}) + "\n")
                p.stdin.flush()
                deadline = time.monotonic() + self.limits.extract_timeout_s
                while True:
                    ready, _, _ = select.select([p.stdout], [], [], 0.5)
                    if ready:
                        break
                    if time.monotonic() >= deadline:
                        self._kill()
                        return {"ok": False, "kind": "timeout", "error":
                                f"extraction exceeded {self.limits.extract_timeout_s} s"}
                    rss = _rss_mb(p.pid)
                    if self.limits.extract_mem_mb and rss > self.limits.extract_mem_mb:
                        self._kill()
                        return {"ok": False, "kind": "memory", "error":
                                f"extraction used {rss} MB > limit "
                                f"{self.limits.extract_mem_mb} MB"}
                line = p.stdout.readline()
            except (BrokenPipeError, OSError):
                line = ""
            if not line:
                code = p.poll()
                self._kill()
                return {"ok": False, "kind": "crash",
                        "error": f"the extractor died on this file (exit {code})"}
        try:
            res = json.loads(line)
        except ValueError:
            return {"ok": False, "kind": "crash", "error": "unreadable extractor answer"}
        if res.get("ok"):
            norm = " ".join(res["text"].split()).lower()
            res["text_sha"] = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        return res
