"""Agent decision schema: pydantic validation + the strict JSON schema sent to the model.

The JSON schema is written by hand to stay within strict-mode limits (every property required,
no additionalProperties, no free-form maps); numeric bounds and list sizes are enforced by
pydantic after parsing, and anything invalid triggers one retry, then the baseline fallback.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

STRATEGIES = ("basis_tmn_usdt", "mean_reversion", "momentum")
TIMEFRAMES = (1, 5, 15, 60)


class StrategyWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    basis_tmn_usdt: float = Field(ge=0, le=1)
    mean_reversion: float = Field(ge=0, le=1)
    momentum: float = Field(ge=0, le=1)


class WatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: Literal["mean_reversion", "momentum"]
    timeframe: int
    market: str
    action: Literal["paper", "recheck"]
    why: str = Field(max_length=200)

    @field_validator("timeframe")
    @classmethod
    def _tf(cls, v: int) -> int:
        if v not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {TIMEFRAMES}")
        return v


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stance: Literal["flat", "active"]
    strategy_weights: StrategyWeights
    active_markets: list[str] = Field(max_length=20)
    pause_markets: list[str] = Field(max_length=20)
    risk_multiplier: float = Field(ge=0, le=1)
    rationale: str = Field(max_length=600)
    watch_requests: list[WatchRequest] = Field(max_length=5)


def json_schema() -> dict[str, Any]:
    num = {"type": "number"}
    strs = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "stance",
            "strategy_weights",
            "active_markets",
            "pause_markets",
            "risk_multiplier",
            "rationale",
            "watch_requests",
        ],
        "properties": {
            "stance": {"type": "string", "enum": ["flat", "active"]},
            "strategy_weights": {
                "type": "object",
                "additionalProperties": False,
                "required": list(STRATEGIES),
                "properties": {s: num for s in STRATEGIES},
            },
            "active_markets": strs,
            "pause_markets": strs,
            "risk_multiplier": num,
            "rationale": {"type": "string"},
            "watch_requests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["strategy", "timeframe", "market", "action", "why"],
                    "properties": {
                        "strategy": {"type": "string", "enum": ["mean_reversion", "momentum"]},
                        "timeframe": {"type": "integer", "enum": list(TIMEFRAMES)},
                        "market": {"type": "string"},
                        "action": {"type": "string", "enum": ["paper", "recheck"]},
                        "why": {"type": "string"},
                    },
                },
            },
        },
    }


class Explanation(BaseModel):
    """exp-reason fixed output format."""

    model_config = ConfigDict(extra="forbid")
    summary: str
    why_this_action: str
    risk: str
    confidence: Literal["low", "medium", "high"]


def explanation_schema() -> dict[str, Any]:
    s = {"type": "string"}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "why_this_action", "risk", "confidence"],
        "properties": {
            "summary": s,
            "why_this_action": s,
            "risk": s,
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        },
    }
