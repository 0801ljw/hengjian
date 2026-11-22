"""Offline acceptance for pure projections and dependency-free browser code."""
import copy
import json
import re
from pathlib import Path
import shutil
import subprocess
import urllib.request

import pytest

from fusion.ai_view import run_view
from fusion.httpapi import API
from fusion.tests.test_ai_view import populate
from fusion.tests.test_http_server import http  # noqa: F401 - existing loopback fixture
from fusion.webui import render_artifacts, render_ledger, render_run, render_tasks

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "fusion" / "web"


def test_reject_revise_approve_and_purity(tmp_path, monkeypatch):
    from fusion.core import Store
    store = Store(tmp_path)
    client = populate(store)
    snapshot, records = store.snapshot(), client.summary()["steps"]
    before = copy.deepcopy((snapshot, records))

    def forbidden(*args, **kwargs):
        raise AssertionError("view must not access files")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", forbidden)
        view = render_ledger(snapshot, "AI-test", records)
        tasks = render_tasks(snapshot)
    assert (snapshot, records) == before
    assert [s["actor"] for s in view["timeline"]] == [
        "taizi", "zhongshu", "menxia", "zhongshu", "menxia", "shangshu", "gongbu"]
    reviews = [s for s in view["timeline"] if s["review_attempt"]]
    assert [s["review_attempt"] for s in reviews] == [1, 2]
    assert [s["verdict"] for s in reviews] == ["reject", "approve"]
    assert reviews[0]["reasons"] == ["unsafe"]
    assert reviews[0]["required_changes"]
    assert reviews[1]["passed_after_revision"]
    assert reviews[1]["plan_revision"] == 2
    assert view["artifacts"][0]["name"] == "manifest.json"
    assert view["status"] == "awaiting_board"
    view["timeline"][0]["decision"]["reason"] = "mutated"
    tasks[0]["runs"][0]["artifact"]["path"] = "mutated"
    assert (snapshot, records) == before


def test_ledger_fallback_without_invented_changes(tmp_path):
    from fusion.core import Store
    store = Store(tmp_path)
    populate(store)
    view = render_ledger(store.snapshot(), "AI-test")
    reviews = [s for s in view["timeline"] if s["review_attempt"]]
    assert [s["verdict"] for s in reviews] == ["reject", "approve"]
    assert reviews[0]["reasons"] == ["unsafe"]
    assert reviews[0]["required_changes"] == []
    assert reviews[1]["passed_after_revision"]


def test_model_retries_are_not_review_rounds():
    steps = [
        {"actor": "menxia", "status": "invalid", "attempt": 1, "decision": {"verdict": "reject"}},
        {"actor": "menxia", "status": "valid", "attempt": 2, "decision": {"verdict": "reject"}},
        {"actor": "menxia", "status": "transport_error", "attempt": 1},
        {"actor": "menxia", "status": "valid", "attempt": 2, "decision": {"verdict": "approve"}},
    ]
    timeline = render_run({"steps": steps})["timeline"]
    assert [s["review_attempt"] for s in timeline] == [None, 1, None, 2]
    assert timeline[-1]["passed_after_revision"]


@pytest.mark.parametrize("url", ["https://evil.test/a", "//evil.test/a", "javascript:alert(1)",
                                 "/\\evil.test/a", "/\nevil.test/a", "file:///etc/passwd", "relative"])
def test_unsafe_download_links(url):
    items = render_artifacts({"artifacts": [{"path": "/private/report.md"}]},
                             {"/private/report.md": url})
    assert items[0]["download_url"] is None


def test_downloads_need_explicit_adapter_mapping():
    source = {"artifacts": [{"path": "/private/report.md", "sha256": "abcd", "bytes": 12,
                             "download_url": "https://evil.test"}]}
    assert render_artifacts(source)[0]["download_url"] is None
    item = render_artifacts(source, {"/private/report.md": "/api/artifacts/report"})[0]
    assert item == {"name": "report.md", "path": "/private/report.md", "sha256": "abcd",
                    "bytes": 12, "download_url": "/api/artifacts/report"}


def test_empty_projection():
    snapshot = {"tasks": [], "runs": [], "events": []}
    assert render_tasks(snapshot) == []
    view = render_ledger(snapshot, "missing")
    assert view["timeline"] == view["artifacts"] == []
    assert view["status"] == "incomplete"


def test_static_resources_and_safe_sinks():
    html, js, css = [(WEB / name).read_text() for name in ("index.html", "app.js", "style.css")]
    for content in (html, js, css):
        assert "cdn." not in content and "https://" not in content
    assert 'src="app.js"' in html and 'href="style.css"' in html
    # 空态文案的措辞属于设计层，可随笔风改变；但每个状态位必须存在且有实际文字，
    # 不能退化成空壳。断言契约（元素在、有内容），不断言某一句话。
    assert 'role="status"' in html
    for element in ("list-status", "detail-status", "submit-status", "artifact-status"):
        match = re.search(rf'id="{element}"[^>]*>([^<]+)<', html)
        assert match and len(match.group(1).strip()) >= 4, f"empty state {element} has no text"
    assert "加载中" in js and "加载错误" in js
    for sink in ("innerHTML", "document.write", "localStorage", "sessionStorage", "eval("):
        assert sink not in js
    assert "textContent =" in js
    assert "Content-Security-Policy" in html


@pytest.fixture
def node():
    binary = shutil.which("node")
    if not binary:
        pytest.skip("node unavailable: JavaScript behavior not checked")
    return binary


def test_js_python_projection_parity(node, tmp_path):
    from fusion.core import Store
    store = Store(tmp_path)
    client = populate(store)
    view = run_view(store.snapshot(), client.summary()["steps"], "AI-test")
    script = r"""
const {renderRun, safeDownloadURL} = require('./fusion/web/app.js');
const fs = require('node:fs');
const assert = require('node:assert/strict');
for (const url of ['javascript:x', '//evil/x', '/\\evil/x', '/\x01x']) assert.equal(safeDownloadURL(url), null);
assert.equal(safeDownloadURL('/api/artifacts/a'), '/api/artifacts/a');
const raw = JSON.parse(fs.readFileSync(0, 'utf8'));
const before = JSON.stringify(raw);
const view = renderRun(raw);
assert.equal(JSON.stringify(raw), before);
console.log(JSON.stringify(view));
"""
    result = subprocess.run([node, "-e", script], cwd=ROOT, input=json.dumps(view),
                            text=True, capture_output=True, check=True)
    actual = json.loads(result.stdout)
    expected = render_run(view)
    for left, right in zip(actual["timeline"], expected["timeline"]):
        for key in ("actor", "label", "review_attempt", "verdict", "revised", "passed_after_revision"):
            assert left[key] == right[key]


def test_controller_submit_empty_error_and_text(node):
    # DOM double tests the actual controller, not browser layout/rendering.
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.handlers = {}; this.dataset = {}; this.value = ''; this._text = ''; this.classList = {toggle(){}}; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(event, handler) { this.handlers[event] = handler; }
  setAttribute(key, value) { this[key] = value; }
  querySelectorAll(tag) { return this.children.flatMap(c => [...(c.tag === tag ? [c] : []), ...c.querySelectorAll(tag)]); }
}
const elements = new Map();
global.document = {
  documentElement: new Element('html'),
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); },
  createElement(tag) { return new Element(tag); }
};
const $ = id => document.getElementById(id);
let requests = [], failure = false, taskExists = false, detailed = false, runError = false, holdDetail = false, releaseDetail;
const title = '<img src=x onerror=alert(1)>';
global.fetch = async (url, options) => {
  requests.push({url, options});
  if (failure) return {ok: false, status: 401};
  let data;
  if (url === '/api/requests') {
    assert.deepEqual(JSON.parse(options.body), {request: 'write report'});
    taskExists = true; data = {id: 'AI-x', status: 'queued'};
  } else if (url === '/api/tasks') data = {tasks: taskExists ? [{id: 'AI-x', title}] : []};
  else if (url === '/api/ai-runs') data = {runs: []};
  else if (url === '/api/runs/AI-x') {
    if (holdDetail) await new Promise(resolve => { releaseDetail = resolve; });
    data = {id: 'AI-x', title, status: 'queued', steps: [], artifacts: []};
    if (detailed) {
      data.steps = [
        {actor: 'menxia', status: 'valid', decision: {verdict: 'reject'}, reasons: ['缺少验收项'], required_changes: ['补充验收']},
        {actor: 'zhongshu', status: 'valid', plan_revision: 2, decision: {plan: '修订计划'}},
        {actor: 'menxia', status: 'valid', plan_revision: 2, decision: {verdict: 'approve'}, reasons: ['已补全']}
      ];
      data.artifacts = [{path: '/private/report.md', bytes: 12, sha256: 'abc'}];
    }
    if (runError) {
      data.status = 'error'; data.steps = []; data.artifacts = [];
      data.error = {error_type: 'PolicyError', message: 'Execution refused <img src=x>', at: '2026-11-08T00:20:00+08:00'};
    }
  } else throw new Error('unexpected endpoint ' + url);
  assert.equal(options.headers.Authorization, 'Bearer test-only');
  return {ok: true, json: async () => data};
};
vm.runInThisContext(fs.readFileSync('fusion/web/app.js', 'utf8'));
(async () => {
  await $('refresh').handlers.click();
  assert.match($('list-status').textContent, /Token/);
  assert.equal(requests.length, 0);
  $('token').value = 'test-only';
  await $('refresh').handlers.click();
  assert.match($('list-status').textContent, /暂无任务/);
  $('request').value = '   ';
  await $('request-form').handlers.submit({preventDefault(){}});
  assert.match($('submit-status').textContent, /1–12000/);
  $('request').value = 'write report';
  await $('request-form').handlers.submit({preventDefault(){}});
  assert.match($('submit-status').textContent, /已入队/);
  assert.match($('detail-status').textContent, /暂无决策记录/);
  const button = $('tasks').children[0].children[0];
  assert.ok(button.textContent.includes(title));
  assert.ok(button.children.length > 0);  // card layout: chip + id + title
  assert.equal($('submit').disabled, false);
  detailed = true;
  await button.handlers.click();
  const timeline = $('timeline').children;
  assert.match(timeline[0].textContent, /第 1 次审议 · 封驳/);
  assert.ok(timeline[0].children.some(c => c.textContent.includes('缺少验收项')));
  assert.ok(timeline[0].children.some(c => c.textContent.includes('补充验收')));
  assert.match(timeline[2].textContent, /第 2 次审议 · 通过 · 修订后通过/);
  assert.equal($('artifacts').children.length, 1);
  assert.equal($('artifacts').querySelectorAll('a').length, 0);
  assert.equal($('export').disabled, false);
  runError = true;
  await button.handlers.click();
  assert.match($('detail-status').textContent, /PolicyError · Execution refused <img src=x>/);
  assert.match($('detail-status').textContent, /2026-11-08T00:20:00/);
  assert.ok(!$('detail-status').textContent.includes('可能仍在排队'));
  failure = true;
  await $('refresh').handlers.click();
  assert.match($('list-status').textContent, /Token 无效/);
  assert.equal($('timeline').children.length, 0);
  assert.equal($('export').disabled, true);
  failure = false; holdDetail = true;
  const old = button.handlers.click();
  await Promise.resolve();
  $('company').value = 'default';
  $('company').handlers.change();
  releaseDetail(); await old;
  assert.equal($('token').value, '');
  assert.match($('detail-status').textContent, /暂无数据/);
  assert.equal($('run-summary').textContent, '');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)


def test_api_accepts_exact_ui_submit_body(tmp_path):
    from fusion.core import Store
    api = API(Store(tmp_path))
    token = "test-offline-only-" + "x" * 32
    api.add_token("test", token)
    headers = {"Authorization": "Bearer " + token}
    status, queued = api.handle("POST", "/api/requests", headers, {"request": "write report"})
    assert status == 200 and queued["status"] == "queued"
    status, view = api.handle("GET", queued["run_url"], headers, b"")
    assert status == 200 and view["status"] == "queued"
    assert render_run(view)["timeline"] == []


def test_real_http_static_mount_and_queued_roundtrip(http):
    server, base = http
    for path, mime in [("/", "text/html"), ("/app.js", "application/javascript"),
                       ("/style.css", "text/css")]:
        with urllib.request.urlopen(base + path, timeout=5) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == mime
            assert response.read()
    token = "test-static-http-" + "x" * 32
    server.api.add_token("test-webui", token)
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    request = urllib.request.Request(base + "/api/requests", headers=headers,
                                     data=json.dumps({"request": "offline UI test"}).encode())
    with urllib.request.urlopen(request, timeout=5) as response:
        queued = json.load(response)
    request = urllib.request.Request(base + queued["run_url"], headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        assert json.load(response)["status"] == "queued"
