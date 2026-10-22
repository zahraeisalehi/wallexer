# wallexer runbook

Event end: **2026-10-23 20:00 UTC** (`EVENT_END_UTC`). New entries stop at **18:30 UTC** (T-90 min);
the engine force-flattens everything to TMN from **19:30 UTC** (T-30 min).
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
```
liara disk create -a wallexer --team-id $T -n data -s 1   # done 2026-10-22
# secrets: run these yourself (never commit them, never paste them into chat)
liara env set -a wallexer --team-id $T WALLEX_API_KEY=... WALLEX_SUBACCOUNT_ID=... BOXAPI_API_KEY=...
liara env set -a wallexer --team-id $T EVENT_END_UTC=2026-10-23T20:00:00Z WALLEXER_MODE=shadow
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
- During T-30 → end, the flatten phase is re-detected on start and continues.
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
- value-vs-locked: pending (needs an accepted resting order).
