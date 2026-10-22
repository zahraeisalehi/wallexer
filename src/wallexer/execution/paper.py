"""Paper books: one per strategy, always running (backtest replay, shadow and trade modes).

Fill model (shared with ShadowExecutor):
- taker: walks the book levels (or, in the candle replay, close +/- half the current spread),
  then `slippage_bps` against us. Fee = taker rate from the real fee table.
- maker: a LIMIT resting at our price fills when a trade prints at/through it (or the ask
  crosses it for a buy); cancelled after `maker_timeout_s`. Fee = maker rate.
Fees are charged in the quote asset and converted to TMN for PnL.

USDT inventory per book: a USDT-quoted entry converts TMN -> USDT on USDTTMN only when the book's
USDT is short (taker fee + spread paid then), the USDT is kept while used, and converted back once
idle for `usdt_min_hold_min`. The conversion is a 'conversion' trade row (cost charged once per
cycle, to this strategy). While a cycle is open, USDT is marked net of the sell-back leg, so paper
equity is never overstated. Positions are likewise marked at bid net of the exit taker fee.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from wallexer.data.features import Features, MarketFeatures
from wallexer.exchange.models import Book, Side, Trade
from wallexer.exchange.rules import MarketRules
from wallexer.strategies.base import BookView, Position, Signal

BPS = Decimal(10000)
ZERO = Decimal(0)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


@dataclass
class PaperTrade:
    strategy: str
    source: str  # backtest | shadow
    kind: str  # trade | cycle | conversion
    market: str
    qty: Decimal
    entry_ts: str
    exit_ts: str
    entry_px: Decimal
    exit_px: Decimal
    fees_tmn: Decimal
    gross_tmn: Decimal
    net_tmn: Decimal
    entry_reason: dict[str, Any] = field(default_factory=dict)
    exit_reason: dict[str, Any] = field(default_factory=dict)


@dataclass
class FillModel:
    slippage_bps: Decimal

    def taker_price(
        self, side: Side, qty: Decimal, bid: Decimal, ask: Decimal, book: Book | None
    ) -> Decimal:
        px = ask if side is Side.BUY else bid
        if book is not None and qty > 0:
            levels = (
                sorted(book.ask, key=lambda lv: lv.price)
                if side is Side.BUY
                else sorted(book.bid, key=lambda lv: lv.price, reverse=True)
            )
            left, cost = qty, ZERO
            for lv in levels:
                take = min(left, lv.quantity)
                cost += take * lv.price
                left -= take
                if left <= 0:
                    break
            if levels:
                # Not enough depth: the remainder fills at the last level seen (optimistic floor).
                cost += left * levels[-1].price if left > 0 else ZERO
                px = cost / qty
        slip = self.slippage_bps / BPS
        return px * (1 + slip) if side is Side.BUY else px * (1 - slip)


@dataclass
class _Lot:
    pos: Position
    quote: str
    entry_fee_tmn: Decimal
    entry_usdt_tmn: Decimal  # USDT/TMN mid at entry (1 for TMN markets)
    reason: dict[str, Any]


@dataclass
class _Pending:
    symbol: str
    qty: Decimal
    px: Decimal
    ts: float
    signal: Signal


class PaperBook:
    def __init__(
        self,
        strategy: str,
        *,
        capital_tmn: Decimal,
        position_frac: Decimal,
        fill: FillModel,
        rules: dict[str, MarketRules],
        min_entry_tmn: Decimal,
        maker_timeout_s: float,
        usdt_min_hold_s: float,
        source: str,
    ) -> None:
        self.strategy = strategy
        self.source = source
        self.capital = capital_tmn
        self.position_frac = position_frac
        self.fill = fill
        self.rules = rules
        self.min_entry_tmn = min_entry_tmn
        self.maker_timeout_s = maker_timeout_s
        self.usdt_min_hold_s = usdt_min_hold_s
        self.cash: dict[str, Decimal] = {"TMN": capital_tmn, "USDT": ZERO}
        self.usdt_cost_tmn = ZERO  # TMN paid for the USDT bought in the open cycle
        self.usdt_units = ZERO  # USDT bought in the open cycle (cost basis = cost / units)
        self.usdt_cycle_open_ts: float | None = None
        self.usdt_last_used_ts: float | None = None
        self.lots: dict[str, _Lot] = {}
        self.pending: dict[str, _Pending] = {}
        self.last_exit_ts: dict[str, float] = {}
        self.closed: list[PaperTrade] = []
        self.fees_tmn = ZERO
        self.peak_equity = capital_tmn
        self.skipped: dict[str, int] = {}

    # ------------------------------------------------------------------ views

    def view(self) -> BookView:
        return BookView(
            positions={s: lot.pos for s, lot in self.lots.items()},
            last_exit_ts=dict(self.last_exit_ts),
            pending=frozenset(self.pending),
        )

    def equity(self, f: Features) -> Decimal:
        """Liquidation value in TMN: positions at bid net of exit fee, USDT net of sell-back."""
        ut = f.markets.get("USDTTMN")
        usdt_px = ut.bid * (1 - ut.taker_fee) if ut else (f.usdt_tmn_bid or ZERO)
        eq = self.cash["TMN"] + self.cash["USDT"] * usdt_px
        for sym, lot in self.lots.items():
            m = f.markets.get(sym)
            px = m.bid * (1 - m.taker_fee) if m else lot.pos.entry_px
            value = lot.pos.qty * px
            eq += value * usdt_px if lot.quote == "USDT" else value
        return eq

    def realized_tmn(self) -> Decimal:
        return sum((t.net_tmn for t in self.closed), ZERO)

    def _skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1

    # ------------------------------------------------------------------ helpers

    def _usdt_mid(self, f: Features) -> Decimal | None:
        ut = f.markets.get("USDTTMN")
        return ut.mid if ut else None

    def _to_tmn(self, amount: Decimal, quote: str, f: Features) -> Decimal:
        """USDT amounts are valued at the open cycle's cost basis, so trade rows and the
        conversion row add up exactly to the TMN change (no double counting)."""
        if quote == "TMN":
            return amount
        if self.usdt_units > 0:
            return amount * self.usdt_cost_tmn / self.usdt_units
        mid = self._usdt_mid(f)
        return amount * mid if mid else ZERO

    def _convert_tmn_to_usdt(self, usdt_needed: Decimal, f: Features, now: float) -> bool:
        ut = f.markets.get("USDTTMN")
        if ut is None:
            return False
        px = self.fill.taker_price(Side.BUY, usdt_needed, ut.bid, ut.ask, None)
        tmn_cost = usdt_needed * px / (1 - ut.taker_fee)  # gross up so net USDT received = needed
        if tmn_cost > self.cash["TMN"]:
            return False
        self.cash["TMN"] -= tmn_cost
        self.cash["USDT"] += usdt_needed
        self.usdt_cost_tmn += tmn_cost
        self.usdt_units += usdt_needed
        if self.usdt_cycle_open_ts is None:
            self.usdt_cycle_open_ts = now
        self.fees_tmn += tmn_cost - usdt_needed * px
        return True

    def _maybe_close_usdt_cycle(self, f: Features, now: float) -> PaperTrade | None:
        if self.cash["USDT"] <= 0 or any(lot.quote == "USDT" for lot in self.lots.values()):
            return None
        if any(
            self.rules[p.symbol].quote_asset == "USDT"
            for p in self.pending.values()
            if p.symbol in self.rules
        ):
            return None
        if (
            self.usdt_last_used_ts is not None
            and now - self.usdt_last_used_ts < self.usdt_min_hold_s
        ):
            return None
        ut = f.markets.get("USDTTMN")
        if ut is None:
            return None
        usdt = self.cash["USDT"]
        px = self.fill.taker_price(Side.SELL, usdt, ut.bid, ut.ask, None)
        fee = usdt * px * ut.taker_fee
        tmn = usdt * px - fee
        self.cash["USDT"] = ZERO
        self.cash["TMN"] += tmn
        self.fees_tmn += fee
        # Conversion row = sell-back proceeds vs the cost basis of the USDT sold back, i.e. the
        # conversion fees/spread plus the USDT/TMN move. Trade PnL is already in trade rows.
        basis = self.usdt_cost_tmn / self.usdt_units if self.usdt_units > 0 else px
        cost = usdt * basis
        t = PaperTrade(
            self.strategy,
            self.source,
            "conversion",
            "USDTTMN",
            usdt,
            iso(self.usdt_cycle_open_ts or now),
            iso(now),
            basis,
            px,
            fee,
            tmn + fee - cost,
            tmn - cost,
            {"cycle": "TMN->USDT"},
            {"cycle": "USDT->TMN", "idle_s": round(now - (self.usdt_last_used_ts or now))},
        )
        self.usdt_cost_tmn = ZERO
        self.usdt_units = ZERO
        self.usdt_cycle_open_ts = None
        self.closed.append(t)
        return t

    # ------------------------------------------------------------------ execution

    def _open(
        self, sig: Signal, m: MarketFeatures, f: Features, now: float, px: Decimal, *, maker: bool
    ) -> None:
        fee_rate = m.maker_fee if maker else m.taker_fee
        rules = self.rules.get(sig.market)
        if rules is None:
            self._skip("no_rules")
            return
        budget_tmn = self.equity(f) * self.position_frac
        quote_per_tmn = (
            Decimal(1)
            if m.quote == "TMN"
            else (1 / self._usdt_mid(f) if self._usdt_mid(f) else ZERO)
        )
        qty = rules.round_qty(budget_tmn * quote_per_tmn / px) if px > 0 else ZERO
        cost_q = qty * px
        fee_q = cost_q * fee_rate
        if qty <= 0 or self._to_tmn(cost_q, m.quote, f) < self.min_entry_tmn:
            self._skip("below_entry_floor")
            return
        if m.quote == "USDT":
            short = cost_q + fee_q - self.cash["USDT"]
            if short > 0 and not self._convert_tmn_to_usdt(short, f, now):
                self._skip("no_usdt")
                return
            self.usdt_last_used_ts = now
        if self.cash[m.quote] < cost_q + fee_q:
            self._skip("no_cash")
            return
        self.cash[m.quote] -= cost_q + fee_q
        fee_tmn = self._to_tmn(fee_q, m.quote, f)
        self.fees_tmn += fee_tmn
        stop = sig.stop if sig.stop is not None else px * Decimal("0.98")
        entry_fx = (self._usdt_mid(f) or Decimal(1)) if m.quote == "USDT" else Decimal(1)
        self.lots[sig.market] = _Lot(
            Position(sig.market, qty, px, now, stop, m.bid, sig.entry_dev or 0.0),
            m.quote,
            fee_tmn,
            entry_fx,
            {**sig.reason, "fill": "maker" if maker else "taker"},
        )

    def _close(
        self, sym: str, m: MarketFeatures, f: Features, now: float, reason: dict[str, Any]
    ) -> PaperTrade:
        lot = self.lots.pop(sym)
        qty = lot.pos.qty
        px = self.fill.taker_price(Side.SELL, qty, m.bid, m.ask, None)
        proceeds = qty * px
        fee_q = proceeds * m.taker_fee
        self.cash[lot.quote] += proceeds - fee_q
        fee_tmn = self._to_tmn(fee_q, lot.quote, f)
        self.fees_tmn += fee_tmn
        gross_q = (px - lot.pos.entry_px) * qty
        # USDT markets: trade PnL in TMN at the current USDT/TMN mid; USDT price moves on the
        # inventory itself land in the conversion row.
        gross_tmn = self._to_tmn(gross_q, lot.quote, f)
        fees = lot.entry_fee_tmn + fee_tmn
        if lot.quote == "USDT":
            self.usdt_last_used_ts = now
        self.last_exit_ts[sym] = now
        t = PaperTrade(
            self.strategy,
            self.source,
            "trade",
            sym,
            qty,
            iso(lot.pos.entry_ts),
            iso(now),
            lot.pos.entry_px,
            px,
            fees,
            gross_tmn,
            gross_tmn - fees,
            lot.reason,
            reason,
        )
        self.closed.append(t)
        return t

    def _asset_tmn(self, asset: str, f: Features) -> Decimal:
        """Mid price of one unit of `asset` in TMN (for valuing fees)."""
        if asset == "TMN":
            return Decimal(1)
        m = f.markets.get(f"{asset}TMN")
        return m.mid if m else ZERO

    def _run_cycle(self, sig: Signal, f: Features, now: float) -> PaperTrade | None:
        """Basis triangle, simulated atomically: TMN -> leg1 -> leg2 -> leg3 -> TMN.

        Each leg pays the taker fee in the asset received; fees are valued in TMN at mid.
        """
        legs = [(f.markets.get(s), side) for s, side in sig.legs]
        if any(m is None or not m.book_ok for m, _ in legs):
            self._skip("cycle_leg_missing")
            return None
        start = self.cash["TMN"] * self.position_frac
        if start < self.min_entry_tmn:
            self._skip("below_entry_floor")
            return None
        amount, fees_tmn, prices = start, ZERO, []
        for m, side in legs:
            assert m is not None
            rules = self.rules.get(m.symbol)
            base = rules.base_asset if rules else m.symbol[: -len(m.quote)]
            px = self.fill.taker_price(side, ZERO, m.bid, m.ask, None)
            prices.append(str(px))
            # BUY: spend quote, receive base. SELL: spend base, receive quote.
            received, asset = (amount / px, base) if side is Side.BUY else (amount * px, m.quote)
            fee = received * m.taker_fee
            fees_tmn += fee * self._asset_tmn(asset, f)
            amount = received - fee
        net = amount - start
        self.cash["TMN"] += net
        self.fees_tmn += fees_tmn
        self.last_exit_ts[sig.market] = now
        t = PaperTrade(
            self.strategy,
            self.source,
            "cycle",
            sig.market,
            start,
            iso(now),
            iso(now),
            Decimal(prices[0]),
            Decimal(prices[-1]),
            fees_tmn,
            net + fees_tmn,
            net,
            sig.reason,
            {"legs": [f"{s}:{side.value}" for s, side in sig.legs], "prices": prices},
        )
        self.closed.append(t)
        return t

    def step(
        self,
        signals: list[Signal],
        f: Features,
        now: float,
        *,
        trades: dict[str, list[Trade]] | None = None,
        replay: bool = False,
    ) -> list[PaperTrade]:
        """Apply one tick: resolve resting maker orders, then exits, then entries/cycles.

        replay=True (candle backtest): every entry fills as taker at close +/- half spread.
        """
        done: list[PaperTrade] = []
        # 1. resting maker orders
        for sym, p in list(self.pending.items()):
            m = f.markets.get(sym)
            if m is None:
                continue
            printed = any(t.price <= p.px for t in (trades or {}).get(sym, []))
            if printed or m.ask <= p.px:
                del self.pending[sym]
                self._open(p.signal, m, f, now, p.px, maker=True)
            elif now - p.ts >= self.maker_timeout_s:
                del self.pending[sym]
                self._skip("maker_timeout")
        # 2. marks: trailing peaks
        for sym, lot in self.lots.items():
            m = f.markets.get(sym)
            if m is not None and m.bid > lot.pos.peak:
                pos = lot.pos
                lot.pos = replace(pos, peak=m.bid)
        # 3. signals
        for sig in signals:
            m = f.markets.get(sig.market)
            if sig.kind == "exit":
                if sig.market in self.lots and m is not None:
                    done.append(self._close(sig.market, m, f, now, sig.reason))
            elif sig.kind == "cycle":
                t = self._run_cycle(sig, f, now)
                if t:
                    done.append(t)
            elif sig.kind == "entry" and m is not None and sig.market not in self.lots:
                if sig.urgency == "maker" and not replay:
                    self.pending[sig.market] = _Pending(sig.market, ZERO, m.bid, now, sig)
                else:
                    px = self.fill.taker_price(Side.BUY, ZERO, m.bid, m.ask, None)
                    self._open(sig, m, f, now, px, maker=False)
        # 4. USDT cycle housekeeping
        t = self._maybe_close_usdt_cycle(f, now)
        if t:
            done.append(t)
        eq = self.equity(f)
        self.peak_equity = max(self.peak_equity, eq)
        return done

    def close_all(self, f: Features, now: float, reason: str) -> list[PaperTrade]:
        """Close every position at taker prices (walk-forward segment end), cancel resting orders."""
        self.pending.clear()
        done = []
        for sym in list(self.lots):
            m = f.markets.get(sym)
            if m is not None:
                done.append(self._close(sym, m, f, now, {"exit": reason}))
        self.usdt_last_used_ts = None
        t = self._maybe_close_usdt_cycle(f, now)
        if t:
            done.append(t)
        return done

    def drawdown_pct(self, f: Features) -> Decimal:
        eq = self.equity(f)
        return (self.peak_equity - eq) / self.peak_equity * 100 if self.peak_equity > 0 else ZERO
