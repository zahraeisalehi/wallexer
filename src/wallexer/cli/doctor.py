"""`wallexer doctor`: environment and exchange checks. Read-only: never places or cancels orders."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

from wallexer.cli.fmt import human, pct
from wallexer.config import ConfigError, Settings
from wallexer.exchange.account import check_trading_credit
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, TransportError
from wallexer.exchange.prop import fetch_package, save_unavailable
from wallexer.exchange.universe import load_not_allowed, market_allowed
from wallexer.store.db import init_db
from wallexer.store.repo import has_live_history

OK, WARN, FAIL, SKIP = "[green]✓[/]", "[yellow]⚠[/]", "[red]✗[/]", "[dim]–[/]"
NET_ERRORS = (ApiError, TransportError)


@dataclass
class Row:
    name: str
    status: str
    detail: str


async def _measure_window(
    client: WallexClient, timeout_s: float, step_s: float = 2.0
) -> tuple[str, str]:
    """Poll a cheap public endpoint and watch x-ratelimit-remaining for resets.

    One reset tells us the window phase; two resets give the window length.
    """
    samples: list[tuple[float, int]] = []
    resets: list[float] = []  # full resets: remaining jumps back near the limit
    recoveries = 0  # small increases: requests ageing out of a sliding window
    limit = 0
    start = time.monotonic()
    while time.monotonic() - start < timeout_s and len(resets) < 2:
        try:
            await client.trades("USDTTMN")
        except NET_ERRORS:
            await asyncio.sleep(step_s)
            continue
        h = client.last_headers or {}
        rem, lim = h.get("x-ratelimit-remaining"), h.get("x-ratelimit-limit")
        if rem is not None and lim is not None:
            now, rem_i, limit = time.monotonic(), int(rem), int(lim)
            if samples and rem_i > samples[-1][1]:
                if rem_i >= limit * 0.95 and samples[-1][1] < limit * 0.95:
                    resets.append(now)
                else:
                    recoveries += 1
            samples.append((now, rem_i))
        await asyncio.sleep(step_s)
    if not samples:
        return WARN, "no x-ratelimit-remaining header seen"
    span = samples[-1][0] - samples[0][0]
    lo, hi = min(r for _, r in samples), max(r for _, r in samples)
    if len(resets) >= 2:
        w = resets[1] - resets[0]
        return (
            OK,
            f"fixed window ≈ {w:.0f}s (two full resets; ±{step_s:.0f}s). Set ratelimit.market.window_s",
        )
    if recoveries:
        return WARN, (
            f"looks like a sliding window: remaining {lo}–{hi} of {limit} over {span:.0f}s, "
            f"{recoveries} gradual recoveries, no full reset. Keeping config window"
        )
    return (
        WARN,
        f"no reset in {span:.0f}s (remaining {samples[0][1]}→{samples[-1][1]}); window > {span:.0f}s",
    )


async def _check_llm(settings: Settings) -> list[Row]:
    key = settings.secrets.boxapi_api_key
    if key is None or not key.get_secret_value():
        return [Row("BoxAPI", FAIL, "BOXAPI_API_KEY not set")]
    from openai import AsyncOpenAI

    lcfg = settings.cfg.llm
    llm = AsyncOpenAI(
        base_url=lcfg.base_url,
        api_key=key.get_secret_value(),
        timeout=lcfg.timeout_s,
        max_retries=0,
    )
    msgs = [
        {"role": "system", "content": 'Reply with JSON only: {"ok": true}'},
        {"role": "user", "content": "ping"},
    ]
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "ping",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        },
    }
    variants = [
        ("temperature=0", {"temperature": 0}),
        ("json_object", {"temperature": 0, "response_format": {"type": "json_object"}}),
        ("json_schema", {"temperature": 0, "response_format": schema}),
    ]
    rows: list[Row] = []
    try:
        for label, extra in variants:
            t0 = time.monotonic()
            try:
                resp = await llm.chat.completions.create(model=lcfg.model, messages=msgs, **extra)
                ms = (time.monotonic() - t0) * 1000
                text = (resp.choices[0].message.content or "").strip()
                try:
                    parsed_ok = json.loads(text).get("ok") is True
                except (ValueError, AttributeError):
                    parsed_ok = False
                status = OK if parsed_ok else WARN
                rows.append(
                    Row(
                        f"BoxAPI {label}", status, f"{lcfg.model}, {ms:.0f} ms, reply {text[:40]!r}"
                    )
                )
            except Exception as e:  # report any gateway/SDK failure as a check result
                rows.append(Row(f"BoxAPI {label}", FAIL, f"{type(e).__name__}: {str(e)[:90]}"))
    finally:
        await llm.close()
    return rows


async def run(
    settings: Settings, console: Console, *, quick: bool = False, check_llm: bool = True
) -> bool:
    rows: list[Row] = []
    cfg = settings.cfg

    # Local setup
    forbidden: frozenset[str] = frozenset()
    try:
        forbidden = load_not_allowed(cfg.path(cfg.universe.not_allowed_path))
        rows.append(Row("not_allowed list", OK, f"{len(forbidden)} forbidden markets"))
    except FileNotFoundError as e:
        rows.append(Row("not_allowed list", FAIL, str(e)))
    first_start = True
    try:
        conn = init_db(cfg.path(cfg.store.db_path))
        first_start = not has_live_history(conn)
        conn.close()
        rows.append(Row("SQLite (WAL)", OK, cfg.store.db_path))
    except Exception as e:
        rows.append(Row("SQLite (WAL)", FAIL, str(e)[:90]))
    end = settings.secrets.event_end_utc
    rows.append(
        Row(
            "EVENT_END_UTC",
            OK if end else WARN,
            end.isoformat() if end else "not set: no auto-flatten",
        )
    )
    rows.append(Row("mode", OK, str(settings.mode)))

    async with WallexClient(settings) as client:
        # 1. base URL
        t0 = time.monotonic()
        try:
            mk = await client.markets()
            ms = (time.monotonic() - t0) * 1000
            rows.append(
                Row("base URL", OK, f"{cfg.exchange.base_url}: {len(mk)} markets, {ms:.0f} ms")
            )
            allowed = [m for m in mk if m.is_spot and market_allowed(m, forbidden)]
            no_market = sum(1 for m in allowed if not m.market_orders_enabled)
            rows.append(
                Row(
                    "MARKET-disabled markets",
                    OK,
                    f"{no_market} of {len(allowed)} allowed spot markets: exits use aggressive LIMIT",
                )
            )
        except NET_ERRORS as e:
            rows.append(Row("base URL", FAIL, str(e)[:100]))

        # 5. clock skew (Date header has 1 s resolution). Use an uncached endpoint.
        client.last_server_time = None
        with contextlib.suppress(*NET_ERRORS):
            await client.trades("USDTTMN")
        if client.last_server_time and client.last_local_time:
            skew = client.last_local_time - client.last_server_time
            st = OK if abs(skew) < 2 else WARN if abs(skew) < 10 else FAIL
            rows.append(Row("clock skew", st, f"local − server ≈ {skew:+.1f}s (Date header, ±1s)"))
        else:
            rows.append(Row("clock skew", WARN, "no Date header"))

        # 2/3/4. private: keys, balances with sub-account header, trading credit, fees
        try:
            settings.require_private()
            rows.append(Row("API key + sub-account id", OK, "set (values not shown)"))
            private = True
        except ConfigError as e:
            rows.append(Row("API key + sub-account id", FAIL, str(e)))
            private = False
        if private:
            try:
                credit = await check_trading_credit(client, settings, first_start=first_start)
                rows.append(Row("balances (sub-account header)", OK, "authenticated"))
                held = ", ".join(f"{a} {human(v)}" for a, v in credit.assets_tmn.items()) or "none"
                label = "TMN ≈ 5M (first start)" if first_start else "equity vs 5M (later start)"
                rows.append(
                    Row(
                        label,
                        OK if credit.ok else FAIL,
                        f"{credit.message}; coins: {held}"
                        + (f"; locked: {', '.join(credit.locked)}" if credit.locked else ""),
                    )
                )
            except NET_ERRORS as e:
                rows.append(Row("balances (sub-account header)", FAIL, str(e)[:100]))
                rows.append(Row("trading credit ≈ 5M TMN", FAIL, "balance check failed"))
            try:
                fees = await client.fees()
                if fees.source == "default":
                    rows.append(
                        Row(
                            "fee table",
                            WARN,
                            f"no usable data (shape {fees.raw_shape}); using config default "
                            f"maker {pct(fees.default.maker)} taker {pct(fees.default.taker)}",
                        )
                    )
                else:
                    sample = fees.rates.get("BTCTMN") or next(iter(fees.rates.values()))
                    zero = sum(1 for f in fees.rates.values() if f.maker == 0 and f.taker == 0)
                    detail = (
                        f"{len(fees.rates)} markets; BTCTMN maker {pct(sample.maker)} "
                        f"taker {pct(sample.taker)}; zero-fee: {zero}"
                    )
                    if fees.skipped:
                        detail += f"; {len(fees.skipped)} unusable → default: " + ", ".join(
                            sorted(fees.skipped)[:5]
                        )
                    rows.append(Row("fee table", OK if not fees.skipped else WARN, detail))
            except NET_ERRORS as e:
                rows.append(Row("fee table", FAIL, str(e)[:100]))
            try:
                pkg = await fetch_package(client, settings)
                save_unavailable(cfg.path(cfg.universe.unavailable_path), pkg)
                rows.append(
                    Row(
                        "trading-credit package",
                        OK,
                        f"id {pkg.id} '{pkg.title}' {pkg.status}; loan {pkg.loan:,} {pkg.currency}; "
                        f"warning_limit {pkg.warning_limit} / liquidity_limit {pkg.liquidity_limit} TMN",
                    )
                )
                extra = sorted(pkg.unavailable - forbidden)
                rows.append(
                    Row(
                        "unavailable markets",
                        OK,
                        f"{len(pkg.unavailable)} (saved to {cfg.universe.unavailable_path}); "
                        f"beyond not_allowed: {', '.join(extra) or 'none'}",
                    )
                )
            except (*NET_ERRORS, ConfigError) as e:
                rows.append(Row("trading-credit package", FAIL, str(e)[:120]))
        else:
            for name in ("balances (sub-account header)", "trading credit ≈ 5M TMN", "fee table"):
                rows.append(Row(name, SKIP, "needs API key + sub-account id"))

        # 7. /depth/all
        try:
            books = await client.depth_all()
            rows.append(
                Row("/depth/all", OK, f"{len(books)} books{' (with key)' if private else ''}")
            )
        except NET_ERRORS as e:
            rows.append(
                Row("/depth/all", WARN, f"failed: {str(e)[:70]} → universe uses /v1/depth fallback")
            )

        # 6. rate-limit headers + window
        try:
            await client.trades("USDTTMN")
            h = client.last_headers or {}
            lim, rem = h.get("x-ratelimit-limit"), h.get("x-ratelimit-remaining")
            rows.append(
                Row("rate-limit headers", OK if lim else WARN, f"limit {lim}, remaining {rem}")
            )
        except NET_ERRORS as e:
            rows.append(Row("rate-limit headers", FAIL, str(e)[:100]))
        if quick:
            rows.append(
                Row(
                    "rate-limit window",
                    SKIP,
                    f"--quick; using config {cfg.ratelimit.market.window_s:.0f}s",
                )
            )
        else:
            with console.status("Measuring rate-limit window (up to ~2 min)…"):
                st, detail = await _measure_window(client, timeout_s=130)
            rows.append(Row("rate-limit window", st, detail))
        o = cfg.ratelimit.orders
        rows.append(
            Row(
                "order limiter",
                OK,
                f"{o.max_requests} create+cancel / {o.window_s:.0f}s (exchange cap 20/10s)",
            )
        )

    # 8. min order value
    oc = cfg.orders
    if oc.min_order_verified:
        rows.append(Row("min order value", OK, f"{oc.min_order_value_tmn:,} TMN (verified)"))
    else:
        rows.append(
            Row(
                "min order value",
                WARN,
                f"{oc.min_order_value_tmn:,} TMN is an UNVERIFIED placeholder; learned on first too-small reject",
            )
        )

    # 9. BoxAPI
    if check_llm:
        with console.status("Checking BoxAPI…"):
            rows.extend(await _check_llm(settings))
    else:
        rows.append(Row("BoxAPI", SKIP, "--no-llm"))

    t = Table(title="wallexer doctor", header_style="bold cyan")
    t.add_column("Check")
    t.add_column("", justify="center")
    t.add_column("Detail", overflow="fold")
    for r in rows:
        t.add_row(r.name, r.status, r.detail)
    console.print(t)
    failed = [r for r in rows if r.status == FAIL]
    if failed:
        console.print(
            f"[red]{len(failed)} check(s) failed.[/] Fix these before running shadow/trade."
        )
    else:
        console.print("[green]All required checks passed.[/]")
    return not failed
