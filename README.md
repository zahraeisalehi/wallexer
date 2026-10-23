# wallexer

```sh
curl -LsSf https://raw.githubusercontent.com/zahraeisalehi/wallexer/main/install.sh | sh
```

An automated spot-trading engine and CLI for [Wallex](https://wallex.ir), built for the StarCoach
hackathon (5,000,000 TMN trading credit, about two days). Strategies are walk-forward tested,
shadow-traded and promoted only on out-of-sample evidence. An LLM agent (BoxAPI) may only *reduce*
risk, and a final risk gate can only shrink or reject orders. Until a strategy proves an edge after
fees, the engine stays flat by design.

Then `Run: wallexer`. No questions, no keys: it opens straight into the **public demo**
(https://wallexer-demo.liara.run, a shadow-only engine that can never place an order), read-only.

## 30-second tour

Inside the app (or as `wallexer <command>` from your shell):

```sh
status                   # mode, equity, PnL, drawdown, schedule, per-market state
watch                    # live split view: numbers update in place, decisions + events stream in
sum                      # today's market + agent summary: facts, then the LLM explanation
reason 12                # one decision in full: features, signals, weights, risk checks, orders
exp-reason               # the LLM's plain-language explanation of the latest decision
trade                    # refused on the demo: `wallexer setup` runs your own engine
```

## Using the CLI

`wallexer` with no arguments opens a full-screen app (alternate screen; your terminal comes back
as it was when you leave). The header shows LOCAL or REMOTE, DEMO / SHADOW / LIVE badges, equity,
PnL, drawdown, the agent's stance and countdowns to entries-stop, flatten and the end; numbers
update in place and pulse when they change. Commands render below; long ones show a spinner, `watch`
opens a split view, and `sum` / `exp-reason` stream the LLM answer as it arrives (with a local
BoxAPI key; against the demo they show the server's pre-generated answer).
Keys: Enter runs, ↑/↓ history, Tab completes, Ctrl-L clears, F1 help, `q` (empty line) or Ctrl-D
quits. Engines run detached, so quitting never touches one. Without a real terminal (pipes,
`TERM=dumb`, or `WALLEXER_PLAIN=1`) the line shell opens instead. `wallexer <command>` from the
shell prints plain, scriptable output.

| Group | Commands |
|---|---|
| Trading | `shadowtest [MARKET…]`, `trade [MARKET…]`, `stop [MARKET…]`, `flatten [MARKET…]`, `change FROM TO`, `resume` |
| Testing | `backtest [--from --to --markets --timeframe]`, `doctor`, `history sync`, `research spread\|basis` |
| Results | `status`, `watch`, `rslt backtest\|shadowtest`, `sum [DATE]`, `log [N]`, `export` |
| Explain | `reason [N]`, `exp-reason [N]`, `agent now\|replay ID` |
| Guardrails | `config guardrail`, `autoset guardrail` |
| System | `markets`, `setup`, `health`, `engine run`, `help [COMMAND]` |

- `shadowtest` and `trade` start the engine and open the live view (`--no-watch` to skip it). The
  view's numbers update in place and the log appends. Shadow shows a yellow SHADOW banner, live a
  red LIVE banner. `q` or the Quit button closes the view only; the engine keeps running.
- Before any live start (`trade`, `change … trade`, or `trade MARKET` on a running engine), the
  CLI shows the latest backtest and shadow results. It then asks *"Run/review backtest and
  shadowtest first? [Y/n]"* and requires you to type `TRADE`. Both answers are logged.
- `stop MARKET` and `trade MARKET` (or `shadowtest MARKET`) stop and restart single markets on a
  running engine. `status` shows each market's state.
- `backtest` with no options uses the config: that is the run the promotion gate uses. With
  `--from/--to/--markets/--timeframe` it is a custom run, shown by `rslt backtest` and never used
  for promotion.
- `sum [DATE]` takes `today`, `yesterday` or `YYYY-MM-DD` (UTC, with Tehran time shown). Part 1
  is static facts. Part 2 is an LLM explanation that may use only numbers from the input and
  cites decision ids. A past day is cached; `--refresh` regenerates it.

### Remote viewing

The engine on the server serves a token-protected, GET-only API next to `/health`. Run
`wallexer setup --mode remote` (server URL plus view token, checked against the server before it
is saved), or add `--remote` to a command. A fresh install with no `wallexer setup` watches the
public demo, whose API needs no token (`WALLEXER_VIEW_PUBLIC=1`, allowed only on a demo server);
if it is unreachable, `wallexer` says so in one line and runs local public commands
(`markets`, `help`, `setup`).
`status`, `watch`, `log`, `reason`, `rslt` and `sum` then read the server. Commands that change
state are disabled in remote mode. Server setup is in `docs/RUNBOOK.md`.

### Where things live

- **Installed** (`install.sh`): settings and `credentials.env` (mode 600) are in the user config
  dir (`~/.config/wallexer` on Linux, `~/Library/Application Support/wallexer` on macOS). The DB,
  logs and reports are in the user data dir. `WALLEXER_HOME` overrides both.
- **Development** (a checkout): `config/default.toml`, secrets in `./.env` (see `.env.example`),
  data in `./data`. New optional variables: `WALLEXER_VIEW_TOKEN` (the server token, or the token
  sent in remote mode) and `WALLEXER_REMOTE_URL`.

```sh
uv sync && uv run wallexer --help     # development
uv run pytest -q                      # critical tests
```

Hard rules (see `CLAUDE.md`, `docs/rules.md`):
- no withdrawal, IBAN, card or wallet endpoints;
- markets in `data/not_allowed.txt` are filtered twice;
- every order references a decision;
- money math uses `Decimal`.
