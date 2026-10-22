"""Critical test 1: precision/rounding + min order value for order construction.

Also covers the confirmed exchange rules: close-in-full exits, entry floor, aggressive LIMIT
exits on MARKET-disabled markets, re-pricing the remainder, and learning the minimum from a
rejection message.
"""

from decimal import Decimal as D

import pytest

from wallexer.config import AppConfig, Secrets, Settings, StoreCfg
from wallexer.exchange import min_order
from wallexer.exchange.errors import ApiError
from wallexer.exchange.models import Book, DepthLevel, Order, OrderType, Side
from wallexer.exchange.rules import (
    MarketRules,
    OrderRuleError,
    RepriceDecision,
    aggressive_price,
    build_limit_order,
    choose_exit_order,
    exit_quantity,
    make_client_id,
    reprice_exit,
)

MIN = D("100000")
BTCTMN = MarketRules("BTCTMN", "BTC", "TMN", price_precision=0, amount_precision=8)
ETHUSDT = MarketRules("ETHUSDT", "ETH", "USDT", price_precision=2, amount_precision=5)
SOLTMN_NO_MKT = MarketRules("SOLTMN", "SOL", "TMN", 0, 2, market_orders_enabled=False)
TINY = MarketRules("TINYTMN", "TINY", "TMN", price_precision=10, amount_precision=0)


def book(bid: str, ask: str) -> Book:
    return Book(
        bid=[DepthLevel(price=D(bid), quantity=D(1))], ask=[DepthLevel(price=D(ask), quantity=D(1))]
    )


# --- rounding ---------------------------------------------------------------


def test_price_rounding_by_side_precision_0():
    assert BTCTMN.round_price(D("22138741352.7"), Side.BUY) == D("22138741352")
    assert BTCTMN.round_price(D("22138741352.2"), Side.SELL) == D("22138741353")


def test_price_rounding_precision_2_and_10():
    assert ETHUSDT.round_price(D("2437.559"), Side.BUY) == D("2437.55")
    assert ETHUSDT.round_price(D("2437.551"), Side.SELL) == D("2437.56")
    assert TINY.round_price(D("0.000012345678912"), Side.BUY) == D("0.0000123456")
    assert TINY.round_price(D("0.000012345678912"), Side.SELL) == D("0.0000123457")


def test_crossing_rounding_is_opposite():
    # Aggressive exits round toward crossing the book.
    assert ETHUSDT.round_price(D("2437.551"), Side.SELL, crossing=True) == D("2437.55")
    assert ETHUSDT.round_price(D("2437.551"), Side.BUY, crossing=True) == D("2437.56")


def test_quantity_always_rounds_down():
    assert BTCTMN.round_qty(D("0.123456789")) == D("0.12345678")
    assert ETHUSDT.round_qty(D("1.999999")) == D("1.99999")
    assert TINY.round_qty(D("12.99")) == D("12")


def test_body_values_are_plain_strings():
    spec = build_limit_order(
        BTCTMN, Side.BUY, D("0.0001"), D("22138741352"), decision_id=7, n=1, min_value_tmn=MIN
    )
    body = spec.to_body()
    assert body == {
        "symbol": "BTCTMN",
        "side": "BUY",
        "type": "LIMIT",
        "quantity": "0.00010000",
        "price": "22138741352",
        "client_id": "wx-7-1",
    }
    assert all(isinstance(v, str) for v in body.values())


# --- min order value / entry floor -----------------------------------------


def test_below_min_rejected_tmn():
    with pytest.raises(OrderRuleError, match="minimum"):
        # 0.000004 BTC * 22.1e9 ≈ 88,555 TMN < 100,000
        build_limit_order(
            BTCTMN, Side.BUY, D("0.000004"), D("22138741352"), decision_id=1, n=1, min_value_tmn=MIN
        )


def test_min_check_uses_rounded_values():
    # 0.0000045999 rounds down to 0.00000459 → 101,616 TMN ≥ min: accepted after rounding.
    spec = build_limit_order(
        BTCTMN, Side.BUY, D("0.0000045999"), D("22138741352"), decision_id=1, n=1, min_value_tmn=MIN
    )
    assert spec.quantity == D("0.00000459")


def test_below_min_rejected_usdt_market_converted_to_tmn():
    usdt_tmn = D("266721")
    # 0.0001 ETH * 2437.55 USDT * 266,721 ≈ 65,014 TMN < min
    with pytest.raises(OrderRuleError):
        build_limit_order(
            ETHUSDT,
            Side.BUY,
            D("0.0001"),
            D("2437.55"),
            decision_id=1,
            n=1,
            min_value_tmn=MIN,
            usdt_tmn=usdt_tmn,
        )
    with pytest.raises(OrderRuleError, match="USDT/TMN"):
        build_limit_order(
            ETHUSDT, Side.BUY, D("1"), D("2437.55"), decision_id=1, n=1, min_value_tmn=MIN
        )


def test_entry_floor_is_multiple_of_min():
    # 150,000 TMN passes the minimum but not a 2x entry floor.
    kwargs = dict(decision_id=1, n=1, min_value_tmn=MIN)
    build_limit_order(SOLTMN_NO_MKT, Side.BUY, D("1.5"), D("100000"), **kwargs)
    with pytest.raises(OrderRuleError, match="entry floor"):
        build_limit_order(
            SOLTMN_NO_MKT,
            Side.BUY,
            D("1.5"),
            D("100000"),
            entry=True,
            entry_multiple=D(2),
            **kwargs,
        )


def test_zero_after_rounding_rejected():
    with pytest.raises(OrderRuleError, match="zero"):
        build_limit_order(TINY, Side.BUY, D("0.5"), D("1"), decision_id=1, n=1, min_value_tmn=MIN)


def test_client_id_requires_decision():
    assert make_client_id(42, 3) == "wx-42-3"
    with pytest.raises(OrderRuleError):
        make_client_id("", 1)


# --- close in full ----------------------------------------------------------


def test_exit_full_when_desired_covers_position():
    assert exit_quantity(SOLTMN_NO_MKT, D("3.456"), D("5"), D("100000"), min_value_tmn=MIN) == D(
        "3.45"
    )


def test_exit_full_when_remainder_below_min():
    # Position 1.5 SOL @ 100k = 150k. Selling 1.0 leaves 50k < min → sell all.
    assert exit_quantity(SOLTMN_NO_MKT, D("1.5"), D("1.0"), D("100000"), min_value_tmn=MIN) == D(
        "1.50"
    )


def test_partial_exit_when_both_sides_above_min():
    assert exit_quantity(SOLTMN_NO_MKT, D("5"), D("2"), D("100000"), min_value_tmn=MIN) == D("2.00")


def test_partial_exit_too_small_holds():
    # Selling 0.5 (50k) is below min while the remainder stays sellable → hold (0).
    assert exit_quantity(SOLTMN_NO_MKT, D("5"), D("0.5"), D("100000"), min_value_tmn=MIN) == 0


# --- MARKET-disabled markets: aggressive LIMIT exits ------------------------


def test_aggressive_price_sell_and_buy():
    b = book("100000", "100500")
    # SELL: 100000 * 0.995 = 99500; BUY: 100500 * 1.005 = 101002.5 → ceil 101003
    assert aggressive_price(SOLTMN_NO_MKT, Side.SELL, b, D("0.5")) == D("99500")
    assert aggressive_price(SOLTMN_NO_MKT, Side.BUY, b, D("0.5")) == D("101003")


def test_aggressive_price_precision_2_rounds_toward_crossing():
    b = book("2437.55", "2437.99")
    # SELL: 2437.55 * 0.995 = 2425.36225 → floor 2425.36
    assert aggressive_price(ETHUSDT, Side.SELL, b, D("0.5")) == D("2425.36")
    # BUY: 2437.99 * 1.005 = 2450.17995 → ceil 2450.18
    assert aggressive_price(ETHUSDT, Side.BUY, b, D("0.5")) == D("2450.18")


def test_exit_uses_market_when_enabled():
    spec = choose_exit_order(
        BTCTMN,
        Side.SELL,
        D("0.01"),
        book("1", "2"),
        decision_id=9,
        n=1,
        slippage_pct=D("0.5"),
    )
    assert spec.type is OrderType.MARKET and spec.price is None


def test_exit_uses_aggressive_limit_when_market_disabled():
    spec = choose_exit_order(
        SOLTMN_NO_MKT,
        Side.SELL,
        D("2.009"),
        book("100000", "100500"),
        decision_id=9,
        n=1,
        slippage_pct=D("0.5"),
    )
    assert spec.type is OrderType.LIMIT
    assert spec.price == D("99500")
    assert spec.quantity == D("2.00")
    assert spec.client_id == "wx-9-1"


def test_exit_without_bids_raises():
    empty = Book(bid=[], ask=[DepthLevel(price=D(1), quantity=D(1))])
    with pytest.raises(OrderRuleError, match="no bids"):
        choose_exit_order(
            SOLTMN_NO_MKT, Side.SELL, D(1), empty, decision_id=1, n=1, slippage_pct=D("0.5")
        )


def _order(orig: str, executed: str) -> Order:
    return Order.model_validate(
        {
            "clientOrderId": "wx-9-1",
            "symbol": "SOLTMN",
            "type": "LIMIT",
            "side": "SELL",
            "origQty": orig,
            "executedQty": executed,
            "active": False,
        }
    )


def test_reprice_uses_remaining_qty_and_new_client_id():
    d = reprice_exit(
        SOLTMN_NO_MKT,
        _order("3.00", "1.25"),
        book("99000", "99400"),
        decision_id=9,
        attempt=1,
        slippage_pct=D("0.5"),
        max_reprices=5,
    )
    assert d.spec is not None
    assert d.spec.quantity == D("1.75")  # remaining, not the original 3.00
    assert d.spec.price == D("98505")  # fresh book: 99000 * 0.995
    assert d.spec.client_id == "wx-9-2"
    assert not d.exhausted


def test_reprice_done_when_filled_and_exhausted_flag():
    kwargs = dict(decision_id=9, slippage_pct=D("0.5"), max_reprices=5)
    done = reprice_exit(SOLTMN_NO_MKT, _order("3", "3"), book("1", "2"), attempt=1, **kwargs)
    assert done == RepriceDecision(None, False)
    last = reprice_exit(SOLTMN_NO_MKT, _order("3", "1"), book("1000", "1010"), attempt=5, **kwargs)
    assert last.exhausted and last.spec is not None


# --- learning the minimum from a rejection ----------------------------------


@pytest.mark.parametrize(
    "message,fields,expected",
    [
        ("Minimum order value is 100,000 TMN", None, D("100000")),
        ("حداقل مبلغ سفارش ۵۰٬۰۰۰ تومان است", None, D("50000")),
        (
            "validation failed",
            {"price": ["مبلغ سفارش کمتر از حداقل ۲۰۰۰۰۰ تومان است"]},
            D("200000"),
        ),
        ("something went wrong", None, None),  # not a minimum error
        ("minimum is 100 or 200", None, None),  # ambiguous → never guess
    ],
)
def test_parse_min_from_error(message, fields, expected):
    assert min_order.parse_min_from_error(message, fields) == expected


def test_handle_rejection_persists_override(tmp_path):
    overrides = tmp_path / "overrides.toml"
    settings = Settings(
        cfg=AppConfig(store=StoreCfg(overrides_path=str(overrides))),
        secrets=Secrets.model_construct(),  # don't read the real .env in tests
    )
    events = []
    err = ApiError(422, "حداقل ارزش سفارش ۱۵۰,۰۰۰ تومان")
    learned = min_order.handle_rejection(
        settings, err, "TMN", on_event=lambda k, p: events.append(k)
    )
    assert learned == D("150000")
    assert settings.cfg.orders.min_order_value_tmn == D("150000")
    assert settings.cfg.orders.min_order_verified is True
    text = overrides.read_text("utf-8")
    assert 'min_order_value_tmn = "150000"' in text and "min_order_verified = true" in text
    assert events == ["exchange.min_order_learned"]
