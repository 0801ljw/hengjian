#!/usr/bin/env bash
# Fusion 端到端验收：一条命令跑完 ACCEPTANCE.md 里所有可自动化的项。
# 用法: bash fusion/verify_all.sh [--quick]
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
QUICK=0; [ "${1:-}" = "--quick" ] && QUICK=1
PASS=0; FAIL=0; SKIP=0
ok()   { printf '  PASS %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL %s\n' "$*"; FAIL=$((FAIL+1)); }
skip() { printf '  SKIP %s\n' "$*"; SKIP=$((SKIP+1)); }
sec()  { printf '\n== %s ==\n' "$*"; }

PYTHONPATH="$REPO:$REPO/scripts" python3 - "$@" <<'PY'
import json, subprocess, sys, tempfile, threading, urllib.error, urllib.request
from pathlib import Path

QUICK = "--quick" in sys.argv
PASS = FAIL = SKIP = 0

def ok(m):
    global PASS; PASS += 1; print(f"  PASS {m}")
def bad(m):
    global FAIL; FAIL += 1; print(f"  FAIL {m}")
def skip(m):
    global SKIP; SKIP += 1; print(f"  SKIP {m}")
def sec(m):
    print(f"\n== {m} ==")

repo = Path.cwd()
tmp = Path(tempfile.mkdtemp())
priv, pub = tmp/"p.pem", tmp/"u.pem"
subprocess.run(["openssl","genpkey","-algorithm","ed25519","-out",str(priv)],capture_output=True,check=True)
subprocess.run(["openssl","pkey","-in",str(priv),"-pubout","-out",str(pub)],capture_output=True,check=True)

from fusion.server import serve
from fusion.core import Store, PolicyError
from fusion.users import UserStore
from fusion import webui

httpd = serve(tmp/"db", port=0, board_key=pub)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_port}"
TOKEN = "a"*8 + "b7f3c9d1e2a4"*3 + "f0e1d2c3b4a5"
httpd.api.add_token("verify", TOKEN)

def call(path, token=None, method="GET", body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base+path, data=data, method=method)
    if token: req.add_header("Authorization", f"Bearer {token}")
    if data: req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            payload = r.read()
            return r.status, (payload if raw else json.loads(payload or b"{}"))
    except urllib.error.HTTPError as e:
        return e.code, e.read()

# ── A ────────────────────────────────────────────────────────────────────
sec("A 安装与卸载")
inst = tmp/"home"
env = dict(PYTHONPATH=str(repo)+":"+str(repo/"scripts"),
           FUSION_HOME=str(inst), PREFIX=str(inst/"bin"))
r = subprocess.run(["bash","fusion/install.sh","--no-key"], capture_output=True, text=True, env={**__import__("os").environ, **env})
ok("install.sh 在空目录跑通") if r.returncode == 0 else bad(f"install.sh 失败: {r.stderr.strip()[:120]}")
launched = subprocess.run([str(inst/"bin/fusion"),"--help"], capture_output=True, cwd="/tmp")
ok("launcher 从任意目录可用") if launched.returncode == 0 else bad("launcher 不可用")
r2 = subprocess.run(["bash","fusion/uninstall.sh","--yes"], capture_output=True, text=True, env={**__import__("os").environ, **env})
ok("uninstall.sh 清理干净") if r2.returncode == 0 and not inst.exists() else bad("uninstall 未清理")

# ── E2 ───────────────────────────────────────────────────────────────────
sec("E2 全量测试")
if QUICK:
    skip("全量测试（--quick）")
else:
    cmd = (["uv","tool","run","--offline","--from","pytest","pytest","tests/","fusion/tests/","-q"]
           if subprocess.run(["which","uv"],capture_output=True).returncode == 0
           else ["python3","-m","pytest","tests/","fusion/tests/","-q"])
    r = subprocess.run(cmd, capture_output=True, text=True, env={**__import__("os").environ, "PYTHONPATH": str(repo)+":"+str(repo/"scripts")})
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    ok(f"测试全绿：{tail}") if r.returncode == 0 and "passed" in tail else bad(f"测试失败：{tail}")

# ── B ────────────────────────────────────────────────────────────────────
sec("B Web UI")
s, body = call("/", raw=True)
ok("首页返回 HTML") if s == 200 and b"<html" in body.lower() else bad(f"首页 HTTP {s}")
for asset in ("/app.js", "/style.css"):
    st, _ = call(asset, raw=True)
    ok(f"{asset} 可加载") if st == 200 else bad(f"{asset} HTTP {st}")
html = call("/", raw=True)[1].decode("utf-8", "ignore")
# 首次启动是初始化向导，工作台要等初始化完成后才出现。
first_run = "ob-single" in html and "ob-multi" in html
if first_run:
    ok("首次启动进入初始化向导")
    st, ob = call("/api/onboarding")
    ok("/api/onboarding 可读") if st == 200 and "agents" in ob else bad(f"onboarding 状态 HTTP {st}")
    ok("首屏两个选项都在") if ("ob-single" in html and "ob-multi" in html) else bad("缺选项卡片")
    ok("含说明板块") if ("三省六部" in html or "长板" in html) else bad("缺系统说明板块")
    choice = call("/api/onboarding/single", method="POST",
                  body={"agent": "codex"})
    ok("单 agent 模式可提交") if choice[0] in (200, 201) else bad(f"单 agent 提交 HTTP {choice[0]}")
    html2 = call("/", raw=True)[1].decode("utf-8", "ignore")
    ok("初始化后进入工作台") if 'id="username"' in html2 and "ob-single" not in html2 \
        else bad("初始化后仍返回向导")
else:
    ok("页面含登录控件") if 'id="username"' in html and 'id="password"' in html else bad("缺少登录控件")
view = {"id": "r1", "steps": [
    {"actor": "taizi", "status": "valid", "decision": {"verdict": "task"}},
    {"actor": "zhongshu", "status": "valid", "decision": {"verdict": "plan"}},
    {"actor": "menxia", "status": "valid", "decision": {"verdict": "reject"},
     "reasons": ["unsafe"], "required_changes": ["drop upload"]},
    {"actor": "menxia", "status": "valid", "decision": {"verdict": "approve"}},
    {"actor": "gongbu", "status": "valid", "decision": {}}],
 "artifacts": [{"path": "handoff.md", "bytes": 100, "sha256": "0"*64}]}
try:
    tl = webui.render_timeline(view)
    blob = json.dumps(tl, ensure_ascii=False)
    ok(f"时间线渲染 {len(tl)} 步") if tl and "reject" in blob else bad("时间线未包含封驳")
except Exception as exc:
    bad(f"时间线渲染异常: {exc}")
try:
    arts = webui.render_artifacts(view, {})
    ok(f"产物渲染 {len(arts)} 条") if arts else bad("产物未渲染")
except Exception as exc:
    bad(f"产物渲染异常: {exc}")
js = (repo/"fusion/web/app.js").read_text(encoding="utf-8")
ok("UI 三态文案齐全") if ("暂无" in js and ("失败" in js or "错误" in js or "重试" in js)) else bad("UI 缺三态文案")

# ── C ────────────────────────────────────────────────────────────────────
sec("C 鉴权、审计与身份")
ok("无 token → 401") if call("/api/tasks")[0] == 401 else bad("无 token 未 401")
ok("机器 token → 200") if call("/api/tasks", TOKEN)[0] == 200 else bad("token 未 200")
raw = (httpd.store.home/"tokens.json").read_text()
ok("token 文件无明文") if TOKEN not in raw else bad("token 明文泄漏")
ok("审计落盘") if (httpd.store.home/"api-audit.jsonl").exists() else bad("无审计文件")

httpd.users.add_user("alice", "alice-password-1", "admin")
httpd.users.add_user("bob", "bob-password-12", "viewer")
ok("错误密码 401") if call("/api/login", method="POST", body={"username":"alice","password":"x"})[0] == 401 else bad("错误密码未拒")
st, o = call("/api/login", method="POST", body={"username":"alice","password":"alice-password-1"})
ok("正确登录 200") if st == 200 and o.get("role") == "admin" else bad("登录失败")
sess = o.get("session")
ok("会话可读任务") if call("/api/tasks", sess)[0] == 200 else bad("会话读失败")
ok("会话可读决策链") if call("/api/ai-runs", sess)[0] == 200 else bad("会话读时间线失败")
st_b, ob = call("/api/login", method="POST", body={"username":"bob","password":"bob-password-12"})
bob = ob.get("session")
ok("viewer 只读（写 403）") if call("/api/requests", bob, "POST", {"request":"x"})[0] == 403 else bad("viewer 未被限制")
ok("登出后 401") if call("/api/logout", sess, "POST")[0] == 200 and call("/api/tasks", sess)[0] == 401 else bad("登出未失效")
ok("密码不落明文") if "alice-password-1" not in httpd.users.path.read_text() else bad("密码明文")
import fusion.runtime as rt
env2 = rt.child_env(tmp/"ws", allow="")
import os
os.environ["PENGUIN_GPT_API_KEY"] = "sk-leak-check"
env3 = rt.child_env(tmp/"ws2", allow="")
ok("子进程不继承密钥") if "PENGUIN_GPT_API_KEY" not in env3 else bad("密钥泄漏到子进程")

# ── D ────────────────────────────────────────────────────────────────────
sec("D 取消路径与迁移")
from fusion.policy import GRANTS, granted
ok("窄授权仅两条") if GRANTS == {"gongbu": {"cancel"}, "zhongshu": {"confirm"}} else bad(f"授权范围异常: {GRANTS}")
ok("gongbu 无通用 state") if not granted("gongbu", "state") else bad("gongbu 获得 state")
tests = ["fusion/tests/test_cancel_paths.py", "fusion/tests/test_migrate_legacy.py"]
for t in tests:
    if not (repo/t).exists():
        bad(f"缺少 {t}"); continue
    cmd = (["uv","tool","run","--offline","--from","pytest","pytest",t,"-q"]
           if subprocess.run(["which","uv"],capture_output=True).returncode == 0
           else ["python3","-m","pytest",t,"-q"])
    r = subprocess.run(cmd, capture_output=True, text=True,
                       env={**__import__("os").environ, "PYTHONPATH": str(repo)+":"+str(repo/"scripts")})
    ok(f"{Path(t).name} 通过") if r.returncode == 0 else bad(f"{Path(t).name} 失败")

# ── F ────────────────────────────────────────────────────────────────────
sec("F 端到端：界面提交 → worker 消费 → 真实产物")

# 这一段验证最容易漏掉的路径：请求从 API 进来、worker 从队列取走、五角色走完、
# 产物落盘。provider 调用本身由 test_agent_channels 的假服务覆盖，这里换成可预测的
# 假决策器，验收不花钱也不依赖网络。
import fusion.ai as ai_mod
import fusion.server as server_mod

class _FakeDecision:
    """按各角色 schema 返回合法决策，专门用来验证流水线而不是模型。"""
    def __init__(self, *a, **kw):
        self._steps = []

    def summary(self):
        return {"calls": len(self._steps), "steps": list(self._steps),
                "total_cost_usd": None, "pricing": "unknown",
                "tokens": [], "total_tokens": None}
    def decide(self, step, context):
        decisions = {
            "taizi": {"kind": "task", "title": "写验收文件",
                      "requirement": "在 workspace 下写 accept.md", "reason": "具体工作"},
            "zhongshu": {"goal": "写一个验收文件",
                         "subtasks": [{"id": "accept", "assignee": "gongbu",
                                       "instruction": "在 workspace 下写 accept.md",
                                       "depends_on": [],
                                       "acceptance": ["文件存在且内容匹配"]}]},
            "menxia": {"verdict": "approve", "reasons": ["清晰可验收"], "required_changes": []},
            "shangshu": {"order": ["accept"], "reason": "无依赖"},
            "gongbu": {"files": [{"path": "workspace/accept.md",
                                  "content": "worker end-to-end ok\n"}],
                       "report": "已提供文件内容"},
        }
        return decisions[step]

e2e_home = tmp / "e2e"
_real_decision = ai_mod.DecisionClient
ai_mod.DecisionClient = _FakeDecision
e2e_httpd = None
try:
    e2e_httpd = server_mod.serve(e2e_home, port=0)
    e2e_httpd.api.add_token("e2e", TOKEN)
    threading.Thread(target=e2e_httpd.serve_forever, daemon=True).start()
    threading.Thread(target=server_mod.worker_loop, args=(e2e_httpd,),
                     kwargs={"interval": 0.05, "allow_unpriced": True}, daemon=True).start()
    e2e_base = f"http://127.0.0.1:{e2e_httpd.server_port}"

    def e2e_call(path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(e2e_base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {TOKEN}")
        if data:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")

    status, submitted = e2e_call("/api/requests", "POST",
                                 {"request": "在 workspace 下写一个 accept.md"})
    ok("界面提交返回 queued") if status == 200 and submitted.get("status") == "queued" \
        else bad(f"提交异常: {status} {submitted}")
    run_id = submitted.get("id")

    run = {}
    for _ in range(300):
        _, run = e2e_call(f"/api/runs/{run_id}")
        if run.get("artifacts") or run.get("status") in ("error", "failed", "awaiting_board", "done"):
            break
        threading.Event().wait(0.1)

    ok("worker 消费了队列") if run.get("status") != "queued" else bad("任务一直排队，worker 没有消费")
    if run.get("status") == "error":
        bad(f"端到端失败: {json.dumps(run.get('error'), ensure_ascii=False)[:160]}")
    arts = run.get("artifacts") or []
    ok("run 产出了产物") if arts else bad(f"无产物，status={run.get('status')}")
    if arts:
        first = Path(arts[0]["path"])
        ok("产物 manifest 落盘") if first.exists() else bad(f"产物路径不存在: {first}")
        if first.exists():
            manifest = json.loads(first.read_text())
            files = manifest.get("files") or []
            ok("manifest 记录了产出文件") if files else bad("manifest 无 files")
            if files:
                produced = Path(files[0]["path"])
                sample = produced.read_text() if produced.exists() else ""
                ok("产出内容正确") if "worker end-to-end ok" in sample \
                    else bad(f"产出内容不符: {produced}")
                import hashlib as _h
                ok("产物 SHA-256 与 manifest 一致") \
                    if produced.exists() and _h.sha256(produced.read_bytes()).hexdigest() == files[0].get("sha256") \
                    else bad("产物哈希不一致")
    ok("端到端状态为 awaiting_board 或 done") \
        if run.get("status") in ("awaiting_board", "done") else bad(f"端到端状态异常: {run.get('status')}")
finally:
    ai_mod.DecisionClient = _real_decision
    if e2e_httpd is not None:
        try:
            e2e_httpd.shutdown()
        except Exception:
            pass

# ── E3 ───────────────────────────────────────────────────────────────────
sec("E3 文档与发布门")
for doc in ("fusion/README.md", "fusion/USER-GUIDE.md", "fusion/ISOLATION.md",
            "fusion/DESIGN.md", "fusion/RELEASE-1.0.md"):
    ok(f"{doc} 存在") if (repo/doc).exists() else bad(f"缺少 {doc}")

# 1.0 发布门：版本单一来源 + 页面版本不漂移 + 会话安全测试在位
version_file = (repo / "VERSION").read_text(encoding="utf-8").strip()
ok("VERSION 是 1.x") if version_file.startswith("1.") else bad(f"VERSION 不是 1.x：{version_file}")
page = (repo / "fusion/web/index.html").read_text(encoding="utf-8")
ok(f"页脚版本与 VERSION 一致（v{version_file}）") if f"v{version_file}" in page else bad(
    f"页脚版本与 VERSION 不一致（VERSION={version_file}）")
cookie_test = repo / "fusion/tests/test_session_cookie.py"
ok("会话 cookie 测试存在") if cookie_test.exists() else bad("缺少 fusion/tests/test_session_cookie.py")

# ── E4 ───────────────────────────────────────────────────────────────────
sec("E4 备份可恢复性")
drill_dir = Path(tempfile.mkdtemp(prefix="fusion-drill-"))
drill_output = drill_dir / "drill"
try:
    drill = subprocess.run([sys.executable, "-m", "fusion.restore_drill",
                            "--output", str(drill_output)],
                           capture_output=True, text=True, timeout=180, cwd=repo)
    result_file = drill_output / "result.json"
    if drill.returncode == 0 and result_file.exists():
        report = json.loads(result_file.read_text(encoding="utf-8"))
        if report.get("status") == "passed":
            ok("恢复演练通过（导出→删源→导入→逐项比对一致）")
        else:
            bad("恢复演练未通过：status=%s" % report.get("status"))
        if report.get("source_deleted_before_import"):
            ok("drill 记录了源删除步骤")
        else:
            bad("drill 未记录源删除步骤")
    else:
        bad("恢复演练执行失败（rc=%s）" % drill.returncode)
finally:
    import shutil as _shutil
    _shutil.rmtree(drill_dir, ignore_errors=True)

print(f"\n== 汇总 ==\n  PASS {PASS} / FAIL {FAIL} / SKIP {SKIP}")
sys.exit(0 if FAIL == 0 else 1)
PY
