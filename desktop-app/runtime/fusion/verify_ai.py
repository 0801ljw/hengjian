"""Offline verification of real-round hashes, schemas, artifacts and disposition."""
import argparse
import hashlib
import json
from pathlib import Path

from .ai import MODEL, strict_json
from .ai_schema import SCHEMAS, check_order, check_plan, validate
from .mutate import payload_hash


def verify(home):
    receipt = strict_json((home / "receipt.json").read_text())
    assert receipt["status"] == "verified", receipt.get("error")
    records = receipt["audit"]["steps"]
    assert records == [strict_json(line) for line in (home / "audit/decisions.jsonl").read_text().splitlines()]
    calls = [strict_json(line) for line in (home / "audit/calls.jsonl").read_text().splitlines()]
    assert len(calls) == receipt["audit"]["calls"] <= 12
    assert receipt["audit"]["total_cost_usd"] is None
    valid = []
    for record in records:
        request = strict_json((home / "audit" / record["request_path"]).read_text())
        assert payload_hash(request) == record["prompt_sha256"]
        assert request["model"] == record["model"] == MODEL
        assert payload_hash(MODEL) == record["model_sha256"]
        assert request["stream"] is True and request["reasoning_effort"] == "low"
        raw = (home / "audit" / record["output_path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record["output_sha256"]
        assert record["http_status"] == 200 and record["elapsed_sec"] >= 0
        assert record["at"] and record["pricing"] == "unknown" and record["cost_usd"] is None
        if record["status"] == "valid":
            value = strict_json(raw)
            assert record["decision"] == value
            validate(value, SCHEMAS[record["step"]])
            valid.append(record)
    assert {item["step"] for item in valid} == {"taizi", "zhongshu", "menxia", "shangshu", "gongbu"}
    assert {item["decision"]["kind"] for item in valid if item["step"] == "taizi"} == {"task", "chat"}
    assert {item["decision"]["verdict"] for item in valid if item["step"] == "menxia"} == {"approve", "reject"}
    result = receipt["pipeline"]
    assert result["status"] == "awaiting_board" and result["state"] == "Review"
    snapshot = receipt["snapshot"]
    run = next(run for run in snapshot["runs"] if run["id"] == result["run_id"])
    task = next(task for task in snapshot["tasks"] if task["id"] == run["task_id"])
    assert task["state"] == "Review"
    assert run["status"] == "succeeded"
    assert run["fencing_token"] > 0
    assert run["cost_usd"] is None
    assert not any(d["kind"] == "approval" for d in snapshot["decisions"])
    plan = task["payload"]["plan"]
    check_plan(plan)
    check_order(task["payload"]["dispatch"]["order"], plan)
    # Evidence may be copied elsewhere; resolve generated artifacts by basename
    # under the original run directory, never fetch remote paths.
    workspace = home / "store/runs" / result["run_id"]
    for entry in result["files"] + [result["artifact"]]:
        original = Path(entry["path"])
        relative = original.relative_to(Path(result["workspace"]))
        content = (workspace / relative).read_bytes()
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        assert len(content) == entry["bytes"]
    if receipt.get("previous_pipelines"):
        document = (workspace / "handoff.md").read_text()
        assert all(term in document for term in ("太子", "中书", "门下", "尚书", "工部", "预算", "lease", "签批处"))
    return {"verified": True, "calls": len(calls), "files": len(result["files"]),
            "tokens": receipt["audit"].get("total_tokens"), "total_cost_usd": "unknown",
            "state": "Review", "board_approval": "not granted"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.home), ensure_ascii=False))


if __name__ == "__main__":
    main()
