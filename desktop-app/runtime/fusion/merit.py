"""Read-only 功过簿 (merit ledger) projected from the authoritative ledger.

Pure projection: no model calls, no filesystem writes and no mutation. Metrics
are intentionally few and auditable, and a role with too few samples is shown
as "样本不足" instead of being ranked on noise. Badge thresholds are stated in
each role's `reason` so a human can recompute them.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .core import now as core_now

ROLE_NAMES = {
    "taizi": "太子",
    "zhongshu": "中书",
    "menxia": "门下",
    "shangshu": "尚书",
    "gongbu": "工部",
}
MIN_SAMPLES = 3
RECENT_DAYS = 7
TERMINAL = {"Done", "Cancelled"}
EXECUTION_FAILED = {"failed", "timeout", "unavailable"}


def _ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    delta = (end - start).total_seconds()
    return round(delta, 1) if delta >= 0 else None


def _mean(values: list[float]) -> float | None:
    usable = [value for value in values if value is not None]
    return round(sum(usable) / len(usable), 1) if usable else None


def _badge_quality(samples: int, rate: float | None, *, good: float, bad: float,
                   good_hint: str, bad_hint: str,
                   min_samples: int = MIN_SAMPLES) -> tuple[str, str]:
    if samples < min_samples:
        return "样本不足", f"样本 {samples} < {min_samples}，不参与评比"
    if rate is None:
        return "样本不足", "样本不足，不参与评比"
    if rate >= good:
        return "能臣", f"{good_hint}（{_pct(rate)}）"
    if rate < bad:
        return "需要训练", f"{bad_hint}（{_pct(rate)}）"
    return "待观察", f"{good_hint}（{_pct(rate)}）未达表彰线"


def merit_view(snapshot: dict, *, now: datetime | None = None,
               min_samples: int = MIN_SAMPLES, recent_days: int = RECENT_DAYS) -> dict:
    moment = now or _ts(core_now()) or datetime.now(timezone.utc)
    tasks = list(snapshot.get("tasks") or [])
    events = list(snapshot.get("events") or [])
    runs = list(snapshot.get("runs") or [])

    by_task: dict[str, list[dict]] = defaultdict(list)
    participated: dict[str, set[str]] = defaultdict(set)
    for event in events:
        by_task[event.get("task_id")].append(event)
        if isinstance(event.get("actor"), str):
            participated[event["actor"]].add(event.get("task_id"))
    for run in runs:
        participated["gongbu"].add(run.get("task_id"))
    for bucket in by_task.values():
        bucket.sort(key=lambda item: item.get("seq", 0))

    state = {task.get("id"): task.get("state") for task in tasks}
    total = len(tasks)
    done = sum(1 for value in state.values() if value == "Done")
    cancelled = sum(1 for value in state.values() if value == "Cancelled")
    in_flight = max(0, total - done - cancelled)
    overall = {
        "tasks": total,
        "done": done,
        "cancelled": cancelled,
        "in_flight": in_flight,
        "completion_rate": _ratio(done, total),
    }

    def count_events(actor: str, action: str, **match: object) -> int:
        return sum(1 for event in events
                   if event.get("actor") == actor and event.get("action") == action
                   and all((event.get("detail") or {}).get(key) == value
                           for key, value in match.items()))

    # 太子: routing volume only (no scalar quality).
    routed = count_events("taizi", "state", to="Zhongshu")

    # 中书: plans submitted and how often 门下 sent them back for rework.
    plans = count_events("zhongshu", "state", to="Menxia")
    bounces = sum(1 for event in events
                  if event.get("actor") == "menxia" and event.get("action") == "state"
                  and (event.get("detail") or {}).get("from") == "Menxia"
                  and (event.get("detail") or {}).get("to") == "Zhongshu")

    # 门下: verdicts and time from 中书 submission to the decision.
    reviews = approvals = rejections = 0
    review_durations: list[float] = []
    for task_events in by_task.values():
        submitted_at = None
        for event in task_events:
            detail = event.get("detail") or {}
            if event.get("actor") == "zhongshu" and detail.get("to") == "Menxia":
                submitted_at = _ts(event.get("at"))
            if event.get("actor") == "menxia" and event.get("action") == "review_decision":
                decision = detail.get("decision")
                if decision not in {"accept", "reject"}:
                    continue
                reviews += 1
                if decision == "accept":
                    approvals += 1
                else:
                    rejections += 1
                duration = _seconds(submitted_at, _ts(event.get("at")))
                if duration is not None:
                    review_durations.append(duration)
                submitted_at = None

    # 尚书: dispatches, confirmations and time from 门下放行 to dispatch.
    dispatches = count_events("shangshu", "dispatch")
    confirmations = count_events("shangshu", "confirm")
    dispatch_durations: list[float] = []
    for task_events in by_task.values():
        approved_at = None
        for event in task_events:
            detail = event.get("detail") or {}
            if event.get("actor") == "menxia" and event.get("action") == "state" \
                    and detail.get("to") == "Assigned":
                approved_at = _ts(event.get("at"))
            if event.get("actor") == "shangshu" and event.get("action") == "dispatch":
                duration = _seconds(approved_at, _ts(event.get("at")))
                if duration is not None:
                    dispatch_durations.append(duration)
                approved_at = None

    # 工部: runs, outcomes, duration and post-failure blocking.
    succeeded = sum(1 for run in runs if run.get("status") == "succeeded")
    failed = sum(1 for run in runs if run.get("status") in EXECUTION_FAILED)
    run_durations = [_seconds(_ts(run.get("started_at")), _ts(run.get("ended_at"))) for run in runs]
    blocked_runs = count_events("gongbu", "block")
    success_rate = _ratio(succeeded, len(runs))

    recent_from = moment - timedelta(days=recent_days)
    recent_done = sum(1 for event in events
                      if (event.get("detail") or {}).get("to") == "Done"
                      and (_ts(event.get("at")) or recent_from) >= recent_from)
    recent_failed_runs = sum(1 for run in runs
                             if run.get("status") in EXECUTION_FAILED
                             and (_ts(run.get("ended_at")) or recent_from) >= recent_from)

    gongbu_badge, gongbu_reason = _badge_quality(
        len(runs), success_rate, good=0.9, bad=0.7,
        good_hint="成功率 ≥ 90%", bad_hint="成功率 < 70%",
        min_samples=min_samples)
    # 中书: lower rework is better, so compare directly instead of via _badge_quality.
    zhongshu_rework = _ratio(bounces, plans)
    if plans < min_samples:
        zhongshu_badge, zhongshu_reason = "样本不足", f"计划样本 {plans} < {min_samples}，不参与评比"
    elif zhongshu_rework is None:
        zhongshu_badge, zhongshu_reason = "样本不足", "无可比计划样本"
    elif zhongshu_rework <= 0.2:
        zhongshu_badge, zhongshu_reason = "能臣", f"封驳退回率 {_pct(zhongshu_rework)} ≤ 20%"
    elif zhongshu_rework >= 0.5:
        zhongshu_badge, zhongshu_reason = "需要训练", f"封驳退回率 {_pct(zhongshu_rework)} ≥ 50%"
    else:
        zhongshu_badge, zhongshu_reason = "待观察", f"封驳退回率 {_pct(zhongshu_rework)} 未达表彰线"

    def role_report(role: str, summary: str, badge: str, reason: str) -> dict:
        return {
            "role": role,
            "name": ROLE_NAMES[role],
            "participated": len(participated.get(role, set())),
            "summary": summary,
            "badge": badge,
            "reason": reason,
        }

    roles = [
        role_report("taizi", f"分拣 {routed} 案", "样本不足" if routed < min_samples else "待观察",
                    f"分拣 {routed} 案；该角色无单一质量口径，以指标为准"),
        role_report("zhongshu",
                    f"计划 {plans} · 退回 {bounces} · 返工率 {_pct(zhongshu_rework)}",
                    zhongshu_badge, zhongshu_reason),
        role_report("menxia",
                    f"审议 {reviews}（准 {approvals} / 驳 {rejections}）· 平均 {_mean(review_durations) or '—'}s",
                    "样本不足" if reviews < min_samples else "待观察",
                    f"驳率 {_pct(_ratio(rejections, reviews))}；审议量 {reviews}，"
                    "该角色无单一质量口径，以指标为准"),
        role_report("shangshu",
                    f"派发 {dispatches} · 确认 {confirmations} · 平均 {_mean(dispatch_durations) or '—'}s",
                    "样本不足" if dispatches < min_samples else "待观察",
                    f"派发 {dispatches} 次；该角色无单一质量口径，以指标为准"),
        role_report("gongbu",
                    f"执行 {len(runs)}（成 {succeeded} / 败 {failed}）· 成功率 {_pct(success_rate)}",
                    gongbu_badge, gongbu_reason),
    ]

    return {
        "generated_at": moment.isoformat(),
        "min_samples": min_samples,
        "overall": overall,
        "roles": roles,
        "recent": {"days": recent_days, "done": recent_done, "failed_runs": recent_failed_runs},
        "metrics": {
            "gongbu": {"runs": len(runs), "succeeded": succeeded, "failed": failed,
                       "success_rate": success_rate,
                       "avg_run_seconds": _mean(run_durations), "blocked": blocked_runs},
            "zhongshu": {"plans": plans, "bounces": bounces, "rework_rate": zhongshu_rework},
            "menxia": {"reviews": reviews, "approvals": approvals, "rejections": rejections,
                       "rejection_rate": _ratio(rejections, reviews),
                       "avg_review_seconds": _mean(review_durations)},
            "shangshu": {"dispatches": dispatches, "confirmations": confirmations,
                         "avg_dispatch_seconds": _mean(dispatch_durations)},
            "taizi": {"routed": routed},
        },
    }
