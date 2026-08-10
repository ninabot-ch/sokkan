"""SOKKAN Magnitude — host agent (pure stdlib).

Profiles the machine, benchmarks local GGUF models with llama.cpp, serves one
via llama-server and exposes it to the cockpit through an Anthropic-compatible
shim. Runs on the client's machine with nothing but Python 3 — no pip install.
"""
__version__ = "0.1.0"
