"""Engine daemon: one asyncio process.

feed -> features -> strategies (each on its own paper book, always) -> promotion gate + allocator
-> account rebalancer -> decision row -> risk gate -> executor (shadow | live).

Startup: preflight (key + sub-account; ~5M TMN on first start) -> trading-credit package
restrictions -> fee table -> universe -> balances/open orders from the exchange -> 3-day candle
replay (backtest stats) -> run. Default stance is flat: account orders only follow promoted
strategies, and none is promoted until it has proven edge on paper.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from wallexer.alloc import promotion
from wallexer.alloc.allocator import Allocation, NullAgent, allocate
from wallexer.alloc.regime import classify
from wallexer.alloc.usdt_inventory import decide as usdt_decide
from wallexer.alloc.usdt_inventory import target_frac as usdt_target
from wallexer.config import ConfigError, Mode, Settings
from wallexer.data.features import Features, basis_features, imbalance, market_features
from wallexer.data.feed import MarketFeed
from wallexer.engine import control, health
from wallexer.engine.replay import replay
from wallexer.exchange.account import CreditCheck, PreflightError, preflight_live
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, TransportError
from wallexer.exchange.models import Side
from wallexer.exchange.prop import fetch_package, load_unavailable, save_unavailable
from wallexer.exchange.rules import (
    MarketRules,
    OrderRuleError,
    OrderSpec,
    build_limit_order,
    choose_exit_order,
    exit_quantity,
)
from wallexer.exchange.universe import Universe, build_universe, load_not_allowed
from wallexer.execution.base import AccountSnapshot
from wallexer.execution.live import LiveExecutor
from wallexer.execution.paper import FillModel, PaperBook, PaperTrade
from wallexer.execution.shadow import ShadowExecutor
from wallexer.risk import gate
from wallexer.risk.flatten import plan_flatten
from wallexer.risk.guardrails import Limits
from wallexer.risk.schedule import Phase, phase
from wallexer.store import repo
from wallexer.store.db import event_sink, init_db, log_event
from wallexer.strategies.base import Strategy
from wallexer.strategies.basis_tmn_usdt import BasisTmnUsdt
from wallexer.strategies.mean_reversion import MeanReversion
from wallexer.strategies.momentum import Momentum

log = structlog.get_logger(__name__)
ZERO = Decimal(0)
TMN = "TMN"


class Engine:
    def __init__(self, settings: Settings, mode: Mode, markets: list[str] | None = None) -> None:
        if mode is Mode.backtest:
            raise ConfigError("engine runs shadow or trade; backtest is a separate command")
        self.settings = settings
        self.mode = mode
        self.only = [m.upper() for m in markets] if markets else None
        self.run_id = f"{mode.value}-{datetime.now(UTC):%Y%m%dT%H%M%S}"
        cfg = settings.cfg
        self.conn: sqlite3.Connection = init_db(cfg.path(cfg.store.db_path))
        self.sink = event_sink(self.conn)
        self.client = WallexClient(settings, on_event=self.sink)
        self.started = time.time()
        self.stopping = asyncio.Event()
        self.last_tick: float | None = None
        self.errors = 0
        self.event_end = (
            settings.secrets.event_end_utc.timestamp() if settings.secrets.event_end_utc else None
        )
        # risk state
        self.hard_stopped = False
        self.soft_until = 0.0
        self.risk_multiplier = Decimal(1)
        self.paused: set[str] = set()
        self.peak_equity = cfg.account.expected_equity_tmn
        self.day = datetime.now(UTC).date()
        self.day_start_equity = cfg.account.expected_equity_tmn
        self.equity = ZERO
        self.account: AccountSnapshot = AccountSnapshot()
        self.flatten_requested: set[str] | None = None  # None = no; empty set = all
        self.allocation: Allocation | None = None
        self.statuses: dict[str, promotion.PromotionStatus] = {}
        self.regime: dict[str, float] = {}
        self.last_features: Features | None = None
        self.usdt_last_conversion: float | None = None
        self.usdt_cost_tmn = ZERO  # account USDT cost basis (usdt_inventory scoreboard line)
        self.dust: list[dict[str, str]] = []
        self.replay_info: dict[str, Any] = {}
        self.credit: CreditCheck | None = None

    # ================================================================== startup

    def _new_book(self, name: str, source: str) -> PaperBook:
        p, o = self.settings.cfg.paper, self.settings.cfg.orders
        return PaperBook(
            name,
            capital_tmn=p.capital_tmn,
            position_frac=p.position_frac,
            fill=self.fill,
            rules=self.all_rules,
            min_entry_tmn=o.min_order_value_tmn * o.min_entry_multiple,
            maker_timeout_s=p.maker_timeout_s,
            usdt_min_hold_s=self.settings.cfg.alloc.usdt_min_hold_min * 60,
            source=source,
        )

    async def startup(self) -> None:
        s, cfg = self.settings, self.settings.cfg
        first = not repo.has_live_history(self.conn)
        self.credit = await preflight_live(self.client, s, self.mode, first_start=first)
        log_event(
            self.conn,
            "engine.preflight",
            {"mode": self.mode.value, "first_start": first, "msg": self.credit.message},
        )
        log.info("engine.preflight", msg=self.credit.message, first_start=first)
        if self.event_end is None:
            log_event(self.conn, "engine.no_event_end", {})
        # trading-credit package restrictions
        upath = cfg.path(cfg.universe.unavailable_path)
        try:
            pkg = await fetch_package(self.client, s)
            save_unavailable(upath, pkg)
            self.unavailable = pkg.unavailable
            self.package = {"id": pkg.id, "title": pkg.title, "unavailable": len(pkg.unavailable)}
        except (ApiError, TransportError, ConfigError) as e:
            self.unavailable = load_unavailable(upath)
            if not self.unavailable:
                raise PreflightError(
                    f"Cannot start: trading-credit package restrictions unavailable: {e}"
                ) from e
            self.package = {"id": None, "error": str(e)[:120], "unavailable": len(self.unavailable)}
            log_event(self.conn, "engine.package_fallback", self.package)
        self.forbidden = load_not_allowed(cfg.path(cfg.universe.not_allowed_path))
        self.fees = await self.client.fees()
        self.universe: Universe = await build_universe(
            self.client, s, fees=self.fees, unavailable=self.unavailable, only=self.only
        )
        symbols = self.universe.symbols
        if "USDTTMN" not in symbols:
            symbols.append("USDTTMN")  # needed for USDT valuation and the basis triangle
        if not symbols:
            raise PreflightError("Cannot start: empty universe")
        self.symbols = symbols
        all_markets = await self.client.markets()
        self.markets_by_symbol = {m.symbol: m for m in all_markets if m.is_spot}
        self.all_rules = {
            sym: MarketRules.from_market(m) for sym, m in self.markets_by_symbol.items()
        }
        self.quotes = {sym: self.all_rules[sym].quote_asset for sym in symbols}
        self.limits = Limits.from_settings(
            s,
            forbidden=self.forbidden,
            unavailable=self.unavailable,
            limit_only=self.universe.limit_only,
        )
        self.fill = FillModel(cfg.paper.slippage_bps)
        self.feed = MarketFeed(self.client, self.conn, s, symbols)
        log_event(self.conn, "engine.feed_plan", self.feed.plan_intervals())
        await self.feed.poll_depth()
        await self.feed.load_history(cfg.engine.replay_days)
        # strategies
        sc, g = cfg.strategies, cfg.guardrails
        all_strats: list[Strategy] = [
            BasisTmnUsdt(sc.basis_tmn_usdt),
            MeanReversion(sc.mean_reversion, g),
            Momentum(sc.momentum, g),
        ]
        self.strategies = [x for x in all_strats if getattr(sc, x.name).enabled]
        # backtest replay -> seed promotion stats
        t0 = time.time()
        bt_books = {x.name: self._new_book(x.name, "backtest") for x in self.strategies}
        spreads = {sym: self.feed.spread_bps(sym) or Decimal(0) for sym in symbols}
        steps = replay(
            self.strategies,
            bt_books,
            self.feed.bars,
            spreads,
            self.quotes,
            self.fees,
            vwap_bars=sc.mean_reversion.vwap_bars,
            breakout_bars=sc.momentum.breakout_bars,
            atr_bars=sc.momentum.atr_bars,
            bases=BasisTmnUsdt.bases,
        )
        self.bt_trades: dict[str, list[PaperTrade]] = {n: b.closed for n, b in bt_books.items()}
        for trades in self.bt_trades.values():
            for t in trades:
                repo.insert_paper_trade(self.conn, t, self.run_id)
        self.replay_info = {
            "days": cfg.engine.replay_days,
            "steps": steps,
            "seconds": round(time.time() - t0, 1),
            "bars": {sym: len(b) for sym, b in self.feed.bars.items()},
            "skipped": {n: b.skipped for n, b in bt_books.items()},
        }
        log_event(self.conn, "engine.replay", self.replay_info)
        self.books = {x.name: self._new_book(x.name, "shadow") for x in self.strategies}
        # account / executor from exchange truth
        bals = await self.client.balances()
        start = {a: b.value for a, b in bals.items() if b.value > 0}
        base_of = {sym: r.base_asset for sym, r in self.all_rules.items()}
        if self.mode is Mode.trade:
            self.executor = LiveExecutor(
                s, self.client, self.conn, self.all_rules, lambda: self._usdt_bid(), self.mode
            )
            await self.executor.reconcile_open_orders()
        else:
            self.executor = ShadowExecutor(self.conn, self.fees, self.fill, start, base_of)
        try:
            remote_open = await self.client.open_orders()
            n_open = sum(len(v) for v in remote_open.values())
        except (ApiError, TransportError):
            n_open = -1
        log_event(
            self.conn,
            "engine.reconcile_start",
            {"open_orders_on_exchange": n_open, "assets": sorted(start)},
        )
        self.account = await self.executor.snapshot()
        self._evaluate_promotions()
        self.equity = self._account_equity()
        self.peak_equity = max(self.peak_equity, self.equity)
        self.day_start_equity = self.equity
        log_event(
            self.conn,
            "engine.started",
            {"run_id": self.run_id, "mode": self.mode.value, "symbols": symbols},
        )

    # ================================================================== features

    def _usdt_bid(self) -> Decimal | None:
        b = self.feed.books.get("USDTTMN")
        return b.best_bid if b else None

    def features(self, now: float) -> Features:
        sc = self.settings.cfg.strategies
        markets = {}
        for sym in self.symbols:
            book = self.feed.books.get(sym)
            if book is None or book.best_bid is None or book.best_ask is None:
                continue
            issue = self.feed.issue(sym, now)
            fee = self.fees.get(sym)
            markets[sym] = market_features(
                sym,
                self.quotes[sym],
                self.feed.bars.get(sym, [])[-130:],
                ts=now,
                bid=book.best_bid,
                ask=book.best_ask,
                maker_fee=fee.maker,
                taker_fee=fee.taker,
                book_issue=issue,
                book_imbalance=imbalance(book),
                vwap_bars=sc.mean_reversion.vwap_bars,
                breakout_bars=sc.momentum.breakout_bars,
                atr_bars=sc.momentum.atr_bars,
            )
        return Features(now, markets, basis_features(markets, BasisTmnUsdt.bases), self._usdt_bid())

    # ================================================================== account valuation

    def _price_tmn(self, asset: str) -> Decimal | None:
        if asset == TMN:
            return Decimal(1)
        b = self.feed.books.get(f"{asset}TMN")
        if b and b.best_bid:
            return b.best_bid
        u, ub = self.feed.books.get(f"{asset}USDT"), self._usdt_bid()
        if u and u.best_bid and ub:
            return u.best_bid * ub
        return None

    def _account_equity(self) -> Decimal:
        total = ZERO
        for asset in set(self.account.free) | set(self.account.locked):
            qty = self.account.total(asset)
            if qty == 0:
                continue
            px = self._price_tmn(asset)
            total += qty * px if px is not None else ZERO
        return total

    def _positions_tmn(self) -> dict[str, Decimal]:
        """Account holdings per universe market (base asset valued in TMN)."""
        out: dict[str, Decimal] = {}
        for sym in self.symbols:
            r = self.all_rules.get(sym)
            if r is None or r.base_asset == TMN:
                continue
            qty = self.account.total(r.base_asset)
            px = self._price_tmn(r.base_asset)
            if qty > 0 and px:
                out[sym] = qty * px
        return out

    def _risk_state(self, now: float) -> gate.RiskState:
        held = {
            sym: self.account.free.get(r.base_asset, ZERO)
            for sym, r in self.all_rules.items()
            if sym in self.symbols
        }
        return gate.RiskState(
            now=now,
            equity_tmn=self.equity,
            peak_equity_tmn=self.peak_equity,
            day_start_equity_tmn=self.day_start_equity,
            positions_tmn=self._positions_tmn(),
            held_qty=held,
            open_orders=len(self.executor.open_orders),
            usdt_tmn=self._usdt_bid(),
            event_end=self.event_end,
            hard_stopped=self.hard_stopped,
            soft_stop_until=self.soft_until,
            paused=frozenset(self.paused),
        )

    # ================================================================== tick

    async def tick(self) -> None:
        now = time.time()
        f = self.features(now)
        self.last_features = f
        trades = self.feed.consume_trades()
        for strat in self.strategies:
            book = self.books[strat.name]
            try:
                sigs = strat.on_tick(f, book.view(), now)
            except Exception as e:  # one strategy bug must not stop the engine
                self.errors += 1
                log.exception("strategy.error", strategy=strat.name, error=str(e))
                continue
            for t in book.step(sigs, f, now, trades=trades):
                repo.insert_paper_trade(self.conn, t, self.run_id)
        await self.executor.on_market(self.feed.books, trades, now)
        if isinstance(self.executor, ShadowExecutor):
            self.account = await self.executor.snapshot()
        self.equity = self._account_equity()
        self._risk_monitor(now)
        await self._cancel_stale_entries(now)
        if self.flatten_requested is not None:
            await self.flatten(self.flatten_requested, reason="requested")
        elif not self.hard_stopped:
            await self.rebalance(f, now)
        self.last_tick = now

    def _risk_monitor(self, now: float) -> None:
        g = self.settings.cfg.guardrails
        today = datetime.now(UTC).date()
        if today != self.day:
            self.day, self.day_start_equity = today, self.equity
        self.peak_equity = max(self.peak_equity, self.equity)
        st = self._risk_state(now)
        total_dd, daily_dd = gate.drawdowns(st)
        if not self.hard_stopped and total_dd >= g.total_dd_hard_pct:
            self.hard_stopped = True
            self.flatten_requested = set()
            log_event(
                self.conn,
                "risk.hard_stop",
                {"drawdown_pct": f"{total_dd:.2f}", "equity": str(self.equity)},
            )
            # Effectively shadow from here: account entries stay blocked (rebalance is skipped
            # while hard_stopped), paper books keep running. Resuming needs a human restart.
            log_event(
                self.conn, "risk.entries_blocked_until_human_resume", {"mode": self.mode.value}
            )
        if daily_dd >= g.daily_dd_soft_pct and now >= self.soft_until:
            self.soft_until = now + g.soft_stop_pause_min * 60
            self.risk_multiplier = Decimal("0.5")
            log_event(self.conn, "risk.soft_stop", {"daily_drawdown_pct": f"{daily_dd:.2f}"})
        p = phase(now, self.event_end, g.flatten_no_entry_min * 60, g.flatten_force_min * 60)
        if (
            p in (Phase.flatten, Phase.ended)
            and self.flatten_requested is None
            and self._positions_tmn()
        ):
            self.flatten_requested = set()
            log_event(self.conn, "schedule.force_flatten", {"phase": p.value})

    async def _cancel_stale_entries(self, now: float) -> None:
        timeout = self.settings.cfg.paper.maker_timeout_s
        for cid, o in list(self.executor.open_orders.items()):
            if not o.is_exit and now - o.ts >= timeout:
                await self.executor.cancel(cid)

    # ================================================================== account orders

    def _targets(self, f: Features) -> dict[str, Decimal]:
        """Target fraction of account equity per market = sum of weight x paper position fraction."""
        out: dict[str, Decimal] = {}
        if self.allocation is None:
            return out
        for name, w in self.allocation.final.items():
            if w <= 0 or name in self.settings.cfg.promotion.paper_only:
                continue
            book = self.books.get(name)
            if book is None:
                continue
            eq = book.equity(f)
            if eq <= 0:
                continue
            for sym, lot in book.lots.items():
                px = self._price_tmn(self.all_rules[sym].base_asset) or ZERO
                out[sym] = out.get(sym, ZERO) + w * lot.pos.qty * px / eq
        return out

    async def _submit(self, decision_id: int, spec: OrderSpec, quote: str, is_exit: bool) -> None:
        repo.insert_order(
            self.conn,
            decision_id=decision_id,
            client_id=spec.client_id,
            mode=self.executor.mode,
            symbol=spec.symbol,
            side=spec.side.value,
            type_=spec.type.value,
            price=spec.price,
            quantity=spec.quantity,
            status="new",
        )
        res = await self.executor.submit(
            spec, quote=quote, book=self.feed.books.get(spec.symbol), is_exit=is_exit
        )
        if res.status == "rejected":
            log_event(
                self.conn, "order.rejected", {"client_id": spec.client_id, "error": res.error}
            )

    async def rebalance(self, f: Features, now: float) -> None:
        targets = self._targets(f)
        positions = self._positions_tmn()
        if not targets and not positions:
            return  # flat by design
        o = self.settings.cfg.orders
        busy = {x.spec.symbol for x in self.executor.open_orders.values()}
        for sym in sorted(set(targets) | set(positions)):
            m = f.markets.get(sym)
            r = self.all_rules.get(sym)
            if m is None or r is None or sym in busy or not m.book_ok:
                continue
            tgt = targets.get(sym, ZERO) * self.equity * self.risk_multiplier
            cur = positions.get(sym, ZERO)
            delta = tgt - cur
            px_tmn = self._price_tmn(r.base_asset) or ZERO
            if px_tmn <= 0:
                continue
            contributing = {
                n: str(w)
                for n, w in (self.allocation.final.items() if self.allocation else [])
                if sym in self.books[n].lots
            }
            if delta >= o.min_order_value_tmn * o.min_entry_multiple:
                intent = gate.Intent(sym, Side.BUY, delta / px_tmn, m.bid, is_exit=False)
                res = gate.check(intent, self._risk_state(now), self.limits, r)
                did = repo.record_decision(
                    self.conn,
                    mode=self.mode.value,
                    run_id=self.run_id,
                    symbol=sym,
                    action="buy" if res.approved else "reject",
                    features=_feat_log(m),
                    weights=contributing,
                    risk_checks=res.as_log(),
                    signal={"target_tmn": f"{tgt:.0f}", "current_tmn": f"{cur:.0f}"},
                    reason="follow promoted strategies' paper positions"
                    if res.approved
                    else res.reason,
                )
                if not res.approved:
                    continue
                try:
                    spec = build_limit_order(
                        r,
                        Side.BUY,
                        res.qty,
                        m.bid,
                        decision_id=did,
                        n=1,
                        min_value_tmn=o.min_order_value_tmn,
                        usdt_tmn=self._usdt_bid(),
                        entry=True,
                        entry_multiple=o.min_entry_multiple,
                    )
                except OrderRuleError as e:
                    log_event(
                        self.conn, "order.build_failed", {"decision_id": did, "error": str(e)}
                    )
                    continue
                await self._submit(did, spec, r.quote_asset, is_exit=False)
            elif cur > 0 and (tgt <= 0 or -delta >= o.min_order_value_tmn):
                held = self.account.free.get(r.base_asset, ZERO)
                want = held if tgt <= 0 else min(held, -delta / px_tmn)
                qty = exit_quantity(
                    r,
                    held,
                    want,
                    m.bid,
                    min_value_tmn=o.min_order_value_tmn,
                    usdt_tmn=self._usdt_bid(),
                )
                if qty <= 0:
                    continue
                res = gate.check(
                    gate.Intent(sym, Side.SELL, qty, m.bid, is_exit=True),
                    self._risk_state(now),
                    self.limits,
                    r,
                )
                did = repo.record_decision(
                    self.conn,
                    mode=self.mode.value,
                    run_id=self.run_id,
                    symbol=sym,
                    action="sell",
                    features=_feat_log(m),
                    weights=contributing,
                    risk_checks=res.as_log(),
                    signal={"target_tmn": f"{tgt:.0f}", "current_tmn": f"{cur:.0f}"},
                    reason="reduce to target",
                )
                if not res.approved:
                    continue
                try:
                    spec = choose_exit_order(
                        r,
                        Side.SELL,
                        res.qty,
                        self.feed.books[sym],
                        decision_id=did,
                        n=1,
                        slippage_pct=o.exit_limit_slippage_pct,
                    )
                except OrderRuleError as e:
                    log_event(
                        self.conn, "order.build_failed", {"decision_id": did, "error": str(e)}
                    )
                    continue
                await self._submit(did, spec, r.quote_asset, is_exit=True)

    async def flatten(self, markets: set[str], reason: str) -> dict[str, Any]:
        """Sell every non-TMN balance (or only the bases of `markets`) to TMN. Re-planned each tick."""
        o = self.settings.cfg.orders
        for cid, oo in list(self.executor.open_orders.items()):
            if not oo.is_exit and (not markets or oo.spec.symbol in markets):
                await self.executor.cancel(cid)
        if any(x.is_exit for x in self.executor.open_orders.values()):
            return {"pending_exits": len(self.executor.open_orders)}
        self.account = await self.executor.snapshot()
        only = (
            frozenset(self.all_rules[m].base_asset for m in markets if m in self.all_rules)
            if markets
            else None
        )
        did = repo.record_decision(
            self.conn,
            mode=self.mode.value,
            run_id=self.run_id,
            action="flatten",
            signal={"markets": sorted(markets) or "all"},
            reason=reason,
        )
        books = dict(self.feed.books)
        held = {a: q for a, q in self.account.free.items() if a != TMN and q > 0}
        for a in held:  # books for held assets outside the universe
            for sym in (f"{a}TMN", f"{a}USDT"):
                if sym in self.all_rules and sym not in books:
                    with contextlib.suppress(ApiError, TransportError):
                        books[sym] = await self.client.depth(sym)
        blocked = frozenset(
            s for s in self.all_rules if s in self.forbidden or s in self.unavailable
        )
        plan = plan_flatten(
            self.account.free,
            books,
            self.all_rules,
            decision_id=did,
            dust_cap_tmn=o.dust_cap_tmn,
            slippage_pct=o.exit_limit_slippage_pct,
            blocked=blocked,
            only_assets=only,
        )
        for d in plan.dust:
            if d.asset not in {x["asset"] for x in self.dust}:
                entry = {
                    "asset": d.asset,
                    "qty": str(d.qty),
                    "value_tmn": f"{d.value_tmn:.0f}",
                    "why": d.why,
                }
                self.dust.append(entry)
                log_event(self.conn, "flatten.dust_accepted", entry)
        if plan.unroutable:
            log_event(self.conn, "flatten.unroutable", {"assets": plan.unroutable})
        for spec in plan.orders:
            await self._submit(did, spec, self.all_rules[spec.symbol].quote_asset, is_exit=True)
        if plan.done:
            self.flatten_requested = None
            log_event(self.conn, "flatten.done", {"reason": reason, "dust": len(plan.dust)})
        return {"orders": len(plan.orders), "dust": len(plan.dust), "unroutable": plan.unroutable}

    # ================================================================== allocation

    def _evaluate_promotions(self) -> None:
        pc = self.settings.cfg.promotion
        for strat in self.strategies:
            prev = self.statuses.get(strat.name)
            st = promotion.evaluate(
                strat.name,
                self.bt_trades.get(strat.name, []),
                self.books[strat.name].closed,
                pc,
                currently_promoted=bool(prev and prev.promoted),
            )
            if prev is not None and prev.promoted != st.promoted:
                repo.record_decision(
                    self.conn,
                    mode=self.mode.value,
                    run_id=self.run_id,
                    strategy=strat.name,
                    action="promote" if st.promoted else "demote",
                    signal={"backtest": st.backtest.as_dict(), "shadow": st.shadow.as_dict()},
                    reason=st.reason,
                )
            self.statuses[strat.name] = st

    async def allocate_once(self) -> None:
        f = self.last_features
        if f is None:
            return
        self._evaluate_promotions()
        self.regime = classify(f)
        trades = {n: [*self.bt_trades.get(n, []), *self.books[n].closed] for n in self.books}
        dds = {n: b.drawdown_pct(f) for n, b in self.books.items()}
        snapshot = {
            "regime": self.regime,
            "promoted": [n for n, s in self.statuses.items() if s.promoted],
        }
        self.allocation = await allocate(
            self.statuses,
            trades,
            dds,
            self.regime,
            self.settings.cfg.alloc,
            self.settings.cfg.paper.capital_tmn,
            NullAgent(),
            snapshot,
        )
        a = self.allocation
        repo.insert_allocation(
            self.conn,
            run_id=self.run_id,
            source=a.source,
            baseline={k: str(v) for k, v in a.baseline.items()}
            | {"_scores": a.scores, "_regime": self.regime},
            agent=None if a.agent is None else {k: str(v) for k, v in a.agent.items()},
            final={k: str(v) for k, v in a.final.items()},
        )
        for n, b in self.books.items():
            eq = b.equity(f)
            repo.insert_paper_equity(
                self.conn,
                run_id=self.run_id,
                strategy=n,
                equity=eq,
                realized=b.realized_tmn(),
                unrealized=eq - b.capital - b.realized_tmn(),
                fees=b.fees_tmn,
                drawdown_pct=b.drawdown_pct(f),
            )
        exposure = sum(self._positions_tmn().values(), ZERO)
        repo.insert_equity(
            self.conn,
            mode=self.mode.value,
            run_id=self.run_id,
            equity=self.equity,
            tmn=self.account.total(TMN),
            exposure_pct=exposure / self.equity * 100 if self.equity else ZERO,
            drawdown_pct=(self.peak_equity - self.equity) / self.peak_equity * 100
            if self.peak_equity
            else ZERO,
        )
        await self._usdt_inventory(f)

    def _usdt_strategy_stats(self, f: Features) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        exposure, per_cycle = {}, {}
        for n, b in self.books.items():
            eq = b.equity(f)
            usdt_val = sum(
                (
                    lot.pos.qty * (self._price_tmn(self.all_rules[s].base_asset) or ZERO)
                    for s, lot in b.lots.items()
                    if lot.quote == "USDT"
                ),
                ZERO,
            )
            exposure[n] = usdt_val / eq if eq > 0 else ZERO
            all_t = [*self.bt_trades.get(n, []), *b.closed]
            usdt_trades = sum(1 for t in all_t if t.kind == "trade" and t.market.endswith("USDT"))
            cycles = sum(1 for t in all_t if t.kind == "conversion")
            per_cycle[n] = Decimal(usdt_trades) / Decimal(max(cycles, 1))
        return exposure, per_cycle

    async def _usdt_inventory(self, f: Features) -> None:
        """Real USDT stays 0 until a USDT-quoted strategy is promoted (spec in architecture.md)."""
        if self.allocation is None:
            return
        ac = self.settings.cfg.alloc
        exposure, per_cycle = self._usdt_strategy_stats(f)
        weights = {
            n: w
            for n, w in self.allocation.final.items()
            if n not in self.settings.cfg.promotion.paper_only
        }
        target = usdt_target(weights, exposure, per_cycle, ac.usdt_breakeven_trades_per_cycle)
        usdt_val = self.account.total("USDT") * (self._usdt_bid() or ZERO)
        current = usdt_val / self.equity if self.equity > 0 else ZERO
        if target == 0 and current == 0:
            return
        d = usdt_decide(
            target,
            current,
            now=time.time(),
            last_conversion_ts=self.usdt_last_conversion,
            band_pct=ac.usdt_rebalance_band_pct,
            min_hold_s=ac.usdt_min_hold_min * 60,
        )
        if d.delta_frac == 0:
            return
        r = self.all_rules.get("USDTTMN")
        book = self.feed.books.get("USDTTMN")
        if r is None or book is None or book.best_bid is None or book.best_ask is None:
            return
        side = Side.BUY if d.delta_frac > 0 else Side.SELL
        px = book.best_bid if side is Side.BUY else book.best_ask
        qty = r.round_qty(abs(d.delta_frac) * self.equity / px)
        if side is Side.SELL:
            qty = min(qty, self.account.free.get("USDT", ZERO))
        res = gate.check(
            gate.Intent("USDTTMN", side, qty, px, is_exit=side is Side.SELL),
            self._risk_state(time.time()),
            self.limits,
            r,
        )
        did = repo.record_decision(
            self.conn,
            mode=self.mode.value,
            run_id=self.run_id,
            symbol="USDTTMN",
            action="usdt_inventory",
            signal={
                "target_frac": str(d.target_frac),
                "current_frac": str(d.current_frac),
                "trades_per_cycle": {k: str(v) for k, v in per_cycle.items()},
            },
            weights={k: str(v) for k, v in weights.items()},
            risk_checks=res.as_log(),
            reason=d.reason,
        )
        if not res.approved:
            return
        o = self.settings.cfg.orders
        try:
            if side is Side.BUY:
                spec = build_limit_order(
                    r, side, res.qty, px, decision_id=did, n=1, min_value_tmn=o.min_order_value_tmn
                )
            else:
                spec = choose_exit_order(
                    r,
                    side,
                    res.qty,
                    book,
                    decision_id=did,
                    n=1,
                    slippage_pct=o.exit_limit_slippage_pct,
                )
        except OrderRuleError as e:
            log_event(self.conn, "order.build_failed", {"decision_id": did, "error": str(e)})
            return
        self.usdt_last_conversion = time.time()
        await self._submit(did, spec, TMN, is_exit=side is Side.SELL)

    # ================================================================== status

    def snapshot(self) -> dict[str, Any]:
        f = self.last_features
        start = self.settings.cfg.account.expected_equity_tmn
        strategies = []
        for strat in self.strategies:
            n = strat.name
            st = self.statuses.get(n)
            b = self.books[n]
            strategies.append(
                {
                    "name": n,
                    "promoted": bool(st and st.promoted),
                    "paper_only": bool(st and st.paper_only),
                    "status": "paper-only: non-atomic legs"
                    if st and st.paper_only
                    else ("promoted" if st and st.promoted else "paper"),
                    "reason": st.reason if st else "",
                    "backtest": st.backtest.as_dict() if st else {},
                    "shadow": st.shadow.as_dict() if st else {},
                    "weight": str(self.allocation.final.get(n, ZERO)) if self.allocation else "0",
                    "paper_equity": f"{b.equity(f):.0f}" if f else "–",
                    "open_paper_positions": sorted(b.lots),
                    "pending_maker": sorted(b.pending),
                }
            )
        promoted_any = any(s["promoted"] for s in strategies)
        positions = {k: f"{v:.0f}" for k, v in self._positions_tmn().items()}
        usdt_qty = self.account.total("USDT")
        usdt_val = usdt_qty * (self._usdt_bid() or ZERO)
        feed = {}
        if f:
            for sym in self.symbols:
                m = f.markets.get(sym)
                feed[sym] = {
                    "spread_bps": f"{m.spread_bps:.1f}" if m else "–",
                    "issue": (m.book_issue if m else "no book") or "",
                    "taker": f"{m.taker_fee * 100:.2f}%" if m else "–",
                    "rt_bps": f"{m.round_trip_bps():.1f}" if m else "–",
                }
        events = self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM events WHERE ts >= ? GROUP BY kind ORDER BY n DESC",
            (datetime.fromtimestamp(self.started, UTC).isoformat(timespec="milliseconds"),),
        ).fetchall()
        ml = self.client.market_limiter
        g = self.settings.cfg.guardrails
        return {
            "running": not self.stopping.is_set(),
            "mode": self.mode.value,
            "run_id": self.run_id,
            "uptime_s": round(time.time() - self.started),
            "last_tick_age_s": None
            if self.last_tick is None
            else round(time.time() - self.last_tick, 1),
            "equity_tmn": f"{self.equity:.0f}",
            "pnl_pct_vs_start": f"{(self.equity - start) / start * 100:+.3f}" if start else "0",
            "drawdown_pct": f"{(self.peak_equity - self.equity) / self.peak_equity * 100:.3f}"
            if self.peak_equity
            else "0",
            "exposure_tmn": f"{sum(self._positions_tmn().values(), ZERO):.0f}",
            "positions": positions,
            "flat_by_design": not promoted_any and not positions,
            "strategies": strategies,
            "allocation_source": self.allocation.source if self.allocation else None,
            "regime": self.regime,
            "usdt_inventory": {
                "qty": str(usdt_qty),
                "value_tmn": f"{usdt_val:.0f}",
                "cost_tmn": f"{self.usdt_cost_tmn:.0f}",
            },
            "phase": phase(
                time.time(), self.event_end, g.flatten_no_entry_min * 60, g.flatten_force_min * 60
            ).value,
            "event_end_utc": self.settings.secrets.event_end_utc.isoformat()
            if self.settings.secrets.event_end_utc
            else None,
            "hard_stopped": self.hard_stopped,
            "soft_stop_active": time.time() < self.soft_until,
            "package": getattr(self, "package", None),
            "feed": feed,
            "depth_source": self.feed.depth_source,
            "depth_interval_s": round(self.feed.depth_interval, 2),
            "ratelimit_remaining": ml.remaining,
            "errors": self.errors + self.feed.errors,
            "events": {r["kind"]: r["n"] for r in events},
            "replay": self.replay_info,
            "dust": self.dust,
            "open_orders": len(self.executor.open_orders),
            "preflight": self.credit.message if self.credit else None,
        }

    def health(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "last_tick": self.last_tick,
            "equity_tmn": f"{self.equity:.0f}",
            "errors": self.errors + self.feed.errors,
            "ok": self.last_tick is not None and time.time() - self.last_tick < 30,
        }

    def _write_status_file(self) -> None:
        path = self.settings.cfg.path("data/engine_status.json")
        with contextlib.suppress(OSError):
            path.write_text(
                json.dumps({"written_at": time.time(), **self.snapshot()}, default=str),
                encoding="utf-8",
            )

    # ================================================================== control

    async def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        markets = {m.upper() for m in req.get("markets") or []}
        if cmd == "ping":
            return {"ok": True, "mode": self.mode.value}
        if cmd == "status":
            return {"ok": True, "status": self.snapshot()}
        if cmd == "stop":
            if markets:
                self.paused |= markets
                for cid, o in list(self.executor.open_orders.items()):
                    if o.spec.symbol in markets:
                        await self.executor.cancel(cid)
                log_event(self.conn, "control.stop_markets", {"markets": sorted(markets)})
                return {"ok": True, "paused": sorted(self.paused)}
            log_event(self.conn, "control.stop_all", {})
            self.stopping.set()
            return {"ok": True, "stopping": True}
        if cmd == "flatten":
            self.flatten_requested = markets
            if markets:
                self.paused |= markets
            log_event(self.conn, "control.flatten", {"markets": sorted(markets) or "all"})
            return {"ok": True, "flatten": sorted(markets) or "all"}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    # ================================================================== loops

    async def _every(self, interval: float, fn: Any, name: str) -> None:
        while not self.stopping.is_set():
            t0 = time.monotonic()
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the loop alive; count and log
                self.errors += 1
                log.exception(f"{name}.error", error=str(e)[:200])
                log_event(self.conn, f"{name}.error", {"error": str(e)[:300]})
            delay = max(0.0, interval - (time.monotonic() - t0))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=delay)

    async def _depth_and_tick(self) -> None:
        await self.feed.poll_depth()
        await self.tick()

    async def _reconcile(self) -> None:
        if isinstance(self.executor, LiveExecutor):
            self.account = await self.executor.snapshot()
            await self.executor.reconcile_open_orders()

    async def _alloc_and_status(self) -> None:
        await self.allocate_once()
        self._write_status_file()

    async def run(self) -> None:
        ecfg = self.settings.cfg.engine
        servers: list[asyncio.AbstractServer] = []
        sock = self.settings.cfg.path(ecfg.socket_path)
        try:
            await self.startup()
            servers.append(await control.serve(sock, self.handle))
            servers.append(await health.serve(ecfg.health_port, self.health))
            tasks = [
                asyncio.create_task(
                    self._every(self.feed.depth_interval, self._depth_and_tick, "tick")
                ),
                asyncio.create_task(
                    self._every(ecfg.trades_interval_s, self.feed.poll_trades, "trades")
                ),
                asyncio.create_task(
                    self._every(ecfg.candles_interval_s, self.feed.poll_candles, "candles")
                ),
                asyncio.create_task(
                    self._every(ecfg.allocator_interval_s, self._alloc_and_status, "alloc")
                ),
                asyncio.create_task(
                    self._every(ecfg.reconcile_interval_s, self._reconcile, "reconcile")
                ),
            ]
            await self.stopping.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self.shutdown(servers, sock)

    async def shutdown(self, servers: list[asyncio.AbstractServer], sock: Path) -> None:
        self.stopping.set()
        if isinstance(getattr(self, "executor", None), LiveExecutor):
            for cid, o in list(self.executor.open_orders.items()):
                if not o.is_exit:  # entries are cancelled; exits keep working on the exchange
                    with contextlib.suppress(ApiError, TransportError):
                        await self.executor.cancel(cid)
        for s in servers:
            s.close()
        with contextlib.suppress(OSError):
            sock.unlink()
        if hasattr(self, "feed"):
            self._write_status_file()
        log_event(self.conn, "engine.stopped", {"run_id": self.run_id})
        await self.client.aclose()
        self.conn.close()


def _feat_log(m: Any) -> dict[str, Any]:
    return {
        "bid": str(m.bid),
        "ask": str(m.ask),
        "spread_bps": f"{m.spread_bps:.2f}",
        "taker_fee": str(m.taker_fee),
        "maker_fee": str(m.maker_fee),
        "vwap_z": m.vwap_z,
        "atr_bps": m.atr_bps,
        "imbalance": m.imbalance,
        "book_issue": m.book_issue,
    }
