"""Interactive prompt: `wallexer` with no arguments. Every command works without the prefix.

Unknown commands get the closest suggestion; errors print short messages (the commands' own
`friendly` handling); `exit`, `quit` or Ctrl-D leave.
"""

from __future__ import annotations

import difflib
import shlex
from typing import Any

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from rich.console import Console

from wallexer.config import ROOT


def run_repl(app: typer.Typer, console: Console) -> None:
    # Typer vendors click; duck-type the command group instead of importing click.
    group: Any = typer.main.get_command(app)
    names = sorted(group.commands)
    words = names + [
        f"{g} {sub}"
        for g in names
        if hasattr(group.commands[g], "commands")
        for sub in group.commands[g].commands
    ]
    hist = ROOT / "data" / ".repl_history"
    hist.parent.mkdir(parents=True, exist_ok=True)
    session: PromptSession[str] = PromptSession(
        completer=WordCompleter(words, sentence=True), history=FileHistory(str(hist))
    )
    console.print(
        "[bold]wallexer[/] interactive · commands: " + ", ".join(names) + " · `help`, `exit`"
    )
    while True:
        try:
            line = session.prompt("wallexer> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("exit", "quit"):
            break
        try:
            args = shlex.split(line)
        except ValueError as e:
            console.print(f"[red]error:[/] {e}")
            continue
        if args[0] == "help":
            args = ["--help"] if len(args) == 1 else [*args[1:], "--help"]
        elif args[0] not in group.commands:
            close = difflib.get_close_matches(args[0], names, n=1)
            hint = f" Did you mean `{close[0]}`?" if close else ""
            console.print(f"[red]unknown command[/] `{args[0]}`.{hint} Type `help` for the list.")
            continue
        try:
            group.main(args=args, prog_name="wallexer", standalone_mode=False)
        except SystemExit:
            pass
        except Exception as e:  # click usage errors, aborts: short message, no traceback
            if type(e).__name__ in ("Exit",):
                continue
            msg = e.format_message() if hasattr(e, "format_message") else str(e)
            console.print(f"[red]error:[/] {msg or type(e).__name__}")
