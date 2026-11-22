"""Product CLI for AI proposals through the existing trusted Pipeline."""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from .ai import DecisionClient
from .ai_pipeline import Pipeline
from .core import PolicyError, Store


class RunParser(argparse.ArgumentParser):
    def error(self, message):
        raise PolicyError(message)


class ProgressClient:
    """Observe proposals without changing validation or mutation authority."""

    def __init__(self, client, output):
        self.client = client
        self.output = output
        self.reviews = 0

    def decide(self, step, context):
        label = {"taizi": "太子", "zhongshu": "中书", "menxia": "门下",
                 "shangshu": "尚书", "gongbu": "工部"}[step]
        print(f"[{label}] 请求决策…", file=self.output, flush=True)
        value = self.client.decide(step, context)
        if step == "menxia":
            self.reviews += 1
            detail = (f"第{self.reviews}次 {value['verdict']} 理由："
                      + "; ".join(value["reasons"]))
            if value["required_changes"]:
                detail += "；需修改：" + "; ".join(value["required_changes"])
        elif step == "taizi":
            detail = f"{value['kind']}：{value['reason']}"
        elif step == "zhongshu":
            detail = f"第{context['revision'] + 1}版方案：{value['goal']}"
        elif step == "shangshu":
            detail = "调度提议：" + ", ".join(value["order"])
        else:
            detail = f"文件提议 {len(value['files'])} 个；等待可信写入"
        print(f"[{label}] {detail}", file=self.output, flush=True)
        return value

    def summary(self):
        return self.client.summary()


def main(argv=None, *, client_factory=None):
    """Inject a DecisionClient factory for offline tests; never switch providers."""
    argv = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in argv
    progress = sys.stderr if json_output else sys.stdout
    started = time.monotonic()
    client = None
    report = {"status": "error", "cost_usd": None, "pricing": "unknown",
              "total_tokens": None}
    code = 3
    parser = RunParser(prog="fusion run", description=__doc__)
    parser.add_argument("request_text", nargs="?", help="local text-file request")
    parser.add_argument("--request", help="alternative to positional request")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--company", default="default")
    parser.add_argument("--allow-unpriced", action="store_true")
    parser.add_argument("--json", action="store_true", help="JSON stdout, live progress stderr")
    parser.add_argument("--input-root", action="append", default=[], metavar="DIR",
                        help="extra directory that --input files may live under "
                             "(repeatable); cwd and --home are always allowed")
    parser.add_argument("--input", action="append", default=[], metavar="PATH",
                        help="explicit source file to hand to the worker (repeatable, "
                             "must live under --home or the current directory)")
    try:
        args = parser.parse_args(argv)
        if args.request_text is not None and args.request is not None:
            raise PolicyError("use positional request OR --request, not both")
        request = args.request if args.request is not None else args.request_text
        if request is None or not request.strip() or len(request) > 12000:
            raise PolicyError("request must be 1..12000 characters")
        store = Store(args.home, Path("/home/ljw/.config/fusion/board_ed25519.pub.pem"),
                      company=args.company)
        task_id = "AI-" + uuid.uuid4().hex
        audit = store.home / "ai" / task_id
        report.update(task_id=task_id, audit=str(audit), company=args.company)
        factory = DecisionClient if client_factory is None else client_factory
        if (args.home / "roles.json").exists():
            from .role_binding import load
            client = factory(audit, bindings=load(args.home)["bindings"], instance=args.home)
        else:
            client = factory(audit)
        from .ai_pipeline import load_sources
        roots = [store.home, Path.cwd()] + [Path(item) for item in args.input_root]
        sources, rejected = load_sources(args.input, roots=roots)
        for item in rejected:
            print(f"[输入] 略过 {item['path']}：{item['reason']}", file=progress, flush=True)
        for item in sources:
            print(f"[输入] 读入 {item['path']}（{item['bytes']} 字节）", file=progress, flush=True)
        report.update(inputs=[{k: v for k, v in src.items() if k != "text"} for src in sources],
                      inputs_rejected=rejected)
        result = Pipeline(store, ProgressClient(client, progress), task_id).run(
            request, allow_unpriced=args.allow_unpriced, sources=sources)
        report.update(result)
        code = 2 if result["status"] == "rejected" else 0
        for item in result.get("files", []):
            print(f"[工部] 产出 {item['path']}", file=progress, flush=True)
    except (PolicyError, ValueError, TypeError, OSError, KeyError) as exc:
        report.update(status="error", error=str(exc), error_type=type(exc).__name__)
    finally:
        if client is not None:
            usage = client.summary()
            report.update(total_tokens=usage["total_tokens"],
                          cost_usd=usage["total_cost_usd"], pricing=usage["pricing"],
                          calls=usage["calls"])
        report["elapsed_sec"] = round(time.monotonic() - started, 3)
    report["exit_code"] = code
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"结果：{report['status']}；耗时 {report['elapsed_sec']}s")
        print(f"token：{report['total_tokens'] if report['total_tokens'] is not None else 'unknown'}；"
              f"成本 USD：{report['cost_usd'] if report['cost_usd'] is not None else 'unknown'}")
        if "workspace" in report:
            print(f"产物路径：{report['workspace']}")
            print("等待签批处独立审批；未自动关闭任务。")
        if "audit" in report:
            print(f"审计路径：{report['audit']}")
        if "error" in report:
            print(f"错误：{report['error']}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
