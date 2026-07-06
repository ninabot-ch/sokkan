#!/usr/bin/env python3
"""provision.py — SOKKAN: open connector to the NINABOT provisioning control plane.

This is the *auditable* half of the open-core boundary: this instance never
holds any cloud credentials. When configured, it forwards environment requests
(list / spawn / destroy) to a remote provisioner over HTTPS with a bearer
token scoped to this instance. The provisioner (closed, operated by NINABOT)
enforces quotas and runs Terraform against Exoscale.

Disabled unless BOTH env vars are set — on a plain self-hosted install the
feature simply doesn't exist (404):

  SOKKAN_PROVISIONER_URL     e.g. https://provision.sokkan.ch (or loopback)
  SOKKAN_PROVISIONER_TOKEN   bearer issued by NINABOT for this instance
"""
from __future__ import annotations

import os

import httpx

URL = (os.environ.get("SOKKAN_PROVISIONER_URL") or "").rstrip("/")
TOKEN = os.environ.get("SOKKAN_PROVISIONER_TOKEN", "")
ENABLED = bool(URL and TOKEN)
TIERS = ("starter", "standard", "studio")


class ProvisionerError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def _req(method: str, path: str, json: dict | None = None, params: dict | None = None) -> dict | list:
    try:
        r = httpx.request(
            method, f"{URL}{path}", json=json, params=params,
            headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30.0,
        )
    except httpx.HTTPError as e:
        raise ProvisionerError(502, f"provisioner unreachable: {e}") from e
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text[:200])
        except ValueError:
            detail = r.text[:200]
        raise ProvisionerError(r.status_code, detail)
    return r.json()


def list_envs() -> list:
    return _req("GET", "/envs")


def env_detail(client: str) -> dict:
    return _req("GET", f"/envs/{client}")


def spawn(client: str, tier: str, owner_email: str) -> dict:
    return _req("POST", "/envs", json={"client": client, "tier": tier, "owner_email": owner_email})


def destroy(client: str) -> dict:
    return _req("DELETE", f"/envs/{client}", params={"confirm": client})
