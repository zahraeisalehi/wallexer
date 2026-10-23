# BoxAPI LLM reference (boxapi.ir)

OpenAI-compatible gateway to many models (700+). Use the official `openai` Python SDK.

```python
from openai import AsyncOpenAI

client = AsyncOpenAI(base_url="https://ai.boxapi.ir/ai/v1", api_key=settings.boxapi_api_key)
resp = await client.chat.completions.create(
    model=settings.llm_model,  # e.g. "gpt-5.4-mini" (from config, never hard-coded)
    messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": snapshot_json}],
    temperature=0,
)
```

- Base URL: `https://ai.boxapi.ir/ai/v1`
- Auth: `Authorization: Bearer <key>` (env `BOXAPI_API_KEY`)
- Endpoints available: `/chat/completions` (use this), `/responses`, `/embeddings`, streaming via `stream: true`.
- Model ids: configured in `config/default.toml` → `llm.model` and `llm.fallback_model`.

## Not confirmed by the docs: verify in `doctor`, then decide
1. Does `response_format={"type": "json_schema", ...}` or `{"type": "json_object"}` work through the gateway?
2. Does `temperature` get passed through for the chosen model (some reasoning models reject it)?
3. Typical latency and timeouts.

## How we call it (regardless of the above)
- Ask for JSON only, include the JSON schema in the system prompt, validate with pydantic.
- On invalid JSON: one retry with the validation error appended; then fall back to baseline weights.
- Timeout 30 s. The trading loop never waits on the LLM: agent runs as a separate async task and publishes the latest valid allocation.
- Log every request/response to `llm_calls` (prompt hash, model, latency, raw text, parsed result, fallback flag).
- `--replay` mode returns recorded responses by prompt hash instead of calling the API.
- Never send API keys, account ids or balances in absolute TMN to the LLM unless needed; send ratios/percentages.
