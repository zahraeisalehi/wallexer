# Deployment on Liara (liara.ir)

Liara is an Iranian PaaS with a CLI (`liara`, installed via `npm install -g @liara/cli`). Explore commands with `liara --help` / `liara <cmd> --help`; don't assume flags from memory.

## What we deploy
- One app: the **engine daemon** (`wallexer engine run --mode <shadow|trade>`), built from the repo's `Dockerfile` (Python 3.12 + uv).
- The CLI is used on the server through Liara's shell into the running container, e.g. `wallexer status`, `wallexer watch`.

## Requirements
- **Persistent disk** mounted at `/app/data` for the SQLite DB and logs. Without it, a redeploy wipes our decision logs (they are a submission requirement).
- **Env vars** set via Liara (never in the image or repo): `WALLEX_API_KEY`, `BOXAPI_API_KEY`, `WALLEXER_MODE`, `EVENT_END_UTC`.
- If the platform requires a listening port, the engine serves `GET /health` on `PORT` (default 8000) returning mode, last tick time, equity, error count.
- Restart on crash is handled by the platform; the engine must be safe to restart: on startup it reconciles open orders and balances from Wallex before trading.
- Logs go to stdout (JSON) as well as the DB.

## Deploy rules for Claude Code
- `liara deploy` and `liara env` changes require the user's confirmation.
- Never deploy in `trade` mode without the user explicitly saying so. Default deploy mode is `shadow`.
- After each deploy: check logs, hit `/health`, run `wallexer doctor` in the container.

## Verified setup (2026-10-22)
- A private network is required before `app create`: `liara network create -n wallexer-net`, then `app create ... -n wallexer-net`.
- `app create` needs a feature bundle: API ids `free | standard | pro` (the interactive prompt labels them differently; prices for standard-base-g2: 0 / 825,000 / 1,475,000 TMN/month). We use `--feature-plan standard`.
- Non-interactive create: `liara app create -a wallexer --platform docker --plan standard-base-g2 --feature-plan standard -n wallexer-net -r false`.
- Build location `iran` (user choice). Image: `Dockerfile` (python 3.12-slim + uv, `uv sync --frozen --no-dev`), start script `deploy/entrypoint.sh`, config `liara.json` (port 8000, disk `data` → `/app/data`).
- Step-by-step operations: `docs/RUNBOOK.md`.
- Demo app `wallexer-demo` (2026-10-23): deploy ONLY via `deploy/deploy-demo.sh` (uses `liara.demo.json`, refuses the live app `wallexer`, bakes `WALLEXER_DEMO=1`). `liara.json` is the live app: never run a bare `liara deploy` for the demo.
