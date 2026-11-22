"""`fusion recover --home DIR`：手动触发启动期同款回收（只读检查 + 幂等回收）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import Store
from .recovery import reconcile


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="fusion recover",
                                description="回收断电/崩溃后遗留的 running 提交与租约")
    p.add_argument("--home", type=Path, required=True)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    store = Store(a.home)
    outcome = reconcile(store)
    queue = outcome.get("queue") or {}
    payload = {"ok": True, "reclaimed": queue.get("reclaimed") or [],
               "skipped": queue.get("skipped") or "", "checked": queue.get("checked", 0)}
    if a.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if payload["skipped"] == "consumer_online":
            print("消费进程在线，跳过回收（不打扰运行中的任务）")
        elif payload["reclaimed"]:
            print("已回收 %d 个遗留提交：" % len(payload["reclaimed"]))
            for item in payload["reclaimed"]:
                print("  -", item)
        else:
            print("没有需要回收的提交（检查了 %d 个）" % payload["checked"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
