#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON_BIN:-/opt/homebrew/bin/python3.12}"
VENV="${SIDECAR_VENV:-$ROOT/.build-venv}"

if [ ! -x "$PY" ]; then
  PY="$(command -v python3.12 || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "需要 Python 3.12 来构建 sidecar" >&2
  exit 1
fi

"$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q pyinstaller
cd "$ROOT"
"$VENV/bin/pyinstaller" --noconfirm --clean --onefile --name fusion-server \
  --paths runtime --collect-all fusion \
  --add-data "runtime/scripts:scripts" \
  --add-data "runtime/agents.json:." \
  --add-data "runtime/VERSION:." \
  sidecar_entry.py
echo "built: $ROOT/dist/fusion-server"
