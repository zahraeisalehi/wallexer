# wallexer

Automated spot-trading engine + CLI for the Wallex (wallex.ir) hackathon challenge.
Capital 5M TMN, ~2 days, scored on return, drawdown, stability, explainability, AI usage, reproducibility.

## Read before working
- `docs/rules.md`: hackathon constraints and scoring. Never violate the hard constraints.
- `docs/architecture.md`: the design. Follow it; propose changes in plan mode before deviating.
- Touching exchange code → read `docs/wallex-api.md` (exact schemas: `docs/ref/wallex-spot-trading.json`, `docs/ref/wallex-basic-services.json`).
- Touching CLI code → read `docs/cli-spec.md` (the user's full command and feature requirements; all are mandatory).
- Touching LLM code → read `docs/boxapi.md`.
- Touching deployment → read `docs/liara-deploy.md`.

## Ground rules
- **Do not invent API endpoints, fields, or behaviors.** If something is not in `docs/`, stop and ask, or verify with a read-only call (public endpoint or the `wallex` MCP).
- "Wallex" here means **wallex.ir** (Iranian crypto exchange), NOT wallex.asia (a payments company). Ignore anything you remember about wallex.asia.
- **Never call withdrawal, IBAN, card, or wallet-address endpoints.** The bot only reads market data/balances/fees and places/cancels/queries spot orders.
- **Never place a real order** during development. Live orders only happen in `trade` mode, started by the user.
- Never read, print, or commit `.env` or any API key.
- Markets in `data/not_allowed.txt` must be filtered out at load time and rejected again in the risk gate (defense in depth).
- Money math: `decimal.Decimal` only. Round to each market's precision before sending.
- Strategies are pure and deterministic given features. Randomness only with a seeded RNG from config.
- Every order must reference a `decision_id`. No decision row → no order.
- The risk gate runs last and can only shrink or reject. The LLM can never bypass it.

## Stack
Python 3.12, uv, Typer + Rich (commands), Textual (`watch` dashboard), httpx (async),
pydantic v2 + pydantic-settings, numpy/polars, statsmodels/hmmlearn, SQLite (WAL), structlog (JSON),
openai SDK pointed at boxapi.ir (OpenAI-compatible).

## Layout
```
src/wallexer/
  cli/          Typer app, Textual dashboard
  engine/       daemon, event loop, control server (unix socket), health endpoint
  exchange/     Wallex REST client, symbol rules, rate limiter
  data/         market feed, feature store, candle loader
  strategies/   one file per strategy + base protocol
  alloc/        scoreboard, baseline allocator, regime model
  agent/        LLM client, prompts, schemas, replay
  risk/         guardrails, risk gate, flatten logic
  execution/    executors: backtest, shadow, live
  store/        SQLite schema + repositories
config/default.toml       all tunables (guardrails, intervals, universe)
data/not_allowed.txt      forbidden markets
docs/                     references (see above)
tests/                    only the critical tests (see below)
```

## Commands
```
uv sync                          # install
uv run wallexer --help
uv run wallexer doctor           # connectivity, keys, clock skew
uv run pytest -q                 # critical tests
uv run ruff check --fix && uv run ruff format
```

## Testing policy (time-boxed hackathon)
No broad unit-test suite. Only these must exist and pass before any live run:
1. precision/rounding + min order value for order construction
2. risk gate: limits enforced, not_allowed markets rejected, LLM weights clamped
3. PnL/equity accounting incl. fees (paper book)
4. flatten logic sells every non-TMN balance (incl. USDT) to TMN
Everything else is verified by running `shadowtest` against live data.

## Working style
- Work in small phases. Start each phase in plan mode; list files to create/change.
- After each phase: run the relevant command, report what works, what is stubbed, and what is risky.
- Prefer simple, readable code over clever abstractions. Comments explain *why*.
- When done with a phase, print a short summary I can paste to my mentor: files changed, decisions made, open questions.
