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
- **Market data by polling.** `GET /depth/all` (every market's book in one call) when it works, else `/v1/depth` per market, every 3 s + `/v1/trades` every 10 s + `/v1/udf/history` 1-min candles. No WebSocket in v1.
- **Universe default:** the markets with MARKET orders enabled (7 on 2026-10-22: BTC/ETH/XAUT on TMN and USDT + USDTTMN), minus not_allowed and the package's unavailableMarkets. LIMIT-only markets are opt-in (`universe.include_limit_only`) with half the max position.
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
| `basis_tmn_usdt` | X/TMN vs X/USDT × USDT/TMN mispricing, traded as a TMN→…→TMN triangle cycle (no inventory); threshold = Σ 3 taker fees + Σ 3 spreads + margin. **Paper-only** until leg-failure unwinding exists (non-atomic legs) | any regime, needs liquid triangle |
| `mean_reversion` | z-score of price vs rolling VWAP / Bollinger, fade extremes | ranging, low vol |
| `momentum` | breakout of N-bar range with volume confirmation | trending |

**Every strategy always runs on its own paper book** (simulated fills, fees included), in every mode. That gives a live, comparable scoreboard.

**Long-only** (spot). Every entry threshold = the market's real round trip (fees from `/v1/account/fee` + current spread) + a margin; never a global constant. Non-urgent entries are maker (LIMIT at best bid). Books with crossed/locked or stale data produce no signals.

### Promotion gate (default stance: flat)
A strategy gets live weight only when **both** hold:
- **walk-forward (out-of-sample only):** `wallexer backtest --walk-forward` replays ~30 days of stored candles (1/5/15/60-min; 5/15 resampled from 1-min) through the same strategies, feature code, paper fill model and real fees. Tuning (a small, logged grid: 68 configs) uses the oldest 2/3; the IS-best config (≥ 10 IS trades, IS PF > 1) is then judged on the newest 1/3. Gate: OOS ≥ 10 trades, net PnL > 0, PF > 1.2. Results in `backtest_runs`/`backtest_results` and `data/reports/walkforward-*.md`.
- **shadow:** paper trades on live data (`source=shadow`) are not net-negative, with ≥ 3 trades.
A run older than `backtest.max_run_age_h` (36 h) cannot promote (warning at 20 h); an already-promoted strategy is never demoted just because the run aged, only by rolling shadow losses or a newer failing run. `basis_tmn_usdt` is shadow-evaluated only (needs simultaneous books) and paper-only. Promotions/demotions are decision rows. Until something is promoted the account stays flat (`status`: "flat by design").

### Allocator (quant baseline)
- Per strategy, rolling net-of-fee score: EWMA of returns / EWMA of volatility (a rolling Sharpe), plus drawdown penalty.
- Baseline weights = softmax(score / τ), with floor 0, cap `max_strategy_weight`, and weight 0 if the strategy is in its own drawdown stop.
- Regime model: vol/trend classifier on 15-min bars, expressed as percentile ranks against the stored 30-day history (no fixed scaling), multiplies weights by regime fit.
- Account targets per market = Σ over promoted strategies of weight × that strategy's paper position fraction; the account trades the difference (decision row → risk gate → executor).

### USDT inventory (allocator decision, not a per-trade cost)
USDT-quoted markets are the cheap fee tier (0.10/0.13% vs 0.25/0.30% on TMN), but trading them requires holding USDT, bought with TMN on USDTTMN and sold back before the end. That conversion is a **separate decision** made by the allocator, not by each trade.
- **Cost of one cycle** (TMN→USDT→TMN) = USDTTMN round trip = 2 × USDTTMN taker + USDTTMN spread, in bps of the converted amount (`Universe.usdt_conversion_bps`; ≈ 60 bps on 2026-10-22). Break-even vs TMN markets at about 2 USDT-market trades per cycle (≈29 + 60/k bps per trade vs ≈64–84).
- **Target USDT inventory** (fraction of equity) = Σ over USDT-quoted strategies of (strategy weight × expected USDT-market exposure), from their recent paper activity. Zero if the expected trades per cycle are below break-even.
- **Hysteresis:** convert only when |target − current| > `usdt_rebalance_band_pct` of equity and the last conversion is older than `usdt_min_hold_min`, so the inventory is not churned. Each conversion is a USDTTMN order with its own decision row (action `usdt_inventory`, inputs: target, current, expected trades/cycle, conversion bps).
- **Risk:** USDT counts as non-TMN exposure in the risk gate. End-of-event flatten sells all USDT to TMN (unchanged).
- **Paper PnL:** the conversion cost is charged **once per cycle** to the USDT-quoted strategies, pro rata to their USDT usage (time-weighted notional) during the cycle. While a cycle is open, the not-yet-paid sell-back leg is accrued as unrealized cost, so paper equity is never overstated. USDT/TMN price moves on the idle inventory go to a separate `usdt_inventory` line on the scoreboard (an allocator decision, not a strategy's edge).
- **Universe ranking** stays per-trade (round trip without conversion); `wallexer markets` also shows "RT incl. USDT conv" (round trip + one full cycle, worst case) for USDT markets.

### LLM agent (the "AI substantively changes decisions" part)
Implemented in `agent/`. Every `agent.interval_min` (30) and on risk events (drawdown steps, soft/hard stop,
feed-error bursts, promotion/demotion; debounced to 5 min) the agent gets an **anonymized** snapshot
(percentages and bps only: equity % of start, drawdown, exposure; per strategy OOS + shadow stats and
promotion state; regime percentiles; per market spread / 15-min vol / round trip / book health; event
counts; hours to event end). BoxAPI `gpt-5.4-mini`, temperature 0, strict `json_schema`, validated with
pydantic; one retry with the error appended, then fallback to the baseline (logged `agent_fallback`).

Output: `stance` (flat|active), `strategy_weights`, `risk_multiplier` (0–1), `pause_markets`,
`active_markets`, `rationale`, `watch_requests` (strategy × timeframe × market, `paper` | `recheck`).
Applied **shrink-only** (`agent.apply_decision`, covered by critical test 2):
- weights only for promoted strategies, always through `risk.clamp_weights`; stance flat → all 0;
- effective risk multiplier = min(engine, agent);
- agent pauses are added to (never remove) human pauses; `active_markets` only restricts new entries;
- `paper` watch requests run up to 3 extra shadow paper books; `recheck` requests are shown first by the
  next `backtest --walk-forward`. This is how the agent changes what evidence is gathered even while flat.

Every cycle writes a `decisions` row (`agent_decision`: trigger, prompt hash, decision, before → after
effects) and every allocation cycle an `allocations` row (baseline, agent, final, source, llm_call_id).
`status` shows stance, rationale and the baseline → agent → final weight diff.

### Order latch call sites
`client.enable_orders()` is called in exactly two places: `LiveExecutor` (trade mode only) and the
live smoke test (`wallexer doctor --live-smoke`, typed confirmation, one unfillable order then cancel).
A hard drawdown stop persists across restarts until `wallexer resume`. The engine re-runs the
walk-forward every `backtest.rerun_every_h` (12 h) in a `nice` subprocess.

### Reproducibility of LLM calls
- Every request/response is stored in table `llm_calls` with a hash of the prompt.
- `--replay` mode (`wallexer engine run --replay`, `llm.replay = true`) reads recorded responses by prompt hash instead of calling the API; a miss is a logged fallback. `wallexer agent replay <llm_call_id>` re-runs a recorded prompt offline and checks the decision is identical.
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
Trading: `trade [market]`, `stop [market]`, `shadowtest [market]`, `flatten [market]`, `change <from> <to>` (all 6
transitions; trade targets ask the human-in-the-loop question and a typed confirmation before anything stops),
`engine run --mode shadow|trade [--replay]`.
Evidence: `history sync`, `backtest --walk-forward`, `research basis`, `research spread [--collect --hours H]`.
Results/logs: `status`, `rslt <backtest|shadowtest>`, `log [n]`, `reason [n]` (static), `exp-reason [n]` (LLM,
anonymized, fixed format), `export` (zip; never `.env`).
Agent: `agent now`, `agent replay <id>`. Config: `config guardrail` (bounded, applied live via `reload`),
`autoset guardrail` (LLM suggestions, user accepts all/some/none). Setup: `markets`, `doctor`.

## 7. Storage (SQLite, WAL)
`candles`, `depth_snapshots`, `orders`, `fills`, `decisions`, `strategy_paper_pnl`, `paper_trades` (closed paper trades per strategy with `source` backtest|shadow and `kind` trade|cycle|conversion; feeds the promotion gate), `allocations`, `llm_calls`, `equity_curve`, `events` (incl. `flatten.dust_accepted`, `feed.book_crossed`/`feed.book_stale`, `risk.*`).
`export` zips: decisions, orders, fills, equity curve, allocations, llm_calls, config, backtest/shadow reports.
