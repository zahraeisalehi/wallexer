"""Critical test 2: risk gate enforces limits, rejects restricted markets, clamps LLM weights.

The gate may only shrink or reject, never enlarge.
"""

from decimal import Decimal as D

import pytest

from wallexer.exchange.models import Side
from wallexer.exchange.rules import MarketRules
from wallexer.risk import gate
from wallexer.risk.guardrails import Limits, clamp_weights

NOW = 1_800_000_000.0
BTCTMN = MarketRules("BTCTMN", "BTC", "TMN", 0, 8)
ETHUSDT = MarketRules("ETHUSDT", "ETH", "USDT", 2, 5)
SOLTMN = MarketRules("SOLTMN", "SOL", "TMN", 0, 2, market_orders_enabled=False)
PX = D("20000000000")  # BTC price in TMN


def limits(**kw) -> Limits:
    base = dict(
        max_position_pct=D(20),
        max_exposure_pct=D(60),
        max_order_pct=D(10),
        min_order_tmn=D(100000),
        entry_multiple=D(2),
        max_open_orders=10,
        daily_dd_soft_pct=D(4),
        total_dd_hard_pct=D(8),
        soft_stop_pause_s=1800,
        no_entry_before_end_s=90 * 60,
        force_flatten_before_end_s=30 * 60,
        forbidden=frozenset({"ATMN"}),
        unavailable=frozenset({"DAITMN"}),
        limit_only=frozenset({"SOLTMN"}),
    )
    base.update(kw)
    return Limits(**base)


def state(**kw) -> gate.RiskState:
    base = dict(
        now=NOW,
        equity_tmn=D(5_000_000),
        peak_equity_tmn=D(5_000_000),
        day_start_equity_tmn=D(5_000_000),
        positions_tmn={},
        held_qty={},
        open_orders=0,
        usdt_tmn=D(270000),
        event_end=NOW + 86400,
    )
    base.update(kw)
    return gate.RiskState(**base)


def buy(sym="BTCTMN", qty=D("0.01"), price=PX) -> gate.Intent:
    return gate.Intent(sym, Side.BUY, qty, price, is_exit=False)


def test_order_value_is_shrunk_to_max_order_pct():
    # wants 0.1 BTC = 2e9 TMN; max order 10% of 5M = 500,000 TMN → 0.000025 BTC
    r = gate.check(buy(qty=D("0.1")), state(), limits(), BTCTMN)
    assert r.approved and r.qty == D("0.000025")


def test_position_cap_and_halved_for_limit_only():
    # existing BTC position 900k; position cap 20% = 1M → room 100k < entry floor 200k → reject
    r = gate.check(
        buy(qty=D("0.0001")), state(positions_tmn={"BTCTMN": D(900_000)}), limits(), BTCTMN
    )
    assert not r.approved and "entry floor" in r.reason
    # LIMIT-only market: cap 10% = 500k; wants 1M worth → shrunk to 500k
    r = gate.check(buy("SOLTMN", D(10), D(100_000)), state(), limits(max_order_pct=D(50)), SOLTMN)
    assert r.approved and r.qty == D(5)


def test_total_exposure_cap():
    pos = {"ETHTMN": D(1_500_000), "XAUTTMN": D(1_400_000)}  # 2.9M of 3M allowed
    r = gate.check(buy(qty=D("0.0001")), state(positions_tmn=pos), limits(), BTCTMN)
    assert not r.approved  # 100k room < 200k entry floor


def test_gate_never_enlarges():
    for qty in (D("0.00001"), D("0.000012"), D("1")):
        r = gate.check(buy(qty=qty), state(), limits(), BTCTMN)
        assert r.qty <= qty


@pytest.mark.parametrize("sym,why", [("ATMN", "not_allowed"), ("DAITMN", "unavailable")])
def test_restricted_markets_rejected(sym, why):
    rules = MarketRules(sym, sym[:-3], "TMN", 0, 2)
    r = gate.check(buy(sym, D(1000), D(1000)), state(), limits(), rules)
    assert not r.approved and why in r.reason


def test_exits_always_pass_even_on_restricted_and_hard_stop_but_capped_to_held():
    rules = MarketRules("ATMN", "A", "TMN", 0, 2)
    s = state(hard_stopped=True, held_qty={"ATMN": D("3.5")})
    r = gate.check(gate.Intent("ATMN", Side.SELL, D(10), D(1000), is_exit=True), s, limits(), rules)
    assert r.approved and r.qty == D("3.50")


def test_entries_blocked_from_t_minus_90_and_on_stops():
    late = state(event_end=NOW + 89 * 60)
    assert not gate.check(buy(), late, limits(), BTCTMN).approved
    hard = state(equity_tmn=D(4_590_000))  # 8.2% below peak
    assert "hard drawdown" in gate.check(buy(), hard, limits(), BTCTMN).reason
    soft = state(equity_tmn=D(4_790_000))  # 4.2% daily
    assert "soft stop" in gate.check(buy(), soft, limits(), BTCTMN).reason
    paused = state(soft_stop_until=NOW + 60)
    assert not gate.check(buy(), paused, limits(), BTCTMN).approved


def test_max_open_orders_and_usdt_market_valuation():
    assert not gate.check(buy(), state(open_orders=10), limits(), BTCTMN).approved
    # 0.1 ETH * 2400 USDT * 270,000 = 64.8M TMN → shrunk to 500k
    r = gate.check(buy("ETHUSDT", D("0.1"), D(2400)), state(), limits(), ETHUSDT)
    assert r.approved and r.qty * D(2400) * D(270000) <= D(500_000)


def test_clamp_llm_weights():
    w = clamp_weights(
        {"momentum": 0.9, "mean_reversion": -0.3, "rogue": 5, "basis_tmn_usdt": "nan"},
        allowed=["momentum", "mean_reversion", "basis_tmn_usdt"],
        max_weight=D("0.6"),
    )
    assert w == {"momentum": D("0.6"), "mean_reversion": D(0), "basis_tmn_usdt": D(0)}
    w = clamp_weights({"a": 0.6, "b": 0.6}, allowed=["a", "b"], max_weight=D("0.6"))
    assert sum(w.values()) == 1 and w["a"] == w["b"]
    # unpromoted (not allowed) strategies cannot receive weight
    assert clamp_weights({"a": 0.5}, allowed=[], max_weight=D(1)) == {}


# --- LLM agent decisions can only shrink risk ---------------------------------------------


def _decision(**kw):
    from wallexer.agent.schema import AgentDecision

    base = dict(
        stance="active",
        strategy_weights={"basis_tmn_usdt": 1.0, "mean_reversion": 0.9, "momentum": 0.4},
        active_markets=[],
        pause_markets=[],
        risk_multiplier=1.0,
        rationale="test",
        watch_requests=[],
    )
    base.update(kw)
    return AgentDecision.model_validate(base)


def _apply(d, promoted=("momentum",), engine_rm=D(1)):
    from wallexer.agent.agent import apply_decision

    return apply_decision(
        d,
        promoted=promoted,
        paper_only=["basis_tmn_usdt"],
        max_weight=D("0.6"),
        engine_risk_multiplier=engine_rm,
        universe=["BTCTMN", "ETHTMN", "USDTTMN"],
        max_watch_books=3,
    )


def test_agent_weights_only_for_promoted_and_clamped():
    a = _apply(_decision())
    assert a.weights == {"momentum": D("0.4")}  # mean_reversion unpromoted, basis paper-only
    assert any("not promoted" in n for n in a.notes)
    a = _apply(
        _decision(strategy_weights={"basis_tmn_usdt": 0, "mean_reversion": 0, "momentum": 1.0})
    )
    assert a.weights == {"momentum": D("0.6")}  # clamped to max_strategy_weight
    assert _apply(_decision(), promoted=()).weights == {}


def test_agent_flat_stance_zeroes_weights():
    a = _apply(_decision(stance="flat"))
    assert all(w == 0 for w in a.weights.values())


def test_agent_risk_multiplier_can_only_lower():
    assert _apply(_decision(risk_multiplier=1.0), engine_rm=D("0.5")).risk_multiplier == D("0.5")
    assert _apply(_decision(risk_multiplier=0.3), engine_rm=D("0.5")).risk_multiplier == D("0.3")


def test_agent_pauses_and_active_markets_only_restrict():
    a = _apply(_decision(pause_markets=["ethtmn", "FAKE"], active_markets=["BTCTMN", "NOPE"]))
    assert a.agent_paused == {"ETHTMN"}  # unknown markets cannot be added
    assert a.active_markets == {"BTCTMN"}
    assert (
        _apply(_decision(active_markets=["NOPE"])).active_markets is None
    )  # invalid → no restriction


def test_agent_watch_requests_limited_to_universe_and_max():
    reqs = [
        {"strategy": "momentum", "timeframe": 15, "market": m, "action": "paper", "why": "x"}
        for m in ("BTCTMN", "ETHTMN", "USDTTMN", "BTCTMN")
    ] + [
        {
            "strategy": "mean_reversion",
            "timeframe": 60,
            "market": "XAUTTMN",
            "action": "recheck",
            "why": "y",
        }
    ]
    a = _apply(_decision(watch_requests=reqs))
    assert len(a.watch_paper) == 3 and a.watch_recheck == []  # XAUTTMN not in this universe


def test_agent_schema_rejects_out_of_range():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _decision(risk_multiplier=1.5)
    with pytest.raises(ValidationError):
        _decision(strategy_weights={"basis_tmn_usdt": 0, "mean_reversion": 0, "momentum": -0.1})
    with pytest.raises(ValidationError):
        _decision(
            watch_requests=[
                {
                    "strategy": "momentum",
                    "timeframe": 7,
                    "market": "BTCTMN",
                    "action": "paper",
                    "why": "",
                }
            ]
        )
