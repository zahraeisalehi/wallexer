"""Where the read-only views get their data: the local DB/engine, or a server's /api (remote mode).

Remote mode is on with `wallexer --remote`, or `[remote] default = true` in the user config
(`wallexer setup`). In remote mode every command that could change state is refused before any
network call (`guard_write`); the server API has no write endpoints anyway.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import httpx

from wallexer.config import ConfigError, Settings

# Set by the Typer callback (`--remote`); survives across commands inside the REPL.
FORCED_REMOTE = {"on": False}
# Set when the zero-setup demo server is unreachable: run local (public commands only).
FORCED_LOCAL = {"on": False}

# Commands that act on an engine, its settings or the account. Disabled in remote mode.
# (backtest/doctor/markets/history/research stay available: they run locally, read-only on the
# exchange; `doctor --live-smoke` is guarded separately.)
WRITE_COMMANDS = frozenset(
    {
        "trade",
        "stop",
        "flatten",
        "change",
        "resume",
        "shadowtest",
        "config",
        "autoset",
        "engine",
        "agent",
    }
)


class RemoteReadOnly(Exception):
    """A state-changing command was invoked in remote (read-only) mode."""


class RemoteError(Exception):
    """Short, user-facing problem talking to the remote API."""


@dataclass(frozen=True)
class Remote:
    url: str
    token: str  # "" = none (a public demo server needs none)
    demo: bool = False  # the zero-setup default (no `wallexer setup` yet)


def remote_on(settings: Settings) -> bool:
    if FORCED_LOCAL["on"]:
        return False
    return FORCED_REMOTE["on"] or settings.cfg.remote.default


def remote(settings: Settings) -> Remote | None:
    """The remote target if remote mode is on, else None (local)."""
    rc = settings.cfg.remote
    if not remote_on(settings):
        return None
    url = (rc.url or settings.secrets.wallexer_remote_url or "").strip().rstrip("/")
    tok = settings.secrets.wallexer_view_token
    if not url:
        raise ConfigError("remote mode needs a server URL: run `wallexer setup` (remote).")
    # The token is optional: a public demo answers without one, a private server says 401.
    return Remote(url, tok.get_secret_value().strip() if tok else "", rc.zero_config)


def host(url: str) -> str:
    return url.split("://", 1)[-1]


def guard_write(settings: Settings, command: str) -> None:
    """Refuse state-changing commands in remote mode (raised before any request is made)."""
    if remote_on(settings):
        raise RemoteReadOnly(f"`{command}` is disabled in remote mode (read-only viewing)")


def api_get(r: Remote, path: str, params: dict[str, Any] | None = None, timeout: float = 10) -> Any:
    try:
        resp = httpx.get(
            f"{r.url}{path}",
            params={k: v for k, v in (params or {}).items() if v is not None},
            headers={"X-View-Token": r.token} if r.token else {},
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise RemoteError(f"cannot reach {host(r.url)}: {type(e).__name__}") from None
    if resp.status_code == 401:
        from wallexer.config import credentials_path

        raise RemoteError(
            f"{host(r.url)} rejected the view token from {credentials_path()} (re-enter it with "
            "`wallexer setup --mode remote`)"
            if r.token
            else f"{host(r.url)} needs a view token: `wallexer setup --mode remote`"
        )
    if resp.status_code == 404 and path.startswith("/api/") and path.count("/") <= 2:
        raise RemoteError(
            f"{r.url} has no read-only API (WALLEXER_VIEW_TOKEN not set on the server, "
            "or the server runs an older version)"
        )
    try:
        data = resp.json()
    except ValueError:
        raise RemoteError(f"{r.url}{path}: HTTP {resp.status_code}, not JSON") from None
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise RemoteError(f"{r.url}{path}: HTTP {resp.status_code} {data.get('error', '')}")
    return data


def _db(settings: Settings) -> sqlite3.Connection:
    from wallexer.store.db import init_db

    return init_db(settings.cfg.path(settings.cfg.store.db_path))


# ---------------------------------------------------------------------------------- fetchers


def status(settings: Settings) -> tuple[dict[str, Any] | None, bool, str]:
    """(snapshot, live, origin). origin = 'LOCAL' or the remote URL."""
    r = remote(settings)
    if r:
        st = api_get(r, "/api/status")
        return st, True, r.url
    from wallexer.cli.engine_cmds import get_status

    st, live = get_status(settings)
    return st, live, "LOCAL"


def log(
    settings: Settings, n: int | None = None, after: int | None = None, limit: int = 500
) -> dict[str, Any]:
    from wallexer.cli import views

    r = remote(settings)
    if r:
        return api_get(r, "/api/log", {"n": n, "after": after, "limit": limit})
    conn = _db(settings)
    try:
        return views.log_data(conn, n, after, limit)
    finally:
        conn.close()


def reason(settings: Settings, n: int | None) -> dict[str, Any] | None:
    from wallexer.cli import views

    r = remote(settings)
    if r:
        return api_get(r, "/api/reason" if n is None else f"/api/reason/{n}")
    conn = _db(settings)
    try:
        return views.reason_overview_data(conn) if n is None else views.reason_data(conn, n)
    finally:
        conn.close()


def rslt(settings: Settings, which: str) -> dict[str, Any] | None:
    from wallexer.cli import views

    r = remote(settings)
    if r:
        return (api_get(r, f"/api/rslt/{which}") or {}).get("result")
    cfg = settings.cfg
    conn = _db(settings)
    try:
        if which == "backtest":
            return views.rslt_backtest_data(conn, float(cfg.paper.capital_tmn))
        return views.rslt_shadow_data(
            conn, float(cfg.account.expected_equity_tmn), float(cfg.paper.capital_tmn)
        )
    finally:
        conn.close()


def origin_label(settings: Settings) -> str:
    try:
        r = remote(settings)
    except ConfigError:
        return "REMOTE (not configured)"
    return f"REMOTE {r.url}" if r else "LOCAL"


def reachable(settings: Settings, timeout: float = 3.0) -> str | None:
    """None if the remote answers /api/status (or remote mode is off), else a short reason."""
    try:
        r = remote(settings)
        if r:
            api_get(r, "/api/status", timeout=timeout)
    except (RemoteError, ConfigError) as e:
        return str(e)
    return None
