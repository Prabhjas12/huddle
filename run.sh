#!/usr/bin/env bash
# Start Huddle: creates a private Python environment the first time, then opens the app.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3 is required. Install it from https://www.python.org/downloads/ (or: brew install python) and run this again."
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Huddle needs Python 3.9 or newer. You have $("$PY" --version). Install a newer Python from python.org."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up Huddle's Python environment (about a minute)…"
  "$PY" -m venv .venv
fi
STAMP=".venv/.requirements-installed"
if [ ! -f "$STAMP" ] || ! cmp -s requirements.txt "$STAMP"; then
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  cp requirements.txt "$STAMP"
fi

if [ "${1:-}" = "--connect-claude" ]; then
  exec .venv/bin/python -m huddle.connect_claude
fi
exec .venv/bin/python -m huddle "$@"
