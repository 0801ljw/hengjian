#!/usr/bin/env bash
# No installs, downloads, external notifications, or systemd mutations.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$HOME/.local/node/bin:$PATH"
if ! command -v python3 >/dev/null; then
    # Keep the result machine-readable even when the coordinator cannot start.
    OUT=/home/ljw/boss-lab
    if [[ $# -eq 2 && $1 == --output-dir ]]; then OUT=$2; fi
    mkdir -p "$OUT"
    exec 9>"$OUT/ci.lock"
    flock -n 9 || { echo 'SKIP: another CI run holds ci.lock' >&2; exit 2; }
    TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    COMMIT=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)
    RESULT=$(printf '{"time":"%s","commit":"%s","tests_passed":0,"tests_failed":0,"acceptance_pass":0,"acceptance_fail":0,"ok":false,"tests_status":"SKIP","acceptance_status":"SKIP","reasons":["SKIP: python3 missing"]}' "$TIME" "$COMMIT")
    printf '%s\n' "$RESULT" >"$OUT/ci-status.json.tmp"
    mv "$OUT/ci-status.json.tmp" "$OUT/ci-status.json"
    printf '%s\n' "$RESULT" >>"$OUT/ci-failures.log"
    printf '%s\n' "$RESULT"
    exit 2
fi
exec python3 "$REPO/fusion/ci/run.py" "$@"
