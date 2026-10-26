#!/usr/bin/env bash
# wallexer install (Linux / macOS):
#   curl -fsSL https://raw.githubusercontent.com/zahraeisalehi/wallexer/main/install.sh | bash
# Checks for uv (installs it if missing) and Python 3.12 (uv provides it if missing), clones the
# repo (or updates an existing clone), runs `uv sync --frozen`, copies .env.example to .env (never
# overwrites an existing .env) and puts a `wallexer` command on PATH. Safe to re-run.
# WALLEXER_DIR overrides the clone location (default ~/wallexer).
set -euo pipefail

REPO="https://github.com/zahraeisalehi/wallexer.git"
DIR="${WALLEXER_DIR:-$HOME/wallexer}"
BIN="$HOME/.local/bin"
USER_PATH="$PATH"  # the caller's PATH: decides whether a bare `wallexer` works there

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Linux|Darwin) ;;
  *) fail "unsupported OS $(uname -s): Linux and macOS only" ;;
esac
command -v git >/dev/null 2>&1 || fail "git is required"

# uv
if ! command -v uv >/dev/null 2>&1 && [ -x "$HOME/.local/bin/uv" ]; then
  PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (https://astral.sh/uv)…"
  curl -LsSf https://astral.sh/uv/install.sh | UV_PRINT_QUIET=1 sh
  PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || fail "uv not found after installing it"
say "uv: $(uv --version)"

# Python 3.12 (the project pins it in .python-version; uv downloads it if the system has none)
if ! uv python find 3.12 >/dev/null 2>&1; then
  say "Python 3.12 not found: installing it with uv…"
  uv python install 3.12
fi
say "python: $(uv python find 3.12)"

# clone or update
if [ -d "$DIR/.git" ]; then
  say "Updating $DIR…"
  git -C "$DIR" pull --ff-only
else
  say "Cloning into $DIR…"
  git clone "$REPO" "$DIR"
fi
cd "$DIR"
uv sync --frozen

# .env from the template, only if there is none yet
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  say "Created $DIR/.env from .env.example (fill in your keys)."
fi

# `wallexer` on PATH: a link to the project's own entry point (it uses this checkout and its .env)
mkdir -p "$BIN"
ln -sf "$DIR/.venv/bin/wallexer" "$BIN/wallexer"

say ""
say "wallexer installed in $DIR"
case ":$USER_PATH:" in
  *":$BIN:"*) ;;
  *) say "Add $BIN to your PATH first:  export PATH=\"$BIN:\$PATH\"" ;;
esac
say "Next:"
say "  cd $DIR && vim .env      # WALLEX_API_KEY (in single quotes), WALLEX_SUBACCOUNT_ID, BOXAPI_API_KEY, EVENT_END_UTC"
say "  wallexer doctor          # keys, sub-account, balances, fees, clock"
say "  wallexer shadowtest      # live data, simulated orders"
say "  wallexer                 # the full-screen app"
