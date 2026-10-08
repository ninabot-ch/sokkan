"""teams.manifest — the Teams app package of this instance (one source for the admin route
``GET /api/admin/teams/manifest`` and for ``scripts/teams-manifest.py``).

Manifest schema **v1.17** on purpose: everything Nina uses (a bot in personal / team /
groupChat scopes, command lists, validDomains, webApplicationInfo) exists since 1.x, and 1.17
is accepted by every current Teams client and admin center — a newer schema (1.30 in
08.2026) would add nothing used here. The package = ``manifest.json`` + ``color.png``
(192×192) + ``outline.png`` (32×32, white on transparent), zipped flat.
"""
from __future__ import annotations

import io
import re
import struct
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urlparse

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MANIFEST_VERSION = "1.17"
SCHEMA_URL = (f"https://developer.microsoft.com/en-us/json-schemas/teams/v{MANIFEST_VERSION}/"
              "MicrosoftTeams.schema.json")
ACCENT = "#0E7C86"
COMMANDS = [
    ("status", "Status of the linked project"),
    ("decision:", "Note a decision in the project memory"),
    ("card:", "Create a card"),
    ("run", "Propose a run of an agent (approval)"),
    ("approvals", "Pending approvals"),
]


class ManifestError(ValueError):
    pass


def _app_version() -> str:
    try:
        v = (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()
    except OSError:
        v = ""
    parts = v.split("-")[0].split(".")
    return v if len(parts) == 3 and all(p.isdigit() for p in parts) else "1.0.0"


def host_of(public_url: str) -> str:
    u = urlparse(public_url if "://" in public_url else f"https://{public_url}")
    if u.scheme != "https" or not u.hostname or u.hostname in ("localhost",) or "." not in u.hostname:
        raise ManifestError("SOKKAN_TEAMS_PUBLIC_URL (or SOKKAN_PUBLIC_URL) must be the public "
                            "https address of the instance (Teams calls it)")
    return u.hostname.lower()


def build(app_id: str, public_url: str, *, name: str = "Nina (SOKKAN)",
          developer: str = "SOKKAN", sso: bool = False, version: str = "") -> dict:
    """The manifest. ``validDomains`` = the instance's public host only; ``webApplicationInfo``
    only when ``sso`` (it must match an « Expose an API » URI of the Entra app)."""
    if not app_id:
        raise ManifestError("set SOKKAN_TEAMS_APP_ID first")
    if not _GUID.match(app_id):
        raise ManifestError("SOKKAN_TEAMS_APP_ID must be the application (client) id, a GUID")
    host = host_of(public_url)
    site = f"https://{host}"
    m = {
        "$schema": SCHEMA_URL,
        "manifestVersion": MANIFEST_VERSION, "version": version or _app_version(),
        "id": app_id,
        "developer": {"name": developer[:32], "websiteUrl": site, "privacyUrl": site,
                      "termsOfUseUrl": site},
        "name": {"short": name[:30], "full": f"{name} — SOKKAN assistant"[:100]},
        "description": {"short": "Ask Nina about your SOKKAN projects."[:80],
                        "full": "Project status, cards, agent approvals and decision capture, "
                                "answered as you, within your clearance."},
        "icons": {"color": "color.png", "outline": "outline.png"}, "accentColor": ACCENT,
        "bots": [{"botId": app_id, "scopes": ["personal", "team", "groupChat"],
                  "supportsFiles": False, "isNotificationOnly": False,
                  "commandLists": [{"scopes": ["personal", "team", "groupChat"],
                                    "commands": [{"title": t, "description": d}
                                                 for t, d in COMMANDS]}]}],
        "permissions": ["identity", "messageTeamMembers"],
        "validDomains": [host],
    }
    if sso:
        m["webApplicationInfo"] = {"id": app_id, "resource": f"api://{host}/{app_id}"}
    return m


# ---- icons: written by hand (zlib + struct, no imaging library in the image) ------------
def _png(w: int, h: int, pixel) -> bytes:
    rows = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(w)) for y in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def _mark(x: float, y: float, size: int) -> bool:
    """A ship's wheel: a rim, a hub and eight spokes ending in handles (centre at size/2)."""
    import math
    c = (size - 1) / 2
    dx, dy = x - c, y - c
    r = math.hypot(dx, dy)
    if 0.25 * size <= r <= 0.32 * size or r <= 0.08 * size:
        return True                                    # rim, hub
    if r > 0.47 * size:
        return False
    a = math.atan2(dy, dx) % (math.pi / 4)
    off = r * math.sin(min(a, math.pi / 4 - a))        # distance to the nearest spoke
    width = 0.035 * size if r <= 0.32 * size else 0.06 * size   # handles are thicker
    return off <= width


def color_icon() -> bytes:
    acc = tuple(int(ACCENT[i:i + 2], 16) for i in (1, 3, 5))
    return _png(192, 192, lambda x, y: (255, 255, 255, 255) if _mark(x, y, 192)
                else (*acc, 255))


def outline_icon() -> bytes:
    """32×32, white on a transparent background (Teams' rule for the outline icon)."""
    return _png(32, 32, lambda x, y: (255, 255, 255, 255) if _mark(x, y, 32) else (0, 0, 0, 0))


def package(manifest: dict) -> bytes:
    import json
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        z.writestr("color.png", color_icon())
        z.writestr("outline.png", outline_icon())
    return buf.getvalue()
