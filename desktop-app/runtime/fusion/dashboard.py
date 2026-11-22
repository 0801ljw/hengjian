"""Original dashboard UI over a read-only SQLite projection; no OpenClaw jobs."""
import argparse
import sys
from http.server import HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .core import ROOT, Store
from .ai_view import read_runs

sys.path.insert(0, str(ROOT / "dashboard"))
import server as upstream  # noqa: E402


AI_PANEL = '''
<section id="fusion-ai" style="background:#111827;color:#f3f4f6;padding:24px;font:15px system-ui">
<h2>三省决策</h2><p>只读决策记录 · 产物以账本为准</p>
<div style="display:grid;grid-template-columns:minmax(160px,1fr) 3fr;gap:24px">
<nav id="ai-list" aria-label="AI 运行列表">暂无 AI 决策记录</nav>
<div id="ai-timeline" aria-live="polite">请选择运行</div></div></section>
<script>
(() => {
 const list = document.getElementById('ai-list'), timeline = document.getElementById('ai-timeline');
 const labels = {taizi:'太子',zhongshu:'中书',menxia:'门下',shangshu:'尚书',gongbu:'工部'};
 function text(parent, tag, value) {
   const node = document.createElement(tag); node.textContent = value; parent.appendChild(node); return node;
 }
 async function select(id) {
   try {
     const response = await fetch('/api/ai-runs/' + encodeURIComponent(id));
     if (!response.ok) throw Error();
     const run = await response.json(); timeline.replaceChildren();
     text(timeline,'h3',run.title + ' · ' + run.status);
     for (const step of run.steps) {
       const item = text(timeline,'article',''); item.style.cssText='border-left:2px solid #64748b;padding:8px 16px;margin:12px 0';
       const verdict = step.decision.verdict || step.decision.kind || step.status;
       text(item,'h4',step.step + '. ' + (labels[step.actor] || step.actor) + ' · ' + verdict
          + (step.plan_revision ? ' · 方案第' + step.plan_revision + '版' : ''));
       text(item,'p',step.reasons.join('；'));
       if (step.required_changes.length) text(item,'p','需修改：' + step.required_changes.join('；'));
       text(item,'small',(step.timestamp || '时间未知') + ' · ' + (step.model || '模型未知')
          + ' · token ' + JSON.stringify(step.tokens) + ' · 调用尝试 ' + step.attempt);
       const detail = text(item,'details',''); text(detail,'summary','决策详情');
       text(detail,'pre',JSON.stringify(step.decision,null,2)).style.whiteSpace='pre-wrap';
     }
     text(timeline,'h4','实际产物');
     for (const artifact of run.artifacts) text(timeline,'p',artifact.path);
     if (!run.artifacts.length) text(timeline,'p','暂无落盘产物');
   } catch { timeline.textContent='决策记录暂不可用，请刷新重试'; }
 }
 fetch('/api/ai-runs').then(r => {if (!r.ok) throw Error(); return r.json();}).then(data => {
   if (!data.runs.length) return;
   list.replaceChildren();
   for (const run of data.runs) {
     const button = text(list,'button',run.title + ' · ' + run.status);
     button.style.cssText='display:block;width:100%;margin:8px 0;padding:10px;cursor:pointer';
     button.onclick=() => select(run.id);
   }
   select(data.runs[0].id);
 }).catch(() => {list.textContent='决策记录暂不可用，请刷新重试';});
})();
</script>
'''


def make_server(store: Store, port: int = 7891):
    upstream.DATA = store.home
    upstream._ACTIVE_TASK_DATA_DIR = store.home
    upstream.auth_init(store.home)
    store.project()

    class Handler(upstream.Handler):
        def send_file(self, path, mime='text/html; charset=utf-8'):
            if path == upstream.DIST / 'index.html' and path.is_file():
                body = path.read_text().replace('</body>', AI_PANEL + '</body>').encode()
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().send_file(path, mime)

        def do_POST(self):
            self.send_json({"ok": False, "error": "read-only dashboard; use fusion.control"}, 405)

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/")
            if path == '/api/ai-runs' or path.startswith('/api/ai-runs/'):
                if self._check_auth():
                    return
                runs = read_runs(store)
                if path == '/api/ai-runs':
                    self.send_json({'runs': [{k: v for k, v in run.items()
                                              if k not in {'steps', 'reviews', 'runs', 'ledger_events'}} for run in runs]})
                else:
                    run_id = path[len('/api/ai-runs/'):]
                    result = next((run for run in runs if run['id'] == run_id), None)
                    self.send_json(result or {'error': 'AI run not found'}, 200 if result else 404)
            elif path == "/api/live-status":
                store.project()
                # Never consult upstream's module-global DATA: two dashboards
                # in one process must not switch each other's tenant.
                import json
                self.send_json(json.loads((store.home / "live_status.json").read_text()))
            elif path == "/api/fusion/status":
                self.send_json(store.snapshot())
            elif path.startswith("/api/") and path not in {"/api/live-status", "/api/auth/status"}:
                # Some upstream GET handlers migrate config or inspect OpenClaw.
                self.send_json({"ok": False, "error": "not exposed by P0 read-only dashboard"}, 404)
            else:
                super().do_GET()

    return HTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path("fusion/var"))
    parser.add_argument("--company", default="default")
    parser.add_argument("--port", type=int, default=7891)
    args = parser.parse_args()
    server = make_server(Store(args.home, company=args.company), args.port)
    print(f"Fusion read-only dashboard: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
