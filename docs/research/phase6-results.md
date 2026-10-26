# Phase 6 results: final algorithm selection by backtest

## Verdict
**Nothing passes. Stay flat.** None of the 9 tested candidates meets the pre-registered rule out of
sample; #7 could not be tested. Every candidate fails at least (a) or (c):
- **(a):** the bootstrap CI includes or is below zero for every candidate except carry_usdt.
- **(c):** no candidate can reach 90% confidence in the ~12 h left to the event end.

Every per-trade strategy (mean reversion, big-move fades, grids, resting quotes) **loses money after
fees and spread, in-sample and out-of-sample.** The only positive returns come from holding USDT, which
is the rial's depreciation, not a trading edge. It takes 30-day holds to beat the ~60 bps
TMN→USDT→TMN cost; a 1-day hold is profitable only 30% of the time.

**The pre-registered stop condition was triggered.** Rule (e) says a significant random-entry control
means the harness is broken: stop and report.
- **Trigger:** carry_usdt's control C2 (random 30-day USDT holds) was significant in 73 of 100 seeds.
- **Diagnosis:**
  - The same control on USDT/TMN with the out-of-sample drift (+19.2 bps/day) removed: significant in
    **1 of 100** seeds, median t −0.17.
  - All other candidates' controls: 0 of 100 significant, with strongly negative median t where
    costs dominate (e.g. −15.9 for #5).
- **Conclusion:** the costs and fills work. The control detected market drift that any USDT holding
  earns, so carry_usdt has no timing edge.
- **Rule unchanged:** I did not relax (e). Under it, carry_usdt fails anyway, and it also fails (c)
  and (d).

## Setup
- **Pre-registration:** `docs/research/phase6-prereg.md`, sha256
  `f4d880f94368ef1300b32f8e1c94cceff9cf077e3e6e5cf794f99c00b32a9cc4`, unchanged since 08:15 UTC
  (before any data was fetched).
- **Multiple testing:** K = 56 configs. E[max of 56 N(0,1)] = 2.293, so z\* = **2.4** (the floor).
- **Timing:** computed 2026-10-26 08:29 UTC. Event end assumed 2026-10-26 20:20 UTC (`.env` still says
  2026-10-23 20:20), so T_rem = 11.85 h = 0.49 days.
- **Fees** (read live from the account fee table, matching the documented one): TMN markets
  0.25% / 0.30%, USDT markets 0.10% / 0.13% (maker / taker).
- **Half spread** (median of recorded depth snapshots), bps:

  | BTCTMN | ETHTMN | XAUTTMN | USDTTMN | BTCUSDT | ETHUSDT | XAUTUSDT |
  |---|---|---|---|---|---|---|
  | 4.0 | 8.1 | 2.5 | 0.2 | 3.2 | 8.1 | 4.0 |

- **USDT conversion cycle:** 60.4 bps.
- **Limit orders:** confirmed only on BTCTMN. The primary analysis is taker everywhere else; the
  maker-everywhere sensitivity is below and changes no verdict.
- **Capital and units:** 1,000,000 TMN per candidate, so a drawdown limit of 40,000 TMN. Trades are
  per trade (per window for #1, per event-day for #6); "days" means a per-day P&L series (hold
  strategies).

## Results (best config chosen by in-sample t; rules judged on OOS)
| Candidate | Best config (by IS t) | Seg | Trades | μ_net TMN | σ | t | 95% CI | N90 | n_rem | maxDD TMN | P(profit) | a | b | c | d | e | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 carry_usdt | h=30 | IS | 48 | 14,445 | 66,595 | 1.50 | -4,335 … 33,819 | 35 | 0.02 | 185,473 | 0.511 |  | | | |  |  |
|  |  | OOS | 24 | 57,473 | 98,082 | 2.87 | 22,555 … 86,838 | 5 | 0.02 | 210,170 | 0.530 | ✓ | ✓ | ✗ | ✗ | ✗ | **FAIL** |
| 2 tsmom_usdttmn | res=D, look=14 | IS | 538 days | 1,343 | 10,485 | 2.97 | 475 … 2,220 | 100 | 0.49 | 134,954 | 0.536 |  | | | |  |  |
|  |  | OOS | 269 days | 899 | 9,133 | 1.61 | -367 … 2,203 | 170 | 0.49 | 101,370 | 0.528 | ✗ | ✗ | ✗ | ✗ | ✓ | **FAIL** |
| 3 rebal_bands | w=(0.5,0.3,0.2), band=0.542 | IS | 540 days | 5,742 | 36,051 | 3.70 | 2,671 … 9,267 | 65 | 0.49 | 484,110 | 0.545 |  | | | |  |  |
|  |  | OOS | 270 days | 1,924 | 17,347 | 1.82 | -633 … 4,418 | 134 | 0.49 | 259,641 | 0.531 | ✗ | ✗ | ✗ | ✗ | ✓ | **FAIL** (also ≤ B&H) |
| 4 mr_usdt_maker | market=ETHUSDT, anchor=240 | IS | 44 | -3,806 | 9,944 | -2.54 | -5,691 … -1,916 | ∞ | 0.95 | 170,957 | 0.355 |  | | | |  |  |
|  |  | OOS | 31 | -2,520 | 9,683 | -1.45 | -5,750 … 759 | ∞ | 1.34 | 85,613 | 0.382 | ✗ | ✗ | ✗ | ✗ | ✓ | **FAIL** |
| 5 mr_bigmove_tmn | k=5, window=60 | IS | 128 | -2,679 | 9,443 | -3.21 | -4,401 … -623 | ∞ | 2.76 | 416,585 | 0.319 |  | | | |  |  |
|  |  | OOS | 38 | -5,378 | 4,089 | -8.11 | -6,480 … -4,229 | ∞ | 1.64 | 205,558 | 0.046 | ✗ | ✗ | ✗ | ✗ | ✓ | **FAIL** |
| 6 cap_momentum | X=4 | IS | 11 | -5,909 | 36,522 | -0.54 | -23,458 … 11,336 | ∞ | 0.00 | 150,678 | 0.496 |  | | | |  |  |
|  |  | OOS | 14 | 1,510 | 22,894 | 0.25 | -5,295 … 10,647 | 378 | 0.01 | 41,212 | 0.503 | ✗ | ✗ | ✗ | ✗ | ✓ | **FAIL** |
| 7 nobitex_lead | – | – | – | – | – | – | – | – | – | – | – | – | – | – | – | – | **not tested** (Nobitex unreachable) |
| 8 grid_wide | market=BTCUSDT, g=1 | IS | 33 | 212 | 2,975 | 0.41 | -1,575 … 1,351 | 323 | 0.71 | 30,739 | 0.524 |  | | | |  |  |
|  |  | OOS | 13 | -1,585 | 3,867 | -1.48 | -4,544 … 1,012 | ∞ | 0.56 | 28,898 | 0.379 | ✗ | ✗ | ✗ | ✓ | ✓ | **FAIL** |
| 9 maker_markout | market=BTCTMN | IS | 47 | -2,780 | 1,453 | -13.11 | -3,258 … -2,367 | ∞ | 166.06 | 130,639 | 0.000 |  | | | |  |  |
|  |  | OOS | 0 | 0 | 0 | 0.00 | – … – | ∞ | 0.00 | 0 | 0.000 | ✗ | ✗ | ✗ | ✓ | n/a | **FAIL** |
| 10 tsmom_crypto | res=D, look=30 | IS | 538 days | 326 | 5,895 | 1.28 | -173 … 817 | 537 | 0.49 | 67,551 | 0.515 |  | | | |  |  |
|  |  | OOS | 269 days | 397 | 6,346 | 1.03 | -324 … 1,389 | 420 | 0.49 | 42,169 | 0.518 | ✗ | ✗ | ✗ | ✗ | ✓ | **FAIL** |

Reading the table:
- **(c):** hold strategies have n_rem = 0.49 days, so N90 would have to be < 1. The per-trade
  strategies' N90 is ∞ because their μ_net ≤ 0.
- **carry_usdt:** μ_net and the CI are per 30-day window. Its 210k TMN drawdown is from 30-day
  USDT holds in a falling-USDT stretch.

## Per-candidate notes
- **1 carry_usdt** (USDTTMN daily, 2020-10 → now):
  - Net P(profit) of rolling windows over the whole history, after the 60 bps cycle:
    **1 d 29.7% · 2 d 36.8% · 7 d 50.2% · 30 d 64.1%.**
  - The h = 1 config lost 40 bps per window in OOS (t = −6.8).
  - With 12 h left, a USDT position is a coin flip with a 60 bps handicap.
  - The rolling walk-forward cannot hold a 30-day window inside 30-day test months (n = 0).
- **2 tsmom_usdttmn:**
  - Daily lookbacks are mildly positive: L = 7, OOS t = 2.34; the chosen L = 14, OOS t = 1.61.
  - Hourly lookbacks lose (t −0.5 to −4.1).
  - Rolling walk-forward over 12 months: t = 2.10, CI 4 … 2,088 TMN/day. That is below z\* and is
    mostly the same USDT drift.
- **3 rebal_bands:**
  - The chosen band (0.54) never triggered a rebalance, so OOS it *is* buy-and-hold: excess vs B&H =
    0 TMN/day (pre-registered extra condition: FAIL).
  - Its OOS +1,924 TMN/day is the mix's drift, with a 260k drawdown. Walk-forward t = 2.85 is drift
    too.
  - The random-entry control is degenerate here (always invested = B&H).
- **4 mr_usdt_maker:**
  - AR(1) half-lives are short and the gate is open everywhere (c/σ_eq 0.16–1.64, H 11–56 min).
  - The deviations still don't revert enough after costs: every config loses in-sample and
    out-of-sample, with taker fills (primary) and with maker fills (sensitivity: OOS t −0.3 to −7.6).
  - ETHUSDT VWAP60 has an outlier σ_eq of 2.6% (one bad print), so its c/σ is understated.
- **5 mr_bigmove_tmn:** fading big drops loses badly: OOS μ −2,654 to −6,074 TMN per trade on all six
  configs. Big moves on Wallex TMN books continue rather than revert, net of a 60 bps round trip.
- **6 cap_momentum:**
  - After a USDTTMN day above +4%, the next day averaged +75 bps in OOS (14 events) against a 60 bps
    cost.
  - In-sample (11 events) it was +1.3 bps. t = 0.25, so it is noise.
- **7 nobitex_lead:** not tested. `api.nobitex.ir` refuses connections from this host (ConnectError
  in < 10 ms on every request).
- **8 grid_wide:**
  - Empirical P(+g before −g) from hourly starts is never above the break-even ½ + f/g:

    | Market, step | IS | OOS | Break-even |
    |---|---|---|---|
    | XAUTUSDT 1% | 0.23 | 0.07 | 0.84 |
    | XAUTUSDT 2% | 0.04 | 0.00 | 0.67 |
    | XAUTUSDT 3% | 0.00 | 0.00 | 0.61 |
    | BTCUSDT 1% | 0.55 | 0.32 | 0.82 |
    | BTCUSDT 2% | 0.44 | 0.57 | 0.66 |
    | BTCUSDT 3% | 0.49 | 0.56 | 0.61 |

  - Grid P&L loses OOS on every config (maker sensitivity too).
- **9 maker_markout:**
  - Recorded data is about 5 hours: trades from 2026-10-22 13:34 to 18:35 alongside depth snapshots.
  - In-sample BTCTMN, a resting quote earned **−26 to −28 bps per side** after the 25 bps maker fee
    (markout 1/5/30 min). The other markets are similar or worse in the sensitivity run.
  - OOS has no fills: the last third of the window has no overlapping snapshots.
  - Adverse selection exceeds the 2.5–8 bps half-spread.
- **10 tsmom_crypto:**
  - Daily lookbacks are around zero: OOS t 0.63 / 1.03. Walk-forward t = 0.46.
  - Hourly and 4-hour lookbacks lose (t −0.06 to −8.7).

## Measured properties (1-min returns, 2026-09-22 → 10-26)
| Market | Hill α (top 2.5%) | VR(5) | VR(15) | VR(60) | Hurst (R/S) |
|---|---|---|---|---|---|
| BTCTMN | 2.81 | 0.61 | 0.51 | 0.40 | 0.485 |
| ETHTMN | 3.57 | 0.74 | 0.60 | 0.45 | 0.485 |
| XAUTTMN | 3.27 | 0.76 | 0.57 | 0.39 | 0.458 |
| USDTTMN | 2.82 | 0.73 | 0.73 | 0.64 | 0.558 |
| BTCUSDT | 3.30 | 0.82 | 0.68 | 0.56 | 0.496 |
| ETHUSDT | 3.35 | 0.92 | 0.81 | 0.65 | 0.500 |
| XAUTUSDT | 3.44 | 0.85 | 0.67 | 0.46 | 0.459 |

- **Tails:** heavy (α ≈ 2.8–3.6). Variance-based risk understates large moves.
- **VR < 1 at every horizon:** strong short-horizon mean reversion in 1-min prices. Candidates 4, 5,
  8 and 9 show it is not tradable: the reversion is mostly the bid-ask bounce, and it is smaller than
  a round trip.
- **Hurst:** ≈ 0.46–0.50, essentially a random walk at 1-min scale. USDTTMN (0.56) trends slightly.
- **AR(1) half-lives** (deviation from VWAP, in-sample):

  | Market | VWAP 60 | VWAP 240 |
  |---|---|---|
  | BTCUSDT | 11.5 min | 49.2 min |
  | ETHUSDT | 20.9 min | 56.2 min |
  | XAUTUSDT | 18.8 min | 32.7 min |

- **USDTTMN daily** (2,173 days from 2020-10-28):
  - **σ** 1.43% per day, 1.52% over the last 30 days.
  - **Drift** +10.4 bps per day over the full history, +47.5 bps per day over the last 30 days.

## Deviations from the pre-registration (none change a verdict)
- **Event end:** assumed 2026-10-26 20:20 UTC, because `.env` was not updated. With 12 h left, (c)
  fails for every hold strategy at any plausible end time today.
- **#7 Nobitex:** not tested (unreachable). K was kept at 56, which is conservative.
- **XAUTUSDT daily bars:** built from hourly bars (2024-08 →) because the daily endpoint served only
  1 year.
- **#2 and #10 data window:** both bar types use the common hourly window (2024-08 → now), so daily
  and hourly configs are judged on the same period.
- **#9 per-fill P&L:** uses the 5-minute markout. The pre-registration listed 1/5/30 min without
  fixing which one feeds the series; all three are reported and are within 2 bps of each other.
- **Random-entry control for hold strategies:** daily long/flat with the candidate's in-position
  fraction. Turnover is not matched exactly. For #3 it is degenerate (always invested).
- **Bertram band (#4):** the expected cycle time uses the symmetric-band formula. The band is
  searched over 0.1–3 σ_eq.

## Reproduce
```
uv run python research/phase6/fetch.py   # data → data/research/phase6.db (engine DB is only read)
uv run python research/phase6/run.py     # → data/research/phase6_results.json
```
Code: `research/phase6/{harness,candidates,run,fetch}.py`. The seeds are fixed (6; C2 seeds 6000–6099).
No orders were sent, and no engine, config or `.env` change was made.
