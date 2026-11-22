#!/usr/bin/env python3
"""Standalone OFFLINE human tool: copy to the board's Mac, never to automation.

Requires Python 3 and an OpenSSL executable supporting Ed25519 pkeyutl -rawin.
Only test keys may be used on the controller host. No default private-key path.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def require_human_terminal():
    if any(k.startswith(("PAPERCLIP_", "CODEX_")) for k in os.environ):
        raise ValueError("board signing is disabled inside an agent runtime; use an independent human terminal")


def sign_document(document, private_key_file):
    """Return an Ed25519 envelope; private-key bytes stay in OpenSSL, not Python."""
    require_human_terminal()
    if not isinstance(document, dict):
        raise ValueError("document must be a JSON object")
    # Must match fusion.mutate.canonical exactly, including UTF-8 and NaN denial.
    data = json.dumps(document, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")
    public = subprocess.run(["openssl", "pkey", "-in", str(private_key_file),
                             "-passin", "pass:", "-pubout", "-outform", "DER"],
                            capture_output=True, timeout=10, check=True).stdout
    if len(public) != 44 or public[:12] != bytes.fromhex("302a300506032b6570032100"):
        raise ValueError("signing key must be Ed25519")
    with tempfile.TemporaryDirectory(prefix="fusion-board-sign-") as directory:
        path = Path(directory) / "document"
        path.write_bytes(data)
        result = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(private_key_file),
                                 "-passin", "pass:", "-rawin", "-in", str(path)],
                                capture_output=True, timeout=10, check=True)
    if len(result.stdout) != 64:
        raise ValueError("invalid Ed25519 signature length")
    return {"alg": "ed25519", "document": document, "signature": result.stdout.hex()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", type=Path, required=True, help="inspected document JSON")
    parser.add_argument("--private-key", type=Path, required=True, help="offline Ed25519 PEM path")
    args = parser.parse_args()
    try:
        require_human_terminal()
        envelope = sign_document(json.loads(args.document.read_text(encoding="utf-8")), args.private_key)
    except (ValueError, OSError, subprocess.SubprocessError):
        # Never echo OpenSSL stderr or private-key material into output/logs.
        parser.error("signing refused: use an independent human terminal, valid document and offline Ed25519 key")
    print(json.dumps(envelope, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
