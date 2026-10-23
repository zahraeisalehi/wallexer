"""Demo guard (WALLEXER_DEMO=1, the public wallexer-demo app): no trade mode, the order latch can
never open, and every trading command is refused, in the CLI and again in the engine."""

import pytest

from wallexer.cli import app as cli_app
from wallexer.cli import engine_cmds, source
from wallexer.config import ConfigError, Mode, load_settings
from wallexer.engine.daemon import Engine
from wallexer.exchange.client import WallexClient
from wallexer.exchange.errors import OrdersDisabled


@pytest.fixture(autouse=True)
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLEXER_HOME", str(tmp_path))
    monkeypatch.setenv("WALLEXER_DEMO", "1")
    for k in ("WALLEXER_VIEW_TOKEN", "WALLEXER_REMOTE_URL", "WALLEX_API_KEY", "BOXAPI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    source.FORCED_REMOTE["on"] = False
    return tmp_path


def test_engine_refuses_trade_mode():
    with pytest.raises(ConfigError, match="demo"):
        Engine(load_settings(), Mode.trade)


async def test_order_latch_can_never_be_enabled():
    client = WallexClient(load_settings(), orders_enabled=True)
    try:
        assert client._orders_enabled is False  # the constructor flag is ignored
        with pytest.raises(OrdersDisabled):
            client.enable_orders()
        client._orders_enabled = True  # even if something forces the flag
        with pytest.raises(OrdersDisabled):
            client._check_latch()
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "cmd",
    [
        ["trade"],
        ["trade", "BTCTMN"],
        ["stop"],
        ["flatten"],
        ["change", "shadowtest", "trade"],
        ["resume"],
    ],
    ids=" ".join,
)
def test_cli_refuses_trading_commands(monkeypatch, cmd):
    monkeypatch.setattr("wallexer.engine.control.send", lambda *a, **k: pytest.fail("socket"))
    monkeypatch.setattr(engine_cmds, "spawn", lambda *a, **k: pytest.fail("spawn"))
    monkeypatch.setattr(cli_app.console, "input", lambda *a, **k: pytest.fail("prompted"))
    errors = []
    monkeypatch.setattr(cli_app, "_err", lambda msg, hint: errors.append(msg))
    assert cli_app.run_args(cmd) == 2
    assert errors and "demo server" in errors[0]


@pytest.mark.parametrize("cmd", ["stop", "flatten", "resume", "start_markets"])
async def test_engine_control_refuses_trading_commands(cmd):
    engine = Engine.__new__(Engine)  # the guard runs before any engine state is touched
    res = await engine.handle({"cmd": cmd, "markets": ["BTCTMN"], "mode": "shadow"})
    assert res["ok"] is False and "demo" in res["error"]
