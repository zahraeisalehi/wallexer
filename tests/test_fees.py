"""Fee table parsing (supports critical test 3: PnL needs a fee for every market).

Regression: /v1/account/fee on the trading-credit account returned result = [] and crashed doctor.
"""

from decimal import Decimal as D

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from wallexer.config import AppConfig, Secrets, Settings
from wallexer.exchange.client import WallexClient
from wallexer.exchange.fees import parse_fee_table

DM, DT = D("0.003"), D("0.003")


@pytest.mark.parametrize("result", [[], {}, None, "unexpected"])
def test_no_fee_data_falls_back_to_default(result):
    t = parse_fee_table(result, DM, DT)
    assert t.rates == {}
    assert t.source == "default"
    fee = t.get("BTCTMN")
    assert (fee.maker, fee.taker, fee.source) == (DM, DT, "default")


def test_empty_list_entries_are_skipped_others_kept():
    result = {
        "BTCTMN": {"makerFeeRate": "0.00100000", "takerFeeRate": "0.00150000"},
        "ETHTMN": [],
        "USDTTMN": {},
        "SOLTMN": {"makerFeeRate": "not-a-number"},
    }
    t = parse_fee_table(result, DM, DT)
    assert set(t.rates) == {"BTCTMN"}
    assert sorted(t.skipped) == ["ETHTMN", "SOLTMN", "USDTTMN"]
    assert t.source == "mixed"
    assert t.get("BTCTMN").maker == D("0.001") and t.get("BTCTMN").source == "exchange"
    assert t.get("ETHTMN").source == "default"


def test_observed_shape_meta_keys_are_not_markets():
    # Shape returned by the trading-credit account: "default" is [], "metaData" is tier info.
    result = {
        "BTCTMN": {
            "makerFeeRate": "0.00250000",
            "takerFeeRate": "0.00300000",
            "recent_days_sum": 0,
        },
        "default": [],
        "metaData": {"levels": [], "coin_levels": [], "latestTradesSum": 0},
    }
    t = parse_fee_table(result, DM, DT)
    assert set(t.rates) == {"BTCTMN"}
    assert t.skipped == []
    assert sorted(t.meta_keys) == ["default", "metaData"]
    assert t.source == "exchange"
    assert t.get("BTCTMN").taker == D("0.003")


def test_shape_description_has_structure_only():
    t = parse_fee_table({"BTCTMN": {"makerFeeRate": "0.002", "takerFeeRate": "0.002"}}, DM, DT)
    assert t.raw_shape == {
        "type": "dict",
        "len": 1,
        "sample_keys": ["BTCTMN"],
        "entry_type": "dict",
        "entry_keys": ["makerFeeRate", "takerFeeRate"],
    }
    assert parse_fee_table([], DM, DT).raw_shape == {"type": "list", "len": 0}


async def test_client_fees_with_empty_list_result_does_not_crash():
    key = "test-key-not-real"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/account/fee"
        assert request.headers["sub-account-client-id"] == "sub-1"
        return httpx.Response(200, json={"success": True, "message": "ok", "result": []})

    settings = Settings(
        cfg=AppConfig(),
        secrets=Secrets.model_construct(
            wallex_api_key=SecretStr(key), wallex_subaccount_id="sub-1"
        ),
    )
    client = WallexClient(settings, transport=httpx.MockTransport(handler))
    try:
        with capture_logs() as logs:
            table = await client.fees()
            await client.fees()  # shape is logged once per client
    finally:
        await client.aclose()

    assert table.source == "default"
    assert table.get("BTCTMN").taker == D("0.003")  # config default (highest tier)
    shape_logs = [e for e in logs if e["event"] == "fees.raw_shape"]
    assert len(shape_logs) == 1
    assert shape_logs[0]["shape"] == {"type": "list", "len": 0}
    assert key not in repr(logs)
