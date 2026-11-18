"""The session layer: one contract (`contract`), one interface (`base.Harness`), the
engines that implement it (`registry`). See README.md in this directory.

Importing this package imports no engine: an adapter is loaded when it is created.
"""
from . import contract  # noqa: F401
from .base import Harness  # noqa: F401
