"""Offline human-operated signing tool. Never invoked by the agent controller."""
import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess

from .approvals import approval_document, independent, live


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-key", "--key-file", dest="private_key", type=Path, required=True,
                        help="offline Ed25519 private PEM on the board's Mac only")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--actor", required=True, help="board:<human-id>")
    parser.add_argument("--run-id", required=True, help="independent human session ID")
    parser.add_argument("--expires-at", required=True, help="timezone-aware ISO timestamp")
    args = parser.parse_args()
    if any(k.startswith("PAPERCLIP_") or k.startswith("CODEX_") for k in os.environ):
        parser.error("board signing is disabled inside an agent runtime; use an independent human terminal")
    if not args.actor.startswith("board:"):
        parser.error("only an identified board human may sign")
    request = json.loads(args.request.read_text())
    independent(args.actor, args.run_id, request["actor"], request["run_id"])
    document = approval_document(request, actor=args.actor, run_id=args.run_id, expires_at=args.expires_at)
    live(document)
    signer = runpy.run_path(str(Path(__file__).with_name("board-sign.py")))
    try:
        envelope = signer["sign_document"](document, args.private_key)
    except (ValueError, OSError, subprocess.SubprocessError):
        parser.error("signing failed: use an offline Ed25519 private PEM")
    print(json.dumps(envelope, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
