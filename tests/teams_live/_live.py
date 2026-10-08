"""Shared by the live Teams tests (a plain module: `conftest` is a name pytest owns)."""
import os

import pytest


def need(*names: str) -> dict:
    """The env values, or skip naming what is missing."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip("missing " + ", ".join(missing))
    return {n: os.environ[n] for n in names}
