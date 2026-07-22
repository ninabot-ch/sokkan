#!/usr/bin/env python3
"""termproxy.py — SOKKAN : proxy authentifié vers ttyd (terminal /term).

ttyd ne connaît pas la session SOKKAN. On le place DERRIÈRE le backend : toute
requête /term (HTTP assets + WebSocket) passe par ici, qui vérifie l'identité
(auth.current_user) AVANT de relayer vers ttyd loopback. → le terminal web hérite
de l'auth SOKKAN (OIDC/cf-access), plus besoin de CF Access sur /term.
"""
from __future__ import annotations

import asyncio
import os

import httpx
import websockets
from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import Response

import auth

TTYD = os.environ.get("SOKKAN_TTYD", "127.0.0.1:7681")
_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "content-encoding", "content-length"}


def _authorized(scope_obj) -> bool:
    """Le terminal ttyd = shell interactif DANS le conteneur api (≈ root
    fonctionnel) → réservé admin+ (même barre que les endpoints tmux). Résoudre
    une simple identité ne suffit PAS : un viewer/dev ne doit jamais l'atteindre."""
    import iam
    try:
        u = auth.current_user(scope_obj)  # lit headers/cookies (Request ou WebSocket)
    except HTTPException:
        return False
    return iam.rank(u.get("role", "")) >= iam.rank("admin")


async def http(request: Request, path: str) -> Response:
    if not _authorized(request):
        raise HTTPException(401, "authentication required")
    url = f"http://{TTYD}/term/{path}"
    async with httpx.AsyncClient(timeout=20) as c:
        up = await c.request(
            request.method, url, params=request.query_params,
            content=await request.body() if request.method in ("POST", "PUT") else None,
        )
    headers = {k: v for k, v in up.headers.items() if k.lower() not in _HOP}
    return Response(content=up.content, status_code=up.status_code, headers=headers)


async def ws(client: WebSocket, path: str) -> None:
    if not _authorized(client):
        await client.close(code=4401)
        return
    # négocier le sous-protocole ttyd ("tty")
    req_protos = client.headers.get("sec-websocket-protocol", "")
    sub = "tty" if "tty" in req_protos else None
    await client.accept(subprotocol=sub)

    qs = client.url.query
    up_url = f"ws://{TTYD}/term/{path}" + (f"?{qs}" if qs else "")
    try:
        async with websockets.connect(
            up_url, subprotocols=["tty"] if sub else None, max_size=None, open_timeout=10
        ) as up:
            async def c2u():
                try:
                    while True:
                        msg = await client.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("bytes") is not None:
                            await up.send(msg["bytes"])
                        elif msg.get("text") is not None:
                            await up.send(msg["text"])
                except Exception:  # noqa: BLE001
                    pass

            async def u2c():
                try:
                    async for m in up:
                        if isinstance(m, bytes):
                            await client.send_bytes(m)
                        else:
                            await client.send_text(m)
                except Exception:  # noqa: BLE001
                    pass

            await asyncio.gather(c2u(), u2c())
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
