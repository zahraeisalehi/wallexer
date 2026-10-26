# Phase 6 pre-registration: final algorithm selection by backtest

Written **2026-10-26 08:15 UTC, before any data was fetched or any candidate was run**. Nothing in this
file is changed after the runs start; any deviation forced by data or the API is listed in the results
file under "Deviations", with the reason.

## Fixed facts and assumptions
- **Event end:** `.env` still says `EVENT_END_UTC=2026-10-23T20:20:00Z`; the user says it moved to
  2026-10-26 but no time was given. Assumed: **2026-10-26 20:20 UTC** (same clock time). Remaining time
  `T_rem` is measured from the moment the results are computed, about 12 h. It is one parameter, so the
  (c) and P(profit) columns can be recomputed if the real time differs.
- **Capital per candidate** (for drawdown): 1,000,000 TMN, the 20% max position of the 5M account.
  P&L is in TMN.
- **Fees** (account fee table, observed 2026-10-22, re-read live at run time): TMN markets
  maker 0.25% / taker 0.30%, USDT markets maker 0.10% / taker 0.13%.
- **Taker cost per side** = taker fee + half spread. The half spread is the median of the recorded
  `depth_snapshots` for the market. If there are none for a market, the 1-min high-low median / 4.
- **Maker fill:** only when a trade prints **through** our price (1-min bars: `low < bid` for a buy,
  `high > ask` for a sell; recorded trades: `price < bid` / `price > ask`). Touching is not a fill.
  An unfilled maker exit falls back to a taker exit after a timeout (stated per candidate).
- **Limit-order acceptance:** confirmed only on BTCTMN (live smoke test 2026-10-22). It cannot be
  confirmed elsewhere without placing orders, which development rules forbid.
  - **Primary analysis** (the one the decision uses): maker fills only on BTCTMN; every other market is
    taker on both sides.
  - **Sensitivity**: maker allowed on all 7 markets. It is reported, but a candidate that passes only
    here is marked "needs limit-order confirmation", not PASS.
- **USDT conversion:** strategies that trade USDT-quoted markets pay one TMN→USDT→TMN cycle
  (2 × USDTTMN taker side) per actual cycle, meaning each contiguous stretch with USDT inventory. The
  cycle cost is spread evenly over that stretch's trades for the per-trade statistics. The P&L is
  computed in USDT and converted at USDTTMN at exit.
- **Split:** tune on the oldest 2/3 of each candidate's data, judge on the newest 1/3. The best config
  is chosen by in-sample t-stat (ties: higher μ_net). Daily and hourly candidates (1, 2, 3, 6, 10)
  also get a rolling-origin walk-forward: re-tune on all data before each month, test on that month.
  It is reported, not used for the decision.

## Decision rule (fixed)
A candidate PASSES only if its best config, on **out-of-sample** data, meets all of:
- **(a)** μ_net > 0 per trade (per day for hold strategies: 1, 2, 3, 6, 10), **and** the 95%
  block-bootstrap CI lower bound > 0. Settings: 1000 resamples, circular blocks of length
  ⌈n^(1/3)⌉, seed 6.
- **(b)** t = μ_net / (σ / √n) ≥ **z\* = 2.4**.
- **(c)** N90 = (1.2816 · σ / μ_net)² ≤ n_remaining = (observed OOS trade rate) × T_rem. For a hold
  strategy, units are days: n_remaining = T_rem in days.
- **(d)** Max drawdown of the OOS cumulative P&L ≤ 4% of allocated capital (40,000 TMN).
- **(e)** The random-entry control C2 for that candidate is NOT significant.
  - C2: 100 seeds of random entries on the same OOS data, with the candidate's trade count, holding
    times, markets and cost model.
  - "Significant" means more than 10 of the 100 seeds reach t ≥ z\* with μ_net > 0. With costs
    positive, random entries should lose; if they don't, the harness is broken: **stop and report**.
- Passers are ranked by **P(profit by event end) = Φ(√n_remaining · μ_net / σ)**.
- Nothing is relaxed after the fact. If nothing passes, the result is "nothing passes".

## Multiple testing
- K = total configs over all candidates below = **56**.
- E[max of 56 iid N(0,1)] = 2.2929 (numerical integral; Monte Carlo 2.2921).
- **z\* = max(2.2929, 2.4) = 2.4.**

## Candidates and grids (K = 56)
| # | Candidate | Grid | Configs | Unit | Data |
|---|---|---|---|---|---|
| 1 | carry_usdt: buy USDT (taker) at window start, sell at window end | hold h ∈ {1, 2, 7, 30} days | 4 | per window (non-overlapping); P(profit) vs h from rolling windows | USDTTMN daily, ≥ 1 y |
| 2 | tsmom_usdttmn: hold USDT while sign(return over L) > 0, else TMN; size = min(1, 20%/realized vol (annualized, 30 bars)) | L ∈ {1d, 3d, 7d, 14d} × bars ∈ {D, 60m} | 8 | per day | USDTTMN daily + 60m |
| 3 | rebal_bands: hold target weights of {USDT, XAUT, BTC}, rebalance on USDT markets (taker) when a weight drifts outside its band (relative to target) | weights ∈ {(0.5, 0.3, 0.2), (⅓, ⅓, ⅓)} × band b ∈ {b\* = (12f)^(1/3), 0.5·b\*, 2·b\*}, f = USDT-market taker side cost | 6 | per day vs TMN; also excess vs buy-and-hold of the same mix (must be > 0 to pass) | XAUTUSDT, BTCUSDT, USDTTMN daily |
| 4 | mr_usdt_maker: buy below an anchor, sell above it (long-only spot). AR(1) on x = log(price/anchor) → φ, half-life H = ln 2 / −ln φ, σ_eq = σ_ε / √(1−φ²). Entry/exit bands from Bertram's optimum for an OU process with cost c. Trades only if c/σ_eq ≤ 3 and H ≤ 120 min; else 0 trades (FAIL). Unfilled exit → taker after 4·H | market ∈ {BTCUSDT, ETHUSDT, XAUTUSDT} × anchor ∈ {VWAP 60m, VWAP 240m} | 6 | per trade | 1-min, 30 d |
| 5 | mr_bigmove_tmn: buy after a drop > k·ATR(14 bars of the window size) over the window, skipped if the last ≥ 3 one-min bars are all down; exit when close > VWAP(window), stop −2%, timeout 24 h. Taker | k ∈ {3, 4, 5} × window ∈ {1h, 4h}, pooled over BTCTMN, ETHTMN, XAUTTMN, USDTTMN | 6 | per trade | 1-min, 30 d |
| 6 | cap_momentum: after a USDTTMN daily return > +X%, hold USDT the next day | X ∈ {2, 3, 4}% | 3 | per event-day; also mean next-day return vs a 50 bps cost | USDTTMN daily |
| 7 | nobitex_lead: cross-correlation of 1-min returns Nobitex→Wallex, lags 0–10. If the best lag L\* ≥ 1 (chosen in-sample), buy the Wallex market when the Nobitex L\*-min move > m·cost, hold L\* min. Taker. Not tested if Nobitex is unreachable | asset ∈ {BTC, USDT} × m ∈ {1, 2} | 4 | per trade | Nobitex udf 1-min (IRT / 10), Wallex 1-min |
| 8 | grid_wide: long-only grid, buy at −g steps below the start, sell each lot +g above its buy. First report P(+g before −g) vs ½ + f/g (f = round-trip cost), then P&L | market ∈ {XAUTUSDT, BTCUSDT} × g ∈ {1, 2, 3}% | 6 | per round trip | 1-min, 30 d |
| 9 | maker_markout: rest at best bid / best ask from recorded depth; fill when a recorded trade prints through. Markout at 1/5/30 min vs mid. Edge per side = half-spread − markout − maker fee | market ∈ the 7 markets | 7 | per fill | `depth_snapshots` + `scan_trades` (only about 1.5 days / 5 h) |
| 10 | tsmom_crypto: long when the return over L > 0 (flat otherwise), vol-scaled as #2, equal weight over BTC/ETH/XAUT on USDT markets, taker | (60m, L = 24), (60m, 72), (240m, 6), (240m, 42), (D, 7), (D, 30) | 6 | per day | 60m + daily, ≥ 1 y |
| C1 | flat | – | 0 | – | – |
| C2 | random entries matched to each candidate's best config (100 seeds) | – | 0 (control) | as candidate | as candidate |

## Measured properties (reported, not part of the decision)
- Hill tail index of 1-min absolute returns (top 2.5%).
- Variance ratio VR(q) for q ∈ {5, 15, 60} on 1-min returns.
- Hurst exponent (rescaled range).
- AR(1) half-lives from candidate 4.
- USDTTMN daily σ and drift (full history and last 30 d).

## Data
- **Existing:** 30 d of 1-min candles for the 7 markets; recorded depth and trades.
- **New fetch:**
  - Wallex 60m and daily candles as far back as available (≥ 1 y) for USDTTMN, BTC/ETH/XAUT on USDT and TMN;
  - a 1-min top-up to now;
  - Nobitex `market/udf/history` 1-min for BTCIRT and USDTIRT over the same window (IRT/10 = TMN).
- New data goes to a separate research DB (`data/research/phase6.db`); the engine's DB is not written.
- No orders, no engine or config changes, no trade mode.
