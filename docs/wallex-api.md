# Wallex API reference (wallex.ir)

Full schemas: `docs/ref/wallex-spot-trading.json` (orders, depth/all) and `docs/ref/wallex-basic-services.json` (market data, balances, fees, candles). This file is the summary; open the JSON only for exact field shapes.

## Basics
- Base URL: `https://api.wallex.ir` (the swagger files say `api.wallex.com`; use `.ir` and verify in `doctor`).
- Auth headers on **every private request**: `x-api-key: <key>` (env `WALLEX_API_KEY`) **and** `sub-account-client-id: <id>` (env `WALLEX_SUBACCOUNT_ID`). Both are required; see "Trading-credit account" below.
- Envelope: `{"success": bool, "message": str, "result": ...}`. Errors: `{"success": false, "code": int, "message": str, "result": {field: [msgs]}}`.
- Status codes seen: 200/201 ok, 400 bad params, 401 auth, 404 not found, 422 validation.
- Numbers often arrive as **strings** with 16 decimals → parse to `Decimal`. Depth `price`/`quantity` arrive as JSON **floats** → parse JSON with `parse_float=Decimal`. Some markets send `""` for numeric fields (e.g. `price`) → treat as null.
- Symbol format: `BTCTMN`, `ETHUSDT`, `USDTTMN` (no slash). `data/not_allowed.txt` uses `BTC/TMN` → strip the slash to compare.
- Rate limit (market data): **600 requests per 39 s window** (user-confirmed). Responses carry `x-ratelimit-limit` and `x-ratelimit-remaining` (seen on `/v1/trades`, not on cached `/markets`). Consecutive responses can report very different `remaining` (e.g. 556 → 386 → 460), apparently from different backend nodes, so treat the header as a hint. The feed plans its own budget (≤ 70% of the limit per window) and the client backs off at < 10% remaining.
- Order rate limit: see "Order rate limit" below (separate limiter).
- Minimum order value: see "Minimum order value" below.
- `/hector/web/v1/markets` is served from a cache (`cache-control: max-age=30`, `x-cache-status: HIT`): prices there can be 30 s old and its `Date` header is stale (don't use it for clock skew).

## Market data
| Method | Path | Notes |
|---|---|---|
| GET | `/hector/web/v1/markets` | all markets: `symbol, base_asset, quote_asset, price, volume_24h, quote_volume_24h, change_24h, is_spot, is_tmn_based, is_usdt_based, is_zero_fee, amount_precision, price_precision, is_market_type_enable`. `quote_volume_24h` is in the market's own quote asset (convert USDT markets to TMN). `otc_min_notional` is OTC-only: not a spot minimum |
| GET | `/depth/all` | order book for **all markets in one call**: `result[SYMBOL] = {ask:[{price,quantity,sum}], bid:[...]}`. Use this for polling (1 call instead of N). **Observed 2026-10-22: nginx HTML 503, persistently (without key)** → client falls back to `/v1/depth` per market |
| GET | `/v1/depth?symbol=` | order book for one market |
| GET | `/v1/trades?symbol=` | latest trades: `result.latestTrades[] = {quantity, price, sum, isBuyOrder, timestamp}` |
| GET | `/v1/udf/history?symbol=&resolution=&from=&to=` | candles, TradingView UDF format: `{s:"ok", t:[], o:[], h:[], l:[], c:[], v:[]}` (**no** success/result envelope); resolution `1,5,15,60,D`; from/to unix seconds. Use for backtests. **1-min bars: request ≤ 1 day per call** (a 3-day request returns `s: "no_data"`; 1-day chunks return 1440 bars). Gaps (missing minutes) occur |

## Account
| Method | Path | Notes |
|---|---|---|
| GET | `/v1/account/balances` | `result.balances[ASSET] = {asset, value, locked, fiat, is_dust}` (value/locked are strings) |
| GET | `/v1/account/fee` | `result[SYMBOL] = {makerFeeRate, takerFeeRate, recent_days_sum}` e.g. `"0.00200000"` = 0.2%. Load at startup, use in every PnL/edge calc. **Observed on the trading-credit account:** `result` also holds non-market keys `default: []` and `metaData: {levels, coin_levels, latestTradesSum, latestTetherTradesSum}` (ignored by the parser). Rates seen (maker/taker), split exactly by quote asset: all 187 TMN markets 0.25%/0.30%, all 186 USDT markets 0.10%/0.13%. Parsing is tolerant: `result` `{}`/`[]`/missing or an entry `[]` → config default `fees.default_maker/taker` (0.30%, the highest observed tier), marked `source = default`, ⚠ in doctor |

## Orders (spot)
| Method | Path | Notes |
|---|---|---|
| POST | `/v1/account/orders` | body: `{symbol, side: BUY/SELL, type: LIMIT/MARKET/STOP_LIMIT/STOP_MARKET, price, quantity, stop_Price, client_id}`. All values as **strings**. Returns 201 + order |
| GET | `/v1/account/orders/{client_id}` | single order status |
| DELETE | `/v1/account/orders/{client_id}` | cancel by client id |
| DELETE | `/v1/account/orders` | cancel, body `{clientOrderId}` |
| GET | `/v1/account/openOrders?symbol=&page=&per_page=` | active orders; `result` is an object keyed by symbol → list of orders |
| GET | `/v1/account/orders?market=&type=&side=&page=&per_page=&from=&to=` | order history, dates `YYYY-MM-DD`, max 100 per page |

Order resource fields: `clientOrderId, symbol, type, side, price, origQty, executedQty, executedPrice, executedSum, executedPercent, status, active, fee, fills[], stopPrice, transactTime, created_at, updated_at`.
Fill fields: `price, quantity, sum, fee, feeAsset, feeCoefficient, makerFeeCoefficient, takerFeeCoefficient, isBuyer, timestamp`.

Rules for our client:
- **Always set our own `client_id`** (e.g. `wx-<decision_id>-<n>`) so a timed-out request can be checked with `GET /v1/account/orders/{client_id}` instead of re-sent (prevents double orders).
- Note the casing: request uses `client_id` and `stop_Price`; responses use `clientOrderId` and `stopPrice`.
- Status strings are not enumerated in the docs. Log every distinct status seen; treat `active == false` as terminal.
- Prefer LIMIT. MARKET only for stop exits and the final flatten, and only where MARKET is enabled (below).

## MARKET order type per market (confirmed)
`is_market_type_enable = false` in `/markets` means the **MARKET order type is disabled** on that market. The market itself is still tradable; this is unrelated to `not_allowed.txt`. Observed 2026-10-22: disabled on 113 of 120 allowed spot markets (enabled only on BTCTMN, BTCUSDT, ETHTMN, ETHUSDT, USDTTMN, XAUTTMN, XAUTUSDT).
- Stored per market (`Market.market_orders_enabled` → `MarketRules.market_orders_enabled`), refreshed at startup; shown in `wallexer markets`.
- On those markets, exits (stop-loss, final flatten) use an **aggressive LIMIT**: SELL at `best_bid × (1 − 0.5%)`, BUY at `best_ask × (1 + 0.5%)` (`orders.exit_limit_slippage_pct`), rounded toward crossing.
- If still unfilled after `orders.exit_reprice_after_s` (10 s): cancel, re-read the order (`executedQty`), re-place the **remaining** quantity at a fresh aggressive price with the next `client_id` (`wx-<decision_id>-<n+1>`). After `orders.exit_max_reprices` (5) a `risk.exit_unfilled` event is logged and re-pricing continues.

## Order rate limit (exchange rule)
- Exchange: **max 20 order requests per 10 s**, counting **create and cancel**.
- Our client: dedicated `OrderLimiter`, separate from the market-data limiter, capped at **15 per 10 s** (`ratelimit.orders`).
- Requests are **queued** (FIFO), never dropped. If `backlog_warn` (5) requests are waiting, or one waits longer than a window, a `risk.order_queue_backlog` event is logged (at most once per window).

## Minimum order value
- Not documented and not in `/markets`. Config: `orders.min_order_value_tmn = 100000` with `min_order_verified = false` (placeholder).
- Live trading is allowed with the unverified value; `doctor` shows a warning.
- If the exchange rejects an order as too small (400/422), the client parses the minimum from the error (Persian/Arabic digits handled; ambiguous → ignored), writes it to `data/overrides.toml` with `min_order_verified = true`, applies it live, and logs `exchange.min_order_learned`. USDT-market rejections are assumed to state the minimum in USDT and converted to TMN.
- Positions are always closed in full: a partial exit that would leave a remainder below the minimum sells everything. Entries must be ≥ `orders.min_entry_multiple` (2×) the minimum so a losing position stays sellable. Exits are never blocked by the local (unverified) minimum; the exchange decides.

## WebSocket (not used in v1)
Centrifugo-based at `wss://api.wallex.ir/connection/websocket` (exact path, no trailing slash). Channel names are undocumented here → we poll REST (`/depth/all` + `/v1/trades`) instead. Revisit only if polling hits rate limits.

## Trading-credit package (read-only, user-confirmed)
| Method | Path | Notes |
|---|---|---|
| GET | `/v1/prop/packages` | `result: [ {id, title, currency, loan, assurance, warning_limit, liquidity_limit, status, expire_days, daily_fee, final_fee, ...} ]` (22 packages). Ours: the one with `loan == "5000000"` and `currency == "TMN"` → id 183 "هکاتون استارکوچ \| والکس" |
| GET | `/v1/prop/packages/{id}` | `result: {detail: {...same fields}, unavailableMarkets: [symbols], faq: []}`. 357 unavailable symbols = all of not_allowed.txt + BAL, CATS, DAI, IP, OM, RDNT (TMN + USDT) |

- `unavailableMarkets` is saved to `data/unavailable_markets.txt`, excluded from the universe, and rejected again by the risk gate.
- `warning_limit "1000"` / `liquidity_limit "900"` are plain TMN amounts (the UI shows "900 تومان"): liquidation is effectively unreachable; our 8% hard stop is what limits losses.
- Only these two GETs. No package purchase, transfer or any other `/v1/prop/*` call.

## Out of scope: do not use
- OTC / easy-trade (`/v1/otc/*`, `/v1/account/easy-trade/orders`): not spot order-book trading; excluded unless organizers confirm it's allowed.
- Sub-account management, transfers, any prop/package endpoint other than the two read-only GETs above, withdrawals, IBAN, cards, wallet addresses.

## Trading-credit account (confirmed)
We trade **only** with the trading-credit (sub) account (spot only; no margin/futures).
- `WALLEX_API_KEY` must be **the trading-credit account's own API key**, not the main account's.
- `WALLEX_SUBACCOUNT_ID` is **required**. Header `sub-account-client-id: <WALLEX_SUBACCOUNT_ID>` is sent on **every** private request.
- Key + sub-account id are always mandatory; without them shadow and trade refuse to start.
- **First start** (no trade-mode decisions/orders in the DB): the **TMN balance** must be ≈ 5,000,000 (±10%), otherwise shadow/trade refuse to start (`preflight_live`).
- **Later starts:** equity and drawdown vs 5M are only reported (warning), so a restart while holding positions can always start and sell back to TMN.
- Equity = Σ (value + locked), coins marked at best bid. Assumption: `value` excludes `locked` (verify on the first live open order; double counting would hide drawdown).
- Never call `/sub-accounts/transfers` or any transfer endpoint.

## Wallex MCP
`https://api.wallex.ir/mcp` (Streamable HTTP). Public tools need no key; private tools need `X-API-Key`.
Used by: Claude Code during development (public/read-only), and the LLM agent at runtime as read-only market tools. **Never place orders through MCP.**
