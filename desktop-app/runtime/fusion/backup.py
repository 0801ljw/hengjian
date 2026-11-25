"""Consistent instance backup for the fusion data directory."""
from __future__ import annotations

import argparse
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

FILES = ("tokens.json", "roles.json", "issue-policy.json", "pricing.json",
         "onboarding.json", "launcher.json", "sync-journal.sqlite3")
DIRS = ("auth", "issue-files", "issue-runs", "agents")


def backup(instance, out_dir, keep=14):
    instance = Path(instance).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"fusion-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="fusion-backup-") as tmp:
        tmp_path = Path(tmp)
        db_copy = tmp_path / "fusion.sqlite3"
        with sqlite3.connect(instance / "fusion.sqlite3") as source, \
                sqlite3.connect(db_copy) as dest:
            source.backup(dest)
        with tarfile.open(target, "w:gz") as archive:
            archive.add(db_copy, arcname="fusion.sqlite3")
            for name in FILES:
                path = instance / name
                if path.is_file():
                    archive.add(path, arcname=name)
            for name in DIRS:
                path = instance / name
                if path.is_dir():
                    archive.add(path, arcname=name)
    target.chmod(0o600)
    cutoff = time.time() - int(keep) * 86400
    for old in out_dir.glob("fusion-*.tar.gz"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    return {"path": str(target), "bytes": target.stat().st_size, "keep": int(keep)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args(argv)
    result = backup(args.home, args.out, args.keep)
    print(result["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
