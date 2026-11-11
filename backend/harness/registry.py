"""The engines this instance can run a session on: declared here, detected on demand,
selected by configuration.

`SOKKAN_ENGINE_RUNTIME` names the engine of new runs (default: the first declared). An
unknown or undeclared name falls back to the default, and says so on stderr — a typo in
the environment never leaves an instance without sessions.

An adapter module is imported only when its engine is created or detected: declaring an
engine costs nothing to an instance that does not use it.
"""
from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass

from .base import Harness
from .contract import Capabilities


@dataclass(frozen=True)
class Engine:
    name: str           # the id: SOKKAN_ENGINE_RUNTIME, `run.started.harness`
    label: str          # for people (Setup › Engines)
    module: str         # where the adapter lives
    factory: str        # the Harness subclass in that module

    def load(self):
        return importlib.import_module(self.module)

    def create(self) -> Harness:
        return getattr(self.load(), self.factory)()

    def capabilities(self) -> Capabilities:
        # asked of an instance, not of the module: what an engine can do may depend on how
        # it is deployed (a remote engine is probed), and is known before any run starts
        return self.create().capabilities()

    def detect(self) -> tuple[bool, str]:
        """(usable here, why / how). Never raises."""
        try:
            return self.load().detect()
        except Exception as e:  # noqa: BLE001 — the adapter's dependency is missing
            return False, f"{type(e).__name__}: {e}"


# declaration order = preference order; the first one is the default
_DECLARED: dict[str, Engine] = {}


def declare(engine: Engine) -> None:
    _DECLARED[engine.name] = engine


declare(Engine(name="legacy", label="Claude Code CLI (Claude Agent SDK)",
               module="harness.legacy", factory="LegacyHarness"))

ENV = "SOKKAN_ENGINE_RUNTIME"


def declared() -> list[str]:
    return list(_DECLARED)


def default() -> str:
    return next(iter(_DECLARED))


def get(name: str) -> Engine:
    return _DECLARED[name]


def selected() -> str:
    """The engine of new runs on this instance."""
    v = (os.environ.get(ENV) or "").strip().lower()
    if not v:
        return default()
    if v not in _DECLARED:
        print(f"[sokkan] {ENV}={v!r} unknown (declared: {', '.join(_DECLARED)}): "
              f"using {default()}", file=sys.stderr)
        return default()
    return v


def create(name: str | None = None) -> Harness:
    """A new, not yet started, harness of the selected (or named) engine."""
    return _DECLARED[name or selected()].create()


def capabilities(name: str | None = None) -> Capabilities:
    return _DECLARED[name or selected()].capabilities()


def describe() -> list[dict]:
    """Every declared engine: selected or not, usable here or not, what it can do."""
    cur = selected()
    out = []
    for e in _DECLARED.values():
        ok, note = e.detect()
        caps = e.capabilities().to_dict() if ok else None
        out.append({"name": e.name, "label": e.label, "selected": e.name == cur,
                    "available": ok, "note": note, "capabilities": caps})
    return out
