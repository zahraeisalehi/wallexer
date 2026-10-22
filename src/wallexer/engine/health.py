"""Minimal GET /health HTTP endpoint (for hosting). No framework: one route, JSON body."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any


async def serve(port: int, snapshot: Callable[[], dict[str, Any]]) -> asyncio.AbstractServer:
    # Bind publicly only when the platform provides PORT (Liara); locally stay on loopback.
    host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5)
            while (await asyncio.wait_for(reader.readline(), timeout=5)) not in (
                b"\r\n",
                b"\n",
                b"",
            ):
                pass
            if request.startswith(b"GET /health"):
                status, body = "200 OK", json.dumps(snapshot(), default=str)
            else:
                status, body = "404 Not Found", '{"error": "not found"}'
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body.encode())}\r\nConnection: close\r\n\r\n{body}".encode()
            )
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(on_client, host, int(os.environ.get("PORT", port)))
