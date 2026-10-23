# wallexer runbook

Event end: **2026-10-23 20:20 UTC** (`EVENT_END_UTC=2026-10-23T20:20:00Z`, confirmed real end).
New entries stop at **18:50 UTC** (T-90, `guardrails.flatten_no_entry_min`); the engine force-flattens
everything to TMN from **19:45 UTC** (T-35, `guardrails.flatten_force_min`). `wallexer status` prints
these three times on the "Schedule" line.
The engine is **flat by design** unless a strategy passes the walk-forward OOS gate and shadow.

## 0. Before going live (once)
```
uv run wallexer doctor --quick          # keys, sub-account, ~5M TMN, fees, package 183, clock
! uv run wallexer doctor --live-smoke   # YOU type SMOKE: 1 unfillable LIMIT BUY on BTCTMN, then cancel
```
The smoke test must end with "SMOKE TEST PASSED". It also records whether balance `value` includes
`locked` (event `smoke.result`). If anything fails it only cancels, never re-sends.

## 1. Deploy to Liara (app `wallexer`, team `6ad87ba7cf87f9a8e45d4d3a`, network `wallexer-net`)
The app exists (created in the console: Docker, plan Mars = medium-g2, bundle Base = `basic`). Every
liara command needs `--team-id 6ad87ba7cf87f9a8e45d4d3a` (also in `liara.json`); below `$T` stands for it.
Deploy from a clean staging copy (no `.env`, no DB): see `deploy` section of the handoff / rsync excludes.
`liara.json` targets this LIVE app; the demo app has its own guarded path (§2b).
```
liara disk create -a wallexer --team-id $T -n data -s 1   # done 2026-10-22
# secrets: run these yourself (never commit them, never paste them into chat)
liara env set -a wallexer --team-id $T WALLEX_API_KEY=... WALLEX_SUBACCOUNT_ID=... BOXAPI_API_KEY=...
liara env set -a wallexer --team-id $T EVENT_END_UTC=2026-10-23T20:20:00Z WALLEXER_MODE=shadow
liara deploy --app wallexer --team-id $T --port 8000 --build-location iran --disks data:/app/data --no-app-logs
```
Container start (`deploy/entrypoint.sh`): seed `not_allowed.txt` onto the disk → `doctor --quick`
(refuses to start on failure) → `wallexer engine run --mode $WALLEXER_MODE`. The engine opens
`/health` first (`"starting": true`), then syncs history; a walk-forward runs 2 min after start
when the last run is > 20 h old, then every 12 h. Ready in ~2 min.
Note: `liara logs` returned nothing on this plan; use `liara shell ... -c "wallexer status"`,
`wallexer health`, `wallexer log` instead.

## 2. Check it
```
liara logs -a wallexer --team-id $T                                   # JSON logs
liara shell -a wallexer --team-id $T -c "wallexer status"             # full status incl. agent panel
liara shell -a wallexer --team-id $T -c "wallexer health"        # or: curl https://wallexer.liara.run/health
liara shell -a wallexer --team-id $T -c "wallexer doctor --quick --no-llm"
liara shell -a wallexer --team-id $T -c "wallexer rslt shadowtest"
```
Healthy: `/health` → `"ok": true`, `status` shows the mode banner, `errors: 0`, feed books "ok",
agent stance + rationale (first decision ~90 s after start).

### Remote viewing (read-only API, replaces `liara shell` for looking)
The engine serves `/api/status`, `/api/log?n=&after=`, `/api/reason[/{id}]`, `/api/rslt/{backtest|shadowtest}`,
`/api/equity`, `/api/sum?date=` next to `/health`. GET only (anything else → 405), header
`X-View-Token` must equal the env var `WALLEXER_VIEW_TOKEN` (unset → the API does not exist, 404),
DB opened read-only. There are no write endpoints: stop/flatten/resume still need `liara shell`.
```
# once, yourself (the value never goes into git or chat); this restarts the app:
liara env set -a wallexer --team-id $T WALLEXER_VIEW_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
# on your laptop:
wallexer setup --mode remote        # URL https://wallexer.liara.run + the same token (hidden prompt, stored 0600)
wallexer status · wallexer watch · wallexer log · wallexer reason 12 · wallexer rslt shadowtest · wallexer sum
```
In a dev checkout instead: `WALLEXER_REMOTE_URL=https://wallexer.liara.run` + `WALLEXER_VIEW_TOKEN=...`
in `.env`, then `wallexer --remote status`. In remote mode trade/stop/flatten/change/resume/config/
autoset/shadowtest/engine/agent/doctor --live-smoke are refused before any request is made.

## 2b. Demo app `wallexer-demo` (public demo, never trades)
Separate app: team `$T`, network `wallexer-demo-net`, plan Mars + Base, its own disk `data` → `/app/data`
(created 2026-10-23, 1 GB). Its env vars are set by you, with a **read-only** Wallex key.
**`liara.json` is the LIVE app.** A bare `liara deploy` from the repo deploys production. The demo is
deployed only with the guarded script, which never reads `liara.json`:
```
deploy/deploy-demo.sh --check   # checks + clean staging copy, no deploy
deploy/deploy-demo.sh           # deploy wallexer-demo
```
The script refuses (exit 1) if `liara.demo.json` or `$APP` name `wallexer` or anything other than
`wallexer-demo`, if `WALLEX_API_KEY`/`WALLEX_SUBACCOUNT_ID` are missing on the demo app (key names only
are listed, never values), if `WALLEXER_MODE` is set and not `shadow`, or if `WALLEXER_DEMO` is set and
not `1`. It stages tracked files without `.env*`, DBs, logs or `liara.json`, then runs
`liara deploy --app wallexer-demo --liara-json liara.demo.json --build-arg WALLEXER_DEMO=1 ...`.

`WALLEXER_DEMO=1` (baked into the demo image; an env var with another value would override it, the
script refuses that) means:
- the engine refuses `--mode trade`; `LiveExecutor` refuses to start;
- the order latch can never open: `enable_orders()` raises, and every order/cancel request is refused
  again even if the flag were set;
- the CLI refuses `trade/stop/flatten/change/resume` (exit 2); the engine's control socket refuses
  `stop/flatten/resume/start_markets`;
- `config/demo.toml` is layered on top: depth every 15 s, trades 60 s, candles 180 s, feed budget 15 %
  of the rate limit, 6 markets, 10 days of history (strategy/risk/allocation settings unchanged);
- `/health` and `/api/status` report `"demo": true`.

`WALLEXER_VIEW_PUBLIC=1` (demo only; the engine refuses to start with it unless `WALLEXER_DEMO=1`)
makes the read-only `/api/*` answer without a token, so a fresh install watches the demo with zero
setup. Still GET-only on the read-only DB. The demo engine pre-generates the LLM answers it shows
(today's `sum` hourly, yesterday's once, `exp-reason` for each new agent decision, with the demo's
BoxAPI key) and `/api/sum`, `/api/explain[/{id}]` only read them: no request can trigger an LLM call.
`deploy/deploy-demo.sh` refuses unless it is set:
```
liara env set -a wallexer-demo --team-id $T WALLEXER_VIEW_PUBLIC=1
```
```
curl -s https://wallexer-demo.liara.run/health                               # mode shadow, demo true
curl -s -H "X-View-Token: $TOKEN" https://wallexer-demo.liara.run/api/status  # 200
curl -s -o /dev/null -w '%{http_code}\n' https://wallexer-demo.liara.run/api/status   # 401
liara shell -a wallexer-demo --team-id $T -c "wallexer trade"                # refused, exit 2
```

## 3. Switch shadow → trade (only after the smoke test passed and you said so)
```
liara env set -a wallexer --team-id $T WALLEXER_MODE=trade   # restarts the app
liara shell -a wallexer --team-id $T -c "wallexer status"   # banner must say TRADE; "flat by design" unless promoted
```
Back to shadow: `liara env set -a wallexer --team-id $T WALLEXER_MODE=shadow` (flatten first if positions exist, §4).

## 4. Stop / flatten / resume
```
liara shell -a wallexer --team-id $T -c "wallexer flatten"            # sell every non-TMN balance (incl. USDT) to TMN
liara shell -a wallexer --team-id $T -c "wallexer stop BTCTMN"        # stop one market (cancels its open orders)
liara shell -a wallexer --team-id $T -c "wallexer stop"               # stop everything (Liara restarts the container!)
liara stop -a wallexer --team-id $T                                   # really stop the app
liara shell -a wallexer --team-id $T -c "wallexer resume"             # lift a hard drawdown stop (type RESUME)
```
`wallexer stop` makes the engine exit; on Liara the platform restarts the container, so to stay
stopped use `liara stop`. Flatten first if positions exist.

## 5. Submission bundle
```
liara shell -a wallexer --team-id $T -c "wallexer export"             # → /app/data/exports/wallexer-export-*.zip
liara shell -a wallexer --team-id $T -c "ls -la /app/data/exports"
```
Download: `liara shell -a wallexer --team-id $T -c "base64 -w0 /app/data/exports/<file>.zip" > bundle.b64 && base64 -d bundle.b64 > bundle.zip`.
The zip holds decisions, orders, fills, equity curve, allocations, llm_calls, paper trades,
walk-forward runs/results/reports, basis study, events, dust list and config (never `.env`).
Also attach the local `data/reports/*.md` and `wallexer research spread` output.

## 6. If the server restarts
- The engine reconciles balances and open orders from Wallex first (exchange = source of truth).
- Later starts don't need TMN ≈ 5M; they report equity/drawdown vs 5M.
- A **hard stop persists** across restarts until `wallexer resume`.
- During T-35 → end (19:45–20:20 UTC), the flatten phase is re-detected on start and continues.
- Walk-forward reruns every 12 h in the background; the agent re-plans after ~90 s.
- Check `wallexer log` for `engine.*`, `risk.*`, `backtest.*` events.

## 7. Emergency checklist
1. `liara shell -a wallexer --team-id $T -c "wallexer flatten"` → wait → `wallexer status` shows no positions.
2. `liara stop -a wallexer`.
3. If the container is unreachable: flatten manually in the Wallex UI (sub-account), sell everything to TMN.
4. Export the bundle as soon as the app is reachable again.

## Smoke-test record
- 2026-10-22 16:22 UTC, `wx-smoke6-1`: crashed before the POST (duplicate `client_id` log kwarg);
  never sent (openOrders empty, get_order 404, TMN locked 0). Bug fixed, regression test added.
- 2026-10-22 17:10 UTC, `wx-smoke8-1`: BUY 0.00000566 BTC @ 17,680,089,818 (-20% vs best bid)
  **rejected** by Wallex: "اطلاعات وارد شده اشتباه است". Not retried, nothing placed.
  Likely cause: `/markets` → `fair_price.spot` = {threshold 4, max_acceptable_buy 8, max_acceptable_sell 8}
  for BTCTMN (USDTTMN: 3/4/4), i.e. limit prices must stay within a few % of the fair price
  (undocumented; inferred). Re-run inside the band: `wallexer doctor --live-smoke --discount-pct 3.5`.
- 2026-10-22 ~18:30 UTC, `wx-smoke9-1`/`wx-smoke10-1`: with the MAIN account's key the POST hit the
  main account (422, error_code 1006, quantity: "موجودی شما کافی نیست. موجودی شما 0 تومان می باشد").
  The `sub-account-client-id` header switches reads, not order placement. Fixed by a key created inside
  the trading-credit account. Next run placed the order, then a parse error on `fills` triggered the
  recovery cancel (verified CANCELED, executed 0). Models made tolerant; raw responses kept.
- **2026-10-22 20:02 UTC, `wx-smoke11-1` (decision #11): PASSED, all 12 steps.** BUY 0.00000469 BTC @
  21,330,388,556 (-3.5% vs best bid 22,104,029,592), notional 100,040 TMN; placed (NEW, active), found by
  get_order and openOrders, cancelled (CANCELED, executed 0), gone from openOrders, balances restored,
  timeout lookup path OK. Nothing filled, cost 0.
- **value vs locked (settled):** while the order was open `value` stayed 5,000,000 and `locked` rose to
  100,039, so `value` is the TOTAL. Equity = value (coins at best bid); free = value − locked.
