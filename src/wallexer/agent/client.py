"""BoxAPI (OpenAI-compatible) structured calls with validation, one retry, logging and replay.

- temperature 0, response_format json_schema (strict), timeout from config.
- Invalid JSON or a schema/validation error: one retry with the error appended; then None
  (the caller falls back to the baseline).
- Every attempt is written to llm_calls (prompt hash = sha256 of the messages).
- Replay mode: the latest successful recorded response with the same prompt hash is returned
  without calling the API; a miss is a fallback (logged as replay_miss).
Never sent: API keys, account ids, absolute balances (callers pass anonymized payloads).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from wallexer.config import Settings
from wallexer.store.db import dumps, now_iso

log = structlog.get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


@dataclass
class CallResult:
    parsed: BaseModel | None
    source: str  # llm | replay | fallback
    llm_call_id: int | None
    prompt_hash: str
    error: str | None = None


def prompt_hash(messages: list[dict[str, str]]) -> str:
    return hashlib.sha256(
        json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _record(
    conn: sqlite3.Connection,
    *,
    purpose: str,
    model: str,
    phash: str,
    request: Any,
    response: str | None,
    parsed: Any,
    latency_ms: int | None,
    fallback: bool,
    error: str | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO llm_calls (ts, purpose, model, prompt_hash, request, response, parsed, latency_ms,"
        " fallback, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            now_iso(),
            purpose,
            model,
            phash,
            dumps(request),
            response,
            dumps(parsed) if parsed is not None else None,
            latency_ms,
            int(fallback),
            error,
        ),
    )
    return int(cur.lastrowid)


class LlmClient:
    def __init__(
        self, settings: Settings, conn: sqlite3.Connection, *, replay: bool | None = None
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.replay = settings.cfg.llm.replay if replay is None else replay
        self._client = None

    def _openai(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            key = self.settings.secrets.boxapi_api_key
            if key is None or not key.get_secret_value():
                raise RuntimeError("BOXAPI_API_KEY not set")
            lc = self.settings.cfg.llm
            self._client = AsyncOpenAI(
                base_url=lc.base_url,
                api_key=key.get_secret_value(),
                timeout=lc.timeout_s,
                max_retries=0,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _replayed(self, phash: str, model: type[T]) -> T | None:
        row = self.conn.execute(
            "SELECT response FROM llm_calls WHERE prompt_hash=? AND fallback=0 AND response IS NOT NULL"
            " AND parsed IS NOT NULL ORDER BY id DESC LIMIT 1",
            (phash,),
        ).fetchone()
        if row is None:
            return None
        try:
            return model.model_validate_json(row[0])
        except ValidationError:
            return None

    async def structured(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: type[T],
        schema_name: str,
    ) -> CallResult:
        lc = self.settings.cfg.llm
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        phash = prompt_hash(messages)
        if self.replay:
            hit = self._replayed(phash, model)
            cid = _record(
                self.conn,
                purpose=purpose,
                model="replay",
                phash=phash,
                request=messages,
                response=hit.model_dump_json() if hit else None,
                parsed=hit.model_dump() if hit else None,
                latency_ms=0,
                fallback=hit is None,
                error=None if hit else "replay_miss",
            )
            return CallResult(
                hit, "replay" if hit else "fallback", cid, phash, None if hit else "replay_miss"
            )
        attempt_msgs = list(messages)
        last_error = None
        cid = None
        for attempt in range(2):
            t0 = time.monotonic()
            text = None
            try:
                resp = await self._openai().chat.completions.create(
                    model=lc.model,
                    messages=attempt_msgs,
                    temperature=0,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                    },
                )
                text = (resp.choices[0].message.content or "").strip()
                parsed = model.model_validate_json(text)
                cid = _record(
                    self.conn,
                    purpose=purpose,
                    model=lc.model,
                    phash=phash,
                    request=attempt_msgs,
                    response=text,
                    parsed=parsed.model_dump(),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    fallback=False,
                    error=None if attempt == 0 else f"retry_ok: {last_error}",
                )
                return CallResult(parsed, "llm", cid, phash)
            except (ValidationError, ValueError) as e:
                last_error = str(e)[:500]
            except Exception as e:  # network, gateway, SDK errors -> fallback, never crash
                last_error = f"{type(e).__name__}: {str(e)[:300]}"
            cid = _record(
                self.conn,
                purpose=purpose,
                model=lc.model,
                phash=phash,
                request=attempt_msgs,
                response=text,
                parsed=None,
                latency_ms=int((time.monotonic() - t0) * 1000),
                fallback=attempt == 1,
                error=last_error,
            )
            log.warning("llm.invalid", purpose=purpose, attempt=attempt + 1, error=last_error[:200])
            attempt_msgs = [
                *messages,
                {
                    "role": "user",
                    "content": f"Your previous answer was invalid: {last_error}. Return only valid JSON for the schema.",
                },
            ]
        return CallResult(None, "fallback", cid, phash, last_error)
