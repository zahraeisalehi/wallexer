"""Control channel: unix socket, one JSON object per line in each direction.

Commands: ping | status | stop [markets] | flatten [markets]. The CLI uses `send()`.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)
Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class EngineNotRunning(Exception):
    pass


async def serve(path: Path, handler: Handler) -> asyncio.AbstractServer:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()  # stale socket from a crashed run

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            try:
                req = json.loads(line or b"{}")
                resp = await handler(req if isinstance(req, dict) else {})
            except Exception as e:  # a bad request must never kill the engine
                log.warning("control.error", error=str(e)[:200])
                resp = {"ok": False, "error": str(e)[:200]}
            writer.write((json.dumps(resp, default=str) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_unix_server(on_client, path=str(path))
    path.chmod(0o600)
    return server


def send(path: Path, cmd: str, timeout: float = 30.0, **args: Any) -> dict[str, Any]:
    """Blocking client for the CLI."""
    if not path.exists():
        raise EngineNotRunning()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError) as e:
        raise EngineNotRunning() from e
    try:
        s.sendall((json.dumps({"cmd": cmd, **args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf or b"{}")
    finally:
        s.close()
