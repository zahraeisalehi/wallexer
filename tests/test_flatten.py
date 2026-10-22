"""Critical test 4: flatten sells every non-TMN balance (incl. USDT) to TMN; dust cap respected."""

from decimal import Decimal as D

from wallexer.exchange.models import Book, DepthLevel, OrderType, Side
from wallexer.exchange.rules import MarketRules
from wallexer.risk.flatten import plan_flatten

RULES = {
    "BTCTMN": MarketRules("BTCTMN", "BTC", "TMN", 0, 8),
    "ETHTMN": MarketRules("ETHTMN", "ETH", "TMN", 0, 6),
    "USDTTMN": MarketRules("USDTTMN", "USDT", "TMN", 0, 2),
    "SOLTMN": MarketRules("SOLTMN", "SOL", "TMN", 0, 2, market_orders_enabled=False),
    "SUIUSDT": MarketRules("SUIUSDT", "SUI", "USDT", 4, 1),  # no SUITMN rule → via USDT
    "ATMN": MarketRules("ATMN", "A", "TMN", 0, 2),
}


def bk(bid, ask):
    return Book(
        bid=[DepthLevel(price=D(bid), quantity=D(100))],
        ask=[DepthLevel(price=D(ask), quantity=D(100))],
    )


BOOKS = {
    "BTCTMN": bk("20000000000", "20010000000"),
    "ETHTMN": bk("660000000", "661000000"),
    "USDTTMN": bk("270000", "270100"),
    "SOLTMN": bk("50000000", "50100000"),
    "SUIUSDT": bk("3.5", "3.51"),
    "ATMN": bk("1000", "1010"),
}


def plan(balances, **kw):
    args = dict(decision_id=7, dust_cap_tmn=D(1000), slippage_pct=D("0.5"))
    args.update(kw)
    return plan_flatten(balances, BOOKS, RULES, **args)


def test_sells_every_non_tmn_asset_including_usdt():
    p = plan({"TMN": D(1_000_000), "BTC": D("0.01"), "ETH": D("0.5"), "USDT": D(10), "SOL": D(3)})
    sold = {o.symbol: o for o in p.orders}
    assert set(sold) == {"BTCTMN", "ETHTMN", "USDTTMN", "SOLTMN"}
    assert all(o.side is Side.SELL for o in p.orders)
    assert sold["USDTTMN"].quantity == D("10.00")
    assert p.orders[-1].symbol == "USDTTMN"  # USDT last
    assert len({o.client_id for o in p.orders}) == len(p.orders)
    assert all(o.client_id.startswith("wx-7-") for o in p.orders)
    assert p.done is False and not p.unroutable


def test_market_vs_aggressive_limit():
    p = plan({"BTC": D("0.01"), "SOL": D(3)})
    sold = {o.symbol: o for o in p.orders}
    assert sold["BTCTMN"].type is OrderType.MARKET
    assert sold["SOLTMN"].type is OrderType.LIMIT
    assert sold["SOLTMN"].price == D("49750000")  # 0.5% below best bid


def test_asset_without_tmn_market_routes_via_usdt():
    p = plan({"SUI": D(100)})
    [o] = p.orders
    assert o.symbol == "SUIUSDT" and o.side is Side.SELL
    # the USDT it produces is sold on the next pass
    p2 = plan({"USDT": D(350)})
    assert [o.symbol for o in p2.orders] == ["USDTTMN"]


def test_dust_below_cap_is_accepted_not_sold():
    # 0.00000004 BTC ≈ 800 TMN < 1000 cap
    p = plan({"BTC": D("0.00000004"), "USDT": D("0.003")})
    assert not p.orders and p.done
    assert {d.asset for d in p.dust} == {"BTC", "USDT"}
    assert all(d.value_tmn < 1000 for d in p.dust)


def test_rounding_remainder_is_dust_or_flagged():
    # USDT precision 2: 10.009 → sell 10.00, remainder 0.009 USDT ≈ 2,430 TMN > cap → flagged
    p = plan({"USDT": D("10.009")})
    assert p.orders[0].quantity == D("10.00")
    assert p.over_cap_remainders and p.over_cap_remainders[0].asset == "USDT"
    p = plan({"USDT": D("10.001")})  # 0.001 USDT ≈ 270 TMN → accepted dust
    assert p.dust and p.dust[0].why == "rounding_remainder"


def test_blocked_market_is_not_used_and_reported():
    p = plan({"A": D(1000)}, blocked=frozenset({"ATMN"}))
    assert not p.orders and p.unroutable == ["A"] and not p.done


def test_only_selected_assets():
    p = plan({"BTC": D("0.01"), "ETH": D("0.5")}, only_assets=frozenset({"ETH"}))
    assert [o.symbol for o in p.orders] == ["ETHTMN"]
