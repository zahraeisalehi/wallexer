"""`wallexer watch`: live Textual view of the engine (read-only).

- Numbers panel (equity, PnL, drawdown, exposure, positions, weights, agent stance) is updated in
  place every 2 s, never printed twice.
- Log panel appends new decisions and events as they happen.
- Shadow and trade use clearly different themes (yellow "SHADOW" banner vs red "LIVE TRADE").
- The Quit button and `q` close the view only; the engine keeps running (use `wallexer stop`).
- Local mode reads the engine socket + DB; remote mode (`--remote`) reads the server's read-only
  API with the same panels (refreshed every 5 s).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, RichLog, Static

from wallexer.cli import source
from wallexer.cli.engine_cmds import get_status
from wallexer.cli.theme import duration, pct, tmn
from wallexer.config import Settings
from wallexer.store.db import connect

REFRESH_S = 2.0
REMOTE_REFRESH_S = 5.0


class LocalFeed:
    origin = "LOCAL"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.conn: sqlite3.Connection = connect(settings.cfg.path(settings.cfg.store.db_path))
        self.last_decision = self.conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM decisions"
        ).fetchone()[0]
        self.last_event = max(
            0, self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0] - 20
        )

    def status(self) -> tuple[dict[str, Any] | None, bool]:
        return get_status(self.settings)

    def decisions(self) -> list[dict[str, Any]]:
        rows = [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, ts, action, symbol, reason FROM decisions WHERE id > ? ORDER BY id",
                (self.last_decision,),
            )
        ]
        if rows:
            self.last_decision = rows[-1]["id"]
        return rows

    def events(self) -> list[dict[str, Any]]:
        rows = [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, ts, kind, payload FROM events WHERE id > ? ORDER BY id",
                (self.last_event,),
            )
        ]
        if rows:
            self.last_event = rows[-1]["id"]
        return rows

    def close(self) -> None:
        self.conn.close()


class RemoteFeed:
    def __init__(self, r: source.Remote) -> None:
        self.r = r
        self.origin = r.url
        self.last_decision: int | None = None
        self.last_event: int | None = None

    def status(self) -> tuple[dict[str, Any] | None, bool]:
        try:
            return source.api_get(self.r, "/api/status", timeout=5), True
        except source.RemoteError:
            return None, False

    def decisions(self) -> list[dict[str, Any]]:
        try:
            if self.last_decision is None:  # start from the newest, like the local view
                ov = source.api_get(self.r, "/api/reason", timeout=5) or {}
                self.last_decision = max((d["id"] for d in ov.get("decisions") or []), default=0)
                return []
            rows = (
                source.api_get(self.r, "/api/reason", {"after": self.last_decision}, timeout=5)
                or {}
            ).get("decisions") or []
        except source.RemoteError:
            return []
        if rows:
            self.last_decision = rows[-1]["id"]
        return rows

    def events(self) -> list[dict[str, Any]]:
        try:
            if self.last_event is None:
                rows = (source.api_get(self.r, "/api/log", {"limit": 20}, timeout=5) or {}).get(
                    "entries"
                ) or []
            else:
                rows = (
                    source.api_get(self.r, "/api/log", {"after": self.last_event}, timeout=5) or {}
                ).get("entries") or []
        except source.RemoteError:
            return []
        if rows:
            self.last_event = rows[-1]["id"]
        elif self.last_event is None:
            self.last_event = 0
        return rows

    def close(self) -> None:
        pass


def _numbers(st: dict[str, Any]) -> str:
    ag = st.get("agent") or {}
    weights = ", ".join(
        f"{s['name']} {s['weight']}" for s in st.get("strategies", []) if not s.get("watch")
    )
    lines = [
        f"Equity     {tmn(st['equity_tmn']):>14} TMN    PnL vs 5M  {pct(st['pnl_pct_vs_start'], 3)}",
        f"Drawdown   {pct(st['drawdown_pct'], 3, signed=False):>14}        Exposure   {tmn(st['exposure_tmn'])} TMN",
        f"Positions  {', '.join(f'{k} {tmn(v)} TMN' for k, v in st['positions'].items()) or 'none'}",
        f"Weights    {weights}",
        f"Phase      {st['phase']} · hard stop {'ACTIVE' if st['hard_stopped'] else 'off'} · "
        f"soft stop {'active' if st['soft_stop_active'] else 'off'} · open orders {st.get('open_orders')}",
        f"Agent      {ag.get('stance')} ({ag.get('source')}, {ag.get('age_min')} min ago) · "
        f"risk × {ag.get('effective_risk_multiplier')}",
        f"Rationale  {(ag.get('rationale') or '')[:160]}",
    ]
    if st.get("flat_by_design"):
        lines.append("flat by design: no strategy has proven edge yet")
    feed = st.get("feed") or {}
    lines.append(
        "Books      "
        + "  ".join(f"{k} {v['spread_bps']}bps{'!' if v['issue'] else ''}" for k, v in feed.items())
    )
    return "\n".join(lines)


class WatchApp(App):
    CSS = """
    Screen { layout: vertical; }
    #banner { height: 3; content-align: center middle; text-style: bold; }
    #numbers { height: auto; padding: 1 2; border: round $accent; }
    #log { height: 1fr; border: round $secondary; }
    #bar { height: 3; }
    .shadow #banner { background: #b58900; color: black; }
    .trade #banner { background: #b00020; color: white; }
    .unknown #banner { background: #444444; color: white; }
    """
    BINDINGS = [("q", "quit", "Quit view (engine keeps running)")]

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        r = source.remote(settings)
        self.feed: LocalFeed | RemoteFeed = RemoteFeed(r) if r else LocalFeed(settings)
        self.interval = REMOTE_REFRESH_S if r else REFRESH_S

    def compose(self) -> ComposeResult:
        yield Static("connecting…", id="banner")
        with Vertical():
            yield Static("", id="numbers")
            yield RichLog(id="log", wrap=True, markup=True)
        with Horizontal(id="bar"):
            yield Button("Quit view", id="quit", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_view()
        self.set_interval(self.interval, self.refresh_view)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit":
            self.exit()

    def refresh_view(self) -> None:
        st, live = self.feed.status()
        banner = self.query_one("#banner", Static)
        where = "" if self.feed.origin == "LOCAL" else f" · REMOTE {self.feed.origin}"
        if st is None:
            self.screen.set_classes("unknown")
            banner.update(
                f"engine not reachable{where} · start one with `wallexer shadowtest`"
                if where
                else "engine not running · start with `wallexer shadowtest`"
            )
            return
        if st.get("starting"):
            self.screen.set_classes("trade" if st.get("mode") == "trade" else "shadow")
            banner.update(f"engine starting (preflight, history sync)…{where}")
            return
        mode = st.get("mode")
        self.screen.set_classes("trade" if mode == "trade" else "shadow")
        tag = "LIVE TRADE (real orders)" if mode == "trade" else "SHADOW (simulated orders)"
        banner.update(
            f"{tag} · run {st.get('run_id')} · uptime {duration(st.get('uptime_s'))}{where}"
            + ("" if live else " · stale (engine down)")
        )
        self.query_one("#numbers", Static).update(_numbers(st))
        log = self.query_one("#log", RichLog)
        for r in self.feed.decisions():
            log.write(
                f"[bold cyan]{r['ts'][11:19]} decision #{r['id']}[/] {r['action']} {r['symbol'] or ''} · "
                f"{(r['reason'] or '')[:140]}"
            )
        for r in self.feed.events():
            kind = r["kind"]
            color = (
                "red"
                if kind.startswith("risk.")
                else "yellow"
                if kind.startswith("feed.")
                else "dim"
            )
            log.write(f"[{color}]{r['ts'][11:19]} {kind}[/] {(r['payload'] or '')[:140]}")

    def on_unmount(self) -> None:
        self.feed.close()


def run_watch(settings: Settings) -> None:
    WatchApp(settings).run()
