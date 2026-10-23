# Walk-forward run #5

- History: 2026-10-13 00:00 UTC → 2026-10-23 09:12 UTC; split at 2026-10-19 22:08 UTC (oldest 67% in-sample, newest 33% out-of-sample, same split for every timeframe).
- Grid: **34 configurations** = mean_reversion {"z_entry": [1.5, 2.0, 2.5], "exit_frac": [0.0, 0.25, 0.5]} + momentum {"breakout_bars": [20.0, 40.0], "atr_target_mult": [2.0, 3.0], "vol_ratio_min": [1.5, 2.0]}, each × timeframes [15, 60] min.
- Selection: in-sample only (best IS net PnL with ≥ 10 IS trades and IS PF > 1). Promotion gate: selected config's OOS stats only (≥ 10 trades, net > 0, PF > 1.2).
- Fills: taker at bar close ± half the market's spread + 2 bps slippage; real fees from /v1/account/fee. Spreads: {"BTCUSDT": "6.36 (median 24h snapshots)", "ETHUSDT": "16.54 (median 24h snapshots)"}.
- Book imbalance (momentum) is not available historically: candle proxy (c − o)/(h − l).
- 5/15-min bars are resampled from 1-min (the API has no 5/15 resolution); 60-min is native.
- Max DD = peak-to-trough of 5M capital + cumulative realised net PnL (trade order).
- basis_tmn_usdt is not candle-backtested (needs simultaneous books): shadow-evaluated, paper-only.

| Strategy | TF | Market | Fee M/T % | Params | IS n | IS net | IS PF | IS DD% | OOS n | OOS net | OOS PF | OOS DD% | Selected |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| mean_reversion | 15m | ALL (pooled, incl. USDT conversions) |  | exit_frac=0, z_entry=1.5 (IS-invalid) | 0 | 0 | – | 0.00 | 0 | 0 | – | 0.00 |  |
| mean_reversion | 60m | ALL (pooled, incl. USDT conversions) |  | exit_frac=0, z_entry=1.5 (IS-invalid) | 0 | 0 | – | 0.00 | 0 | 0 | – | 0.00 |  |
| momentum | 15m | ALL (pooled, incl. USDT conversions) |  | atr_target_mult=2, breakout_bars=20, vol_ratio_min=1.5 (IS-invalid) | 0 | 0 | – | 0.00 | 0 | 0 | – | 0.00 |  |
| momentum | 60m | ALL (pooled, incl. USDT conversions) |  | atr_target_mult=2, breakout_bars=20, vol_ratio_min=1.5 (IS-invalid) | 0 | 0 | – | 0.00 | 0 | 0 | – | 0.00 |  |

## All configurations (pooled)

| Strategy | TF | Params | IS n | IS net | IS PF | OOS n | OOS net | OOS PF |
|---|---|---|---|---|---|---|---|---|
| mean_reversion | 15m | exit_frac=0, z_entry=1.5 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 15m | exit_frac=0, z_entry=2 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 15m | exit_frac=0, z_entry=2.5 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 15m | exit_frac=0.25, z_entry=1.5 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 15m | exit_frac=0.25, z_entry=2 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 15m | exit_frac=0.25, z_entry=2.5 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 15m | exit_frac=0.5, z_entry=1.5 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 15m | exit_frac=0.5, z_entry=2 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 15m | exit_frac=0.5, z_entry=2.5 | 0 | 0 | – | 0 | 0 | – |
| momentum | 15m | atr_target_mult=2, breakout_bars=20, vol_ratio_min=1.5 | 0 | 0 | – | 0 | 0 | – |
| momentum | 15m | atr_target_mult=2, breakout_bars=20, vol_ratio_min=2 | 0 | 0 | – | 0 | 0 | – |
| momentum | 15m | atr_target_mult=3, breakout_bars=20, vol_ratio_min=1.5 | 0 | 0 | – | 0 | 0 | – |
| momentum | 15m | atr_target_mult=3, breakout_bars=20, vol_ratio_min=2 | 0 | 0 | – | 0 | 0 | – |
| momentum | 15m | atr_target_mult=2, breakout_bars=40, vol_ratio_min=1.5 | 0 | 0 | – | 0 | 0 | – |
| momentum | 15m | atr_target_mult=2, breakout_bars=40, vol_ratio_min=2 | 0 | 0 | – | 0 | 0 | – |
| momentum | 15m | atr_target_mult=3, breakout_bars=40, vol_ratio_min=1.5 | 0 | 0 | – | 0 | 0 | – |
| momentum | 15m | atr_target_mult=3, breakout_bars=40, vol_ratio_min=2 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 60m | exit_frac=0, z_entry=1.5 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 60m | exit_frac=0, z_entry=2 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 60m | exit_frac=0, z_entry=2.5 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 60m | exit_frac=0.25, z_entry=1.5 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 60m | exit_frac=0.25, z_entry=2 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 60m | exit_frac=0.25, z_entry=2.5 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 60m | exit_frac=0.5, z_entry=1.5 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 60m | exit_frac=0.5, z_entry=2 | 0 | 0 | – | 0 | 0 | – |
| mean_reversion | 60m | exit_frac=0.5, z_entry=2.5 | 0 | 0 | – | 0 | 0 | – |
| momentum | 60m | atr_target_mult=2, breakout_bars=20, vol_ratio_min=1.5 | 0 | 0 | – | 0 | 0 | – |
| momentum | 60m | atr_target_mult=2, breakout_bars=20, vol_ratio_min=2 | 0 | 0 | – | 0 | 0 | – |
| momentum | 60m | atr_target_mult=3, breakout_bars=20, vol_ratio_min=1.5 | 0 | 0 | – | 0 | 0 | – |
| momentum | 60m | atr_target_mult=3, breakout_bars=20, vol_ratio_min=2 | 0 | 0 | – | 0 | 0 | – |
| momentum | 60m | atr_target_mult=2, breakout_bars=40, vol_ratio_min=1.5 | 0 | 0 | – | 0 | 0 | – |
| momentum | 60m | atr_target_mult=2, breakout_bars=40, vol_ratio_min=2 | 0 | 0 | – | 0 | 0 | – |
| momentum | 60m | atr_target_mult=3, breakout_bars=40, vol_ratio_min=1.5 | 0 | 0 | – | 0 | 0 | – |
| momentum | 60m | atr_target_mult=3, breakout_bars=40, vol_ratio_min=2 | 0 | 0 | – | 0 | 0 | – |

## Verdict

- **mean_reversion**: does NOT pass out-of-sample: no IS-valid config (>= min IS trades and IS PF > 1)
- **momentum**: does NOT pass out-of-sample: no IS-valid config (>= min IS trades and IS PF > 1)
- **basis_tmn_usdt**: does NOT pass out-of-sample: not candle-backtested: shadow-evaluated, paper-only
