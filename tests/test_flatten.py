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


async def test_end_of_event_flatten_schedule(tmp_path):
    """Engine wiring at T-25 min: the schedule requests a flatten, every non-TMN balance (incl.
    USDT) is sold to TMN via the executor, dust is accepted and logged, and entries are blocked."""
    from datetime import UTC, datetime

    from wallexer.config import AppConfig, Mode, Secrets, Settings, StoreCfg
    from wallexer.engine.daemon import Engine
    from wallexer.exchange.fees import parse_fee_table
    from wallexer.exchange.models import Side
    from wallexer.execution.paper import FillModel
    from wallexer.execution.shadow import ShadowExecutor
    from wallexer.risk import gate
    from wallexer.risk.guardrails import Limits

    end = datetime(2026, 10, 23, 20, 20, tzinfo=UTC)  # real event end
    settings = Settings(
        cfg=AppConfig(
            store=StoreCfg(db_path=str(tmp_path / "t.db"), overrides_path=str(tmp_path / "o.toml"))
        ),
        secrets=Secrets.model_construct(event_end_utc=end, wallexer_mode=Mode.shadow),
    )
    eng = Engine(settings, Mode.shadow)
    rules = {k: v for k, v in RULES.items() if k in ("BTCTMN", "USDTTMN", "ETHTMN")}
    rules["XAUTTMN"] = MarketRules("XAUTTMN", "XAUT", "TMN", 0, 3)
    books = dict(BOOKS) | {"XAUTTMN": bk("400000000", "401000000")}
    eng.symbols = list(rules)
    eng.all_rules = rules
    eng.markets_by_symbol = {}  # no fair-price data in this test → no band clamp
    eng.forbidden = eng.unavailable = frozenset()
    eng.fees = parse_fee_table({}, D("0.003"), D("0.003"))
    eng.limits = Limits.from_settings(settings, forbidden=frozenset(), unavailable=frozenset())

    class FeedStub:
        pass

    eng.feed = FeedStub()
    eng.feed.books = books
    start = {"TMN": D(4_000_000), "BTC": D("0.00004"), "USDT": D(3), "XAUT": D("0.000002")}
    eng.executor = ShadowExecutor(
        eng.conn, eng.fees, FillModel(D(0)), start, {s: r.base_asset for s, r in rules.items()}
    )
    eng.account = await eng.executor.snapshot()
    eng.equity = eng._account_equity()
    eng.peak_equity = eng.day_start_equity = eng.equity

    t_flatten = end.timestamp() - 34 * 60  # 19:46 UTC: inside the T-35 flatten window
    eng._risk_monitor(t_flatten)
    assert eng.flatten_requested == set()
    assert (
        eng.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='schedule.force_flatten'"
        ).fetchone()[0]
        == 1
    )

    await eng.flatten(set(), "schedule")  # sells BTC (MARKET) and USDT; XAUT is dust (< 1000 TMN)
    eng.account = await eng.executor.snapshot()
    assert eng.account.free.get("BTC", 0) == 0 and eng.account.free.get("USDT", 0) == 0
    assert eng.account.free["XAUT"] == D("0.000002")  # 0.000002 * 4e8 = 800 TMN dust
    await eng.flatten(set(), "schedule")  # next tick: nothing left to sell → done
    assert eng.flatten_requested is None
    kinds = [r[0] for r in eng.conn.execute("SELECT kind FROM events")]
    assert "flatten.dust_accepted" in kinds and "flatten.done" in kinds
    sides = {r[0] for r in eng.conn.execute("SELECT side FROM orders")}
    assert sides == {"SELL"}
    assert all(r[0] is not None for r in eng.conn.execute("SELECT decision_id FROM orders"))

    # entries are blocked from T-90 min
    st = eng._risk_state(end.timestamp() - 89 * 60)
    r = gate.check(
        gate.Intent("BTCTMN", Side.BUY, D("0.0001"), D("20000000000"), is_exit=False),
        st,
        eng.limits,
        rules["BTCTMN"],
    )
    assert not r.approved and "end of event" in r.reason
    await eng.client.aclose()
    eng.conn.close()


def test_exit_price_never_beyond_fair_band_even_in_a_crashing_book():
    from wallexer.exchange.rules import FairBand, aggressive_price, choose_exit_order

    sol = RULES["SOLTMN"]  # MARKET disabled → aggressive LIMIT
    fair = FairBand(D(50_000_000), D(4))
    crash = bk("40000000", "40100000")  # book 20% below fair
    # unclamped: 40,000,000 x 0.995 = 39,800,000 (-20.4%); clamped: fair x 0.96 = 48,000,000
    assert aggressive_price(sol, Side.SELL, crash, D("0.5")) == D("39800000")
    assert aggressive_price(sol, Side.SELL, crash, D("0.5"), fair) == D("48000000")
    # a normal book is untouched (49,750,000 is within 4% of fair)
    assert aggressive_price(sol, Side.SELL, bk("50000000", "50100000"), D("0.5"), fair) == D(
        "49750000"
    )
    # buys are capped above fair
    spike = bk("59000000", "60000000")
    assert aggressive_price(sol, Side.BUY, spike, D("0.5"), fair) == D("52000000")
    spec = choose_exit_order(
        sol, Side.SELL, D(3), crash, decision_id=1, n=1, slippage_pct=D("0.5"), band=fair
    )
    assert spec.price == D("48000000")
    # flatten passes the per-market band through
    p = plan_flatten(
        {"SOL": D(3)},
        {**BOOKS, "SOLTMN": crash},
        RULES,
        decision_id=7,
        dust_cap_tmn=D(1000),
        slippage_pct=D("0.5"),
        bands={"SOLTMN": fair},
    )
    assert p.orders[0].price == D("48000000")


def test_fair_band_uses_market_threshold_when_tighter():
    from wallexer.exchange.models import Market

    m = Market.model_validate(
        {
            "symbol": "USDTTMN",
            "base_asset": "USDT",
            "quote_asset": "TMN",
            "amount_precision": 2,
            "price_precision": 0,
            "fair_price": {"bid": "272000", "ask": "272000", "spot": {"threshold": "3"}},
        }
    )
    assert m.fair_band(D(4)) == (D(272000), D(3))
    assert (
        Market.model_validate({**m.model_dump(by_alias=True), "fair_price": None}).fair_band(D(4))
        is None
    )
