"""`wallexer agent now` (force a decision) and `wallexer agent replay <id>` (reproducibility check)."""

from __future__ import annotations

import json

from rich.console import Console

from wallexer.agent.client import LlmClient, prompt_hash
from wallexer.agent.schema import AgentDecision, json_schema
from wallexer.config import Settings
from wallexer.store.db import init_db


async def replay_call(settings: Settings, console: Console, call_id: int) -> bool:
    """Re-ask a recorded agent request in replay mode: must return the recorded decision, no API call."""
    conn = init_db(settings.cfg.path(settings.cfg.store.db_path))
    try:
        row = conn.execute(
            "SELECT request, parsed, prompt_hash, purpose FROM llm_calls WHERE id=?", (call_id,)
        ).fetchone()
        if row is None:
            console.print(f"[red]llm_call #{call_id} not found[/]")
            return False
        msgs = json.loads(row[0])[:2]  # system + user (a retry appends a correction message)
        if prompt_hash(msgs) != row[2]:
            console.print(
                "[yellow]note: recorded hash is of the original prompt; using the first two messages[/]"
            )
        client = LlmClient(settings, conn, replay=True)
        res = await client.structured(
            purpose=f"replay:{row[3]}",
            system=msgs[0]["content"],
            user=msgs[1]["content"],
            schema=json_schema(),
            model=AgentDecision,
            schema_name="agent_decision",
        )
        recorded = json.loads(row[1]) if row[1] else None
        same = res.parsed is not None and res.parsed.model_dump() == recorded
        console.print(
            f"replay source: {res.source} · prompt hash {res.prompt_hash[:16]}… · identical to recording: "
            + ("[green]yes[/]" if same else "[red]no[/]")
        )
        if res.parsed is not None:
            console.print_json(res.parsed.model_dump_json())
        return same
    finally:
        conn.close()
