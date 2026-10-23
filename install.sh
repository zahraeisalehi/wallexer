#!/bin/sh
# wallexer one-line install (Linux / macOS):
#   curl -LsSf https://raw.githubusercontent.com/zahraeisalehi/wallexer/main/install.sh | sh
# Installs uv if missing, then the `wallexer` command via `uv tool install`. Safe to re-run:
# it upgrades in place. WALLEXER_SOURCE overrides where it installs from (a git URL or a path).
set -eu

SOURCE="${WALLEXER_SOURCE:-git+https://github.com/zahraeisalehi/wallexer}"

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Linux|Darwin) ;;
  *) fail "unsupported OS $(uname -s): Linux and macOS only" ;;
esac

# uv: use an existing one, else the official installer (into ~/.local/bin).
if ! command -v uv >/dev/null 2>&1; then
  for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$d/uv" ] && PATH="$d:$PATH" && export PATH
  done
fi
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (https://astral.sh/uv)…"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    fail "need curl or wget to install uv"
  fi
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi
command -v uv >/dev/null 2>&1 || fail "uv not found after installing it"

# --force + --reinstall-package: re-running upgrades in place (never a stale cached build).
say "Installing wallexer from $SOURCE…"
uv tool install --force --reinstall-package wallexer --python 3.12 "$SOURCE"

BIN="$(uv tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) uv tool update-shell >/dev/null 2>&1 || true
     say "Note: add $BIN to your PATH (open a new shell, or: export PATH=\"$BIN:\$PATH\")" ;;
esac

say ""
say "wallexer installed."
say "Next:"
say "  wallexer            open the shell (first run: setup wizard, local keys or remote viewing)"
say "  wallexer markets    allowed markets, spreads, fees (no keys needed)"
say "  wallexer help       every command, grouped, with examples"
