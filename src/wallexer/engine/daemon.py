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

from wallexer.agent.agent import Applied, anonymize_snapshot, apply_decision
from wallexer.agent.agent import ask as agent_ask
from wallexer.agent.client import LlmClient
from wallexer.alloc import promotion
from wallexer.alloc.allocator import Allocation, NullAgent, allocate
from wallexer.alloc.regime import REGIME_TF, build_reference, classify
from wallexer.alloc.usdt_inventory import decide as usdt_decide
from wallexer.alloc.usdt_inventory import target_frac as usdt_target
from wallexer.backtest.report import latest_run, stats_from_json
from wallexer.config import ConfigError, Mode, Settings, is_demo, is_view_public
from wallexer.data.features import Features, basis_features, imbalance, market_features
from wallexer.data.feed import MarketFeed
from wallexer.data.history import load_bars, sync_history
from wallexer.engine import control, health
from wallexer.engine.api import ReadOnlyApi
from wallexer.exchange.account import CreditCheck, PreflightError, preflight_live
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import ApiError, TransportError
from wallexer.exchange.models import Side
from wallexer.exchange.prop import fetch_package, load_unavailable, save_unavailable
from wallexer.exchange.rules import (
    FairBand,
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
from wallexer.execution.paper import FillModel, PaperBook
from wallexer.execution.shadow import ShadowExecutor
from wallexer.risk import gate
from wallexer.risk.flatten import plan_flatten
from wallexer.risk.guardrails import Limits
from wallexer.risk.schedule import Phase, phase
from wallexer.risk.schedule import times as schedule_times
from wallexer.store import repo
from wallexer.store.db import dumps, event_sink, init_db, log_event, now_iso
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
        if mode is Mode.trade and is_demo():
            raise ConfigError("demo server (WALLEXER_DEMO=1): trade mode is refused; shadow only")
        if is_view_public() and not is_demo():
            # A token-free view is for the public demo only, never for an engine with real keys.
            raise ConfigError("WALLEXER_VIEW_PUBLIC=1 is only allowed with WALLEXER_DEMO=1")
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
        self.paused: set[str] = set()  # human `stop` pauses
        # LLM agent state (all effects shrink-only; see agent/agent.py)
        self.agent_applied: Applied | None = None
        self.agent_applied_ts = 0.0
        self.agent_state: dict[str, Any] = {}
        self.agent_last_run = 0.0
        self.agent_reason = "interval"
        self.agent_event: asyncio.Event | None = None
        self.last_dd_step = 0
        self.watch_books: dict[str, tuple[str, int, str, Any, PaperBook]] = {}
        self.recheck_requests: list[tuple[str, int, str]] = []
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
        self._age_events: set[str] = set()
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
        # A hard stop survives restarts: it is only lifted by a human `wallexer resume`.
        last_stop = self.conn.execute(
            "SELECT MAX(id) FROM events WHERE kind='risk.hard_stop'"
        ).fetchone()[0]
        last_resume = self.conn.execute(
            "SELECT MAX(id) FROM events WHERE kind='control.resume'"
        ).fetchone()[0]
        if last_stop and (last_resume is None or last_stop > last_resume):
            self.hard_stopped = True
            log_event(self.conn, "engine.hard_stop_persisted", {"hard_stop_event": last_stop})
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
        # candle history (incremental) -> live bars + regime reference
        await sync_history(self.client, self.conn, symbols, cfg.backtest.history_days)
        self.feed = MarketFeed(self.client, self.conn, s, symbols)
        log_event(self.conn, "engine.feed_plan", self.feed.plan_intervals())
        self.feed.load_from_store()
        await self.feed.poll_depth()
        since = int(time.time()) - cfg.backtest.history_days * 86400
        self.regime_ref = build_reference(
            {sym: load_bars(self.conn, sym, "15", since) for sym in symbols}
        )
        # strategies, with the walk-forward selection (IS-chosen timeframe + params)
        self.wf = self._load_walk_forward()
        sc, g = cfg.strategies, cfg.guardrails
        mr_cfg = sc.mean_reversion.model_copy(
            update=self.wf["strategies"].get("mean_reversion", {}).get("update", {})
        )
        mo_cfg = sc.momentum.model_copy(
            update=self.wf["strategies"].get("momentum", {}).get("update", {})
        )
        all_strats: list[Strategy] = [
            BasisTmnUsdt(sc.basis_tmn_usdt),
            MeanReversion(mr_cfg, g),
            Momentum(mo_cfg, g),
        ]
        self.strategy_cfg = {
            "basis_tmn_usdt": sc.basis_tmn_usdt,
            "mean_reversion": mr_cfg,
            "momentum": mo_cfg,
        }
        self.strategies = [x for x in all_strats if self.strategy_cfg[x.name].enabled]
        log_event(
            self.conn,
            "engine.walk_forward",
            {k: v for k, v in self.wf.items() if k != "strategies"}
            | {"selected": {n: v.get("update") for n, v in self.wf["strategies"].items()}},
        )
        self.books = {x.name: self._new_book(x.name, "shadow") for x in self.strategies}
        self.llm = LlmClient(s, self.conn)
        # account / executor from exchange truth
        bals = await self.client.balances()
        start = {a: b.value for a, b in bals.items() if b.value > 0}
        base_of = {sym: r.base_asset for sym, r in self.all_rules.items()}
        if self.mode is Mode.trade:
            self.executor = LiveExecutor(
                s,
                self.client,
                self.conn,
                self.all_rules,
                lambda: self._usdt_bid(),
                self.mode,
                band=self.band,
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

    def band(self, symbol: str) -> FairBand | None:
        """Exchange fair price + allowed exit distance for `symbol` (refreshed every minute)."""
        m = self.markets_by_symbol.get(symbol)
        if m is None:
            return None
        fb = m.fair_band(self.settings.cfg.orders.exit_max_fair_deviation_pct)
        return FairBand(*fb) if fb else None

    async def _refresh_markets(self) -> None:
        """Fair prices move: re-read /markets (one cached call) for the exit price band."""
        fresh = await self.client.markets()
        self.markets_by_symbol = {m.symbol: m for m in fresh if m.is_spot}

    def _usdt_bid(self) -> Decimal | None:
        b = self.feed.books.get("USDTTMN")
        return b.best_bid if b else None

    def features(self, now: float, timeframe: int = 1, cfg: Any = None) -> Features:
        """Features on `timeframe` bars (1/5/15 resampled live, 60 native) + live top of book."""
        cfg = cfg or self.strategy_cfg["momentum"]
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
                self.feed.bars_tf(sym, timeframe),
                ts=now,
                bid=book.best_bid,
                ask=book.best_ask,
                maker_fee=fee.maker,
                taker_fee=fee.taker,
                book_issue=issue,
                book_imbalance=imbalance(book),
                vwap_bars=cfg.vwap_bars,
                breakout_bars=cfg.breakout_bars,
                atr_bars=cfg.atr_bars,
                timeframe=timeframe,
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

    def _agent_fresh(self) -> bool:
        ttl = self.settings.cfg.agent.decision_ttl_min * 60
        return self.agent_applied is not None and time.time() - self.agent_applied_ts <= ttl

    def _effective_risk_multiplier(self) -> Decimal:
        """min(engine soft-stop multiplier, agent multiplier): the agent can only lower it."""
        if self._agent_fresh():
            assert self.agent_applied is not None
            return min(self.risk_multiplier, self.agent_applied.risk_multiplier)
        return self.risk_multiplier

    def _blocked_markets(self) -> frozenset[str]:
        """No new entries: human pauses + agent pauses + markets outside the agent's active set."""
        blocked = set(self.paused)
        if self._agent_fresh():
            assert self.agent_applied is not None
            blocked |= self.agent_applied.agent_paused
            if self.agent_applied.active_markets is not None:
                blocked |= set(self.symbols) - self.agent_applied.active_markets
        return frozenset(blocked)

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
            paused=self._blocked_markets(),
        )

    # ================================================================== tick

    async def tick(self) -> None:
        now = time.time()
        f = self.features(now)  # 1-min features: account valuation, basis, rebalancing
        self.last_features = f
        per_strategy = {
            n: f if c.timeframe == 1 else self.features(now, c.timeframe, c)
            for n, c in self.strategy_cfg.items()
        }
        trades = self.feed.consume_trades()
        for strat in self.strategies:
            book = self.books[strat.name]
            sf = per_strategy[strat.name]
            try:
                sigs = strat.on_tick(sf, book.view(), now)
            except Exception as e:  # one strategy bug must not stop the engine
                self.errors += 1
                log.exception("strategy.error", strategy=strat.name, error=str(e))
                continue
            for t in book.step(sigs, sf, now, trades=trades):
                repo.insert_paper_trade(self.conn, t, self.run_id)
        self._tick_watch_books(now, trades)
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
        step = int(total_dd / Decimal(str(self.settings.cfg.agent.dd_step_pct)))
        if step > self.last_dd_step:
            self.last_dd_step = step
            self.trigger_agent(f"drawdown_step:{total_dd:.2f}%")
        if not self.hard_stopped and total_dd >= g.total_dd_hard_pct:
            self.hard_stopped = True
            self.flatten_requested = set()
            self.trigger_agent("risk.hard_stop")
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
            self.trigger_agent("risk.soft_stop")
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
            tgt = targets.get(sym, ZERO) * self.equity * self._effective_risk_multiplier()
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
                        band=self.band(sym),
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
            bands={sym: b for sym in self.all_rules if (b := self.band(sym)) is not None},
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

    def _load_walk_forward(self) -> dict[str, Any]:
        """Latest walk-forward run: per strategy the IS-selected timeframe/params + OOS stats."""
        row = latest_run(self.conn)
        if row is None:
            return {"run_id": None, "age_h": None, "strategies": {}}
        ts = datetime.fromisoformat(row["ts"]).timestamp()
        sel = json.loads(row["selected"])
        out: dict[str, Any] = {}
        for name, v in sel.items():
            chosen = v.get("selected")
            update: dict[str, Any] = {}
            if chosen:
                update = {"timeframe": int(chosen["timeframe"]), **chosen["params"]}
                if "breakout_bars" in update:
                    update["breakout_bars"] = int(update["breakout_bars"])
            out[name] = {
                "update": update,
                "oos": stats_from_json(v["oos"]) if v.get("oos") else None,
                "reason": v.get("reason", ""),
            }
        return {
            "run_id": row["id"],
            "ts": ts,
            "split_ts": row["split_ts"],
            "grid_size": row["grid_size"],
            "report": row["report_path"],
            "strategies": out,
        }

    def _run_age_h(self) -> float | None:
        ts = self.wf.get("ts")
        return None if ts is None else (time.time() - ts) / 3600

    def _check_run_age(self) -> None:
        """Warn early (warn_run_age_h) so the walk-forward is rerun before it expires."""
        b = self.settings.cfg.backtest
        age = self._run_age_h()
        if age is None:
            return
        for kind, limit in (
            ("backtest.run_aging", b.warn_run_age_h),
            ("backtest.run_expired", b.max_run_age_h),
        ):
            key = f"{kind}:{self.wf['run_id']}"
            if age >= limit and key not in self._age_events:
                self._age_events.add(key)
                log_event(
                    self.conn,
                    kind,
                    {"run_id": self.wf["run_id"], "age_h": round(age, 1), "limit_h": limit},
                )
                log.warning(
                    kind,
                    run_id=self.wf["run_id"],
                    age_h=round(age, 1),
                    hint="wallexer backtest --walk-forward",
                )

    def _evaluate_promotions(self) -> None:
        pc = self.settings.cfg.promotion
        age = self._run_age_h()
        for strat in self.strategies:
            prev = self.statuses.get(strat.name)
            wf = self.wf["strategies"].get(strat.name, {})
            st = promotion.evaluate(
                strat.name,
                wf.get("oos"),
                wf.get("reason", "no walk-forward selection"),
                self.books[strat.name].closed,
                pc,
                currently_promoted=bool(prev and prev.promoted),
                run_age_h=age,
                max_run_age_h=self.settings.cfg.backtest.max_run_age_h,
            )
            if prev is not None and prev.promoted != st.promoted:
                repo.record_decision(
                    self.conn,
                    mode=self.mode.value,
                    run_id=self.run_id,
                    strategy=strat.name,
                    action="promote" if st.promoted else "demote",
                    signal={
                        "oos": st.oos.as_dict() if st.oos else None,
                        "shadow": st.shadow.as_dict(),
                        "walk_forward_run": self.wf.get("run_id"),
                    },
                    reason=st.reason,
                )
                self.trigger_agent(f"{'promotion' if st.promoted else 'demotion'}:{strat.name}")
            self.statuses[strat.name] = st

    async def allocate_once(self) -> None:
        f = self.last_features
        if f is None:
            return
        self._evaluate_promotions()
        self._check_run_age()
        self.regime = classify(
            self.features(time.time(), REGIME_TF, self.strategy_cfg["momentum"]), self.regime_ref
        )
        trades = {n: list(self.books[n].closed) for n in self.books}
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
        # The agent's (already shrink-only) weights replace the baseline while its decision is
        # fresh; re-clamped here because promotions may have changed since it was made.
        source = "baseline"
        agent_w = None
        if self._agent_fresh():
            assert self.agent_applied is not None
            agent_w = self.agent_applied.weights
            promoted = {n for n, st in self.statuses.items() if st.promoted}
            final = {n: (agent_w.get(n, ZERO) if n in promoted else ZERO) for n in a.baseline}
            source = "agent"
        else:
            final = dict(a.baseline)
            if self.agent_state.get("source") == "fallback":
                source = "fallback"
        self.allocation = Allocation(a.baseline, agent_w, final, source, a.scores)
        a = self.allocation
        self.conn.execute(
            "INSERT INTO allocations (ts, run_id, baseline, agent, final, source, llm_call_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                now_iso(),
                self.run_id,
                dumps(
                    {k: str(v) for k, v in a.baseline.items()}
                    | {"_scores": a.scores, "_regime": self.regime}
                ),
                None if a.agent is None else dumps({k: str(v) for k, v in a.agent.items()}),
                dumps({k: str(v) for k, v in a.final.items()}),
                a.source,
                self.agent_state.get("llm_call_id") if source == "agent" else None,
            ),
        )
        self._feed_error_trigger()
        books_all = {**self.books, **{n: w[4] for n, w in self.watch_books.items()}}
        for n, b in books_all.items():
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

    # ================================================================== LLM agent

    def trigger_agent(self, reason: str) -> None:
        self.agent_reason = reason
        if self.agent_event is not None:
            self.agent_event.set()

    def _feed_error_trigger(self) -> None:
        since = datetime.fromtimestamp(time.time() - 300, UTC).isoformat(timespec="milliseconds")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE ts >= ? AND kind LIKE 'feed.%'", (since,)
        ).fetchone()[0]
        if n >= self.settings.cfg.agent.feed_error_threshold:
            self.trigger_agent(f"feed_errors:{n}/5min")

    def agent_snapshot(self) -> dict[str, Any]:
        now = time.time()
        f15 = self.features(now, REGIME_TF, self.strategy_cfg["momentum"])
        vol = {s: m.vol_bps for s, m in f15.markets.items() if m.vol_bps is not None}
        hours = None if self.event_end is None else (self.event_end - now) / 3600
        return anonymize_snapshot(
            self.snapshot(),
            vol_15m_bps=vol,
            allowed_markets=self.symbols,
            hours_to_end=hours,
            capital=self.settings.cfg.account.expected_equity_tmn,
        )

    async def agent_cycle(self, reason: str) -> dict[str, Any]:
        """One agent decision: snapshot -> LLM -> validate -> apply shrink-only -> log everything."""
        cfg = self.settings.cfg
        self.agent_last_run = time.time()
        snap = self.agent_snapshot()
        res = await agent_ask(self.llm, snap)
        before = {
            "risk_multiplier": str(self._effective_risk_multiplier()),
            "blocked_markets": sorted(self._blocked_markets()),
            "weights": {
                k: str(v) for k, v in (self.allocation.final if self.allocation else {}).items()
            },
            "watch_books": sorted(self.watch_books),
        }
        if res.parsed is None:
            self.agent_state = {
                "ts": time.time(),
                "source": "fallback",
                "trigger": reason,
                "error": res.error,
                "llm_call_id": res.llm_call_id,
                "stance": self.agent_state.get("stance"),
                "rationale": f"fallback to baseline ({res.error})",
            }
            repo.record_decision(
                self.conn,
                mode=self.mode.value,
                run_id=self.run_id,
                action="agent_fallback",
                signal={
                    "trigger": reason,
                    "prompt_hash": res.prompt_hash,
                    "llm_call_id": res.llm_call_id,
                },
                risk_checks={"before": before},
                reason=f"LLM unavailable/invalid: {res.error}",
            )
            return self.agent_state
        d = res.parsed
        promoted = [n for n, st in self.statuses.items() if st.promoted]
        applied = apply_decision(
            d,
            promoted=promoted,
            paper_only=cfg.promotion.paper_only,
            max_weight=cfg.alloc.max_strategy_weight,
            engine_risk_multiplier=self.risk_multiplier,
            universe=self.symbols,
            max_watch_books=cfg.agent.max_watch_books,
        )
        self.agent_applied, self.agent_applied_ts = applied, time.time()
        self._apply_watch(applied)
        after = {
            "risk_multiplier": str(self._effective_risk_multiplier()),
            "blocked_markets": sorted(self._blocked_markets()),
            "weights": {k: str(v) for k, v in applied.weights.items()},
            "watch_books": sorted(self.watch_books),
            "recheck": [list(x) for x in applied.watch_recheck],
            "notes": applied.notes,
        }
        baseline = {
            k: str(v) for k, v in (self.allocation.baseline if self.allocation else {}).items()
        }
        self.agent_state = {
            "ts": time.time(),
            "source": res.source,
            "trigger": reason,
            "llm_call_id": res.llm_call_id,
            "stance": d.stance,
            "rationale": d.rationale,
            "risk_multiplier": d.risk_multiplier,
            "pause_markets": sorted(applied.agent_paused),
            "active_markets": None
            if applied.active_markets is None
            else sorted(applied.active_markets),
            "watch_requests": [w.model_dump() for w in d.watch_requests],
            "agent_weights_raw": d.strategy_weights.model_dump(),
            "baseline": baseline,
            "final": after["weights"],
            "notes": applied.notes,
        }
        repo.record_decision(
            self.conn,
            mode=self.mode.value,
            run_id=self.run_id,
            action="agent_decision",
            signal={
                "trigger": reason,
                "prompt_hash": res.prompt_hash,
                "llm_call_id": res.llm_call_id,
                "source": res.source,
                "decision": d.model_dump(),
            },
            weights={
                "baseline": baseline,
                "agent_raw": d.strategy_weights.model_dump(),
                "final": after["weights"],
            },
            risk_checks={"before": before, "after": after},
            reason=d.rationale,
        )
        for key in applied.watch_recheck:
            log_event(
                self.conn,
                "agent.recheck_request",
                {"strategy": key[0], "timeframe": key[1], "market": key[2]},
            )
        return self.agent_state

    def _apply_watch(self, applied: Applied) -> None:
        """Extra shadow paper books for the agent's 'paper' watch requests (max N, oldest dropped)."""
        cfg = self.settings.cfg
        for strat_name, tf, market in applied.watch_paper:
            name = f"{strat_name}@{tf}m:{market}"
            if name in self.watch_books:
                continue
            if len(self.watch_books) >= cfg.agent.max_watch_books:
                oldest = next(iter(self.watch_books))
                del self.watch_books[oldest]
                log_event(self.conn, "agent.watch_book_dropped", {"name": oldest})
            base = self.strategy_cfg[strat_name]
            wcfg = base.model_copy(update={"timeframe": tf, "quotes": []})
            g = cfg.guardrails
            obj = MeanReversion(wcfg, g) if strat_name == "mean_reversion" else Momentum(wcfg, g)
            book = self._new_book(name, "watch")
            self.watch_books[name] = (strat_name, tf, market, obj, book)
            log_event(self.conn, "agent.watch_book_started", {"name": name})

    def _tick_watch_books(self, now: float, trades: dict[str, Any]) -> None:
        for name, (_s, tf, market, obj, book) in list(self.watch_books.items()):
            cfg = obj.cfg
            full = self.features(now, tf, cfg)
            keep = {k: v for k, v in full.markets.items() if k in (market, "USDTTMN")}
            f = Features(now, keep, {}, full.usdt_tmn_bid)
            try:
                sigs = [x for x in obj.on_tick(f, book.view(), now) if x.market == market]
            except Exception as e:
                self.errors += 1
                log.exception("watch.error", name=name, error=str(e))
                continue
            for t in book.step(sigs, f, now, trades=trades):
                repo.insert_paper_trade(self.conn, t, self.run_id)

    async def _demo_explain_loop(self) -> None:
        """Demo only: pre-generate the LLM answers the public view shows (`sum` for today hourly
        and yesterday once, `exp-reason` for each new agent decision). The read-only API only
        reads them, so no visitor can trigger an LLM call. Never touches trading state."""
        from datetime import timedelta

        from wallexer.cli import daily, views
        from wallexer.cli.logs import explain_decision

        await asyncio.sleep(120)  # after the first agent decision (~90 s after start)
        while not self.stopping.is_set():
            try:
                row = self.conn.execute(
                    "SELECT d.id FROM decisions d LEFT JOIN explanations e ON e.decision_id = d.id"
                    " WHERE d.action = 'agent_decision' AND e.decision_id IS NULL"
                    " ORDER BY d.id DESC LIMIT 1"
                ).fetchone()
                if row:
                    rec = views.reason_data(self.conn, row[0])
                    if rec:
                        await explain_decision(
                            self.settings, self.conn, row[0], rec, float(self.equity or 0) or 5e6
                        )
                today = datetime.now(UTC).date()
                for day, max_age_s in ((today, 3600.0), (today - timedelta(days=1), None)):
                    have = self.conn.execute(
                        "SELECT created_at, explanation FROM daily_summaries"
                        " WHERE date=? AND origin='LOCAL'",
                        (day.isoformat(),),
                    ).fetchone()
                    if have and have[1]:
                        if max_age_s is None:
                            continue
                        age = time.time() - datetime.fromisoformat(have[0]).timestamp()
                        if age < max_age_s:
                            continue
                    facts = daily.daily_facts(self.conn, day)
                    if not facts["has_data"]:
                        continue
                    e, call_id, _err = await daily.explain(self.settings, self.conn, facts)
                    daily.store_summary(self.conn, day, "LOCAL", facts, e, call_id)
                    log_event(self.conn, "demo.summary", {"date": day.isoformat(), "ok": bool(e)})
            except Exception as e:  # explanations are cosmetic: never take the engine down
                log.warning("demo.explain_error", error=str(e)[:200])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=300)

    async def _agent_loop(self) -> None:
        acfg = self.settings.cfg.agent
        assert self.agent_event is not None
        while not self.stopping.is_set():
            wait = max(1.0, self.agent_last_run + acfg.interval_min * 60 - time.time())
            triggered = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.agent_event.wait(), timeout=wait)
                triggered = True
            if self.stopping.is_set():
                return
            if triggered:
                self.agent_event.clear()
                gap = time.time() - self.agent_last_run
                if gap < acfg.debounce_min * 60:
                    await asyncio.sleep(acfg.debounce_min * 60 - gap)
            reason = self.agent_reason if triggered else "interval"
            self.agent_reason = "interval"
            try:
                await self.agent_cycle(reason)
            except Exception as e:  # the agent must never take the engine down
                self.errors += 1
                log.exception("agent.error", error=str(e)[:200])
                log_event(self.conn, "agent.error", {"error": str(e)[:300]})

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
            all_t = list(b.closed)
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
                    band=self.band("USDTTMN"),
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
                    "status": "paper-only: non-atomic legs · shadow-evaluated"
                    if st and st.paper_only
                    else ("promoted" if st and st.promoted else "paper"),
                    "reason": st.reason if st else "",
                    "timeframe": self.strategy_cfg[n].timeframe,
                    "oos": st.oos.as_dict() if st and st.oos else {},
                    "shadow": st.shadow.as_dict() if st else {},
                    "weight": str(self.allocation.final.get(n, ZERO)) if self.allocation else "0",
                    "paper_equity": f"{b.equity(f):.0f}" if f else "–",
                    "open_paper_positions": sorted(b.lots),
                    "pending_maker": sorted(b.pending),
                }
            )
        from wallexer.alloc.promotion import stats as _stats

        for name, (_s, tf, market, _obj, book) in self.watch_books.items():
            strategies.append(
                {
                    "name": name,
                    "watch": True,
                    "promoted": False,
                    "paper_only": True,
                    "status": "agent watch book (paper)",
                    "reason": f"requested by agent on {market}",
                    "timeframe": tf,
                    "oos": {},
                    "shadow": _stats(book.closed).as_dict(),
                    "weight": "0",
                    "paper_equity": f"{book.equity(f):.0f}" if f else "–",
                    "open_paper_positions": sorted(book.lots),
                    "pending_maker": sorted(book.pending),
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
            "demo": is_demo(),
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
            "agent": {
                **{k: v for k, v in self.agent_state.items() if k != "ts"},
                "age_min": round((time.time() - self.agent_state["ts"]) / 60, 1)
                if self.agent_state.get("ts")
                else None,
                "applied": self._agent_fresh(),
                "effective_risk_multiplier": str(self._effective_risk_multiplier()),
                "blocked_markets": sorted(self._blocked_markets()),
            },
            "regime": self.regime,
            "usdt_inventory": {
                "qty": str(usdt_qty),
                "value_tmn": f"{usdt_val:.0f}",
                "cost_tmn": f"{self.usdt_cost_tmn:.0f}",
            },
            "phase": phase(
                time.time(), self.event_end, g.flatten_no_entry_min * 60, g.flatten_force_min * 60
            ).value,
            "schedule": schedule_times(
                self.event_end, g.flatten_no_entry_min * 60, g.flatten_force_min * 60
            ),
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
            "walk_forward": {
                "run_id": self.wf.get("run_id"),
                "age_h": None if self._run_age_h() is None else round(self._run_age_h(), 1),
                "max_age_h": self.settings.cfg.backtest.max_run_age_h,
                "grid_size": self.wf.get("grid_size"),
                "split": datetime.fromtimestamp(self.wf["split_ts"], UTC).isoformat()
                if self.wf.get("split_ts")
                else None,
                "report": self.wf.get("report"),
            },
            "dust": self.dust,
            "market_states": self._market_states(),
            "open_orders": len(self.executor.open_orders),
            "preflight": self.credit.message if self.credit else None,
        }

    def health(self) -> dict[str, Any]:
        """Served from the very start (before startup finishes) so the platform sees a live port."""
        feed = getattr(self, "feed", None)
        return {
            "mode": self.mode.value,
            "demo": is_demo(),
            "view_public": is_view_public() and is_demo(),
            "starting": self.last_tick is None,
            "last_tick": self.last_tick,
            "equity_tmn": f"{self.equity:.0f}",
            "errors": self.errors + (feed.errors if feed else 0),
            "ok": self.last_tick is not None and time.time() - self.last_tick < 30,
        }

    def _api_status(self) -> dict[str, Any]:
        # Startup (preflight, history sync) has not built the state yet: say so instead of failing.
        if self.last_tick is None:
            return {
                "starting": True,
                "mode": self.mode.value,
                "demo": is_demo(),
                "run_id": self.run_id,
            }
        return self.snapshot()

    def _market_states(self) -> dict[str, str]:
        """Per-market scope for `status`: human stop > agent pause > agent inactive > book issue."""
        f = self.last_features
        fresh = self._agent_fresh() and self.agent_applied is not None
        out = {}
        for sym in self.symbols:
            m = f.markets.get(sym) if f else None
            if sym in self.paused:
                out[sym] = "stopped (human)"
            elif fresh and sym in self.agent_applied.agent_paused:  # type: ignore[union-attr]
                out[sym] = "paused (agent)"
            elif (
                fresh
                and self.agent_applied.active_markets is not None  # type: ignore[union-attr]
                and sym not in self.agent_applied.active_markets  # type: ignore[union-attr]
            ):
                out[sym] = "not active (agent)"
            elif m is None or m.book_issue:
                out[sym] = f"book issue: {m.book_issue if m else 'no book'}"
            else:
                out[sym] = "active"
        return out

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
        if is_demo() and cmd in ("stop", "flatten", "resume", "start_markets"):
            return {"ok": False, "error": f"demo server (WALLEXER_DEMO=1): `{cmd}` is refused"}
        if cmd == "agent_now":
            if not self.settings.cfg.agent.enabled:
                return {"ok": False, "error": "agent disabled in config"}
            state = await self.agent_cycle("manual")
            return {"ok": True, "agent": state}
        if cmd == "reload":
            from wallexer.config import load_settings

            fresh = load_settings()
            self.settings.cfg.guardrails = fresh.cfg.guardrails
            self.limits = Limits.from_settings(
                self.settings,
                forbidden=self.forbidden,
                unavailable=self.unavailable,
                limit_only=self.universe.limit_only,
            )
            log_event(
                self.conn,
                "control.reload",
                {"guardrails": fresh.cfg.guardrails.model_dump(mode="json")},
            )
            return {"ok": True, "reloaded": "guardrails"}
        if cmd == "resume":
            self.hard_stopped = False
            self.flatten_requested = None
            self.peak_equity = self.equity  # drawdown is measured again from here
            log_event(self.conn, "control.resume", {"equity": str(self.equity), "by": "human"})
            return {"ok": True, "resumed": True}
        if cmd == "ping":
            return {"ok": True, "mode": self.mode.value}
        if cmd == "start_markets":
            # Inverse of `stop <market>`: clear the human pause. Same mode only (the CLI asks the
            # human-in-the-loop questions first for trade); never changes mode or limits.
            if req.get("mode") != self.mode.value:
                return {"ok": False, "error": f"engine runs in {self.mode.value} mode"}
            unknown = sorted(markets - set(self.symbols))
            if unknown:
                return {"ok": False, "error": f"not in this engine's universe: {unknown}"}
            self.paused -= markets
            log_event(self.conn, "control.start_markets", {"markets": sorted(markets)})
            return {"ok": True, "started": sorted(markets), "paused": sorted(self.paused)}
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

    async def _walk_forward_rerun(self) -> None:
        """Re-run the walk-forward in a low-priority subprocess, then reload its selection.

        Promotion uses the new OOS stats/age. An unpromoted strategy whose selection changed is
        rebuilt (fresh paper book); a promoted one keeps running unchanged until restart.
        """
        import sys

        log_event(self.conn, "backtest.rerun_started", {})
        proc = await asyncio.create_subprocess_exec(
            "nice",
            "-n",
            "10",
            sys.executable,
            "-m",
            "wallexer.cli.app",
            "backtest",
            "--walk-forward",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.settings.cfg.path(".")),
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            log_event(
                self.conn,
                "backtest.rerun_failed",
                {"code": proc.returncode, "stderr": (err or b"").decode()[-400:]},
            )
            return
        old = {n: v.get("update") for n, v in self.wf["strategies"].items()}
        self.wf = self._load_walk_forward()
        sc, g = self.settings.cfg.strategies, self.settings.cfg.guardrails
        rebuilt = []
        for name, cls in (("mean_reversion", MeanReversion), ("momentum", Momentum)):
            new_upd = self.wf["strategies"].get(name, {}).get("update", {})
            st = self.statuses.get(name)
            if new_upd != old.get(name) and not (st and st.promoted):
                cfg = getattr(sc, name).model_copy(update=new_upd)
                self.strategy_cfg[name] = cfg
                self.strategies = [x if x.name != name else cls(cfg, g) for x in self.strategies]
                self.books[name] = self._new_book(name, "shadow")
                rebuilt.append(name)
        self._age_events.clear()
        log_event(
            self.conn, "backtest.rerun_done", {"run_id": self.wf.get("run_id"), "rebuilt": rebuilt}
        )
        self.trigger_agent("walk_forward_rerun")

    async def _rerun_loop(self) -> None:
        b = self.settings.cfg.backtest
        every = b.rerun_every_h * 3600
        age = self._run_age_h()
        # First run soon after start when there is no run or it is getting old (fresh server).
        wait = 120.0 if age is None or age >= b.warn_run_age_h else every
        while not self.stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=wait)
            wait = every
            if self.stopping.is_set():
                return
            try:
                await self._walk_forward_rerun()
            except Exception as e:  # a failed re-run must not affect trading
                self.errors += 1
                log_event(self.conn, "backtest.rerun_failed", {"error": str(e)[:300]})

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
            # Health first: the platform must see the port while startup (sync, preflight) runs.
            api = ReadOnlyApi(
                self.settings.cfg.path(self.settings.cfg.store.db_path),
                self._api_status,
                float(self.settings.cfg.account.expected_equity_tmn),
                float(self.settings.cfg.paper.capital_tmn),
            )
            servers.append(await health.serve(ecfg.health_port, self.health, api))
            await self.startup()
            servers.append(await control.serve(sock, self.handle))
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
                asyncio.create_task(self._every(60.0, self._refresh_markets, "markets")),
            ]
            if self.settings.cfg.backtest.rerun_every_h > 0:
                tasks.append(asyncio.create_task(self._rerun_loop()))
            if self.settings.cfg.agent.enabled:
                self.agent_event = asyncio.Event()
                # first decision ~90 s after start, once promotions/regime/allocation exist
                self.agent_last_run = time.time() - self.settings.cfg.agent.interval_min * 60 + 90
                tasks.append(asyncio.create_task(self._agent_loop()))
            if is_demo() and self.settings.secrets.boxapi_api_key:
                tasks.append(asyncio.create_task(self._demo_explain_loop()))
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
        if hasattr(self, "llm"):
            await self.llm.close()
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
