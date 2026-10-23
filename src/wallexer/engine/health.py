"""Minimal HTTP server: public GET /health (for hosting) + the token-protected read-only /api/*
(engine/api.py). No framework; JSON bodies; one request per connection."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any

from wallexer.engine.api import ReadOnlyApi

MAX_HEADERS = 64


async def serve(
    port: int, snapshot: Callable[[], dict[str, Any]], api: ReadOnlyApi | None = None
) -> asyncio.AbstractServer:
    # Bind publicly in the container (Liara's proxy must reach it) or when the platform sets
    # PORT; locally stay on loopback.
    public = "PORT" in os.environ or os.environ.get("WALLEXER_IN_CONTAINER") == "1"
    host = "0.0.0.0" if public else "127.0.0.1"

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = (await asyncio.wait_for(reader.readline(), timeout=5)).decode("latin-1")
            headers: dict[str, str] = {}
            for _ in range(MAX_HEADERS + 1):
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            else:
                raise ConnectionError("too many headers")
            method, target = (request.split(" ") + ["", ""])[:2]
            if target == "/health" or target.startswith("/health?"):
                code, data = (200, snapshot()) if method == "GET" else (405, {"error": "GET only"})
            elif api is not None and (target == "/api" or target.startswith("/api/")):
                code, data = await api.handle(method, target, headers)
            else:
                code, data = 404, {"error": "not found"}
            body = json.dumps(data, default=str, ensure_ascii=False)
            reason = {
                200: "OK",
                400: "Bad Request",
                401: "Unauthorized",
                404: "Not Found",
                405: "Method Not Allowed",
                503: "Service Unavailable",
            }.get(code, "Error")
            allow = "Allow: GET\r\n" if code == 405 else ""
            writer.write(
                f"HTTP/1.1 {code} {reason}\r\nContent-Type: application/json; charset=utf-8\r\n"
                f"{allow}Content-Length: {len(body.encode())}\r\nConnection: close\r\n\r\n{body}".encode()
            )
            await writer.drain()
        except (TimeoutError, ConnectionError, UnicodeError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(on_client, host, int(os.environ.get("PORT", port)))
