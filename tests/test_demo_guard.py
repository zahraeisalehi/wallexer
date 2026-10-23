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


# --- public read-only view (WALLEXER_VIEW_PUBLIC=1, demo only) ----------------------------------


def test_view_public_without_demo_refuses_to_start(monkeypatch):
    monkeypatch.delenv("WALLEXER_DEMO")
    monkeypatch.setenv("WALLEXER_VIEW_PUBLIC", "1")
    with pytest.raises(ConfigError, match="WALLEXER_VIEW_PUBLIC"):
        Engine(load_settings(), Mode.shadow)


async def test_view_public_needs_demo_and_stays_get_only(demo, monkeypatch):
    from wallexer.engine.api import ReadOnlyApi
    from wallexer.store.db import init_db

    db = demo / "v.db"
    init_db(db).close()
    api = ReadOnlyApi(db, lambda: {"mode": "shadow"}, 5e6, 5e6)
    monkeypatch.setenv("WALLEXER_VIEW_PUBLIC", "1")
    monkeypatch.setenv("WALLEXER_VIEW_TOKEN", "s3cret")
    # without WALLEXER_DEMO the flag is ignored: the token is still required
    monkeypatch.delenv("WALLEXER_DEMO")
    assert (await api.handle("GET", "/api/status", {}))[0] == 401
    # demo + public: no token needed, and still GET only
    monkeypatch.setenv("WALLEXER_DEMO", "1")
    monkeypatch.delenv("WALLEXER_VIEW_TOKEN")
    assert await api.handle("GET", "/api/status", {}) == (200, {"mode": "shadow"})
    assert (await api.handle("GET", "/api/log?n=1", {}))[0] == 200
    assert (await api.handle("GET", "/api/explain/1", {}))[0] == 404  # not generated yet
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert (await api.handle(method, "/api/status", {}))[0] == 405
