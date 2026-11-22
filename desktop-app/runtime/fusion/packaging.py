"""Offline installation and stopped-instance upgrade; no application mutations."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
import venv

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1


def commit_id(source: Path = ROOT) -> str:
    if (source / "COMMIT").is_file():
        value = (source / "COMMIT").read_text().strip()
        if re.fullmatch(r"[0-9a-f]{7,40}", value):
            return value[:12]
    try:
        return subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "--short=12", "HEAD"],
            stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def version_label() -> str:
    return f"fusion {(ROOT / 'VERSION').read_text().strip()} (commit {commit_id()})"


def check_dependencies() -> None:
    if sys.version_info < (3, 10):
        raise ValueError("需要 Python 3.10+")
    if not shutil.which("openssl"):
        raise ValueError("缺少 openssl (Ed25519)")
    with tempfile.TemporaryDirectory(prefix="fusion-openssl-") as tmp:
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out",
                        str(Path(tmp) / "probe.pem")], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def check_schema(home: Path) -> None:
    """Read-only check. Missing marker and SQLite user_version=0 are legacy v1.

    This release has no migrations; unknown versions are refused, not rewritten.
    """
    marker = home / "schema.json"
    if marker.exists():
        version = json.loads(marker.read_text())["schema_version"]
        if type(version) is not int or version != SCHEMA_VERSION:
            raise ValueError(f"incompatible schema version: {version}")
    for database in home.rglob("fusion.sqlite3"):
        if any(p.startswith("backup-") or p == "releases"
               for p in database.relative_to(home).parts):
            continue
        # SQLite read-only WAL opens can still alter SHM lock bytes. Inspect a
        # stopped-instance copy so compatibility checks cannot touch live data.
        with tempfile.TemporaryDirectory(prefix="fusion-schema-") as tmp:
            copied = Path(tmp) / "fusion.sqlite3"
            for suffix in ("", "-wal", "-shm"):
                original = database.with_name(database.name + suffix)
                if original.exists():
                    if original.is_symlink():
                        raise ValueError("database files must not be symlinks")
                    shutil.copy2(original, copied.with_name(copied.name + suffix))
            check_database(copied)


def check_database(database: Path) -> None:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise ValueError(f"incompatible database schema version: {version}")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError(f"database integrity failed: {database}")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"tasks", "events", "runs", "decisions", "approval_uses"} <= tables:
            raise ValueError(f"unknown database schema: {database}")


def doctor_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Offline prerequisites and schema check")
    parser.add_argument("--home", type=Path)
    args = parser.parse_args(argv)
    try:
        check_dependencies()
        if args.home:
            check_schema(args.home)
        print(json.dumps({"ok": True, "version": version_label(), "schema_version": SCHEMA_VERSION}))
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"doctor failed: {exc}", file=sys.stderr)
        return 1


def paths(home: str, prefix: str) -> tuple[Path, Path]:
    home_path, prefix_path = Path(home).absolute(), Path(prefix).absolute()
    for path in (home_path, prefix_path):
        if path != path.resolve() or ".paperclip" in path.parts:
            raise ValueError("paths must be canonical, without symlinks or .paperclip")
        if path in (Path("/"), Path.home(), ROOT):
            raise ValueError(f"unsafe installation path: {path}")
    if prefix_path == home_path or home_path in prefix_path.parents:
        if prefix_path != home_path / "bin":
            raise ValueError("prefix inside home must be <home>/bin")
    if prefix_path in home_path.parents:
        raise ValueError("instance cannot be inside launcher prefix")
    return home_path, prefix_path


def ensure_stopped(home: Path) -> None:
    """Extra Linux guard; --stopped remains the operator's all-writers contract."""
    if not Path("/proc").is_dir():
        raise ValueError("process verification currently requires Linux /proc")
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            args = (process / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        words = [x.decode(errors="replace") for x in args if x]
        if not any(x == "fusion" or x.endswith("/fusion") or x.startswith("fusion.")
                   or "from fusion.__main__ import main" in x for x in words):
            continue
        for i, arg in enumerate(words[:-1]):
            if arg == "--home":
                target = Path(words[i + 1])
                if not target.is_absolute():
                    target = (process / "cwd").resolve() / target
                if target.resolve() == home or home in target.resolve().parents:
                    raise ValueError(f"stop Fusion process {process.name} before upgrade")


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def children(home: Path) -> list[Path]:
    return [p for p in home.iterdir() if not p.name.startswith("backup-")
            and p.name != ".packaging.lock"]


def manifest(directory: Path) -> dict:
    result = {}
    for path in sorted(directory.rglob("*")):
        name = str(path.relative_to(directory))
        if path.is_symlink():
            value = ["link", os.readlink(path)]
        elif path.is_file():
            value = ["file", path.stat().st_mode & 0o777, hashlib.sha256(path.read_bytes()).hexdigest()]
        elif path.is_dir():
            value = ["dir", path.stat().st_mode & 0o777]
        else:
            raise ValueError(f"unsupported instance entry: {path}")
        result[name] = value
    return result


def copy_entry(source: Path, target: Path) -> None:
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target)


def backup_instance(home: Path, launcher: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / f"backup-{stamp}"
    backup.mkdir(mode=0o700)
    snapshot = backup / "instance"
    snapshot.mkdir(mode=0o700)
    for path in children(home):
        copy_entry(path, snapshot / path.name)
    metadata = {"manifest": manifest(snapshot), "launcher_exists": launcher.exists(),
                "launcher_mode": launcher.stat().st_mode & 0o777 if launcher.exists() else None,
                "launcher": launcher.read_text() if launcher.exists() else None}
    atomic_write(backup / "metadata.json", json.dumps(metadata).encode())
    print(f"BACKUP {backup}", flush=True)
    return backup


def restore(home: Path, launcher: Path, backup: Path) -> None:
    if backup.parent != home or not backup.name.startswith("backup-") or backup.is_symlink():
        raise ValueError("backup must be a direct backup-* child of this instance")
    metadata = json.loads((backup / "metadata.json").read_text())
    snapshot = backup / "instance"
    if manifest(snapshot) != metadata["manifest"]:
        raise ValueError("backup verification failed; refusing destructive restore")
    for path in children(home):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    for path in snapshot.iterdir():
        copy_entry(path, home / path.name)
    if metadata["launcher_exists"]:
        atomic_write(launcher, metadata["launcher"].encode(), metadata["launcher_mode"])
    else:
        launcher.unlink(missing_ok=True)
    actual = {}
    for entry in children(home):
        if entry.is_symlink():
            actual[entry.name] = ["link", os.readlink(entry)]
        elif entry.is_dir():
            actual[entry.name] = ["dir", entry.stat().st_mode & 0o777]
            actual.update({f"{entry.name}/{k}": v for k, v in manifest(entry).items()})
        else:
            actual[entry.name] = ["file", entry.stat().st_mode & 0o777,
                                  hashlib.sha256(entry.read_bytes()).hexdigest()]
    if actual != metadata["manifest"]:
        raise ValueError("CRITICAL: restored instance does not match backup")
    if metadata["launcher_exists"] and launcher.read_text() != metadata["launcher"]:
        raise ValueError("CRITICAL: launcher restore failed")
    print("ROLLBACK VERIFIED (files, permissions, data and launcher)", flush=True)


def install(source: Path, home: Path, launcher: Path, no_key: bool) -> None:
    version = (source / "VERSION").read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("VERSION must contain a semantic version")
    for line in (source / "requirements.lock").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not re.fullmatch(r"[\w.-]+==[\w.+!-]+", line):
            raise ValueError("requirements.lock permits only exact pins, no directives")
    check_dependencies()
    check_schema(home)
    release = home / "releases" / f"{version}-{uuid.uuid4().hex[:12]}"
    release.mkdir(parents=True, mode=0o700)
    shutil.copytree(source / "fusion", release / "fusion", ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", "var", "evidence"))
    for name in ("VERSION", "requirements.lock"):
        shutil.copy2(source / name, release / name)
    # core.py also reads the shared canonical transition rules and agent matrix.
    for name in ("dashboard",):
        shutil.copytree(source / name, release / name, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "node_modules", "dist"))
    for name in ("agents.json", "edict/backend/app/models/task.py",
                 "scripts/kanban_update.py", "scripts/file_lock.py", "scripts/utils.py"):
        target = release / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    commit = commit_id(source)
    if commit == "unknown":
        raise ValueError("missing commit provenance: use git checkout or include COMMIT in source archive")
    (release / "COMMIT").write_text(commit + "\n")
    venv.EnvBuilder(with_pip=True).create(release / ".venv")
    python = release / ".venv/bin/python"
    subprocess.run([str(python), "-I", "-m", "pip", "--isolated", "install",
                    "--no-index", "--no-deps", "--disable-pip-version-check", "--no-cache-dir",
                    "-r", str(release / "requirements.lock")], check=True)
    # Ignore PYTHONPATH and cwd; load exactly this installed release.
    entry = ("import sys; sys.path.insert(0, " + repr(str(release)) + "); "
             "from fusion.__main__ import main; raise SystemExit(main())")
    command = f"{shlex.quote(str(python))} -I -c {shlex.quote(entry)}"
    candidate = f'#!/usr/bin/env bash\nexec {command} "$@"\n'
    subprocess.run([str(python), "-I", "-c", entry, "--version"], check=True)
    subprocess.run([str(python), "-I", "-c", entry, "doctor", "--home", str(home)], check=True)
    if not no_key and not (home / "board-public.pem").exists():
        if (home / "board-private.pem").exists():
            raise ValueError("private key exists without public key; refusing overwrite")
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out",
                        str(home / "board-private.pem")], check=True)
        (home / "board-private.pem").chmod(0o600)
        subprocess.run(["openssl", "pkey", "-in", str(home / "board-private.pem"),
                        "-pubout", "-out", str(home / "board-public.pem")], check=True)
        print("Move board-private.pem to the human signing device.")
    atomic_write(home / "schema.json", json.dumps({"schema_version": SCHEMA_VERSION}).encode())
    atomic_write(launcher, candidate.encode(), 0o755)
    print(f"INSTALLED {version} launcher={launcher} release={release}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "upgrade"))
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--home", default=os.environ.get("FUSION_HOME", str(Path.home() / ".fusion")))
    parser.add_argument("--prefix", default=os.environ.get("PREFIX", str(Path.home() / ".local/bin")))
    parser.add_argument("--no-key", action="store_true")
    parser.add_argument("--stopped", action="store_true", help="confirm ALL services/workers are stopped")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args(argv)
    try:
        home, prefix = paths(args.home, args.prefix)
        launcher = prefix / "fusion"
        if launcher.is_symlink():
            raise ValueError("refusing symlink launcher")
        if args.action == "upgrade" and not args.stopped:
            raise ValueError("upgrade/rollback requires --stopped; stop all Fusion writers first")
        if args.action == "install" and (launcher.exists() or (home / "releases").exists()):
            raise ValueError("already installed; use upgrade.sh --stopped")
        if args.action == "upgrade" and not home.is_dir():
            raise ValueError("instance not found")
        if args.rollback and args.action != "upgrade":
            raise ValueError("--rollback is only supported by upgrade")
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (home / ".packaging.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ensure_stopped(home)
            if args.rollback:
                restore(home, launcher, args.rollback.absolute())
            else:
                backup = backup_instance(home, launcher)
                try:
                    install(args.source.resolve(), home, launcher, args.no_key)
                except (Exception, KeyboardInterrupt):
                    restore(home, launcher, backup)
                    raise
                if args.action == "install":
                    shutil.rmtree(backup)
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"packaging failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
