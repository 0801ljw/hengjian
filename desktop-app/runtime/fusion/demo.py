"""Reproducible real execution and independently checked artifact."""
import argparse
import json
import sys
from pathlib import Path

from .core import Store, artifact
from .runtime import CodexLocal, RunRequest

PROMPT = """Execute one bounded local task. Do not delegate or spawn other agents.
Read input.csv in this working directory. Use a terminal command to compute the
sum of amount grouped by department. Write result.json with exactly this shape:
{"totals": {"department name": integer}, "row_count": integer}.
Also write worklog.txt describing which command you ran and which input you read.
You must actually read the file, execute a command, and write both files.
Use only this working directory. Do not read credentials, change configuration,
call other services, install anything, or contact anyone. Do not modify input.csv.
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path("fusion/var"))
    parser.add_argument("--task-id", default="FUSION-P0-001")
    parser.add_argument("--allow-unpriced", action="store_true")
    args = parser.parse_args()
    parser.error("P0 model demo retired: use fusion.control; no model is started, board approval is mandatory")
    if not args.allow_unpriced:
        parser.error("existing-channel demo requires explicit --allow-unpriced; unknown is not $0")
    store = Store(args.home)
    tid = args.task_id
    store.create(tid, "本地合成部门金额汇总与文件交付")
    store.move(tid, "taizi", "Zhongshu", "固定验收用例：任务分拣", recipient="zhongshu")
    store.move(tid, "zhongshu", "Menxia", "固定方案：只读合成 CSV，产出 JSON 和命令记录", recipient="menxia")
    store.move(tid, "menxia", "Assigned", "fixture 审核：无外联，无配置更改，需独立校验", recipient="shangshu")
    store.move(tid, "shangshu", "Doing", "按原通信矩阵派发工部", recipient="gongbu")
    run_id = store.checkout(tid, args.allow_unpriced)
    workspace = store.home / "runs" / run_id
    workspace.mkdir(parents=True)
    (workspace / "input.csv").write_text("department,amount\ngongbu,17\nhubu,23\ngongbu,5\nhubu,9\n", encoding="utf-8")
    (workspace / "prompt.txt").write_text(PROMPT, encoding="utf-8")
    request = RunRequest(run_id, tid, "gongbu", workspace, PROMPT)
    print(json.dumps({"task_id": tid, "run_id": run_id, "workspace": str(workspace)}), flush=True)
    try:
        result = CodexLocal().execute(request).to_dict()
        if result["status"] == "succeeded":
            try:
                actual = json.loads((workspace / "result.json").read_text())
                assert actual == {"totals": {"gongbu": 22, "hubu": 32}, "row_count": 4}, actual
                assert (workspace / "worklog.txt").stat().st_size > 0
                assert (workspace / "input.csv").read_text() == "department,amount\ngongbu,17\nhubu,23\ngongbu,5\nhubu,9\n"
                events = [json.loads(line) for line in (workspace / "runtime.jsonl").read_text().splitlines()]
                commands = [e for e in events if e.get("item", {}).get("type") == "command_execution"
                            and e.get("type") == "item.completed" and e["item"].get("exit_code") == 0]
                assert commands, "no successful command execution in runtime evidence"
                result["artifact"] = artifact(workspace / "result.json")
                result["verification"] = "exact totals, row count, unchanged input, worklog and successful command event"
            except (AssertionError, OSError, ValueError) as exc:
                result.update(status="failed", validation_error=str(exc))
    except BaseException as exc:
        store.finish(run_id, {"status": "interrupted", "error": f"{type(exc).__name__}: {exc}"})
        raise
    store.finish(run_id, result)
    if result["status"] == "succeeded":
        store.move(tid, "gongbu", "Review", "独立验收通过，工部回报", command="done", recipient="shangshu")
        store.move(tid, "shangshu", "Done", "fixture 汇总后请求门下确认", recipient="menxia")
        store.move(tid, "menxia", "Done", "fixture 门下确认独立校验结果", command="confirm")
    (workspace / "receipt.json").write_text(json.dumps(store.snapshot(), ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
