#!/usr/bin/env bash
# Read-only scheduled audit of non-terminal fusion tasks (systemd timer entry).
#
# Writes a machine-readable status file and logs a warning when something is
# stuck. It NEVER blocks, cancels, re-submits or signs anything: recovery stays
# a human decision (see `fusion stuck --help` and fusion/USER-GUIDE.md).
#
# Exit codes: 0 = nothing stuck; 2 = stuck tasks found (visible as a failed
# unit / journal warning); 1 = the audit itself could not run.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$HOME/.local/node/bin:$PATH"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

INSTANCE="${FUSION_HOME:-$HOME/boss-lab/fusion-instance}"
STATUS="${FUSION_STUCK_STATUS:-$HOME/boss-lab/fusion-stuck-status.json}"
HOURS="${FUSION_STUCK_HOURS:-2}"
mkdir -p "$(dirname "$STATUS")"

exec 9>"$STATUS.lock"
if ! flock -n 9; then
    echo "SKIP: another stuck-check holds $STATUS.lock" >&2
    exit 2
fi

STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
TMP=$(mktemp "${STATUS}.tmp.XXXXXX")
trap 'rm -f "$TMP" "$TMP.err"' EXIT

if ! RESULT=$(cd "$REPO" && python3 -m fusion stuck --home "$INSTANCE" --hours "$HOURS" --json 2>"$TMP.err"); then
    printf '{"time":"%s","ok":false,"error":"fusion stuck failed","home":"%s","hours":%s}\n' \
        "$STARTED" "$INSTANCE" "$HOURS" >"$TMP"
    mv "$TMP" "$STATUS"
    cat "$TMP.err" >&2 || true
    echo "ERROR: fusion stuck failed; see $STATUS" >&2
    exit 1
fi
rm -f "$TMP.err"

RESULT_JSON="$RESULT" python3 - "$TMP" "$STARTED" "$INSTANCE" "$HOURS" <<'PY'
import json
import os
import sys

target, started, instance, hours = sys.argv[1:5]
payload = json.loads(os.environ["RESULT_JSON"])
reports = payload.get("reports") or []
stuck = [report for report in reports if report.get("stuck")]
document = {
    "time": started,
    "home": instance,
    "hours": float(hours),
    "ok": not stuck,
    "non_terminal": len(reports),
    "stuck": len(stuck),
    "reports": reports,
    "applied": [],
}
with open(target, "w", encoding="utf-8") as handle:
    json.dump(document, handle, ensure_ascii=False, indent=1)
PY

STUCK=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["stuck"])' "$TMP")
NON_TERMINAL=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["non_terminal"])' "$TMP")
mv "$TMP" "$STATUS"
trap - EXIT

if [ "$STUCK" -gt 0 ]; then
    echo "WARN: $STUCK stuck task(s) out of $NON_TERMINAL non-terminal; see $STATUS" >&2
    python3 - "$STATUS" <<'PY' >&2
import json
import sys

document = json.load(open(sys.argv[1], encoding="utf-8"))
for report in document["reports"]:
    if report.get("stuck"):
        print("  %-24s %-14s %-16s idle=%sh action=%s" % (
            report.get("task_id"), report.get("state"), report.get("kind"),
            report.get("idle_hours"), report.get("action")))
PY
    exit 2
fi

echo "OK: no stuck tasks ($NON_TERMINAL non-terminal checked, threshold ${HOURS}h)"
exit 0
