#!/bin/sh
# Deploy the DEMO app (wallexer-demo) and nothing else.
#   deploy/deploy-demo.sh            check, stage, deploy
#   deploy/deploy-demo.sh --check    check and stage only
#
# liara.json points at the LIVE app "wallexer", so a bare `liara deploy` would hit production.
# This script never uses liara.json: it passes liara.demo.json and --app explicitly, refuses if
# either names "wallexer", and bakes WALLEXER_DEMO=1 into the image (shadow only, no orders).
set -eu

DEMO_APP="wallexer-demo"
TEAM="6ad87ba7cf87f9a8e45d4d3a"
CONFIG="liara.demo.json"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

fail() { printf 'deploy-demo: REFUSED: %s\n' "$*" >&2; exit 1; }
say() { printf 'deploy-demo: %s\n' "$*"; }

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# 1. Target app: from the demo config, from the constant, and from any APP override. All must be
#    the demo app, and none may be the live app.
cfg_app="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["app"])' "$ROOT/$CONFIG")"
target="${APP:-$DEMO_APP}"
for a in "$cfg_app" "$target"; do
  [ "$a" = "wallexer" ] && fail "target app is \"wallexer\" (the LIVE app)"
  [ "$a" = "$DEMO_APP" ] || fail "target app is \"$a\", expected \"$DEMO_APP\""
done

# 2. Demo env on Liara: key names only (values are never fetched, except the two flags below,
#    which are not secrets).
keys="$(liara env list -a "$DEMO_APP" --team-id "$TEAM" --columns key --output csv | tail -n +2)"
has() { printf '%s\n' "$keys" | grep -qx "$1"; }
value() {
  liara env list -a "$DEMO_APP" --team-id "$TEAM" --filter "key=$1" --columns key,value --output csv \
    | awk -F, -v k="$1" '$1 == k { print $2 }'
}
for k in WALLEX_API_KEY WALLEX_SUBACCOUNT_ID; do
  has "$k" || fail "$k is not set on $DEMO_APP (liara env set -a $DEMO_APP ...)"
done
for k in WALLEXER_VIEW_TOKEN EVENT_END_UTC BOXAPI_API_KEY; do
  has "$k" || say "warning: $k is not set on $DEMO_APP"
done
if has WALLEXER_MODE; then
  [ "$(value WALLEXER_MODE)" = "shadow" ] || fail "WALLEXER_MODE on $DEMO_APP must be shadow"
fi
if has WALLEXER_DEMO; then
  [ "$(value WALLEXER_DEMO)" = "1" ] || fail "WALLEXER_DEMO on $DEMO_APP must be 1 (it overrides the image)"
fi
# Zero-setup installs watch the demo without a token: the public read-only view must be on.
has WALLEXER_VIEW_PUBLIC && [ "$(value WALLEXER_VIEW_PUBLIC)" = "1" ] \
  || fail "WALLEXER_VIEW_PUBLIC=1 is not set on $DEMO_APP (liara env set -a $DEMO_APP --team-id $TEAM WALLEXER_VIEW_PUBLIC=1)"

# 3. Clean staging copy: tracked + untracked-not-ignored files, never .env*, DBs or logs.
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/wallexer-demo.XXXXXX")"
( cd "$ROOT" && git ls-files -co --exclude-standard ) \
  | grep -v -E '^\.claude/|^\.env|(^|/)\.env|^data/.*\.db|^data/logs/|^data/exports/|^liara\.json$' \
  | ( cd "$ROOT" && rsync -a --files-from=- . "$STAGE/" )
cp "$ROOT/$CONFIG" "$STAGE/$CONFIG"
[ -e "$STAGE/.env" ] && fail "staging copy contains .env"
[ -e "$STAGE/liara.json" ] && fail "staging copy contains liara.json"
say "staged $(find "$STAGE" -type f | wc -l | tr -d ' ') files in $STAGE"

if [ "$CHECK_ONLY" = 1 ]; then
  say "check passed; not deploying (--check)"
  exit 0
fi

say "deploying to $DEMO_APP (team $TEAM) with $CONFIG"
cd "$STAGE"
exec liara deploy --app "$DEMO_APP" --team-id "$TEAM" --liara-json "$CONFIG" --path "$STAGE" \
  --port 8000 --build-location iran --disks data:/app/data \
  --build-arg WALLEXER_DEMO=1 --message "demo (shadow, WALLEXER_DEMO=1)" --no-app-logs
