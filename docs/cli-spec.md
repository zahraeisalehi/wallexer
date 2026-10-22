# CLI spec (user's requirements; implement all of these)

Typing `wallexer` with no arguments opens an interactive prompt (REPL, prompt_toolkit) where all commands below work without the `wallexer` prefix. `wallexer <cmd>` also works directly. The CLI must look polished (Rich styling), but no web UI.

## Modes and switching
- `trade [market ...]`: start live trading (all universe markets, or only the given ones). Each market runs as its own independent async task, so markets can be started and stopped separately.
- `stop [market ...]`: stop all trading, or only the given markets. Open orders on stopped markets are cancelled.
- `shadowtest [market ...]`: start shadow mode (live data, simulated orders).
- `backtest [--from --to --markets --resolution]`: no args = use the values from config.
- **Human-in-the-loop prompt:** before `trade` starts (or a switch into trade), always ask "Run/review backtest and shadowtest first? [Y/n]" and show the latest results if any exist. The user may proceed anyway; the answer is logged.
- `change <from> <to>`: all 6 transitions between `backtest`, `shadowtest`, `trade`:
  - shadowtest → trade: confirm; start from real balances; virtual positions not inherited.
  - trade → shadowtest: ask keep or flatten real positions (default flatten).
  - shadowtest/trade → backtest: stop the live mode cleanly (trade asks keep/flatten), then run a backtest with current config.
  - backtest → shadowtest/trade: start that mode with the config/params that were just backtested.

## Live view (shadowtest and trade)
- Textual screen. Shadow uses a clearly different color theme with a "SHADOW" banner; trade has its own theme.
- **Numbers panel:** equity, PnL %, drawdown, positions, strategy weights. Updated in place, never printed twice.
- **Log panel:** new decision/order log lines append and scroll.
- **Quit button** (and `q` key): closes the view only; the engine keeps running unless `stop` is used.

## Results and logs
- `rslt <backtest|shadowtest>`: final summary of the latest run: return %, max drawdown, trade count, win rate, avg trade, fees paid, per-strategy breakdown, agent vs baseline comparison.
- `log [n]`: no arg = all logs (paged); `n` = that log entry in full.
- `reason [n]`: static, no LLM. No arg = overall: the full list of transactions, which algorithms were used, aggregate stats. With `n`: that decision's features, signal values, the math/model and thresholds that fired, strategy weights, risk-gate checks, before/after numbers (differences), and the resulting orders.
- `exp-reason [n]`: an LLM explanation of the `reason` output. Before sending: anonymize user/account data (account ids, keys, absolute balances become percentages); public market data stays as-is. Uses an engineered prompt with a fixed output format (summary, why this action, risk, confidence). Display the response formatted.

## Guardrails
- `config guardrail`: show all guardrail values in a table and let the user edit them by hand (interactive prompt per value, with validation and min/max bounds). Saved to config and applied live.
- `autoset guardrail`: send the guardrail variable names, their bounds, and current market stats to the LLM. The LLM returns only suggested values (JSON). Show current vs suggested side by side; the user accepts all, some, or none. Never auto-applied.

## Extra commands
`status`, `markets`, `flatten [market]`, `doctor`, `export` (see architecture.md).

## Error handling
Every command validates its input and shows a short, readable error (no tracebacks) plus a usage hint. Unknown command → suggest the closest one. If the engine daemon isn't running, say so and how to start it.
