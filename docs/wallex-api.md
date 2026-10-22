# Wallex API reference (wallex.ir)

Full schemas: `docs/ref/wallex-spot-trading.json` (orders, depth/all) and `docs/ref/wallex-basic-services.json` (market data, balances, fees, candles). This file is the summary; open the JSON only for exact field shapes.

## Basics
- Base URL: `https://api.wallex.ir` (the swagger files say `api.wallex.com`; use `.ir` and verify in `doctor`).
- Auth header: `x-api-key: <key>` (env `WALLEX_API_KEY`).
- Envelope: `{"success": bool, "message": str, "result": ...}`. Errors: `{"success": false, "code": int, "message": str, "result": {field: [msgs]}}`.
- Status codes seen: 200/201 ok, 400 bad params, 401 auth, 404 not found, 422 validation.
- Numbers often arrive as **strings** with 16 decimals → parse to `Decimal`.
- Symbol format: `BTCTMN`, `ETHUSDT`, `USDTTMN` (no slash). `data/not_allowed.txt` uses `BTC/TMN` → strip the slash to compare.
- Rate limit: responses carry `x-ratelimit-limit` (seen: 600) and `x-ratelimit-remaining`. Read these headers, keep a client-side limiter, back off at < 10% remaining. Window length not documented: measure it in `doctor`.
- Market minimum order value is NOT in the markets endpoint. Discover it (check Wallex MCP tools, or the 422 message of a too-small order) and store it in config per market. Never guess silently.

## Market data
| Method | Path | Notes |
|---|---|---|
| GET | `/hector/web/v1/markets` | all markets: `symbol, base_asset, quote_asset, price, volume_24h, quote_volume_24h, change_24h, is_spot, is_tmn_based, is_usdt_based, is_zero_fee, amount_precision, price_precision` |
| GET | `/depth/all` | order book for **all markets in one call**: `result[SYMBOL] = {ask:[{price,quantity,sum}], bid:[...]}`. Use this for polling (1 call instead of N) |
| GET | `/v1/depth?symbol=` | order book for one market |
| GET | `/v1/trades?symbol=` | latest trades: `quantity, price, sum, isBuyOrder, timestamp` |
| GET | `/v1/udf/history?symbol=&resolution=&from=&to=` | candles, TradingView UDF format: `{s:"ok", t:[], o:[], h:[], l:[], c:[], v:[]}`; resolution `1,5,15,60,D`; from/to unix seconds. Use for backtests |

## Account
| Method | Path | Notes |
|---|---|---|
| GET | `/v1/account/balances` | `result.balances[ASSET] = {asset, value, locked, fiat, is_dust}` (value/locked are strings) |
| GET | `/v1/account/fee` | `result[SYMBOL] = {makerFeeRate, takerFeeRate}` e.g. `"0.00200000"` = 0.2%. Load at startup, use in every PnL/edge calc |

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
- Prefer LIMIT. MARKET only for stop exits and the final flatten.

## WebSocket (not used in v1)
Centrifugo-based at `wss://api.wallex.ir/connection/websocket` (exact path, no trailing slash). Channel names are undocumented here → we poll REST (`/depth/all` + `/v1/trades`) instead. Revisit only if polling hits rate limits.

## Out of scope: do not use
- OTC / easy-trade (`/v1/otc/*`, `/v1/account/easy-trade/orders`): not spot order-book trading; excluded unless organizers confirm it's allowed.
- Sub-accounts, transfers, prop/trade-credit packages, withdrawals, IBAN, cards, wallet addresses.
## Sub-account (confirmed)
The hackathon's dedicated account **is a sub-account** (spot only; no margin/futures).
- If `WALLEX_SUBACCOUNT_ID` is set, send header `sub-account-client-id: <id>` on every private request.
- `doctor` checks `/v1/account/balances` with and without the header and reports which one shows the ~5M TMN. Use that setting.
- Never call `/sub-accounts/transfers` or any transfer endpoint.

## Wallex MCP
`https://api.wallex.ir/mcp` (Streamable HTTP). Public tools need no key; private tools need `X-API-Key`.
Used by: Claude Code during development (public/read-only), and the LLM agent at runtime as read-only market tools. **Never place orders through MCP.**
