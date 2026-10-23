"""Full-screen app: `wallexer` with no arguments (Textual, alternate screen; the terminal comes back
as it was on exit).

- Header (2 rows, fits 80 columns): LOCAL/REMOTE, DEMO / SHADOW / LIVE badges, equity, PnL, drawdown,
  agent stance and countdowns to entries-stop / flatten / end. Updated in place; a changed number
  pulses briefly.
- Output: every command runs through the same `run_args` as the shell, in a worker thread, with the
  app's console swapped in: Rich tables and panels render here, `console.input()` prompts (trade
  confirmation, flatten y/N, setup) use the command box, `console.status()` drives the spinner.
- `watch` is a split view in the same app; `sum` / `exp-reason` stream the LLM answer (cli/stream.py).
- Keys: Enter runs, Up/Down history, Tab completes, Ctrl-L clears, F1 help, q (empty line) or Ctrl-D
  quits. Engines run detached, so leaving the app never touches one.
Falls back to the line shell (cli/repl.py) when there is no real terminal.
"""

from __future__ import annotations

import io
import os
import queue
import shlex
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.suggester import SuggestFromList
from textual.widgets import Input, RichLog, Static

from wallexer.cli.theme import PALETTE, THEME, duration, pct, tmn

LOGO = """\
╻ ╻┏━┓╻  ╻  ┏━╸╻ ╻┏━╸┏━┓
┃╻┃┣━┫┃  ┃  ┣╸ ┏╋┛┣╸ ┣┳┛
┗┻┛╹ ╹┗━╸┗━╸┗━╸╹ ╹┗━╸╹┗╸"""
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
LOCAL_REFRESH_S, REMOTE_REFRESH_S = 3.0, 5.0
P = PALETTE


def can_run_app() -> bool:
    """A real terminal that can switch to the alternate screen; else the line shell."""
    if os.environ.get("WALLEXER_PLAIN") == "1":
        return False
    if os.environ.get("TERM", "") in ("", "dumb"):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_app(notice: str | None = None) -> None:
    try:
        WallexerApp(notice).run()
    except Exception as e:  # e.g. a terminal Textual cannot drive: degrade, never crash
        from wallexer.cli.app import app as typer_app
        from wallexer.cli.app import console
        from wallexer.cli.repl import run_repl

        console.print(f"[warn]full-screen app unavailable ({type(e).__name__}): line shell[/]")
        if notice:
            console.print(f"[warn]{notice}[/]")
        run_repl(typer_app, console)


# ---------------------------------------------------------------------------------- helpers


def _until(s: str | None, now: float) -> str:
    """'2026-10-23 18:50 UTC' → '7h12m' (or 'passed')."""
    if not s:
        return "–"
    try:
        ts = datetime.strptime(s, "%Y-%m-%d %H:%M UTC").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return "–"
    left = int(ts - now)
    if left <= 0:
        return "passed"
    h, rem = divmod(left, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{sec:02d}s"


def _history_file() -> Any:
    from wallexer.config import data_home

    p = data_home() / "data" / ".repl_history"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_history() -> list[str]:
    """Same file and format as the line shell (prompt_toolkit FileHistory: '+line')."""
    try:
        lines = _history_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line[1:] for line in lines if line.startswith("+")][-500:]


def _save_history(line: str) -> None:
    try:
        with _history_file().open("a", encoding="utf-8") as f:
            f.write(f"\n# {datetime.now():%Y-%m-%d %H:%M:%S}\n+{line}\n")
    except OSError:
        pass


class _Lines(io.TextIOBase):
    """File for the app console: complete lines go to the output log in one batch per write."""

    def __init__(self, emit: Any) -> None:
        self.buf = ""
        self.emit = emit

    def write(self, s: str) -> int:
        self.buf += s
        if "\n" in self.buf:
            *done, self.buf = self.buf.split("\n")
            self.emit(done)
        return len(s)

    def flush(self) -> None:
        if self.buf:
            line, self.buf = self.buf, ""
            self.emit([line])


class AppConsole(Console):
    """Rich console that renders into the app (colors kept, no cursor tricks, no pager)."""

    def __init__(self, ui: WallexerApp, width: int) -> None:
        super().__init__(
            file=_Lines(ui.emit_lines),
            theme=THEME,
            width=width,
            force_terminal=True,
            force_interactive=False,
            color_system="truecolor",
        )
        self._ui = ui

    def input(self, prompt: Any = "", *, password: bool = False, **_kw: Any) -> str:
        return self._ui.ask(str(prompt), password)

    def status(self, status: Any, **_kw: Any) -> Any:  # type: ignore[override]
        return self._ui.busy_label(str(status))


# ---------------------------------------------------------------------------------- screens


class Splash(Screen[None]):
    DEFAULT_CSS = f"""
    Splash {{ align: center middle; background: {P["bg"]}; }}
    #logo {{ width: 100%; text-align: center; color: {P["demo"]}; text-style: bold; }}
    #tag {{ width: 100%; text-align: center; color: {P["muted"]}; margin-top: 1; }}
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(LOGO, id="logo")
            yield Static("spot-trading engine for Wallex · shadow-tested, risk-gated", id="tag")

    def on_mount(self) -> None:
        self.query_one("#box").styles.width = "auto"
        self.query_one("#box").styles.height = "auto"
        self.set_timer(0.55, lambda: self.styles.animate("opacity", 0.0, duration=0.3))
        self.set_timer(0.9, self._done)

    def _done(self) -> None:
        if self.is_current:
            self.dismiss()

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self._done()


class CmdInput(Input):
    """The command line. `q` on an empty line quits (no command starts with q)."""

    async def _on_key(self, event: events.Key) -> None:
        ui: WallexerApp = self.app  # type: ignore[assignment]
        if event.key == "q" and not self.value and ui.prompt_q is None:
            event.stop()
            event.prevent_default()
            ui.exit()
            return
        await super()._on_key(event)


# ---------------------------------------------------------------------------------- the app


class WallexerApp(App[None]):
    TITLE = "wallexer"
    CSS = f"""
    Screen {{ background: {P["bg"]}; color: {P["fg"]}; layout: vertical; }}
    #head {{ height: 2; background: {P["panel"]}; padding: 0 1; }}
    .row {{ height: 1; overflow: hidden hidden; }}
    .row Static {{ width: auto; margin-right: 2; }}
    #where {{ color: {P["head"]}; text-style: bold; }}
    .badge {{ padding: 0 1; text-style: bold; display: none; }}
    #b-demo {{ background: {P["demo"]}; color: white; }}
    #b-mode.shadow {{ background: {P["shadow"]}; color: black; display: block; }}
    #b-mode.trade {{ background: {P["live"]}; color: white; display: block; }}
    #b-demo.on {{ display: block; }}
    .dim {{ color: {P["muted"]}; }}
    #watch {{ display: none; height: 1fr; min-height: 7; border: round {P["shadow"]}; }}
    #watch.on {{ display: block; height: 3fr; }}
    #watch.trade {{ border: round {P["live"]}; }}
    #watch.demo {{ border: round {P["demo"]}; }}
    #wnum {{ height: auto; max-height: 70%; padding: 0 1; overflow: hidden hidden; }}
    #wlog {{ height: 1fr; min-height: 2; background: {P["bg"]}; overflow-x: hidden; }}
    #out {{ height: 2fr; min-height: 4; background: {P["bg"]}; scrollbar-size-vertical: 1;
            overflow-x: hidden; }}
    #stream {{ display: none; height: auto; max-height: 50%; border: round {P["head"]};
               padding: 0 1; }}
    #stream.on {{ display: block; }}
    #cmd {{ height: 3; border: tall {P["panel"]}; background: {P["panel"]}; }}
    #cmd:focus {{ border: tall {P["demo"]}; }}
    #hint {{ height: 1; color: {P["muted"]}; padding: 0 1; }}
    #keys {{ height: 1; color: {P["muted"]}; background: {P["panel"]}; padding: 0 1; }}
    """
    BINDINGS = [
        Binding("ctrl+d", "quit", "Quit", priority=True),
        Binding("ctrl+l", "clear", "Clear", priority=True),
        Binding("f1", "help", "Help", priority=True),
        Binding("tab", "complete", "Complete", priority=True, show=False),
        Binding("up", "history(-1)", show=False, priority=True),
        Binding("down", "history(1)", show=False, priority=True),
        Binding("escape", "close_watch", show=False, priority=True),
        Binding("ctrl+c", "cancel_line", show=False, priority=True),
    ]

    def __init__(self, notice: str | None = None) -> None:
        super().__init__()
        from wallexer.cli import help as help_mod

        self.notice = notice
        self.settings: Any = None
        self.remote: Any = None
        self.status_err: str | None = None
        self.schedule: dict[str, Any] | None = None
        self.history = _load_history()
        self.hpos = len(self.history)
        self.busy = threading.Lock()
        self.prompt_q: queue.Queue[str] | None = None
        self.spin_label: str | None = None
        self.spin_t0 = 0.0
        self.spin_i = 0
        self.last: dict[str, str] = {}
        self.feed: Any = None
        self.stream_text: str | None = None
        self._print_buf = ""
        self.usage = {
            cmd.split()[0]: (cmd, desc, ex)
            for rows in help_mod.GROUPS.values()
            for cmd, desc, ex in rows
        }
        self.words = sorted(
            {*self.usage, "watch", "clear", "help", "quit", "rslt backtest", "rslt shadowtest"}
        )

    # -------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        with Vertical(id="head"):
            with Horizontal(classes="row"):
                yield Static("", id="where")
                yield Static("DEMO", id="b-demo", classes="badge")
                yield Static("", id="b-mode", classes="badge")
                yield Static("", id="equity")
                yield Static("", id="pnl")
                yield Static("", id="dd")
            with Horizontal(classes="row"):
                yield Static("", id="agent")
                yield Static("", id="countdown", classes="dim")
        with Vertical(id="watch"):
            yield Static("", id="wnum")
            yield RichLog(id="wlog", wrap=True, markup=True, max_lines=400)
        yield RichLog(id="out", wrap=True, markup=False, max_lines=5000)
        yield Static("", id="stream")
        yield CmdInput(
            placeholder="type a command · Tab completes · F1 help",
            suggester=SuggestFromList(self.words, case_sensitive=False),
            id="cmd",
        )
        yield Static("", id="hint")
        yield Static(
            "Enter run · ↑↓ history · Tab complete · ^L clear · F1 help · q/^D quit",
            id="keys",
        )

    def on_mount(self) -> None:
        from wallexer.cli import stream
        from wallexer.cli.app import WATCH_HOOK

        WATCH_HOOK["fn"] = lambda: self.call_from_thread(self.toggle_watch, True)
        stream.SINK["fn"] = self._stream_sink
        # click.echo (usage errors, --help) writes to stdout: show it in the output too.
        self.begin_capture_print(self, stdout=True, stderr=True)
        self._reload()
        out = self.query_one("#out", RichLog)
        if self.notice:
            out.write(Text(self.notice, style=P["warn"]))
        if self.remote:
            msg = (
                "Watching the public demo (read-only). Try: status · watch · sum · reason · rslt shadowtest"
                if self.remote.demo
                else f"Remote view of {self.remote.url} (read-only)."
            )
            out.write(Text(msg, style=P["muted"]))
        out.write(
            Text("help lists every command · `wallexer setup` runs your own engine", P["muted"])
        )
        self.query_one("#cmd", Input).focus()
        self.poll_status()
        self.set_interval(REMOTE_REFRESH_S if self.remote else LOCAL_REFRESH_S, self.poll_status)
        self.set_interval(1.0, self._tick_countdown)
        self.set_interval(1 / 12, self._spin)
        self.set_interval(0.1, self._flush_stream)
        if os.environ.get("WALLEXER_NO_SPLASH") != "1":
            self.push_screen(Splash())

    def on_unmount(self) -> None:
        from wallexer.cli import stream
        from wallexer.cli.app import WATCH_HOOK

        WATCH_HOOK["fn"] = None
        stream.SINK["fn"] = None

    def on_print(self, event: events.Print) -> None:
        self._print_buf += event.text
        if "\n" in self._print_buf:
            *done, self._print_buf = self._print_buf.split("\n")
            self._write([Text.from_ansi(line) for line in done])

    def _reload(self) -> None:
        from wallexer.cli import source
        from wallexer.config import ConfigError, load_settings

        try:
            self.settings = load_settings()
            self.remote = source.remote(self.settings)
        except ConfigError as e:
            self.settings, self.remote, self.status_err = None, None, str(e)

    # -------------------------------------------------------------- header

    def poll_status(self) -> None:
        self.run_worker(self._fetch_status, thread=True, exclusive=True, group="status")

    def _fetch_status(self) -> None:
        from wallexer.cli import source
        from wallexer.config import ConfigError

        if self.settings is None:
            self.call_from_thread(self._show_status, None, False, self.status_err)
            return
        try:
            st, live, _o = source.status(self.settings)
            err = None
        except (source.RemoteError, ConfigError) as e:
            st, live, err = None, False, str(e)
        self.call_from_thread(self._show_status, st, live, err)

    def _set(self, wid: str, text: str | Text, pulse: bool = True) -> None:
        key = text.plain if isinstance(text, Text) else text
        if self.last.get(wid) == key:
            return
        first = wid not in self.last
        self.last[wid] = key
        w = self.query_one(f"#{wid}", Static)
        w.update(text)
        if pulse and not first:
            w.styles.background = Color.parse(P["head"]).with_alpha(0.45)
            w.styles.animate("background", Color.parse(P["head"]).with_alpha(0.0), duration=1.2)

    def _show_status(self, st: dict[str, Any] | None, live: bool, err: str | None) -> None:
        from wallexer.cli.source import host

        r = self.remote
        where = "REMOTE demo" if r and r.demo else f"REMOTE {host(r.url)}" if r else "LOCAL"
        self._set("where", where, pulse=False)
        st = st or {}
        demo = self.query_one("#b-demo")
        demo.set_class(bool(st.get("demo")), "on")
        mode = st.get("mode") if (live or r) and st else None
        b = self.query_one("#b-mode", Static)
        b.set_classes(f"badge {mode or ''}")
        b.update("LIVE" if mode == "trade" else "SHADOW" if mode == "shadow" else "")
        if err:
            self._set("equity", Text(f"unreachable: {err}", style=P["err"]), pulse=False)
            for w in ("pnl", "dd", "agent"):
                self._set(w, "", pulse=False)
            self.schedule = None
        elif st.get("starting"):
            self._set("equity", "engine starting…", pulse=False)
        elif not st:
            self._set(
                "equity",
                f"[{P['muted']}]engine not running · `shadowtest` starts one[/]",
                pulse=False,
            )
            for w in ("pnl", "dd", "agent"):
                self._set(w, "", pulse=False)
        else:
            pnl = st.get("pnl_pct_vs_start")
            col = P["err"] if str(pnl).startswith("-") else P["ok"]
            self._set("equity", f"equity {tmn(st.get('equity_tmn'))}")
            self._set("pnl", f"PnL [{col}]{pct(pnl, 3)}[/]")
            self._set("dd", f"DD {pct(st.get('drawdown_pct'), 2, signed=False)}")
            ag = st.get("agent") or {}
            self._set(
                "agent",
                f"agent {ag.get('stance') or '–'} ×{ag.get('effective_risk_multiplier', '–')}",
            )
        # Remote: the schedule comes from the server only (never the local EVENT_END_UTC).
        if not err:
            self.schedule = st.get("schedule") or (None if r else self._local_schedule())
        self._tick_countdown()
        if self.query_one("#watch").has_class("on"):
            self._refresh_watch(st)

    def _local_schedule(self) -> dict[str, Any] | None:
        from wallexer.risk.schedule import times

        if self.settings is None:
            return None
        g, end = self.settings.cfg.guardrails, self.settings.secrets.event_end_utc
        return times(
            end.timestamp() if end else None, g.flatten_no_entry_min * 60, g.flatten_force_min * 60
        )

    def _tick_countdown(self) -> None:
        sch, now = self.schedule, time.time()
        if not sch or not sch.get("end"):
            has_keys = self.settings is not None and self.settings.has_private
            text = "schedule –" if self.remote or not has_keys else "EVENT_END_UTC not set"
        else:
            text = (
                f"entries stop {_until(sch.get('entries_stop'), now)} · "
                f"flatten {_until(sch.get('flatten_from'), now)} · end {_until(sch.get('end'), now)}"
            )
        self._set("countdown", text, pulse=False)

    # -------------------------------------------------------------- watch split

    def toggle_watch(self, on: bool | None = None) -> None:
        from wallexer.cli import watch as watch_mod

        pane = self.query_one("#watch")
        on = not pane.has_class("on") if on is None else on
        pane.set_class(on, "on")
        if on:
            if self.feed is None and self.settings is not None:
                r = self.remote
                self.feed = watch_mod.RemoteFeed(r) if r else watch_mod.LocalFeed(self.settings)
            self.poll_status()
        elif self.feed is not None:
            self.feed.close()
            self.feed = None

    def action_close_watch(self) -> None:
        self.toggle_watch(False)

    def _refresh_watch(self, st: dict[str, Any]) -> None:
        if not st or st.get("starting") or self.feed is None:
            self._set("wnum", "waiting for the engine…", pulse=False)
            return
        from wallexer.cli.watch import _numbers

        pane = self.query_one("#watch")
        pane.set_class(st.get("mode") == "trade", "trade")
        pane.set_class(bool(st.get("demo")), "demo")
        pane.border_title = (
            f"{'DEMO · ' if st.get('demo') else ''}"
            f"{'LIVE' if st.get('mode') == 'trade' else 'SHADOW'} · up {duration(st.get('uptime_s'))}"
            " · Esc closes"
        )
        self._set("wnum", Text(_numbers(st)))
        self.run_worker(self._fetch_watch_rows, thread=True, exclusive=True, group="watch")

    def _fetch_watch_rows(self) -> None:
        feed = self.feed
        if feed is None:
            return
        try:
            rows = [("d", r) for r in feed.decisions()] + [("e", r) for r in feed.events()]
        except Exception:  # a hiccup in polling must not break the view
            return
        self.call_from_thread(self._append_watch, rows)

    def _append_watch(self, rows: list[tuple[str, dict[str, Any]]]) -> None:
        log = self.query_one("#wlog", RichLog)
        for kind, r in rows:
            if kind == "d":
                log.write(
                    f"[bold {P['head']}]{r['ts'][11:19]} decision #{r['id']}[/] {r['action']} "
                    f"{r['symbol'] or ''} · {(r['reason'] or '')[:140]}"
                )
            else:
                k = r["kind"]
                col = (
                    P["err"]
                    if k.startswith("risk.")
                    else P["warn"]
                    if k.startswith("feed.")
                    else P["muted"]
                )
                log.write(f"[{col}]{r['ts'][11:19]} {k}[/] {(r['payload'] or '')[:140]}")

    # -------------------------------------------------------------- output, spinner, stream

    def emit_lines(self, lines: list[str]) -> None:
        """Called from the command thread (or the app thread for captured print)."""
        texts = [Text.from_ansi(line) for line in lines]
        if threading.get_ident() == self._thread_id:
            self._write(texts)
        else:
            self.call_from_thread(self._write, texts)

    def _write(self, texts: list[Text]) -> None:
        out = self.query_one("#out", RichLog)
        for t in texts:
            out.write(t)

    @contextmanager
    def busy_label(self, label: str) -> Iterator[None]:
        prev = self.spin_label
        self.call_from_thread(setattr, self, "spin_label", label)
        try:
            yield
        finally:
            self.call_from_thread(setattr, self, "spin_label", prev)

    def _spin(self) -> None:
        hint = self.query_one("#hint", Static)
        if self.spin_label is None:
            return
        self.spin_i = (self.spin_i + 1) % len(SPINNER)
        hint.update(
            f"[{P['head']}]{SPINNER[self.spin_i]}[/] {self.spin_label} "
            f"[{P['muted']}]{time.monotonic() - self.spin_t0:.1f}s[/]"
        )

    def _stream_sink(self, event: str, payload: Any) -> None:
        if event == "start":
            self.call_from_thread(self._stream_start, str(payload))
        elif event == "text":
            self.stream_text = payload  # rendered by _flush_stream at 10 Hz (no flicker)
        else:
            self.call_from_thread(self._stream_end)

    def _stream_start(self, title: str) -> None:
        w = self.query_one("#stream", Static)
        w.border_title = f"{title} · streaming"
        w.update(Text("waiting for the first tokens…", style=P["muted"]))
        w.add_class("on")

    def _flush_stream(self) -> None:
        text, self.stream_text = self.stream_text, None
        if text is not None:
            self.query_one("#stream", Static).update(text)

    def _stream_end(self) -> None:
        self.stream_text = None
        self.query_one("#stream", Static).remove_class("on")

    # -------------------------------------------------------------- prompts from commands

    def ask(self, prompt: str, password: bool) -> str:
        """Blocks the command thread until the user answers in the command box."""
        q: queue.Queue[str] = queue.Queue()
        self.call_from_thread(self._begin_prompt, prompt, password, q)
        return q.get()

    def _begin_prompt(self, prompt: str, password: bool, q: queue.Queue[str]) -> None:
        self.prompt_q = q
        self.emit_lines([prompt.rstrip()])
        inp = self.query_one("#cmd", Input)
        inp.password = password
        inp.value = ""
        inp.placeholder = Text.from_markup(prompt).plain.strip() or "answer"
        inp.suggester = None
        self.query_one("#hint", Static).update(
            f"[{P['warn']}]answer the prompt above · Enter sends[/]"
        )
        inp.focus()

    def _end_prompt(self, answer: str) -> None:
        inp = self.query_one("#cmd", Input)
        shown = "•" * len(answer) if inp.password else answer
        self._write([Text(f"  › {shown}", style=P["muted"])])
        inp.password = False
        inp.placeholder = "type a command · Tab completes · F1 help"
        inp.suggester = SuggestFromList(self.words, case_sensitive=False)
        q, self.prompt_q = self.prompt_q, None
        if q is not None:
            q.put(answer)

    # -------------------------------------------------------------- input handling

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.prompt_q is not None or self.spin_label is not None:
            return
        word = event.value.strip().split(" ")[0] if event.value.strip() else ""
        hint = self.query_one("#hint", Static)
        if word in self.usage:
            cmd, desc, ex = self.usage[word]
            hint.update(f"[bold]{cmd}[/] [{P['muted']}]· {desc} · e.g. {ex}[/]")
        elif word:
            near = [w for w in self.words if w.startswith(word)][:6]
            hint.update(f"[{P['muted']}]{' · '.join(near) if near else 'unknown command'}[/]")
        else:
            hint.update("")

    def action_complete(self) -> None:
        inp = self.query_one("#cmd", Input)
        if self.prompt_q is not None:
            return
        v = inp.value
        matches = [w for w in self.words if w.startswith(v.lower())] if v else []
        if len(matches) == 1:
            inp.value = matches[0] + " "
        elif matches:
            common = os.path.commonprefix(matches)
            inp.value = common if len(common) > len(v) else v
            self.query_one("#hint", Static).update(f"[{P['muted']}]{' · '.join(matches[:8])}[/]")
        inp.cursor_position = len(inp.value)

    def action_history(self, step: int) -> None:
        if self.prompt_q is not None or not self.history:
            return
        self.hpos = max(0, min(len(self.history), self.hpos + step))
        inp = self.query_one("#cmd", Input)
        inp.value = self.history[self.hpos] if self.hpos < len(self.history) else ""
        inp.cursor_position = len(inp.value)

    def action_clear(self) -> None:
        self.query_one("#out", RichLog).clear()

    def action_cancel_line(self) -> None:
        if self.prompt_q is None:
            self.query_one("#cmd", Input).value = ""

    def action_help(self) -> None:
        self._submit("help")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        if self.prompt_q is not None:
            self._end_prompt(line)
            return
        self._submit(line)

    def _submit(self, line: str) -> None:
        if not line:
            return
        if self.spin_label is not None:
            self._write([Text("a command is still running…", style=P["warn"])])
            return
        if not self.history or self.history[-1] != line:
            self.history.append(line)
            _save_history(line)
        self.hpos = len(self.history)
        if line in ("exit", "quit", "q"):
            self.exit()
            return
        if line in ("clear", "cls"):
            self.action_clear()
            return
        self._write([Text(""), Text(f"› {line}", style=f"bold {P['head']}")])
        try:
            args = shlex.split(line)
        except ValueError as e:
            self._write([Text(f"error: {e}", style=P["err"])])
            return
        if args[0] == "wallexer":
            args = args[1:] or ["help"]
        if args == ["watch"]:
            self.toggle_watch()
            return
        self.spin_label = f"running `{line}`"
        self.spin_t0 = time.monotonic()
        self.query_one("#cmd", Input).placeholder = "running… (prompts appear here)"
        self.run_worker(lambda: self._run(args), thread=True, group="cmd")

    def _run(self, args: list[str]) -> None:
        from wallexer.cli import app as cli_app

        width = max(60, self.query_one("#out", RichLog).size.width - 2)
        saved = cli_app.console, cli_app.err_console
        cli_app.console = AppConsole(self, width)
        cli_app.err_console = AppConsole(self, width)
        code = 0
        try:
            code = cli_app.run_args(args)
        except SystemExit as e:
            code = int(e.code or 0) if isinstance(e.code, int | None) else 1
        except Exception as e:  # never let a command take the app down
            self.emit_lines([f"\x1b[31merror:\x1b[0m {type(e).__name__}: {e}"])
            code = 1
        finally:
            for c in (cli_app.console, cli_app.err_console):
                c.file.flush()
            cli_app.console, cli_app.err_console = saved
        self.call_from_thread(self._done, code)

    def _done(self, code: int) -> None:
        self.spin_label = None
        self.query_one("#hint", Static).update("" if code == 0 else f"[{P['muted']}]exit {code}[/]")
        self.query_one("#cmd", Input).placeholder = "type a command · Tab completes · F1 help"
        self._reload()  # e.g. after `setup`
        self.poll_status()
