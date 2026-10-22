"""The live smoke test against a fake Wallex (httpx.MockTransport): the real order path
(latch, limiter, place/get/openOrders/cancel, logging, decision + order rows) without the exchange.

Regression: a duplicate `client_id` keyword in the order.submit log line crashed the first real run
before the POST. Any such crash in the order path now fails this test.
"""

import json
from decimal import Decimal as D

import httpx
from pydantic import SecretStr
from rich.console import Console

from wallexer.config import AppConfig, EngineCfg, Secrets, Settings, StoreCfg

MARKET = {
    "symbol": "BTCTMN",
    "base_asset": "BTC",
    "quote_asset": "TMN",
    "price": "22000000000",
    "is_spot": True,
    "is_tmn_based": True,
    "amount_precision": 8,
    "price_precision": 0,
    "is_market_type_enable": True,
}


class FakeWallex:
    """Order book + balances with exchange-like locking (value = free, locked = in orders)."""

    def __init__(self, reject: bool = False, fills: object = (), broken_post: bool = False) -> None:
        self.reject = reject
        self.fills = list(fills) if isinstance(fills, tuple) else fills  # live shapes: None, {}, []
        self.broken_post = broken_post
        self.orders: dict[str, dict] = {}
        self.posts = 0
        self.value, self.locked = D(5_000_000), D(0)

    def _ok(self, result, code: int = 200) -> httpx.Response:
        return httpx.Response(code, json={"success": True, "message": "ok", "result": result})

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers.get("sub-account-client-id") == "sub-1" or not req.url.path.startswith(
            "/v1/account"
        )
        path, m = req.url.path, req.method
        if path == "/hector/web/v1/markets":
            return self._ok({"markets": [MARKET]})
        if path == "/v1/depth":
            return self._ok(
                {
                    "bid": [{"price": 22000000000, "quantity": 0.5}],
                    "ask": [{"price": 22010000000, "quantity": 0.5}],
                }
            )
        if path == "/v1/account/balances":
            return self._ok(
                {
                    "balances": {
                        "TMN": {
                            "asset": "TMN",
                            "value": str(self.value),
                            "locked": str(self.locked),
                            "fiat": True,
                        }
                    }
                }
            )
        if path == "/v1/account/openOrders":
            return self._ok({"orders": [o for o in self.orders.values() if o["active"]]})
        if path == "/v1/account/orders" and m == "POST":
            self.posts += 1
            body = json.loads(req.content)
            if self.reject:
                return httpx.Response(
                    422,
                    json={
                        "success": False,
                        "code": 422,
                        "message": "اطلاعات وارد شده اشتباه‌است.",
                        "result": {"price": ["قیمت خارج از محدوده مجاز است."]},
                    },
                )
            notional = D(body["price"]) * D(body["quantity"])
            self.value -= notional
            self.locked += notional
            o = {
                "clientOrderId": body["client_id"],
                "symbol": body["symbol"],
                "type": body["type"],
                "side": body["side"],
                "price": body["price"],
                "origQty": body["quantity"],
                "executedQty": "0",
                "status": "NEW",
                "active": True,
                "fills": self.fills,
            }
            self.orders[body["client_id"]] = o
            if self.broken_post:  # 2xx, but a body we cannot parse as an order
                return self._ok({"unexpected": True}, 201)
            return self._ok(o, 201)
        if path.startswith("/v1/account/orders/"):
            cid = path.rsplit("/", 1)[1]
            o = self.orders.get(cid)
            if o is None:
                return httpx.Response(
                    404, json={"success": False, "code": 404, "message": "not found", "result": {}}
                )
            if m == "DELETE" and o["active"]:
                notional = D(o["price"]) * D(o["origQty"])
                self.value += notional
                self.locked -= notional
                o.update(active=False, status="CANCELED")
            return self._ok(o)
        return httpx.Response(404, json={"success": False, "message": f"unexpected {m} {path}"})


def _settings(tmp_path) -> Settings:
    return Settings(
        cfg=AppConfig(
            store=StoreCfg(db_path=str(tmp_path / "t.db"), overrides_path=str(tmp_path / "o.toml")),
            engine=EngineCfg(socket_path=str(tmp_path / "none.sock")),
        ),
        secrets=Secrets.model_construct(
            wallex_api_key=SecretStr("test-key"), wallex_subaccount_id="sub-1"
        ),
    )


async def _run(tmp_path, monkeypatch, fake: FakeWallex):
    from wallexer.cli import smoke
    from wallexer.exchange import client as client_mod

    real = client_mod.WallexClient

    def patched(settings, **kw):
        return real(settings, transport=httpx.MockTransport(fake.handler), **kw)

    monkeypatch.setattr(smoke, "WallexClient", patched)
    monkeypatch.setattr(smoke.asyncio, "sleep", _no_sleep)
    console = Console(record=True, width=200)
    monkeypatch.setattr(console, "input", lambda prompt="": "SMOKE")
    settings = _settings(tmp_path)
    monkeypatch.setattr("wallexer.cli.engine_cmds.get_status", lambda s: (None, False))
    ok = await smoke.run_smoke(settings, console)
    return ok, console.export_text(), settings


async def _no_sleep(_s):
    return None


async def test_smoke_flow_places_one_order_and_cancels(tmp_path, monkeypatch):
    fake = FakeWallex()
    ok, out, settings = await _run(tmp_path, monkeypatch, fake)
    assert ok, out
    assert fake.posts == 1  # exactly one order, ever
    [o] = fake.orders.values()
    assert o["active"] is False and o["status"] == "CANCELED"
    assert D(o["price"]) == D("17600000000")  # 22e9 * 0.80, rounded down
    assert D("100000") <= D(o["price"]) * D(o["origQty"]) <= D("101000")
    assert fake.value == D(5_000_000) and fake.locked == 0  # restored
    assert "value = FREE balance" in out and "SMOKE TEST PASSED" in out

    import sqlite3

    db = sqlite3.connect(settings.cfg.path(settings.cfg.store.db_path))
    ((did, mode),) = db.execute("SELECT id, mode FROM decisions").fetchall()
    assert mode == "live_smoke"
    assert db.execute("SELECT decision_id, mode FROM orders").fetchall() == [(did, "live_smoke")]
    payload = json.loads(
        db.execute("SELECT payload FROM events WHERE kind='smoke.result'").fetchone()[0]
    )
    assert payload["ok"] is True and payload["facts"]["value_excludes_locked"] is True


async def test_smoke_rejection_is_not_retried(tmp_path, monkeypatch):
    fake = FakeWallex(reject=True)
    ok, out, _ = await _run(tmp_path, monkeypatch, fake)
    assert not ok
    assert fake.posts == 1  # no retry after a rejection
    assert "rejected" in out and "SMOKE TEST FAILED" in out
    # the full error (status, message, per-field result) is printed and stored
    assert "قیمت خارج از محدوده" in out and '"status": 422' in out and "Sent body" in out
    import sqlite3

    db = sqlite3.connect(tmp_path / "t.db")
    (err,) = db.execute("SELECT error FROM orders").fetchone()
    detail = json.loads(err)
    assert detail["status"] == 422 and detail["result"] == {
        "price": ["قیمت خارج از محدوده مجاز است."]
    }
    payload = json.loads(
        db.execute("SELECT payload FROM events WHERE kind='smoke.result'").fetchone()[0]
    )
    assert payload["facts"]["rejection"]["result"]["price"] and payload["facts"]["sent"][
        "client_id"
    ].startswith("wx-smoke")


async def test_every_private_request_carries_the_subaccount_header():
    """Reads, order create and both cancel variants all send x-api-key + sub-account-client-id."""
    from wallexer.exchange.client import WallexClient
    from wallexer.exchange.models import OrderType, Side
    from wallexer.exchange.rules import OrderSpec

    seen: list[tuple[str, str, set[str]]] = []
    order = {
        "clientOrderId": "wx-t-1",
        "symbol": "BTCTMN",
        "type": "LIMIT",
        "side": "BUY",
        "price": "1",
        "origQty": "1",
        "executedQty": "0",
        "active": True,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, {h.lower() for h in req.headers}))
        if req.url.path.endswith("balances"):
            return httpx.Response(200, json={"success": True, "result": {"balances": {}}})
        if "openOrders" in req.url.path:
            return httpx.Response(200, json={"success": True, "result": {"orders": []}})
        return httpx.Response(200, json={"success": True, "result": order})

    c = WallexClient(_settings_plain(), transport=httpx.MockTransport(handler), orders_enabled=True)
    await c.balances()
    await c.fees()
    await c.open_orders("BTCTMN")
    await c.get_order("wx-t-1")
    await c.place_order(
        OrderSpec("BTCTMN", Side.BUY, OrderType.LIMIT, D(1), D(1), "wx-t-1", "1"), quote_asset="TMN"
    )
    await c.cancel_order("wx-t-1")
    await c.cancel_order_body("wx-t-1")
    await c.aclose()
    assert {m for m, _p, _h in seen} == {"GET", "POST", "DELETE"}
    for method, path, headers in seen:
        assert {"x-api-key", "sub-account-client-id"} <= headers, (method, path)


def _settings_plain() -> Settings:
    return Settings(
        cfg=AppConfig(),
        secrets=Secrets.model_construct(
            wallex_api_key=SecretStr("k"), wallex_subaccount_id="sub-1"
        ),
    )


async def test_smoke_flow_with_live_fills_shapes(tmp_path, monkeypatch):
    """Shapes seen live: an open order's `fills` was not a list (null / {})."""
    for fills in (None, {}):
        fake = FakeWallex(fills=fills)
        ok, out, _ = await _run(tmp_path / str(type(fills).__name__), monkeypatch, fake)
        assert ok, out
        assert fake.posts == 1


async def test_unparseable_2xx_place_response_falls_back_to_lookup(tmp_path, monkeypatch):
    """The order exists after a 2xx: never fail-and-resend; look it up by client_id instead."""
    fake = FakeWallex(broken_post=True)
    ok, out, _ = await _run(tmp_path, monkeypatch, fake)
    assert ok, out
    assert fake.posts == 1


def test_order_and_book_models_tolerate_collection_shapes():
    from wallexer.exchange.models import Book, Order

    base = {
        "clientOrderId": "x",
        "symbol": "BTCTMN",
        "type": "LIMIT",
        "side": "BUY",
        "origQty": "1",
    }
    assert Order.model_validate({**base, "fills": None}).fills == []
    assert Order.model_validate({**base, "fills": {}}).fills == []
    assert Order.model_validate(base).fills == []
    one = {"price": "1", "quantity": "2"}
    assert len(Order.model_validate({**base, "fills": [one]}).fills) == 1
    assert len(Order.model_validate({**base, "fills": {"0": one}}).fills) == 1
    assert Book.model_validate({"bid": None, "ask": {}}).best_bid is None
