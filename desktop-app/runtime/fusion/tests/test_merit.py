"""功过簿: pure projection, explicit thresholds, insufficient samples say so."""
import secrets
from datetime import datetime, timedelta, timezone

from fusion import httpapi
from fusion.core import Store
from fusion.merit import merit_view

T0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)


def _at(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _event(seq, task, actor, action, seconds, **detail):
    return {"seq": seq, "task_id": task, "at": _at(seconds),
            "actor": actor, "action": action, "detail": detail}


def _task(task, state):
    return {"id": task, "state": state, "payload": {}, "flow_log": []}


def _view(tasks=(), events=(), runs=(), **kwargs):
    return merit_view({"tasks": list(tasks), "events": list(events),
                       "runs": list(runs)}, now=T0 + timedelta(days=30), **kwargs)


def _role(view, role):
    return next(item for item in view["roles"] if item["role"] == role)


def test_empty_ledger_is_all_insufficient_not_zero_praise():
    view = _view()
    assert view["overall"] == {"tasks": 0, "done": 0, "cancelled": 0,
                               "in_flight": 0, "completion_rate": None}
    assert {item["badge"] for item in view["roles"]} == {"样本不足"}
    assert view["metrics"]["gongbu"]["runs"] == 0


def test_gongbu_success_rate_drives_badge():
    runs = [{"status": "succeeded", "started_at": _at(0), "ended_at": _at(20)}
            for _ in range(5)]
    assert _role(_view(runs=runs), "gongbu")["badge"] == "能臣"
    failing = runs[:3] + [{"status": "failed", "started_at": _at(0), "ended_at": _at(5)}
                          for _ in range(2)]
    view = _view(runs=failing)
    assert _role(view, "gongbu")["badge"] == "需要训练"
    assert view["metrics"]["gongbu"]["success_rate"] == 0.6
    assert _role(_view(runs=runs[:2]), "gongbu")["badge"] == "样本不足"


def test_zhongshu_rework_rate_is_lower_is_better():
    plans = [_event(i, f"T{i}", "zhongshu", "state", i, to="Menxia") for i in range(4)]
    bounces = [_event(10 + i, f"T{i}", "menxia", "state", 10 + i,
                      **{"from": "Menxia", "to": "Zhongshu"}) for i in range(2)]
    view = _view(events=plans + bounces)
    assert view["metrics"]["zhongshu"]["rework_rate"] == 0.5
    assert _role(view, "zhongshu")["badge"] == "需要训练"
    clean = _view(events=plans)
    assert clean["metrics"]["zhongshu"]["rework_rate"] == 0.0
    assert _role(clean, "zhongshu")["badge"] == "能臣"


def test_menxia_counts_verdicts_and_review_latency():
    events = [
        _event(1, "T1", "zhongshu", "state", 0, to="Menxia"),
        _event(2, "T1", "menxia", "review_decision", 10, decision="accept"),
        _event(3, "T2", "zhongshu", "state", 20, to="Menxia"),
        _event(4, "T2", "menxia", "review_decision", 25, decision="reject"),
        _event(5, "T2", "menxia", "state", 26, **{"from": "Menxia", "to": "Zhongshu"}),
        _event(6, "T2", "zhongshu", "state", 30, to="Menxia"),
        _event(7, "T2", "menxia", "review_decision", 33, decision="accept"),
    ]
    view = _view(events=events)
    metrics = view["metrics"]["menxia"]
    assert (metrics["reviews"], metrics["approvals"], metrics["rejections"]) == (3, 2, 1)
    assert metrics["rejection_rate"] == 0.333
    assert metrics["avg_review_seconds"] == 6.0
    assert _role(view, "menxia")["badge"] == "待观察"


def test_recent_window_counts_done_and_failed_runs():
    now = T0 + timedelta(days=30)

    def at(days_ago):
        return (now - timedelta(days=days_ago)).isoformat()

    events = [
        {"seq": 1, "task_id": "T1", "at": at(1), "actor": "menxia",
         "action": "confirm", "detail": {"to": "Done"}},
        {"seq": 2, "task_id": "T2", "at": at(2), "actor": "menxia",
         "action": "confirm", "detail": {"to": "Done"}},
        {"seq": 3, "task_id": "T3", "at": at(10), "actor": "menxia",
         "action": "confirm", "detail": {"to": "Done"}},
    ]
    runs = [{"status": "failed", "started_at": at(3), "ended_at": at(3)}]
    view = merit_view({"tasks": [_task("T1", "Done"), _task("T2", "Done"),
                                 _task("T3", "Done")],
                       "events": events, "runs": runs}, now=now)
    assert view["recent"] == {"days": 7, "done": 2, "failed_runs": 1}


def test_api_merit_requires_token_and_returns_projection(tmp_path):
    api = httpapi.API(Store(tmp_path))
    token = secrets.token_urlsafe(32)
    api.add_token("alice", token)
    headers = {"Authorization": "Bearer " + token}
    status, payload = api.handle("GET", "/api/merit", headers, None)
    assert status == 200
    assert {item["role"] for item in payload["roles"]} == {
        "taizi", "zhongshu", "menxia", "shangshu", "gongbu"}
    assert api.handle("GET", "/api/merit", {}, None)[0] == 401
