"""Minimal HTTP server: public GET /health (for hosting), the token-protected read-only /api/*
(engine/api.py) and GET /export (the `wallexer export` zip, header X-Export-Token must equal env
EXPORT_TOKEN; 403 otherwise, also when EXPORT_TOKEN is not set). No framework; one request per
connection."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from wallexer.engine.api import ReadOnlyApi

MAX_HEADERS = 64
CHUNK = 64 * 1024


def export_allowed(headers: dict[str, str]) -> bool:
    token = os.environ.get("EXPORT_TOKEN", "")
    given = headers.get("x-export-token", "")
    return bool(token) and hmac.compare_digest(given.encode(), token.encode())


async def _export(
    method: str,
    headers: dict[str, str],
    export: Callable[[], Awaitable[bytes]],
    writer: asyncio.StreamWriter,
) -> tuple[int, dict[str, Any]]:
    if method != "GET":
        return 405, {"error": "GET only"}
    if not export_allowed(headers):
        return 403, {"error": "missing or wrong X-Export-Token"}
    try:
        blob = await export()
    except Exception as e:  # report it; never drop the connection silently
        return 500, {"error": f"export failed: {type(e).__name__}"}
    writer.write(
        "HTTP/1.1 200 OK\r\nContent-Type: application/zip\r\n"
        'Content-Disposition: attachment; filename="logs.zip"\r\n'
        f"Content-Length: {len(blob)}\r\nConnection: close\r\n\r\n".encode()
    )
    for i in range(0, len(blob), CHUNK):  # stream in chunks
        writer.write(blob[i : i + CHUNK])
        await writer.drain()
    return 200, {}


async def serve(
    port: int,
    snapshot: Callable[[], dict[str, Any]],
    api: ReadOnlyApi | None = None,
    export: Callable[[], Awaitable[bytes]] | None = None,
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
            if export is not None and (target == "/export" or target.startswith("/export?")):
                code, data = await _export(method, headers, export, writer)
                if code == 200:
                    return  # the zip was written
            elif target == "/health" or target.startswith("/health?"):
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
                403: "Forbidden",
                404: "Not Found",
                405: "Method Not Allowed",
                500: "Internal Server Error",
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
