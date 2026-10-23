"""CLI safety: the human-in-the-loop prompt before any live start, and remote mode being read-only.

Everything runs in a temporary WALLEXER_HOME (own DB, no keys); nothing is spawned for real.
"""

import json
import sqlite3

import httpx
import pytest

from wallexer.cli import app as cli_app
from wallexer.cli import engine_cmds, source


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLEXER_HOME", str(tmp_path))
    for k in ("WALLEXER_VIEW_TOKEN", "WALLEXER_REMOTE_URL", "WALLEX_API_KEY", "BOXAPI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    source.FORCED_REMOTE["on"] = False
    yield tmp_path
    source.FORCED_REMOTE["on"] = False


def _answers(monkeypatch, *answers):
    """Script console.input; record the prompts shown."""
    it, prompts = iter(answers), []

    def fake(prompt="", **kw):
        prompts.append(prompt)
        return next(it)

    monkeypatch.setattr(cli_app.console, "input", fake)
    return prompts


def _events(home, kind):
    conn = sqlite3.connect(home / "data" / "wallexer.db")
    rows = [
        json.loads(r[0]) for r in conn.execute("SELECT payload FROM events WHERE kind=?", (kind,))
    ]
    conn.close()
    return rows


@pytest.fixture
def spawns(monkeypatch):
    calls = []
    monkeypatch.setattr(engine_cmds, "spawn", lambda s, mode, mk: calls.append((mode, mk)) or 4242)
    monkeypatch.setattr(engine_cmds, "wait_ready", lambda s, c: True)
    monkeypatch.setattr(cli_app, "_engine_mode", lambda s: None)
    return calls


# --- human in the loop ------------------------------------------------------------------------


def test_trade_default_answer_reviews_first_and_starts_nothing(home, monkeypatch, spawns):
    prompts = _answers(monkeypatch, "")  # Enter = default Y = review first
    assert cli_app.run_args(["trade", "--no-watch"]) == 1
    assert spawns == []
    assert "Run/review backtest and shadowtest first? \\[Y/n]" in prompts[0]
    assert len(prompts) == 1  # never got to the TRADE confirmation
    [ev] = _events(home, "cli.trade_hitl_prompt")
    assert ev["review_first"] is True and ev["answer"] == "Y(default)"


def test_trade_needs_exact_typed_confirmation(home, monkeypatch, spawns):
    _answers(monkeypatch, "n", "trade")  # wrong case is not a confirmation
    assert cli_app.run_args(["trade", "--no-watch"]) == 1
    assert spawns == []
    [ev] = _events(home, "cli.trade_confirm")
    assert ev["confirmed"] is False and ev["context"] == "trade whole universe"


def test_trade_starts_once_after_n_and_TRADE(home, monkeypatch, spawns):
    from wallexer.config import Mode

    _answers(monkeypatch, "n", "TRADE")
    assert cli_app.run_args(["trade", "ETHTMN", "--no-watch"]) == 0
    assert spawns == [(Mode.trade, ["ETHTMN"])]
    assert _events(home, "cli.trade_hitl_prompt")[0]["review_first"] is False
    assert _events(home, "cli.trade_confirm")[0]["confirmed"] is True


def test_prompt_shows_latest_backtest_and_shadow_results(home, monkeypatch, spawns):
    shown = []
    monkeypatch.setattr(engine_cmds, "show_latest_results", lambda s, c: shown.append(True))
    _answers(monkeypatch, "")
    cli_app.run_args(["trade", "--no-watch"])
    assert shown == [True]


def test_change_into_trade_asks_before_stopping_anything(home, monkeypatch):
    from wallexer.cli import change as change_mod

    stopped = []
    monkeypatch.setattr(change_mod, "_running_mode", lambda s: "shadowtest")
    monkeypatch.setattr(change_mod, "_stop_engine", lambda *a: stopped.append(a) or True)
    monkeypatch.setattr(engine_cmds, "spawn", lambda *a: pytest.fail("spawned"))
    _answers(monkeypatch, "y", "")  # yes to switching, then default "review first"
    assert cli_app.run_args(["change", "shadowtest", "trade"]) == 1
    assert stopped == []  # the shadow engine was left running
    assert _events(home, "cli.trade_hitl_prompt")[0]["context"] == "change shadowtest trade"


# --- remote mode is read-only -----------------------------------------------------------------

WRITES = [
    ["trade"],
    ["trade", "ETHTMN"],
    ["stop"],
    ["stop", "ETHTMN"],
    ["flatten"],
    ["change", "shadowtest", "trade"],
    ["change", "trade", "shadowtest"],
    ["resume"],
    ["config", "guardrail"],
    ["autoset", "guardrail"],
    ["shadowtest"],
    ["agent", "now"],
    ["engine", "run"],
    ["doctor", "--live-smoke"],
]


@pytest.mark.parametrize("cmd", WRITES, ids=" ".join)
def test_remote_mode_refuses_writes_without_any_request(home, monkeypatch, cmd):
    monkeypatch.setenv("WALLEXER_REMOTE_URL", "http://server.invalid")
    monkeypatch.setenv("WALLEXER_VIEW_TOKEN", "t")
    sent = []
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: sent.append(a) or pytest.fail("http"))
    monkeypatch.setattr(
        "wallexer.engine.control.send", lambda *a, **k: sent.append(a) or pytest.fail("socket")
    )
    errors = []
    monkeypatch.setattr(cli_app, "_err", lambda msg, hint: errors.append(msg))
    monkeypatch.setattr(cli_app.console, "input", lambda *a, **k: pytest.fail("prompted"))
    assert cli_app.run_args(["--remote", *cmd]) == 2
    assert sent == []
    assert errors and "disabled in remote mode" in errors[0]


def test_remote_default_from_config_also_refuses(home, monkeypatch):
    (home / "config.toml").write_text('[remote]\nurl = "http://server.invalid"\ndefault = true\n')
    monkeypatch.setattr("wallexer.engine.control.send", lambda *a, **k: pytest.fail("socket"))
    errors = []
    monkeypatch.setattr(cli_app, "_err", lambda msg, hint: errors.append(msg))
    assert cli_app.run_args(["stop"]) == 2
    assert "disabled in remote mode" in errors[0]


async def test_view_api_is_get_only_token_protected_and_read_only(home, monkeypatch):
    from wallexer.engine.api import ReadOnlyApi
    from wallexer.store.db import init_db

    db = home / "v.db"
    init_db(db).close()
    api = ReadOnlyApi(db, lambda: {"mode": "shadow"}, 5e6, 5e6)
    monkeypatch.delenv("WALLEXER_VIEW_TOKEN", raising=False)
    assert (await api.handle("GET", "/api/status", {}))[0] == 404  # no token set: no API at all
    monkeypatch.setenv("WALLEXER_VIEW_TOKEN", "s3cret")
    assert (await api.handle("GET", "/api/status", {}))[0] == 401
    assert (await api.handle("GET", "/api/status", {"x-view-token": "wrong"}))[0] == 401
    ok = {"x-view-token": "s3cret"}
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert (await api.handle(method, "/api/log", ok))[0] == 405
    assert await api.handle("GET", "/api/status", ok) == (200, {"mode": "shadow"})
    assert (await api.handle("GET", "/api/log?n=1", ok))[0] == 200
    with pytest.raises(sqlite3.OperationalError):
        api._connect().execute("INSERT INTO events (ts, kind) VALUES ('x', 'y')")
