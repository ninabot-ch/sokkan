#!/usr/bin/env python3
"""fleetterm.py — SOKKAN : terminal de MAINTENANCE vers les instances de la flotte.

Bridge WebSocket ↔ pty qui spawn `ssh root@<name>.fleet` avec la clé de
maintenance de l'instance (fleet.KEY_PATH). Réservé : c'est un accès ROOT aux
machines du client, pensé maintenance/incident — PAS un poste de travail.

Droits : admin/owner de l'instance, ou user explicitement autorisé par un admin
(grants persistés dans /data). Chaque ouverture de session est auditée.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import signal
import struct
import termios

import fleet

GRANTS_PATH = os.path.join(os.environ.get("SOKKAN_DATA_DIR", "/data"), "fleet_term_grants.json")
_NAME_RE = re.compile(r"^[a-z0-9-]{1,20}$")


def grants() -> list[str]:
    try:
        return json.load(open(GRANTS_PATH))
    except Exception:  # noqa: BLE001
        return []


def set_grants(emails: list[str]) -> list[str]:
    g = sorted({e.strip().lower() for e in emails if e.strip()})
    json.dump(g, open(GRANTS_PATH, "w"))
    return g


def allowed(user: dict, rank, admin_rank: int) -> bool:
    """Admin/owner toujours ; sinon grant explicite posé par un admin."""
    return rank(user["role"]) >= admin_rank or user["email"].lower() in grants()


async def bridge(ws, name: str, cols: int = 120, rows: int = 32) -> None:
    """Relaye le WebSocket vers un ssh dans un pty. Frames texte = clavier ;
    frame JSON {resize:[cols,rows]} = resize ; sortie pty → frames texte."""
    if not _NAME_RE.match(name):
        await ws.close(code=4400)
        return
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-i", fleet.KEY_PATH,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        f"root@{name}.fleet",
        stdin=slave, stdout=slave, stderr=slave,
        preexec_fn=os.setsid,
    )
    os.close(slave)
    loop = asyncio.get_running_loop()

    async def pty_to_ws():
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, master, 65536)
            except OSError:
                break
            if not data:
                break
            await ws.send_text(data.decode(errors="replace"))

    reader = asyncio.create_task(pty_to_ws())
    try:
        while True:
            msg = await ws.receive_text()
            if msg.startswith('{"resize"'):
                try:
                    c, r = json.loads(msg)["resize"]
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", int(r), int(c), 0, 0))
                    continue
                except Exception:  # noqa: BLE001
                    pass
            os.write(master, msg.encode())
    except Exception:  # noqa: BLE001 — déconnexion WS
        pass
    finally:
        reader.cancel()
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
        except ProcessLookupError:
            pass
        os.close(master)
