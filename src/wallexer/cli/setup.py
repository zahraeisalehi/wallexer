"""`wallexer setup`: first-run wizard (also offered when `wallexer` starts without a config).

- local: Wallex trading-credit key + sub-account id, optional BoxAPI key.
- remote: server URL + read-only view token (status/log/reason/rslt/watch/sum from the server).
Secrets go to the credentials file with mode 0600 and are never printed. In a development
checkout the credentials are ./.env, which setup does not touch (edit it yourself).
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from rich.console import Console

from wallexer.config import DEV, _home_override, config_home, credentials_path, user_config_path

SECRET_KEYS = ("WALLEX_API_KEY", "WALLEX_SUBACCOUNT_ID", "BOXAPI_API_KEY", "WALLEXER_VIEW_TOKEN")


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                out[k.strip()] = v.strip()
    return out


def write_credentials(values: dict[str, str], drop: tuple[str, ...] = ()) -> Path:
    """Merge into the credentials file; created 0600 before anything is written to it.
    `drop` removes keys (e.g. an old view token when the server is public)."""
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {k: v for k, v in _read_env(path).items() if k not in drop}
    merged |= {k: v for k, v in values.items() if v}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# wallexer credentials (written by `wallexer setup`; keep private)\n")
        for k, v in merged.items():
            f.write(f"{k}={v}\n")
    os.chmod(path, 0o600)  # also tightens a pre-existing file
    return path


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v))


def write_user_config(remote_url: str, remote_default: bool) -> Path:
    """Keep any other sections the user wrote; replace [remote]."""
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            data = {}
    data["remote"] = {"url": remote_url, "default": remote_default}
    lines = ["# wallexer user settings (written by `wallexer setup`; overrides the defaults)"]
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        lines.append(f"\n[{section}]")
        for k, v in values.items():
            if not isinstance(v, dict):
                lines.append(f"{k} = {_toml_value(v)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _secret(console: Console, label: str, required: bool) -> str:
    while True:
        v = console.input(
            f"{label}{'' if required else ' (Enter to skip)'}: ", password=True
        ).strip()
        if v or not required:
            return v
        console.print("[warn]required[/]")


def _check_remote(console: Console, url: str, token: str) -> bool:
    from wallexer.cli.source import Remote, RemoteError, api_get

    try:
        st = api_get(Remote(url, token.strip()), "/api/status", timeout=10) or {}
    except RemoteError as e:
        console.print(f"[err]remote check failed:[/] {e}")
        return False
    console.print(
        f"[ok]connected[/] · server engine mode {st.get('mode')} · "
        f"equity {st.get('equity_tmn', '–')} TMN"
    )
    return True


def run_setup(console: Console, choice: str | None = None) -> bool:
    if DEV and _home_override() is None:
        console.print(
            "Development checkout: settings come from config/default.toml and secrets from ./.env "
            "(see .env.example). `wallexer setup` does not edit .env. For a user install, use "
            "install.sh (or set WALLEXER_HOME to try setup here)."
        )
        return False
    console.print(
        f"[key]wallexer setup[/] · config in {config_home()}\n"
        "  [key]1[/] local  · run the engine here (Wallex trading-credit key, optional BoxAPI key)\n"
        "  [key]2[/] remote · watch a server read-only (server URL + view token)\n"
        "  [key]3[/] skip   · public commands only (markets, help)"
    )
    choice = choice or console.input("Choose 1, 2 or 3 \\[2]: ").strip() or "2"
    if choice not in ("1", "2", "3"):
        console.print("[err]not a choice[/]: run `wallexer setup` again")
        return False
    if choice == "3":
        write_user_config("", False)
        console.print(
            "Saved. `wallexer markets` and `wallexer help` work without keys; `wallexer setup` any time."
        )
        return True
    if choice == "1":
        key = _secret(console, "Wallex API key (created INSIDE the trading-credit account)", True)
        sub = _secret(console, "Trading-credit sub-account id", True)
        box = _secret(console, "BoxAPI key (LLM agent, explanations)", False)
        path = write_credentials(
            {"WALLEX_API_KEY": key, "WALLEX_SUBACCOUNT_ID": sub, "BOXAPI_API_KEY": box}
        )
        write_user_config("", False)
        console.print(
            f"[ok]saved[/] credentials to {path} (mode 600, values hidden). Next: `wallexer doctor`."
        )
        return True
    url = ""
    while not url.startswith(("http://", "https://")):
        url = console.input("Server URL (e.g. https://wallexer.liara.run): ").strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            console.print("[warn]the URL must start with http:// or https://[/]")
    # Check the token against the server before saving it (a mistyped hidden paste was saved
    # silently before); up to three tries, Enter = a public demo server (no token).
    for _ in range(3):
        token = _secret(
            console, "View token (WALLEXER_VIEW_TOKEN on the server; Enter if public)", False
        )
        ok = _check_remote(console, url, token)
        if ok:
            break
    if not ok:
        console.print("[err]not saved[/]: the server did not accept it · `wallexer setup` again")
        return False
    path = write_credentials(
        {"WALLEXER_VIEW_TOKEN": token}, drop=() if token else ("WALLEXER_VIEW_TOKEN",)
    )
    write_user_config(url, True)
    saved = f" and the token to {path} (mode 600, hidden)" if token else ""
    console.print(f"[ok]saved[/] {user_config_path()}{saved}.")
    console.print(
        "Next: `wallexer status`, `wallexer watch`, `wallexer sum`. Trading commands are disabled in remote mode."
    )
    return ok
