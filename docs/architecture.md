# wallexer architecture

## 1. Shape
```
┌────────────── engine daemon (long-running, one process, asyncio) ──────────────┐
│ MarketFeed ──> FeatureStore ──> Strategies (N, all always running on paper)    │
│                      │                 │ signals                                │
│                      ▼                 ▼                                        │
│               RegimeModel ──>   Allocator  <── LLM Agent (every 15–60 min)      │
│                                       │ target weights                          │
│                                       ▼                                         │
│                                  RiskGate (guardrails, hard limits)             │
│                                       ▼                                         │
│                     Executor: Backtest | Shadow | Live  ──> Wallex REST         │
│                                       ▼                                         │
│                     SQLite (WAL): market data, orders, fills, decisions, llm    │
│ ControlServer (unix socket, JSON lines) + /health HTTP (for hosting)            │
└────────────────────────────────────────────────────────────────────────────────┘
          ▲ control                             ▲ read-only
     `wallexer` CLI (Typer + Rich)  /  `wallexer watch` dashboard (Textual)
```

- **Engine and CLI are separate processes.** The engine survives disconnects; the CLI sends commands over the unix socket and reads SQLite for display.
- **Trading uses Wallex REST directly.** The Wallex MCP is used only by the LLM agent as read-only tools (market data), never for orders.
- **Market data by polling.** `GET /depth/all` (every market's book in one call) every 2–5 s + `/v1/trades` for the active universe + `/v1/udf/history` for candles. No WebSocket in v1.
- **One code path, three executors.** Strategies, allocator and risk gate are identical in backtest, shadow and live. Only the Executor differs.

## 2. Strategy selection is dynamic, not a single strategy
Each strategy implements:
```python
class Strategy(Protocol):
    name: str
    def on_tick(self, features: Features) -> list[Signal]: ...   # pure, deterministic
```
`Signal` = market, side, strength in [-1, 1], suggested stop, reason (dict of the numbers that triggered it).

Initial strategy set (each is small; quality over count):
| name | idea | active best in |
|---|---|---|
| `basis_tmn_usdt` | X/TMN vs X/USDT × USDT/TMN mispricing; trade the leg that is off | any regime, needs liquid triangle |
| `mean_reversion` | z-score of price vs rolling VWAP / Bollinger, fade extremes | ranging, low vol |
| `momentum` | breakout of N-bar range with volume confirmation | trending |

**Every strategy always runs on its own paper book** (simulated fills, fees included), in every mode. That gives a live, comparable scoreboard.

### Allocator (quant baseline)
- Per strategy, rolling net-of-fee score: EWMA of returns / EWMA of volatility (a rolling Sharpe), plus drawdown penalty.
- Baseline weights = softmax(score / τ), with floor 0, cap `max_strategy_weight`, and weight 0 if the strategy is in its own drawdown stop.
- Regime model (HMM or vol/trend classifier) multiplies weights by regime fit.

### LLM agent (the "AI substantively changes decisions" part)
Every `agent_interval_min` (and on risk events) the agent receives a compact JSON snapshot:
scoreboard, regime probabilities, per-market spread/vol/volume, current exposure, drawdown, recent errors.
It must return JSON matching this schema (validated with pydantic, temperature 0):
```json
{
  "strategy_weights": {"basis_tmn_usdt": 0.5, "mean_reversion": 0.3, "momentum": 0.2},
  "active_markets": ["BTCTMN", "ETHTMN", "USDTTMN"],
  "risk_multiplier": 0.8,
  "pause_markets": [],
  "rationale": "short text"
}
```
- Final weights = agent weights clamped to guardrail bounds. If the call fails, times out, or is invalid → baseline weights, logged as `fallback`.
- **Both baseline and agent weights are logged every cycle**, so we can show where and how much the agent changed decisions, and compare agent vs no-agent in backtest.

### Reproducibility of LLM calls
- Every request/response is stored in table `llm_calls` with a hash of the prompt.
- `--replay` mode reads recorded responses instead of calling the API, so a run is bit-for-bit repeatable.
- Backtests call the agent at a coarse cadence (default hourly) to keep cost and time sane.

## 3. Risk gate (defaults; editable via `config guardrail`)
| guardrail | default |
|---|---|
| max position per market | 20% of equity |
| max total exposure (non-TMN) | 60% of equity |
| max order value | 10% of equity |
| min order value | config (placeholder 100,000 TMN, learned from exchange rejection); entries ≥ 2× min; exits close in full |
| stop-loss per position | min(2 × ATR, 2%) |
| take-profit / trailing | strategy-defined, logged |
| daily drawdown soft stop | 4% → risk_multiplier halved, no new entries for 30 min |
| total drawdown hard stop | 8% → flatten all, switch to shadow, require human to resume |
| max consecutive losses per strategy | 4 → strategy weight 0 for 60 min |
| max open orders | 10 |
| end-of-event flatten | start at T-90 min (no new entries), force-sell all at T-30 min |

The risk gate can only reduce or reject orders, never enlarge them. It runs after the LLM, so the LLM cannot breach limits.

## 4. Orders and state
- All money math uses `Decimal`. Prices and amounts are rounded to each market's `price_precision` / `amount_precision`.
- Prefer limit orders at/inside the spread; convert to aggressive only for stops and the final flatten: MARKET where the market allows it, otherwise an aggressive LIMIT 0.5% past the best opposite price, re-priced while unfilled (see wallex-api.md).
- The exchange is the source of truth: reconcile open orders and balances every cycle; never trust local state after a timeout.
- Every order row carries `decision_id`; every decision row stores features, signal, weights, risk checks, and final action.

## 5. Modes and switching (`change`)
- `backtest` → offline, runs on candles from `/v1/udf/history` (+ recorded depth if available).
- `shadow` → live data, simulated fills (fill when the market trades through our price), shown with a distinct "SHADOW" color theme so it can never be confused with live trading.
- `trade` → live orders.
- shadow → trade: explicit confirmation, starts from real balances, no virtual positions are inherited.
- trade → shadow: ask "keep or flatten real positions?" (default: flatten).
- Before `trade` starts, the CLI asks whether a recent backtest + shadow run exists (human-in-the-loop prompt; user may proceed).

## 6. CLI commands
`trade [market]`, `stop [market]`, `shadowtest`, `backtest [--from --to]`, `change <from> <to>`,
`status`, `watch` (live dashboard), `rslt <backtest|shadowtest>`, `log [n]`, `reason [n]`, `exp-reason [n]`,
`config guardrail`, `autoset guardrail`, `markets`, `flatten [market]`, `doctor`, `export`.

## 7. Storage (SQLite, WAL)
`candles`, `depth_snapshots`, `orders`, `fills`, `decisions`, `strategy_paper_pnl`, `allocations`, `llm_calls`, `equity_curve`, `events`.
`export` zips: decisions, orders, fills, equity curve, allocations, llm_calls, config, backtest/shadow reports.
