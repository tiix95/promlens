#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

CONFIG_FILE="${1:-promlens.yaml}"
[[ $# -gt 0 ]] && shift
TOPOLOGY_FILE="${1:-topology.yaml}"
[[ $# -gt 0 ]] && shift
LAYOUT_FILE="${1:-layout.json}"
[[ $# -gt 0 ]] && shift
export CONFIG_FILE TOPOLOGY_FILE LAYOUT_FILE

# Create venv if missing
if [[ ! -d "$VENV" ]]; then
  echo "Creating virtual environment..."
  python3 -m venv "$VENV"
  "$VENV/bin/python3" -m ensurepip --upgrade 2>/dev/null || true
fi

# Install/update dependencies
echo "Checking dependencies..."
"$VENV/bin/python3" -m pip install -q -r "$REQUIREMENTS"

# Launch
BIND_HOST="${BIND_HOST:-127.0.0.1}"
BIND_PORT="${BIND_PORT:-8000}"
cd "$SCRIPT_DIR"
exec "$VENV/bin/python3" -m uvicorn main:app --app-dir "$SCRIPT_DIR/src" --host "$BIND_HOST" --port "$BIND_PORT" "$@"
