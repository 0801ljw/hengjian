"""Offline packaging regressions, including destructive candidate failure."""
import gc
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from fusion.core import Store
from fusion.users import UserStore

ROOT = Path(__file__).resolve().parents[2]


def run(*args, cwd=ROOT):
    return subprocess.run([str(a) for a in args], cwd=cwd, text=True,
                          capture_output=True, timeout=90,
                          env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"})


def source_copy(tmp_path, version="1.0.1"):
    source = tmp_path / "new source"
    source.mkdir()
    shutil.copytree(ROOT / "fusion", source / "fusion",
                    ignore=shutil.ignore_patterns("__pycache__", "var", "evidence"))
    shutil.copy2(ROOT / "requirements.lock", source / "requirements.lock")
    for name in ("dashboard",):
        shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns(
            "__pycache__", "node_modules", "dist"))
    for name in ("agents.json", "edict/backend/app/models/task.py",
                 "scripts/kanban_update.py", "scripts/file_lock.py", "scripts/utils.py"):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    (source / "VERSION").write_text(version + "\n")
    (source / "COMMIT").write_text("abcdef012345\n")
    return source


@pytest.fixture
def instance(tmp_path):
    home, prefix = tmp_path / "instance", tmp_path / "bin"
    result = run("bash", "fusion/install.sh", "--home", home, "--prefix", prefix, "--no-key")
    assert result.returncode == 0, result.stdout + result.stderr
    db = home / "db"
    Store(db)
    # Store's sqlite contexts commit but do not explicitly close connections.
    # Collect their cycles before the stopped-instance snapshot; otherwise a
    # later GC WAL checkpoint can race the byte-for-byte backup assertion.
    gc.collect()
    UserStore(db).add_user("admin", "fixture-password-123", "admin")
    (db / "roles.json").write_text(json.dumps({"mode": "single", "fixture": "keep roles"}))
    (db / "api-audit.jsonl").write_text('{"fixture":"keep audit"}\n')
    (db / "roles.json").chmod(0o600)
    return home, prefix


def data_snapshot(home):
    return {str(p.relative_to(home)): (p.read_bytes(), p.stat().st_mode & 0o777)
            for p in (home / "db").rglob("*") if p.is_file()}


def test_version_single_source(tmp_path):
    from fusion import __version__
    assert __version__ == (ROOT / "VERSION").read_text().strip()
    result = run(sys.executable, "-m", "fusion", "--version")
    assert result.returncode == 0
    assert re.fullmatch(rf"fusion {re.escape(__version__)} \(commit [0-9a-f]{{7,40}}\)\n", result.stdout)
    source = source_copy(tmp_path, "8.7.6")
    result = run(sys.executable, "-m", "fusion", "--version", cwd=source)
    assert result.stdout == "fusion 8.7.6 (commit abcdef012345)\n"


def test_upgrade_preserves_data(instance, tmp_path):
    home, prefix = instance
    before = data_snapshot(home)
    old_launcher = (prefix / "fusion").read_bytes()
    source = source_copy(tmp_path)
    result = run("bash", source / "fusion/upgrade.sh", "--home", home,
                 "--prefix", prefix, "--stopped", "--no-key")
    assert result.returncode == 0, result.stdout + result.stderr
    assert data_snapshot(home) == before
    assert UserStore(home / "db").login("admin", "fixture-password-123")
    assert (prefix / "fusion").read_bytes() != old_launcher
    assert run(prefix / "fusion", "--version").stdout == "fusion 1.0.1 (commit abcdef012345)\n"
    backups = list(home.glob("backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "instance/db/users.json").read_bytes() == before["db/users.json"][0]


def test_upgrade_rollback_on_failure(instance, tmp_path):
    home, prefix = instance
    before = data_snapshot(home)
    launcher = (prefix / "fusion").read_bytes()
    source = source_copy(tmp_path)
    # Corrupt data INSIDE the new release's post-install doctor, not a mock of
    # rollback. The real shell command must restore every file and permission.
    packaging = source / "fusion/packaging.py"
    code = packaging.read_text().replace(
        "if args.home:\n            check_schema(args.home)",
        "if args.home:\n"
        "            (args.home / 'db/roles.json').write_text('corrupted')\n"
        "            (args.home / 'db/users.json').unlink()\n"
        "            with sqlite3.connect(args.home / 'db/fusion.sqlite3') as db:\n"
        "                db.execute('PRAGMA user_version=999')\n"
        "            (args.home / 'db/extra').write_text('should disappear')\n"
        "            raise ValueError('injected new-version check failure')")
    assert "injected new-version" in code
    packaging.write_text(code)
    result = run("bash", "fusion/upgrade.sh", "--source", source, "--home", home,
                 "--prefix", prefix, "--stopped", "--no-key")
    assert result.returncode != 0
    assert "ROLLBACK VERIFIED" in result.stdout
    assert "injected new-version check failure" in result.stderr
    assert data_snapshot(home) == before
    assert (prefix / "fusion").read_bytes() == launcher
    assert len(list((home / "releases").iterdir())) == 1
    assert run(prefix / "fusion", "--version").stdout.startswith("fusion 1.0.0 ")
    assert run(prefix / "fusion", "doctor", "--home", home).returncode == 0


def test_manual_rollback(instance, tmp_path):
    home, prefix = instance
    before = data_snapshot(home)
    launcher = (prefix / "fusion").read_bytes()
    result = run("bash", "fusion/upgrade.sh", "--source", source_copy(tmp_path),
                 "--home", home, "--prefix", prefix, "--stopped", "--no-key")
    assert result.returncode == 0, result.stderr
    backup = next(home.glob("backup-*"))
    (home / "db/roles.json").write_text("changed after upgrade")
    result = run("bash", "fusion/upgrade.sh", "--home", home, "--prefix", prefix,
                 "--stopped", "--rollback", backup)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ROLLBACK VERIFIED" in result.stdout
    assert data_snapshot(home) == before
    assert (prefix / "fusion").read_bytes() == launcher
    assert run(prefix / "fusion", "doctor", "--home", home).returncode == 0


@pytest.mark.parametrize("version", [999, True, "1"])
def test_doctor_rejects_unknown_schema(tmp_path, version):
    from fusion.packaging import doctor_main
    (tmp_path / "schema.json").write_text(json.dumps({"schema_version": version}))
    assert doctor_main(["--home", str(tmp_path)]) == 1


def test_upgrade_requires_stopped(instance):
    home, prefix = instance
    result = run("bash", "fusion/upgrade.sh", "--home", home, "--prefix", prefix)
    assert result.returncode != 0 and "--stopped" in result.stderr
    assert not list(home.glob("backup-*"))


def test_lock_rejects_unpinned_dependency(tmp_path):
    source = source_copy(tmp_path)
    (source / "requirements.lock").write_text("requests>=2\n")
    result = run("bash", source / "fusion/install.sh", "--home", tmp_path / "home",
                 "--prefix", tmp_path / "bin", "--no-key")
    assert result.returncode != 0 and "only exact pins" in result.stderr
    assert not (tmp_path / "bin/fusion").exists()


def test_running_launcher_is_detected(tmp_path):
    from fusion.packaging import ensure_stopped
    home = tmp_path / "home"
    process = subprocess.Popen([sys.executable, "-c",
                                "import time; time.sleep(30) # from fusion.__main__ import main",
                                "serve", "--home", str(home / "db")])
    try:
        with pytest.raises(ValueError, match="stop Fusion process"):
            ensure_stopped(home)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_corrupt_backup_refuses_destructive_restore(instance, tmp_path):
    from fusion.packaging import backup_instance, restore
    home, prefix = instance
    backup = backup_instance(home, prefix / "fusion")
    before = data_snapshot(home)
    (backup / "instance/db/roles.json").write_text("tampered backup")
    with pytest.raises(ValueError, match="backup verification failed"):
        restore(home, prefix / "fusion", backup)
    assert data_snapshot(home) == before


def test_database_future_schema_is_rejected_without_changes(tmp_path):
    import sqlite3
    from fusion.packaging import check_schema
    Store(tmp_path)
    gc.collect()
    with sqlite3.connect(tmp_path / "fusion.sqlite3") as conn:
        conn.execute("PRAGMA user_version=999")
    conn.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises(ValueError, match="incompatible database schema version: 999"):
        check_schema(tmp_path)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
