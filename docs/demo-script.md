# wallexer demo video script (~3:00)

**Persona.** Navid, a part-time trader in Tehran with 5,000,000 TMN of trading credit on Wallex.
Navid has lost money on "obvious" setups before and no longer trusts gut feel: *"Before I risk a
toman, show me an edge that survives fees, on data the strategy has never seen."*

**Arc.**
1. Setup: zero-config install, the app opens on a live engine.
2. Question: why is it flat?
3. Evidence: costs, the walk-forward verdict, the decision record, the AI's explanation.
4. Decision: Navid tries to force a trade and the guardrails refuse.
5. Outcome: no trade today, and every reason is on the record.

The hero is the honest message: **the system refuses to lose money on edges that don't survive
fees, and proves why.**

**Where each beat runs.**
- **DEMO** beats run against the public demo (`wallexer` with no setup, read-only, https://wallexer-demo.liara.run).
- **LOCAL** beats need an engine on the recording machine with its own keys (a checkout with
  `.env`, or `wallexer setup` → 1). They are marked, and recorded as a separate take.

All commands below were run on 2026-10-23 and answered as described. Inside the app, DEMO
commands answer in about 1–3 s, except `markets`: about 10 s, cut.

| # | Time | Where | Narrator | Typed | Viewer notices |
|---|---|---|---|---|---|
| 1 | 0:00–0:15 | shell → DEMO | "I have five million toman on Wallex, and I don't trust my gut anymore. One line to install." | `curl -LsSf https://raw.githubusercontent.com/zahraeisalehi/wallexer/main/install.sh \| sh` then `wallexer` | The installer prints only `Run: wallexer`; the app opens straight into the live engine (no questions), header `DEMO SHADOW · equity 5,000,000 · PnL +0.000% · entries stop in …`. |
| 2 | 0:15–0:28 | DEMO | "First question: what is it doing with the money right now? Nothing. It's flat, on purpose." | `status` | Six lines: equity, PnL, drawdown, positions `none`, `Agent flat ×0.2` with a one-line reason, and the next deadline. |
| 3 | 0:28–0:45 | DEMO | "Why flat? Every trade has to pay for itself first: a round trip costs sixty to eighty-plus basis points before I earn anything." | `markets` *(cut the ~10 s load)* | `Round trip bps` column 60–86; only 7 of 469 markets survive the filters (not allowed, limit-only, low volume, wide spread). |
| 4 | 0:45–1:08 | DEMO | "Did any strategy actually beat that? It tunes on the oldest two-thirds of the data and judges on the newest third. Nothing passes. Here's the trap I'd have fallen into: mean reversion looked great in-sample, sixty-four percent wins, profit factor 1.68, then zero trades out of sample, and across its five-minute settings fees turned it into minus 2.27 million." | `rslt backtest` then `rslt backtest --full` | Compact: `no strategy passes out-of-sample`, three `FAIL` rows. Full: `mean_reversion IS +0.349% · 64% · PF 1.68` vs `OOS 0 trades`; pooled `mean_reversion 5m IS 316 trades −2,272,692`. |
| 5 | 1:08–1:22 | LOCAL | "Maybe just the toman books, last week, fifteen-minute bars? Same answer, and a custom window can never promote a strategy anyway." | `backtest --from 2026-10-16 --to 2026-10-23 --markets BTCTMN,ETHTMN --timeframe 15` *(cut the ~20 s run)* then `rslt backtest` | `Custom backtest #… · no strategy passes out-of-sample · custom: never used for promotion`. |
| 6 | 1:22–1:42 | DEMO | "Every decision is on the record. Decision six: risk cut from one to point two, the two most expensive books paused. And if a judge wants every feature and threshold, it's all there." | `reason 6` then `reason 6 --full` | Compact panel with 5 key numbers (`risk multiplier 1 → 0.2`, `paused ETHTMN, USDTTMN`, `weights all 0`); `--full` scrolls through the features, signal, weights and risk-check JSON panels. |
| 7 | 1:42–1:57 | DEMO | "The AI explains it in plain language, from anonymized numbers only." | `exp-reason 6` | Panel `Explanation of decision #6 (server, 13:14 UTC)`: Summary, Why this action, Risk, `Confidence high`. |
| 8 | 1:57–2:10 | DEMO | "Today in one screen: markets moved about a percent, the agent stayed flat every cycle, and it says why." | `sum 2026-10-23` | Short market table (close, change, realized vol) and the `What the agent decided and why` panel citing decision ids. |
| 9 | 2:10–2:18 | DEMO | "Even small moves are logged: here the agent asks to keep paper-testing mean reversion on gold." | `log 13` | `log #13 · agent.recheck_request` → `mean_reversion`, `15`, `XAUTUSDT`. |
| 10 | 2:18–2:26 | DEMO | "So can I just force a trade? Not here: the public demo can never place an order." | `trade BTCTMN ETHTMN` | `error: trade is disabled in remote mode (read-only viewing)` + `hint: this is the public demo: wallexer setup sets up your own engine`. |
| 11 | 2:26–2:38 | LOCAL | "On my own engine I shadow-test two markets, pull one out, and try to loosen a limit. The bounds say no." | `shadowtest BTCTMN ETHTMN --no-watch` *(cut the ~25 s start)*, `stop ETHTMN`, `config guardrail` → type `50`, then Enter through the rest | `SHADOW engine running · BTCTMN, ETHTMN`; `stopped ETHTMN: no new entries, open orders cancelled`; the bounds table, `max_position_pct must be within [1, 30]`, `No changes.` |
| 12 | 2:38–2:50 | LOCAL | "Going live shows me the evidence first and makes me type TRADE. With nothing that survives fees, I say no." | `change shadowtest trade` → `y`, `n`, `no` | `Switch from SHADOW to LIVE trading? [y/N]` → latest backtest and shadow results → `Run/review backtest and shadowtest first? [Y/n]` → `Type TRADE to confirm:` → `Not confirmed. Nothing started.` |
| 13 | 2:50–3:00 | close | **"wallexer: it trades only on evidence that survives fees, its AI allocates and explains every move, and its risk limits can only shrink what it does. Today, the smartest trade was no trade."** | `q` | The app closes and the terminal comes back as it was; the engine keeps running. |

## Notes on the commands
- **Install line (beat 1):** `curl … | sh` only works once the GitHub repo is public. Before that, record
  the beat with `WALLEXER_SOURCE=/path/to/checkout sh install.sh`, which prints the same single line.
- **`markets` (beat 3):** it has no filter flags; the only option is `--all`, which lists every allowed spot
  market, not just the ranked top N. It queries the exchange live, so it takes about 10 s; cut the wait.
- **LOCAL beats (5, 11, 12):** they need a local engine with keys: a checkout with `.env` (run
  `uv run wallexer`) or an install after `wallexer setup` → 1.
  - On the demo, `backtest`, `shadowtest`, `stop`, `config`, `autoset` and `change` are refused, because
    remote mode is read-only.
  - Beat 12 runs against the shadow engine from beat 11.
  - End the take with `stop`, so no engine is left running.
- **`autoset guardrail`:** not in the cut, because it makes an LLM call and asks accept all/some/none.
  To include it, run it locally and answer `n`.
- **Decision and log ids (6, 13):** these are the demo's, as of 2026-10-23.
  - The demo adds a decision about every 30 min and a walk-forward rerun every 12 h, so check them right before recording (see below).
  - If a rerun changed the numbers in beat 4, update the narration to the new `rslt backtest --full` values.

## Recording checklist
- **Terminal:**
  - 120×34 (the `markets` and `rslt --full` tables need about 120 columns; 80×24 works but the wide tables truncate);
  - 16–18 pt monospace font, dark theme, no transparency, cursor blink off;
  - hide the shell prompt's hostname and path.
- **Fresh HOME for beat 1:** `export HOME=$(mktemp -d)` in the recording shell, so the install and the
  zero-setup start are real. Keep the splash (do not set `WALLEXER_NO_SPLASH`).
- **Warm-up, off camera, just before recording:**
  - `wallexer status`: the demo answers, `DEMO SHADOW`.
  - `wallexer rslt backtest --full`: the numbers still match beat 4's narration.
  - `wallexer reason` and `wallexer reason 6`: decision 6 still exists. Pick a newer one if you prefer.
  - `wallexer exp-reason 6`: shows `(server, …)`, which means the server's pre-generated explanation exists, so no wait and no key.
  - `wallexer sum 2026-10-23`: shows `explanation from the server`, regenerated hourly. Use the recording day's date.
  - `wallexer log 13` and `wallexer markets`: this warms DNS and the exchange.
- **LOCAL take:**
  - Start with no local engine running (`wallexer status` → "engine not running").
  - Clear the app with Ctrl-L between beats.
  - After the take: `stop`, then delete any `data/reports/walkforward-custom-*.md` created during it.
- **Never show:**
  - `.env` or `~/.config/wallexer/credentials.env`;
  - `wallexer setup` prompts, `printenv`/`env`, `liara env list`;
  - `doctor` output (it names the sub-account checks);
  - any API key, the sub-account id or `WALLEXER_VIEW_TOKEN`.
  - The demo needs no token, so none should appear anywhere.
- **Never type `TRADE`** in the LOCAL take: with real keys it would start live trading. Beat 12's answers are `y`, `n`, `no`.
- **Pacing:** type commands at a readable speed, or paste per line. Pause about 1 s on each "viewer notices" line before the narrator moves on.
