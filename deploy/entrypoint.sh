#!/bin/sh
# Container start: seed reference files onto the persistent disk, check the setup, refresh
# evidence, then run the engine in the mode given by WALLEXER_MODE (default shadow).
set -e
mkdir -p /app/data/logs /app/data/reports

# The persistent disk mounted at /app/data hides the image's data/ directory.
cp /app/seed/not_allowed.txt /app/data/not_allowed.txt
[ -f /app/data/unavailable_markets.txt ] || cp /app/seed/unavailable_markets.txt /app/data/unavailable_markets.txt

echo "entrypoint: doctor"
if ! wallexer doctor --quick --no-llm; then
  echo "entrypoint: doctor failed, refusing to start (check env vars / balances)"
  exit 1
fi

# History sync and a stale walk-forward run happen inside the engine (health port is up first).
echo "entrypoint: engine --mode ${WALLEXER_MODE:-shadow}"
exec wallexer engine run --mode "${WALLEXER_MODE:-shadow}"
