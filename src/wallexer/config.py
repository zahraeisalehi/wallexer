"""Configuration: TOML tunables + env secrets.

Layering (later wins): config/default.toml < data/overrides.toml < environment / .env.
data/overrides.toml holds values the engine learns at runtime (e.g. the real min order value),
so the checked-in defaults are never rewritten by the bot.
"""

from __future__ import annotations

import tomllib
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "default.toml"


class ConfigError(Exception):
    """Readable configuration problem (shown to the user without a traceback)."""


class Mode(StrEnum):
    backtest = "backtest"
    shadow = "shadow"
    trade = "trade"


class ExchangeCfg(BaseModel):
    base_url: str = "https://api.wallex.ir"
    timeout_s: float = 10.0
    order_timeout_s: float = 15.0
    get_retries: int = 3
    backoff_base_s: float = 0.5


class MarketLimitCfg(BaseModel):
    window_s: float = 39.0
    limit: int = 600
    backoff_below_frac: float = 0.10


class OrderLimitCfg(BaseModel):
    max_requests: int = 15
    window_s: float = 10.0
    backlog_warn: int = 5


class RateLimitCfg(BaseModel):
    market: MarketLimitCfg = MarketLimitCfg()
    orders: OrderLimitCfg = OrderLimitCfg()


class AccountCfg(BaseModel):
    expected_equity_tmn: Decimal = Decimal("5000000")
    equity_tolerance_pct: Decimal = Decimal("10")


class OrdersCfg(BaseModel):
    min_order_value_tmn: Decimal = Decimal("100000")
    min_order_verified: bool = False
    min_entry_multiple: Decimal = Decimal("2.0")
    exit_limit_slippage_pct: Decimal = Decimal("0.5")
    exit_reprice_after_s: float = 10
    exit_max_reprices: int = 5
    # Exit LIMITs never go further than this % from the exchange fair price (or the market's
    # own spot threshold if lower; Wallex rejects prices outside its band).
    exit_max_fair_deviation_pct: Decimal = Decimal("4")
    # A remainder below this (TMN value) after a full sell is accepted as dust and logged.
    dust_cap_tmn: Decimal = Decimal("1000")


class FeesCfg(BaseModel):
    # Used when /v1/account/fee returns no usable data for a market. Highest observed tier,
    # so a fallback never understates costs.
    default_maker: Decimal = Decimal("0.003")
    default_taker: Decimal = Decimal("0.003")


class UniverseCfg(BaseModel):
    top_n: int = 30
    # Default universe: markets where MARKET orders are enabled (exits can always be immediate).
    # LIMIT-only markets are opt-in; their max position is halved.
    include_limit_only: bool = False
    max_markets: int = 12
    unavailable_path: str = "data/unavailable_markets.txt"
    min_quote_volume_tmn: Decimal = Decimal("500000000")
    max_spread_bps: Decimal = Decimal("100")
    quote_assets: list[str] = ["TMN", "USDT"]
    not_allowed_path: str = "data/not_allowed.txt"


class LlmCfg(BaseModel):
    base_url: str = "https://ai.boxapi.ir/ai/v1"
    model: str = "gpt-5.4-mini"
    fallback_model: str = "gpt-5.4-mini"
    timeout_s: float = 30.0
    # Answer from recorded llm_calls (same prompt hash) instead of calling the API.
    replay: bool = False


class AgentCfg(BaseModel):
    enabled: bool = True
    interval_min: float = 30
    debounce_min: float = 5  # minimum gap between event-triggered cycles
    dd_step_pct: float = 1.0  # each further step of total drawdown triggers a cycle
    feed_error_threshold: int = 10  # feed events within 5 min that trigger a cycle
    max_watch_books: int = 3
    decision_ttl_min: float = 65  # a decision older than this no longer applies (fallback)


class GuardrailsCfg(BaseModel):
    max_position_pct: Decimal = Decimal("20")
    max_exposure_pct: Decimal = Decimal("60")
    max_order_pct: Decimal = Decimal("10")
    stop_loss_atr_mult: Decimal = Decimal("2")
    stop_loss_max_pct: Decimal = Decimal("2")
    daily_dd_soft_pct: Decimal = Decimal("4")
    soft_stop_pause_min: int = 30
    total_dd_hard_pct: Decimal = Decimal("8")
    max_consecutive_losses: int = 4
    loss_cooldown_min: int = 60
    max_open_orders: int = 10
    flatten_no_entry_min: int = 90
    flatten_force_min: int = 35  # force flatten from T-35 min (event end 20:20 UTC → 19:45)


class EngineCfg(BaseModel):
    depth_interval_s: float = 3.0
    trades_interval_s: float = 10.0
    candles_interval_s: float = 60.0
    allocator_interval_s: float = 60.0
    reconcile_interval_s: float = 30.0
    # Fraction of the market-data rate limit the feed may plan to use per window.
    feed_budget_frac: float = 0.7
    stale_after_intervals: float = 3.0
    frozen_book_s: float = 60.0
    socket_path: str = "data/engine.sock"
    health_port: int = 8000
    log_path: str = "data/logs/engine.log"


class PaperCfg(BaseModel):
    capital_tmn: Decimal = Decimal("5000000")
    slippage_bps: Decimal = Decimal("2")
    maker_timeout_s: float = 60.0
    # Paper books never put more than this fraction of their equity into one position.
    position_frac: Decimal = Decimal("0.2")


class StrategyCfg(BaseModel):
    enabled: bool = True
    # Candle timeframe in minutes: 1, 5, 15 (resampled from 1-min) or 60 (native).
    timeframe: int = 15
    # Markets by quote asset; empty = all universe markets.
    quotes: list[str] = []
    edge_margin_bps: Decimal = Decimal("10")
    # mean_reversion
    vwap_bars: int = 60
    z_entry: float = 2.0
    # exit target = VWAP + exit_frac x (entry deviation below VWAP)
    exit_frac: float = 0.25
    # expected move must be >= min_reward_risk x stop distance
    min_reward_risk: float = 1.5
    # momentum
    breakout_bars: int = 30
    vol_ratio_min: float = 1.5
    imbalance_min: float = 0.1
    atr_target_mult: float = 2.0
    # expected move (atr_target_mult x ATR) must be >= cost_mult x round-trip cost
    cost_mult: float = 2.0
    # all
    atr_bars: int = 14
    time_stop_bars: int = 24
    cooldown_min: int = 15


class StrategiesCfg(BaseModel):
    basis_tmn_usdt: StrategyCfg = StrategyCfg(edge_margin_bps=Decimal("5"), timeframe=1)
    mean_reversion: StrategyCfg = StrategyCfg(quotes=["TMN"])
    momentum: StrategyCfg = StrategyCfg(time_stop_bars=16)


class BacktestCfg(BaseModel):
    history_days: int = 30
    split_frac: float = 2 / 3  # oldest part in-sample, newest out-of-sample
    timeframes: list[int] = [1, 5, 15, 60]
    min_is_trades: int = 10  # IS selection needs at least this many trades and PF > 1
    # Engine: runs older than this cannot promote a strategy; warn earlier so it is rerun.
    max_run_age_h: float = 36
    warn_run_age_h: float = 20
    # Engine re-runs the walk-forward in a low-priority subprocess this often (0 = never).
    rerun_every_h: float = 12
    grid_mean_reversion: dict[str, list[float]] = {
        "z_entry": [1.5, 2.0, 2.5],
        "exit_frac": [0.0, 0.25, 0.5],
    }
    grid_momentum: dict[str, list[float]] = {
        "breakout_bars": [20, 40],
        "atr_target_mult": [2.0, 3.0],
        "vol_ratio_min": [1.5, 2.0],
    }


class PromotionCfg(BaseModel):
    min_backtest_trades: int = 10
    min_profit_factor: Decimal = Decimal("1.2")
    min_shadow_trades: int = 3
    demote_window_trades: int = 10
    # Strategies that trade on paper but never get live weight (non-atomic legs).
    paper_only: list[str] = ["basis_tmn_usdt"]


class AllocCfg(BaseModel):
    tau: float = 1.0
    max_strategy_weight: Decimal = Decimal("0.6")
    score_window_trades: int = 30
    usdt_rebalance_band_pct: Decimal = Decimal("5")
    usdt_min_hold_min: int = 60
    usdt_breakeven_trades_per_cycle: Decimal = Decimal("2")


class StoreCfg(BaseModel):
    db_path: str = "data/wallexer.db"
    overrides_path: str = "data/overrides.toml"


class AppConfig(BaseModel):
    exchange: ExchangeCfg = ExchangeCfg()
    ratelimit: RateLimitCfg = RateLimitCfg()
    account: AccountCfg = AccountCfg()
    orders: OrdersCfg = OrdersCfg()
    fees: FeesCfg = FeesCfg()
    universe: UniverseCfg = UniverseCfg()
    llm: LlmCfg = LlmCfg()
    guardrails: GuardrailsCfg = GuardrailsCfg()
    store: StoreCfg = StoreCfg()
    engine: EngineCfg = EngineCfg()
    paper: PaperCfg = PaperCfg()
    strategies: StrategiesCfg = StrategiesCfg()
    promotion: PromotionCfg = PromotionCfg()
    alloc: AllocCfg = AllocCfg()
    backtest: BacktestCfg = BacktestCfg()
    agent: AgentCfg = AgentCfg()

    def path(self, rel: str) -> Path:
        """Resolve a config-relative path against the repo root."""
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p


class Secrets(BaseSettings):
    """Env / .env values. Keys are SecretStr so they never show up in logs or reprs."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    wallex_api_key: SecretStr | None = None
    # Required for any private call: the trading-credit (sub) account id.
    wallex_subaccount_id: str | None = None
    boxapi_api_key: SecretStr | None = None
    wallexer_mode: Mode = Mode.shadow
    event_end_utc: datetime | None = None


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {path}: {e}") from e


class Settings(BaseModel):
    cfg: AppConfig
    secrets: Secrets = Field(default_factory=Secrets)

    @property
    def mode(self) -> Mode:
        return self.secrets.wallexer_mode

    def require_private(self) -> tuple[str, str]:
        """Return (api_key, subaccount_id) or raise. Every private request needs both."""
        missing = []
        key = self.secrets.wallex_api_key
        sub = (self.secrets.wallex_subaccount_id or "").strip()
        if key is None or not key.get_secret_value().strip():
            missing.append("WALLEX_API_KEY (the trading-credit account's own key)")
        if not sub:
            missing.append("WALLEX_SUBACCOUNT_ID (sent as sub-account-client-id header)")
        if missing:
            raise ConfigError(
                "Missing required setting(s): " + "; ".join(missing) + ". Set in .env."
            )
        assert key is not None
        return key.get_secret_value().strip(), sub

    @property
    def has_private(self) -> bool:
        try:
            self.require_private()
        except ConfigError:
            return False
        return True


def load_settings(config_path: Path | None = None) -> Settings:
    raw = _read_toml(config_path or DEFAULT_CONFIG)
    base = AppConfig.model_validate(raw)
    overrides = _read_toml(base.path(base.store.overrides_path))
    try:
        cfg = AppConfig.model_validate(_deep_merge(raw, overrides))
        return Settings(cfg=cfg, secrets=Secrets())
    except ValueError as e:
        raise ConfigError(f"Invalid configuration: {e}") from e
