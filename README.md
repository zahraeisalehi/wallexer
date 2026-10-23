# wallexer

```sh
curl -LsSf https://raw.githubusercontent.com/zahraeisalehi/wallexer/main/install.sh | sh
```

An automated spot-trading engine and CLI for [Wallex](https://wallex.ir), built for the StarCoach
hackathon (5,000,000 TMN trading credit, about two days). Strategies are walk-forward tested,
shadow-traded and promoted only on out-of-sample evidence. An LLM agent (BoxAPI) may only *reduce*
risk, and a final risk gate can only shrink or reject orders. Until a strategy proves an edge after
fees, the engine stays flat by design.

## 30-second tour

```sh
wallexer                 # opens the shell; the first run offers the setup wizard
wallexer markets         # allowed markets, spreads, fees, round-trip cost (no keys needed)
wallexer help            # every command, grouped, with an example each
wallexer setup           # local (Wallex + BoxAPI keys) or remote (server URL + view token)
wallexer status          # mode, equity, PnL, drawdown, schedule, per-market state
wallexer sum yesterday   # daily market + agent summary: facts, then an LLM explanation
```

## Using the CLI

`wallexer` with no arguments opens an interactive shell. It shows a banner with LOCAL or REMOTE,
the engine mode, equity and the event schedule, and every command works there without the prefix.
Tab completes, history is kept, and a typo gets a suggestion. Ctrl-C clears the line and never
touches a running engine, because engines run detached.

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
`wallexer setup --mode remote` (server URL plus view token), or add `--remote` to a command.
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
