"""`wallexer watch`: live Textual view of the engine (read-only).

- Numbers panel (equity, PnL, drawdown, exposure, positions, weights, agent stance) is updated in
  place every 2 s, never printed twice.
- Log panel appends new decisions and events as they happen.
- Shadow and trade use clearly different themes (yellow "SHADOW" banner vs red "LIVE TRADE").
- The Quit button and `q` close the view only; the engine keeps running (use `wallexer stop`).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, RichLog, Static

from wallexer.cli.engine_cmds import get_status
from wallexer.config import Settings
from wallexer.store.db import connect

REFRESH_S = 2.0


def _numbers(st: dict[str, Any]) -> str:
    ag = st.get("agent") or {}
    weights = ", ".join(
        f"{s['name']} {s['weight']}" for s in st.get("strategies", []) if not s.get("watch")
    )
    lines = [
        f"Equity        {int(st['equity_tmn']):>14,} TMN    PnL vs 5M  {st['pnl_pct_vs_start']}%",
        f"Drawdown      {st['drawdown_pct']:>14}%       Exposure   {int(st['exposure_tmn']):,} TMN",
        f"Positions     {', '.join(f'{k} {v}' for k, v in st['positions'].items()) or 'none'}",
        f"Weights       {weights}",
        f"Phase         {st['phase']}  · hard stop {st['hard_stopped']} · soft stop {st['soft_stop_active']}",
        f"Agent         {ag.get('stance')} ({ag.get('source')}, {ag.get('age_min')} min ago) · "
        f"risk × {ag.get('effective_risk_multiplier')}",
        f"Rationale     {(ag.get('rationale') or '')[:160]}",
    ]
    if st.get("flat_by_design"):
        lines.append("flat by design: no strategy has proven edge yet")
    feed = st.get("feed") or {}
    lines.append(
        "Books         "
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
        self.conn: sqlite3.Connection = connect(settings.cfg.path(settings.cfg.store.db_path))
        self.last_decision = self.conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM decisions"
        ).fetchone()[0]
        self.last_event = max(
            0, self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0] - 20
        )

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
        self.set_interval(REFRESH_S, self.refresh_view)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit":
            self.exit()

    def refresh_view(self) -> None:
        st, live = get_status(self.settings)
        banner = self.query_one("#banner", Static)
        if st is None:
            self.screen.set_classes("unknown")
            banner.update("engine not running · start with `wallexer shadowtest`")
            return
        mode = st.get("mode")
        self.screen.set_classes("trade" if mode == "trade" else "shadow")
        tag = "LIVE TRADE (real orders)" if mode == "trade" else "SHADOW (simulated orders)"
        banner.update(
            f"{tag} · run {st.get('run_id')} · uptime {st.get('uptime_s')}s"
            + ("" if live else " · stale (engine down)")
        )
        self.query_one("#numbers", Static).update(_numbers(st))
        log = self.query_one("#log", RichLog)
        for r in self.conn.execute(
            "SELECT id, ts, action, symbol, reason FROM decisions WHERE id > ? ORDER BY id",
            (self.last_decision,),
        ):
            self.last_decision = r[0]
            log.write(
                f"[bold cyan]{r[1][11:19]} decision #{r[0]}[/] {r[2]} {r[3] or ''} · {(r[4] or '')[:140]}"
            )
        for r in self.conn.execute(
            "SELECT id, ts, kind, payload FROM events WHERE id > ? ORDER BY id", (self.last_event,)
        ):
            self.last_event = r[0]
            color = "red" if r[2].startswith(("risk.", "feed.")) else "dim"
            log.write(f"[{color}]{r[1][11:19]} {r[2]}[/] {r[3][:140]}")

    def on_unmount(self) -> None:
        self.conn.close()


def run_watch(settings: Settings) -> None:
    WatchApp(settings).run()
