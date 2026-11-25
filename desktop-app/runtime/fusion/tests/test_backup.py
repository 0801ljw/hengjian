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


def test_encrypted_backup_can_be_decrypted(tmp_path):
    import subprocess
    instance = tmp_path / "instance"
    instance.mkdir()
    import sqlite3
    with sqlite3.connect(instance / "fusion.sqlite3") as conn:
        conn.execute("CREATE TABLE t(x)")
    password = tmp_path / "pass.txt"
    password.write_text("test-passphrase-1")
    result = backup(instance, tmp_path / "backups", keep=7, passphrase_file=str(password))
    target = Path(result["path"])
    assert result["encrypted"] is True and target.suffix == ".enc"
    plain = tmp_path / "decrypted.tar.gz"
    subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
                    "-in", str(target), "-out", str(plain), "-pass", f"file:{password}"],
                   check=True, capture_output=True)
    with tarfile.open(plain, "r:gz") as archive:
        assert "fusion.sqlite3" in archive.getnames()
