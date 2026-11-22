"""Migration, real disaster recovery and independent tenant boundaries."""
import io
import json
import subprocess
import sys
import tarfile
import threading
import urllib.request
from pathlib import Path

import pytest

from fusion.budget import ledger
from fusion.core import PolicyError, Store
from fusion.dashboard import make_server
from fusion.mutate import canonical
from fusion.restore_drill import inspect, seed
from fusion.transfer import export_bundle, import_bundle


@pytest.fixture
def backup(tmp_path):
    public = tmp_path / "TEST-ONLY-public.pem"
    home = tmp_path / "source"
    before = seed(home, public, "alpha")
    bundle = tmp_path / "bundle.tar.gz"
    manifest = export_bundle(home, bundle, company="alpha")
    return home, public, before, bundle, manifest


def rewrite(bundle, change, *, rehash=True):
    """Simulate malformed or deliberately edited logical bundles."""
    from hashlib import sha256
    with tarfile.open(bundle) as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        state = json.load(archive.extractfile("state.json"))
    change(manifest, state)
    data = canonical(state).encode()
    if rehash:
        manifest["state_sha256"] = sha256(data).hexdigest()
    with tarfile.open(bundle, "w:gz") as archive:
        for name, data in (("manifest.json", canonical(manifest).encode()), ("state.json", data)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def test_roundtrip_all_tables_and_budget_approval_chain(backup, tmp_path):
    _, public, before, bundle, manifest = backup
    dest = tmp_path / "destination"
    assert import_bundle(bundle, dest, company="alpha", board_key_file=public) == manifest
    assert inspect(dest, public, "alpha") == before
    assert before["company_remaining_cents"] == 970
    assert before["agent_remaining_cents"] == 470
    store = Store(dest, public, company="alpha")
    assert store.get("T")["state"] == "Done"
    assert store.get("PENDING")["state"] == "PendingConfirm"
    store.move("PENDING", "menxia", "Done", "independent confirmation after restore", command="confirm")
    # A fresh high-risk action remains gated after migration.
    store.create("NEW", "unapproved")
    for actor, target in (("taizi", "Zhongshu"), ("zhongshu", "Menxia"),
                          ("menxia", "Assigned"), ("shangshu", "Doing")):
        store.move("NEW", actor, target, "route")
    store.move("NEW", "gongbu", "Review", "review", command="done")
    with pytest.raises(PolicyError, match="approval"):
        store.move("NEW", "shangshu", "Done", "unapproved completion")


def test_existing_empty_target_supported(backup, tmp_path):
    _, public, _, bundle, _ = backup
    dest = tmp_path / "empty"
    dest.mkdir()
    import_bundle(bundle, dest, company="alpha", board_key_file=public)
    assert Store(dest, public, company="alpha").get("T")["state"] == "Done"


def test_default_legacy_store_roundtrip(tmp_path):
    source = tmp_path / "old"
    store = Store(source)
    store.create("LEGACY", "legacy default company")
    bundle = tmp_path / "old.tar.gz"
    export_bundle(source, bundle)
    dest = tmp_path / "restored"
    import_bundle(bundle, dest)
    assert Store(dest).snapshot() == store.snapshot()


def test_publish_failure_rolls_back_staging(backup, tmp_path, monkeypatch):
    import fusion.transfer as transfer
    def fail(*args):
        raise OSError("injected rename failure")
    monkeypatch.setattr(transfer.os, "rename", fail)
    dest = tmp_path / "failed-publish"
    with pytest.raises(OSError, match="injected"):
        import_bundle(backup[3], dest, company="alpha", board_key_file=backup[1])
    assert not dest.exists()
    assert not list(tmp_path.glob(".fusion-import-*"))


def test_untrusted_archive_path_never_extracted(backup, tmp_path):
    with tarfile.open(backup[3], "w:gz") as archive:
        info = tarfile.TarInfo("../escaped")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(PolicyError, match="archive member"):
        import_bundle(backup[3], tmp_path / "target", company="alpha", board_key_file=backup[1])
    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path / "target").exists()


def test_export_cannot_overwrite_company_database(backup):
    home = backup[0]
    store = Store(home, backup[1], company="alpha")
    with pytest.raises(PolicyError, match="outside"):
        export_bundle(home, store.db, company="alpha")
    assert inspect(home, backup[1], "alpha") == backup[2]


def test_nonempty_target_never_modified(backup, tmp_path):
    _, public, _, bundle, _ = backup
    dest = tmp_path / "nonempty"
    dest.mkdir()
    (dest / "sentinel").write_text("keep")
    with pytest.raises(PolicyError, match="empty"):
        import_bundle(bundle, dest, company="alpha", board_key_file=public)
    assert list(dest.iterdir()) == [dest / "sentinel"]
    assert (dest / "sentinel").read_text() == "keep"


@pytest.mark.parametrize("corruption", ["hash", "version", "counts", "schema", "foreign_key", "signature", "approval_chain", "forged_creation", "columns", "company", "truncated"])
def test_bad_bundles_rejected_without_residue(backup, tmp_path, corruption):
    _, public, _, bundle, _ = backup
    def change(manifest, state):
        if corruption == "hash":
            state["tasks"]["rows"][0][1] = "changed"
        elif corruption == "version":
            manifest["schema_version"] = 999
        elif corruption == "counts":
            manifest["record_counts"]["tasks"] += 1
        elif corruption == "schema":
            manifest["schema_sha256"] = "0" * 64
        elif corruption == "foreign_key":
            state["events"]["rows"][0][1] = "missing-task"
        elif corruption == "signature":
            state["budget_policy"]["rows"][0][2] = canonical({"alg": "ed25519", "document": {}, "signature": "0" * 128})
        elif corruption == "approval_chain":
            state["approval_uses"]["rows"] = []
            manifest["record_counts"]["approval_uses"] = 0
        elif corruption == "forged_creation":
            for row in state["tasks"]["rows"]:
                if row[0] == "OPEN":
                    row[1] = canonical(dict(json.loads(row[1]), state="Done"))
            for row in state["events"]["rows"]:
                if row[1] == "OPEN" and row[4] == "create":
                    row[5] = canonical(dict(json.loads(row[5]), to="Done"))
        elif corruption == "columns":
            state["leases"]["columns"][0] = "unknown_column"
        elif corruption == "company":
            manifest["company"] = "beta"
    if corruption == "truncated":
        bundle.write_bytes(bundle.read_bytes()[:30])
    else:
        rewrite(bundle, change, rehash=corruption != "hash")
    for existing in (False, True):
        dest = tmp_path / f"destination-{existing}"
        if existing:
            dest.mkdir()
        with pytest.raises(PolicyError):
            import_bundle(bundle, dest, company="alpha", board_key_file=public)
        assert (dest.is_dir() and list(dest.iterdir()) == []) if existing else not dest.exists()
        assert list(tmp_path.glob(".fusion-import-*")) == []


def test_missing_verification_key_refuses_signed_state(backup, tmp_path):
    with pytest.raises(PolicyError, match="key"):
        import_bundle(backup[3], tmp_path / "destination", company="alpha")
    assert not (tmp_path / "destination").exists()


@pytest.mark.parametrize("name", ["../alpha", "/tmp/alpha", "", "..", "a/b", "a\\b"])
def test_company_names_reject_path_escape(tmp_path, name):
    with pytest.raises(PolicyError, match="company"):
        Store(tmp_path, company=name)


def test_company_symlink_rejected(tmp_path):
    Store(tmp_path, company="alpha")
    (tmp_path / "companies" / "beta").symlink_to(tmp_path / "companies" / "alpha", target_is_directory=True)
    with pytest.raises(PolicyError, match="symlink"):
        Store(tmp_path, company="beta")


def test_three_query_paths_and_same_task_id_isolation(backup):
    home, public, before, _, _ = backup
    beta = Store(home, public, company="beta")
    # Three independent query paths: task lookup, snapshot and budget ledger.
    with pytest.raises(PolicyError, match="missing task"):
        beta.get("T")
    assert beta.snapshot() == {"tasks": [], "runs": [], "events": [], "decisions": []}
    assert ledger(beta)["reservations"] == ledger(beta)["events"] == []
    assert beta.budget_status()["company"]["known_spend_cents"] == 0
    beta.create("T", "only beta")
    alpha = Store(home, public, company="alpha")
    assert alpha.get("T")["title"] != beta.get("T")["title"]
    assert inspect(home, public, "alpha") == before
    with beta.connect() as conn:
        assert conn.execute("SELECT count(*) FROM decisions").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM leases").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM budget_policy").fetchone() == (0,)
    # Default does not fall back to any named company.
    assert Store(home).snapshot()["tasks"] == []


def test_cross_company_approval_id_and_budget_cannot_be_reused(backup):
    home, public, _, _, _ = backup
    alpha = Store(home, public, company="alpha")
    beta = Store(home, public, company="beta")
    beta.create("T", "beta fixture")
    beta.move("T", "taizi", "Zhongshu", "route")
    beta.move("T", "zhongshu", "Menxia", "plan")
    approval = alpha.get("PENDING")["pending_confirm"]["approval_id"]
    with pytest.raises(PolicyError, match="approval"):
        beta.move("T", "menxia", "Cancelled", "bad shared approval", approval_id=approval)
    for actor, target in (("menxia", "Assigned"), ("shangshu", "Doing")):
        beta.move("T", actor, target, "fixture")
    with pytest.raises(PolicyError, match="BUDGET_NOT_CONFIGURED"):
        beta.checkout("T", provider="offline", estimated_cents=10)
    # Same run ID from alpha is not visible in beta.
    with pytest.raises(PolicyError):
        beta.finish(alpha.snapshot()["runs"][0]["id"], {"status": "failed"})


def test_same_task_and_run_ids_have_independent_leases_and_budgets(tmp_path):
    from fusion.leases import read
    from fusion.tests.test_fusion import doing
    stores = [Store(tmp_path, company=company) for company in ("alpha", "beta")]
    for store in stores:
        doing(store)
        store.checkout("T", True, run_id="SAME-RUN")
    alpha, beta = stores
    with alpha.connect() as conn_a, beta.connect() as conn_b:
        assert read(conn_a, "T")["owner_id"] == read(conn_b, "T")["owner_id"] == "SAME-RUN"
    alpha.finish("SAME-RUN", {"status": "failed"})
    with alpha.connect() as conn_a, beta.connect() as conn_b:
        assert read(conn_a, "T")["owner_id"] is None
        assert read(conn_b, "T")["owner_id"] == "SAME-RUN"
    assert alpha.snapshot()["runs"][0]["status"] == "failed"
    assert beta.snapshot()["runs"][0]["status"] == "running"
    assert ledger(alpha)["reservations"][0]["status"] == "settled"
    assert ledger(beta)["reservations"][0]["status"] == "reserved"
    beta.finish("SAME-RUN", {"status": "failed"})


def test_export_refuses_active_execution_and_keeps_source(tmp_path):
    store = Store(tmp_path / "source", company="alpha")
    from fusion.tests.test_fusion import doing
    doing(store)
    run = store.checkout("T", True)
    before = store.snapshot()
    with pytest.raises(PolicyError, match="quiescent"):
        export_bundle(tmp_path / "source", tmp_path / "no.tar.gz", company="alpha")
    assert store.snapshot() == before
    assert not (tmp_path / "no.tar.gz").exists()
    store.finish(run, {"status": "failed"})
    export_bundle(tmp_path / "source", tmp_path / "ok.tar.gz", company="alpha")


def test_real_process_delete_and_restore(tmp_path):
    out = tmp_path / "drill"
    process = subprocess.run([sys.executable, "-m", "fusion.restore_drill", "--output", str(out)],
                             capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stdout + process.stderr
    evidence = json.loads((out / "result.json").read_text())
    assert evidence["status"] == "passed"
    assert evidence["source_deleted_before_import"]
    assert not (out / "original-store").exists()
    assert evidence["before"] == evidence["after"]
    assert '"operation":"shutil.rmtree"' in (out / "drill.log").read_text()


def test_two_dashboards_do_not_share_global_tenant_data(tmp_path):
    servers, threads = [], []
    try:
        for company in ("alpha", "beta"):
            store = Store(tmp_path, company=company)
            store.create(company, company)
            server = make_server(store, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append(server)
            threads.append(thread)
        for server, company in zip(servers, ("alpha", "beta")):
            for path in ("/api/fusion/status", "/api/live-status"):
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=3) as response:
                    data = json.load(response)
                assert [task["id"] for task in data["tasks"]] == [company]
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


def test_cli_flag_positions_and_export_only_selected_company(backup, tmp_path):
    home, public, _, _, _ = backup
    beta = Store(home, company="beta")
    beta.create("SECRET", "beta-private")
    def cli(*args):
        return subprocess.run([sys.executable, "-m", "fusion", *map(str, args)], capture_output=True, text=True, timeout=20)
    bundle = tmp_path / "cli.tar.gz"
    assert cli("--home", home, "--company", "alpha", "export", "--out", bundle).returncode == 0
    target = tmp_path / "cli-target"
    process = cli("import", "--bundle", bundle, "--home", target, "--company", "alpha", "--board-key-file", public)
    assert process.returncode == 0, process.stdout + process.stderr
    assert not (target / "companies" / "beta").exists()
    assert cli("snapshot", "--home", home, "--company", "beta").stdout.find("beta-private") >= 0
    assert "beta-private" not in cli("snapshot", "--home", home, "--company", "alpha").stdout


def test_pending_confirm_can_reopen_to_review(backup, tmp_path):
    _, public, _, bundle, _ = backup
    dest = tmp_path / "reopen-destination"
    import_bundle(bundle, dest, company="alpha", board_key_file=public)
    store = Store(dest, public, company="alpha")
    assert store.get("PENDING")["state"] == "PendingConfirm"
    store.move("PENDING", "shangshu", "Review", "approval expired; reopen", command="state")
    task = store.get("PENDING")
    assert task["state"] == "Review"
    assert "pending_confirm" not in task
