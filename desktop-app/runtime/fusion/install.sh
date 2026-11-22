#!/usr/bin/env bash
# Fusion installer; unrelated to the upstream root install.sh.
set -euo pipefail
command -v python3 >/dev/null || { echo '缺少 python3 (Python 3.10+)' >&2; exit 1; }
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 -I "$REPO_DIR/fusion/packaging.py" install --source "$REPO_DIR" "$@"
