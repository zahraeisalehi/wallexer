"""Critical test 3: paper PnL / equity accounting including fees (and the USDT conversion cycle)."""

from decimal import Decimal as D

from wallexer.data.features import Features, MarketFeatures
from wallexer.exchange.models import Side, Trade
from wallexer.exchange.rules import MarketRules
from wallexer.execution.paper import FillModel, PaperBook
from wallexer.strategies.base import Signal

RULES = {
    "BTCTMN": MarketRules("BTCTMN", "BTC", "TMN", 0, 8),
    "BTCUSDT": MarketRules("BTCUSDT", "BTC", "USDT", 2, 6),
    "USDTTMN": MarketRules("USDTTMN", "USDT", "TMN", 0, 2),
}
TAKER, MAKER = D("0.003"), D("0.0025")


def mf(sym, bid, ask, quote="TMN", taker=TAKER, maker=MAKER):
    return MarketFeatures(sym, 0.0, D(bid), D(ask), maker, taker, True, None, quote)


def feats(*ms, usdt_bid=None):
    return Features(0.0, {m.symbol: m for m in ms}, {}, D(usdt_bid) if usdt_bid else None)


def book(slip="0", frac="0.2", hold_s=0.0):
    return PaperBook(
        "t",
        capital_tmn=D(5_000_000),
        position_frac=D(frac),
        fill=FillModel(D(slip)),
        rules=RULES,
        min_entry_tmn=D(200_000),
        maker_timeout_s=60,
        usdt_min_hold_s=hold_s,
        source="shadow",
    )


def entry(sym, urgency="taker"):
    return Signal("t", sym, Side.BUY, "entry", urgency, 1.0, {"why": "test"}, stop=D(1))


def exit_(sym):
    return Signal("t", sym, Side.SELL, "exit", "taker", -1.0, {"exit": "test"})


def test_taker_round_trip_pnl_includes_both_fees():
    b = book()
    f1 = feats(mf("BTCTMN", "19990000000", "20000000000"))
    b.step([entry("BTCTMN")], f1, 100.0)
    lot = b.lots["BTCTMN"]
    qty = lot.pos.qty  # 20% of 5M = 1M TMN at ask 20e9 TMN/BTC → 0.00005 BTC
    assert qty == D("0.00005") and lot.pos.entry_px == D("20000000000")
    entry_fee = D("0.00005") * D("20000000000") * TAKER  # 3,000
    assert b.cash["TMN"] == D(5_000_000) - D(1_000_000) - entry_fee

    f2 = feats(mf("BTCTMN", "20400000000", "20410000000"))  # bid +2%
    [t] = b.step([exit_("BTCTMN")], f2, 200.0)
    exit_fee = D("0.00005") * D("20400000000") * TAKER  # 3,060
    assert t.gross_tmn == D("0.00005") * D("400000000")  # +2% move (4e8 TMN/BTC) → 20,000 TMN
    assert t.fees_tmn == entry_fee + exit_fee
    assert t.net_tmn == D(20_000) - entry_fee - exit_fee
    # equity identity: flat book → equity = cash = capital + realized
    assert b.equity(f2) == b.cash["TMN"] == D(5_000_000) + t.net_tmn
    assert b.fees_tmn == entry_fee + exit_fee


def test_mark_to_market_at_bid_net_of_exit_fee():
    b = book()
    b.step([entry("BTCTMN")], feats(mf("BTCTMN", "19990000000", "20000000000")), 1.0)
    f = feats(mf("BTCTMN", "21000000000", "21010000000"))
    expected = b.cash["TMN"] + D("0.00005") * D("21000000000") * (1 - TAKER)
    assert b.equity(f) == expected


def test_slippage_is_against_us():
    b = book(slip="10")  # 10 bps
    b.step([entry("BTCTMN")], feats(mf("BTCTMN", "19990000000", "20000000000")), 1.0)
    assert b.lots["BTCTMN"].pos.entry_px == D("20000000000") * D("1.001")


def test_maker_entry_fills_on_trade_print_with_maker_fee():
    b = book()
    f = feats(mf("BTCTMN", "19990000000", "20000000000"))
    b.step([entry("BTCTMN", "maker")], f, 1.0)
    assert "BTCTMN" in b.pending and not b.lots
    tr = Trade(quantity=D(1), price=D("19990000000"), isBuyOrder=False, timestamp="t")
    b.step([], f, 2.0, trades={"BTCTMN": [tr]})
    lot = b.lots["BTCTMN"]
    assert lot.pos.entry_px == D("19990000000")
    assert lot.entry_fee_tmn == lot.pos.qty * D("19990000000") * MAKER


def test_maker_entry_times_out():
    b = book()
    f = feats(mf("BTCTMN", "19990000000", "20000000000"))
    b.step([entry("BTCTMN", "maker")], f, 1.0)
    b.step([], f, 62.0)
    assert not b.pending and not b.lots and b.skipped["maker_timeout"] == 1


def test_usdt_market_trade_and_conversion_cycle_charged_once():
    taker_u = D("0.0013")
    ut = mf("USDTTMN", "270000", "270100")
    bu = mf("BTCUSDT", "74000", "74010", quote="USDT", taker=taker_u, maker=D("0.001"))
    b = book(hold_s=0.0)
    b.step([entry("BTCUSDT")], feats(ut, bu), 1.0)
    assert b.lots["BTCUSDT"].quote == "USDT"
    assert b.usdt_cost_tmn > 0  # TMN -> USDT conversion happened once
    cost_before = b.usdt_cost_tmn
    [t] = [x for x in b.step([exit_("BTCUSDT")], feats(ut, bu), 2.0) if x.kind == "trade"]
    # trade PnL is in TMN; fees include both USDT-market taker fees, valued in TMN
    assert t.market == "BTCUSDT" and t.fees_tmn > 0 and t.net_tmn < 0  # flat price: fees + spread
    # USDT was idle with min hold 0 → converted back in the same step: one conversion row
    conv = [x for x in b.closed if x.kind == "conversion"]
    assert len(conv) == 1 and conv[0].net_tmn < 0
    assert b.cash["USDT"] == 0 and b.usdt_cost_tmn == 0
    # conversion cost = what we paid for the USDT minus what we got back
    assert -conv[0].net_tmn < cost_before * D("0.01")  # < 1% of converted amount
    # equity identity: everything back in TMN
    f = feats(ut, bu)
    assert b.equity(f) == b.cash["TMN"] == D(5_000_000) + sum(x.net_tmn for x in b.closed)


def test_below_entry_floor_is_skipped():
    b = book(frac="0.01")  # 50k < 200k floor
    b.step([entry("BTCTMN")], feats(mf("BTCTMN", "19990000000", "20000000000")), 1.0)
    assert not b.lots and b.skipped["below_entry_floor"] == 1
