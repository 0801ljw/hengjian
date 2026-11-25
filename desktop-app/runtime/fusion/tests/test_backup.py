import sqlite3
import tarfile
from pathlib import Path

from fusion.backup import backup


def test_backup_contains_database_and_auth(tmp_path):
    instance = tmp_path / "instance"
    instance.mkdir()
    with sqlite3.connect(instance / "fusion.sqlite3") as conn:
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES (1)")
    (instance / "auth").mkdir()
    (instance / "auth" / "users.json").write_text("{}")
    (instance / "tokens.json").write_text("{}")
    result = backup(instance, tmp_path / "backups", keep=7)
    target = Path(result["path"])
    assert target.is_file() and result["bytes"] > 0
    with tarfile.open(target, "r:gz") as archive:
        names = archive.getnames()
    assert "fusion.sqlite3" in names and "auth/users.json" in names and "tokens.json" in names
