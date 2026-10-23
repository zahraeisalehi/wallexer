#!/bin/sh
# wallexer one-line install (Linux / macOS):
#   curl -LsSf https://raw.githubusercontent.com/zahraeisalehi/wallexer/main/install.sh | sh
# Installs uv if missing, then the `wallexer` command via `uv tool install`. No questions; safe to
# re-run (it upgrades in place). `wallexer` then opens straight into the public demo (read-only);
# `wallexer setup` configures your own engine. WALLEXER_SOURCE overrides where it installs from
# (a git URL or a path); WALLEXER_INSTALL_VERBOSE=1 shows the installers' output.
set -eu

SOURCE="${WALLEXER_SOURCE:-git+https://github.com/zahraeisalehi/wallexer}"
USER_PATH="$PATH"  # the caller's PATH: decides whether a bare `wallexer` will work there
LOG="$(mktemp "${TMPDIR:-/tmp}/wallexer-install.XXXXXX")"
trap 'rm -f "$LOG"' EXIT

fail() {
  printf 'install.sh: %s\n' "$*" >&2
  [ -s "$LOG" ] && tail -n 20 "$LOG" >&2
  exit 1
}
quietly() {
  if [ "${WALLEXER_INSTALL_VERBOSE:-0}" = 1 ]; then "$@"; else "$@" >>"$LOG" 2>&1; fi
}

case "$(uname -s)" in
  Linux|Darwin) ;;
  *) fail "unsupported OS $(uname -s): Linux and macOS only" ;;
esac

# uv: use an existing one, else the official installer (into ~/.local/bin, quiet).
if ! command -v uv >/dev/null 2>&1; then
  for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$d/uv" ] && PATH="$d:$PATH" && export PATH
  done
fi
if ! command -v uv >/dev/null 2>&1; then
  UV_PRINT_QUIET=1
  export UV_PRINT_QUIET
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | quietly sh || fail "uv install failed"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | quietly sh || fail "uv install failed"
  else
    fail "need curl or wget to install uv"
  fi
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi
command -v uv >/dev/null 2>&1 || fail "uv not found after installing it"

# --force + --reinstall-package: re-running upgrades in place (never a stale cached build).
quietly uv tool install --force --reinstall-package wallexer --python 3.12 "$SOURCE" \
  || fail "installing wallexer from $SOURCE failed"

BIN="$(uv tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")"
case ":$USER_PATH:" in
  *":$BIN:"*) printf 'Run: wallexer\n' ;;
  *) quietly uv tool update-shell || true
     printf 'Run: %s/wallexer\n' "$BIN" ;;
esac
