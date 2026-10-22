# Hackathon rules (condensed): Wallex challenge

## Setup
- Capital: 5,000,000 TMN (Toman) on a dedicated Wallex account.
- Event: 3 days. Our realistic window: ~2 days total, including build time.
- Score = 300 pts: 150 trading performance + 150 technical review.

## Hard constraints (violations = disqualification risk)
1. No manual trading. Every order is placed by the algorithm.
2. No self-trading / no trading between team accounts.
3. No wash trading or artificial volume.
4. Spot only. Only markets NOT listed in `data/not_allowed.txt` (format `BASE/QUOTE`, Wallex symbol = `BASEQUOTE`, e.g. `BTC/TMN` -> `BTCTMN`).
5. At the official end, the account must hold TMN only. Every coin, USDT included, sold back to TMN.
6. Every trade must have a decision-log entry: signal observed, why buy/sell. Logs are part of the submission.

## How we are scored
Return % = (final value - 5,000,000) / 5,000,000 * 100. Fees included.

| Trading performance (150) | pts |
|---|---|
| Final profit % | 90 |
| Loss control (max drawdown during the event; lower = better) | 30 |
| Algorithm activity (consistent, sensible trading across the whole period) | 15 |
| Execution stability (low error rate, correct orders, no crashes) | 15 |

| Technical quality (150) | pts |
|---|---|
| Strategy logic: buy/sell rationale is explainable | 40 |
| Genuine AI usage: AI substantively changes decisions (not an if/else with a label) | 35 |
| Risk & capital management: size limits, stop-loss, no all-in | 30 |
| Reproducibility: clean code, clear run instructions, judges can re-run | 25 |
| Backtest + shadow test conducted and results presented | 20 |

## Implications for design
- Drawdown is scored directly: risk control beats aggressive return chasing.
- Trades should be spread over the whole period (activity score), not one lucky trade.
- Backtest, shadow and live must share the same strategy code (credibility of results).
- The LLM must be able to change allocations, and we must be able to show that it did.
