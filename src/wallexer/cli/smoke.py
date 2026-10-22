"""`wallexer doctor --live-smoke`: zero-cost live order test (explicit typed confirmation).

Places exactly ONE LIMIT BUY on BTCTMN for the minimum order value at 20% below the best bid (it
cannot fill; if it somehow did, we would buy BTC 20% under market), verifies it on the exchange,
measures how balances report the locked funds, cancels it, verifies it is gone and balances are
restored, and exercises the client_id lookup used after a timeout. The order POST is never
retried; on any failure after placing, the only action taken is a cancel.

This is the second, explicitly confirmed call site of the order latch (the other is LiveExecutor).
Rows are written with mode='live_smoke' so the trade-mode first-start check is unaffected.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from rich.console import Console
from rich.table import Table

from wallexer.config import Settings
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import (
    ApiError,
    OrderRejected,
    OrderStatusUnknown,
    TransportError,
    WallexError,
)
from wallexer.exchange.models import OrderType, Side
from wallexer.exchange.rules import MarketRules, OrderSpec, make_client_id
from wallexer.store import repo
from wallexer.store.db import init_db, log_event

SYMBOL = "BTCTMN"
DEFAULT_DISCOUNT_PCT = Decimal("20")
MAX_NOTIONAL = Decimal("101000")


@dataclass
class Step:
    name: str
    ok: bool
    detail: str


@dataclass
class Smoke:
    steps: list[Step] = field(default_factory=list)
    placed_cid: str | None = None
    attempted_cid: str | None = None  # set right before the single POST
    facts: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, ok: bool, detail: str) -> bool:
        self.steps.append(Step(name, ok, detail))
        return ok


def _tmn(bals: dict[str, Any]) -> tuple[Decimal, Decimal]:
    b = bals.get("TMN")
    return (b.value, b.locked) if b else (Decimal(0), Decimal(0))


async def run_smoke(
    settings: Settings, console: Console, discount_pct: Decimal = DEFAULT_DISCOUNT_PCT
) -> bool:
    from wallexer.cli.engine_cmds import get_status

    st, live = get_status(settings)
    if live and st and st.get("mode") == "trade":
        console.print("[red]A trade-mode engine is running; stop it before the smoke test.[/]")
        return False
    console.print(
        f"[bold red]LIVE SMOKE TEST[/]: places ONE real LIMIT BUY on BTCTMN for ~100,000 TMN at {discount_pct}% "
        "below the best bid, then cancels it. No retries."
    )
    if console.input("Type SMOKE to proceed: ").strip() != "SMOKE":
        console.print("Not confirmed. Nothing placed.")
        return False

    min_value = settings.cfg.orders.min_order_value_tmn
    s = Smoke()
    conn = init_db(settings.cfg.path(settings.cfg.store.db_path))
    try:
        async with WallexClient(settings) as client:
            try:
                ok = await _steps(client, conn, settings, s, min_value, discount_pct)
            except Exception as e:  # any crash is a failure; then only look up + cancel
                ok = s.add("unexpected error", False, f"{type(e).__name__}: {str(e)[:200]}")
                if s.attempted_cid and not s.placed_cid:
                    try:
                        found = await client.get_order(s.attempted_cid)
                    except WallexError as le:
                        found = None
                        s.add("lookup after crash", False, f"{le} -> check openOrders manually")
                    else:
                        s.add(
                            "lookup after crash",
                            True,
                            "order exists on the exchange"
                            if found
                            else "no order on the exchange (not sent)",
                        )
                    if found is not None and found.active is not False:
                        s.placed_cid = s.attempted_cid
                    elif found is None:
                        repo.update_order(
                            conn, s.attempted_cid, status="not_sent", error="crash before/at POST"
                        )
            if not ok and s.placed_cid:
                # The only recovery action ever taken: cancel. Never a second order.
                try:
                    await client.cancel_order(s.placed_cid)
                    s.add("recovery cancel", True, f"cancelled {s.placed_cid}")
                except WallexError as e:
                    s.add("recovery cancel", False, f"{e} -> cancel manually in the Wallex UI")
    finally:
        log_event(
            conn,
            "smoke.result",
            {
                "ok": all(x.ok for x in s.steps),
                "facts": s.facts,
                "steps": [x.__dict__ for x in s.steps],
            },
        )
        conn.close()
    t = Table(title="Live smoke test", header_style="bold cyan")
    for col in ("Step", "", "Detail"):
        t.add_column(col, overflow="fold")
    for x in s.steps:
        t.add_row(x.name, "[green]✓[/]" if x.ok else "[red]✗[/]", x.detail)
    console.print(t)
    if s.facts.get("rejection"):
        console.print("[bold]Sent body:[/]")
        console.print_json(json.dumps(s.facts["sent"], ensure_ascii=False))
        console.print("[bold]Full error from Wallex:[/]")
        console.print_json(json.dumps(s.facts["rejection"], ensure_ascii=False, default=str))
    if s.facts.get("value_semantics"):
        console.print(f"[bold]value vs locked:[/] {s.facts['value_semantics']}")
    passed = all(x.ok for x in s.steps)
    console.print(
        "[green]SMOKE TEST PASSED[/]"
        if passed
        else "[red]SMOKE TEST FAILED: see steps; nothing was retried[/]"
    )
    return passed


async def _steps(
    client: WallexClient,
    conn: Any,
    settings: Settings,
    s: Smoke,
    min_value: Decimal,
    discount_pct: Decimal = DEFAULT_DISCOUNT_PCT,
) -> bool:
    try:
        settings.require_private()
        bals0 = await client.balances()
    except (WallexError, Exception) as e:  # noqa: BLE001 (report anything, place nothing)
        return s.add("preflight + balances", False, str(e)[:200])
    v0, l0 = _tmn(bals0)
    if not s.add(
        "preflight + balances", v0 + l0 > min_value * 2, f"TMN value {v0:,.0f}, locked {l0:,.0f}"
    ):
        return False

    markets = {m.symbol: m for m in await client.markets()}
    rules = MarketRules.from_market(markets[SYMBOL])
    book = await client.depth(SYMBOL)
    if book.best_bid is None:
        return s.add("book", False, "no bid")
    price = (book.best_bid * (1 - discount_pct / 100)).quantize(
        rules.price_step, rounding=ROUND_FLOOR
    )
    qty = (min_value / price).quantize(rules.qty_step, rounding=ROUND_CEILING)
    notional = qty * price
    if not s.add(
        "order sizing",
        min_value <= notional <= MAX_NOTIONAL,
        f"best bid {book.best_bid:,} → price {price:,} (-{discount_pct}%), qty {qty} BTC, notional {notional:,.0f} TMN",
    ):
        return False

    did = repo.record_decision(
        conn,
        mode="live_smoke",
        action="smoke_buy",
        symbol=SYMBOL,
        strategy="live_smoke",
        signal={
            "best_bid": str(book.best_bid),
            "price": str(price),
            "qty": str(qty),
            "notional": str(notional),
        },
        reason="live smoke test: unfillable LIMIT BUY 20% below best bid, then cancel",
    )
    cid = make_client_id(f"smoke{did}", 1)
    spec = OrderSpec(SYMBOL, Side.BUY, OrderType.LIMIT, qty, price, cid, str(did))
    repo.insert_order(
        conn,
        decision_id=did,
        client_id=cid,
        mode="live_smoke",
        symbol=SYMBOL,
        side="BUY",
        type_="LIMIT",
        price=price,
        quantity=qty,
        status="new",
    )
    s.add("decision row", True, f"decision #{did}, client_id {cid}")

    client.enable_orders()  # second call site: explicit typed confirmation above
    s.attempted_cid = cid
    try:
        order = await client.place_order(spec, quote_asset="TMN")
        s.placed_cid = cid
    except OrderStatusUnknown:
        s.placed_cid = cid  # may exist: lookup said unknown; recovery will try to cancel
        return s.add(
            "place (1 POST, no retry)",
            False,
            "timeout and lookup by client_id found nothing; NOT re-sent",
        )
    except OrderRejected as e:
        detail = e.detail()
        repo.update_order(
            conn,
            cid,
            status="rejected",
            error=json.dumps(detail, ensure_ascii=False, default=str)[:2000],
        )
        s.facts["sent"] = spec.to_body()
        s.facts["rejection"] = detail
        return s.add(
            "place (1 POST, no retry)",
            False,
            f"rejected HTTP {e.status} code {e.code}: {e.message} · result={json.dumps(e.fields, ensure_ascii=False, default=str)}",
        )
    except (ApiError, TransportError) as e:
        return s.add("place (1 POST, no retry)", False, str(e)[:200])
    repo.update_order(conn, cid, status=order.status or "open")
    s.facts["placed_raw"] = order.raw  # exact exchange response, kept for the record
    s.add("place (1 POST, no retry)", True, f"status {order.status}, active {order.active}")

    got = await client.get_order(cid)
    s.add(
        "get_order(client_id)",
        got is not None and got.active is not False and got.price == price,
        f"active {got and got.active}, price {got and got.price}, origQty {got and got.orig_qty}",
    )
    await asyncio.sleep(1)
    opens = await client.open_orders(SYMBOL)
    listed = any(o.client_order_id == cid for lst in opens.values() for o in lst)
    s.add(
        "openOrders lists it",
        listed,
        f"{sum(len(v) for v in opens.values())} open order(s) on {SYMBOL}",
    )

    v1, l1 = _tmn(await client.balances())
    dv, dl = v0 - v1, l1 - l0
    tol = notional * Decimal("0.02") + 1
    if abs(dl - notional) <= tol and abs(dv - notional) <= tol:
        sem = "value = FREE balance (value drops, locked rises): equity = value + locked is correct"
        s.facts["value_excludes_locked"] = True
    elif abs(dl - notional) <= tol and abs(dv) <= tol:
        sem = "value = TOTAL balance (unchanged, locked rises): equity must use value only"
        s.facts["value_excludes_locked"] = False
    else:
        sem = f"unclear: value Δ {-dv:,.0f}, locked Δ {dl:+,.0f} vs notional {notional:,.0f}"
    s.facts["value_semantics"] = sem
    s.add(
        "balances while open",
        "unclear" not in sem,
        f"value {v0:,.0f}→{v1:,.0f}, locked {l0:,.0f}→{l1:,.0f}",
    )

    try:
        await client.cancel_order(cid)
        s.add("cancel_order(client_id)", True, "DELETE sent")
    except WallexError as e:
        return s.add("cancel_order(client_id)", False, str(e)[:200])
    await asyncio.sleep(1)
    after = await client.get_order(cid)
    s.placed_cid = None if after is not None and after.active is False else cid
    repo.update_order(
        conn,
        cid,
        status=(after.status if after else "unknown") or "cancelled",
        executed_qty=after.executed_qty if after else None,
    )
    s.add(
        "order inactive after cancel",
        after is not None and after.active is False,
        f"status {after and after.status}, executed {after and after.executed_qty}",
    )
    opens2 = await client.open_orders(SYMBOL)
    s.add(
        "gone from openOrders",
        not any(o.client_order_id == cid for lst in opens2.values() for o in lst),
        "",
    )
    v2, l2 = _tmn(await client.balances())
    s.add(
        "balances restored",
        v2 == v0 and l2 == l0,
        f"value {v2:,.0f} (was {v0:,.0f}), locked {l2:,.0f} (was {l0:,.0f})",
    )

    found = await client.get_order(cid)
    missing = await client.get_order("wx-smoke-never-placed-1")
    s.add(
        "timeout lookup path",
        found is not None and missing is None,
        "existing client_id found; never-placed client_id → None (404) → would report 'unknown', never re-send",
    )
    return all(x.ok for x in s.steps)
