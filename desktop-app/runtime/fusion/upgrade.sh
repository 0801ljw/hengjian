#!/usr/bin/env bash
# Stopped-instance upgrade (or --rollback <backup-directory>).
set -euo pipefail
command -v python3 >/dev/null || { echo '缺少 python3 (Python 3.10+)' >&2; exit 1; }
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 -I "$REPO_DIR/fusion/packaging.py" upgrade --source "$REPO_DIR" "$@"
