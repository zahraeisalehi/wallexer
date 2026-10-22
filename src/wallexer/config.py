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
    window_s: float = 60.0
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


class FeesCfg(BaseModel):
    # Used when /v1/account/fee returns no usable data for a market. Highest observed tier,
    # so a fallback never understates costs.
    default_maker: Decimal = Decimal("0.003")
    default_taker: Decimal = Decimal("0.003")


class UniverseCfg(BaseModel):
    top_n: int = 30
    min_quote_volume_tmn: Decimal = Decimal("500000000")
    max_spread_bps: Decimal = Decimal("100")
    quote_assets: list[str] = ["TMN", "USDT"]
    not_allowed_path: str = "data/not_allowed.txt"


class LlmCfg(BaseModel):
    base_url: str = "https://ai.boxapi.ir/ai/v1"
    model: str = "gpt-5.4-mini"
    fallback_model: str = "gpt-5.4-mini"
    timeout_s: float = 30.0


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
    flatten_force_min: int = 30


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
