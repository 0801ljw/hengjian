# 衡鉴 · 点将台 — 操作执行手册（RUNBOOK）

> 适用版本：v1.0.0（Linux 准绳实例 + DSH 插件 + macOS 桌面 App 原型）
> 更新：2026-11-21
>
> 本手册面向“操作者”，不面向开发者。命令中的 `<TOKEN>`、`<ADMIN_USER>`、`<ADMIN_PASSWORD>` 均为占位符；不要把真实 token / 密码写进本文件、shell 历史或聊天记录。

---

## 1. 当前架构

```text
DSH 面板/工具 ──> /dutang 代理 ──> 127.0.0.1:8894（Mac SSH 隧道）
                                      │
                                      ▼
                              Linux fusion-server :8793
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
              SQLite 账本        Issue 队列        sync journal
              /api/tasks         /api/issues       /api/sync/*
                    │
              worker + 2 个 issue worker
                    │
              codex/claude/pi/hermes/...（通过绑定）

macOS 桌面 App ──> 本地随机端口 fusion-server ──> 同一套 Web UI
                └─ discovery.json（0600，端口 + token）
```

关键点：

- **Linux 是准绳实例**；现有 DSH 插件默认指向它（隧道 8894 → 8793）。
- **桌面 App 是另一套本地实例**，数据默认独立；尚未与 Linux 迁移合并。
- 机器 token 能读案卷/Issue/Agent 清单，不能代替人评审/签批。
- 评审/签批必须使用真实 human session。

---

## 2. 关键路径

### Linux（准绳）

| 对象 | 路径 |
|---|---|
| 代码 | `/home/ljw/boss-lab/edict` |
| 实例 | `/home/ljw/boss-lab/fusion-instance` |
| SQLite | `fusion-instance/fusion.sqlite3` |
| 机器 token | `fusion-instance/tokens.json`（0600） |
| 用户/会话 | `fusion-instance/auth/` |
| 角色绑定 | `fusion-instance/roles.json`（0600） |
| Issue 策略 | `fusion-instance/issue-policy.json`（0600，可选） |
| 模型价格 | `fusion-instance/pricing.json`（0600，可选） |
| Agent 工作区 | `fusion-instance/agents/<agent>/workspace/`（0700） |
| Agent 运行目录 | `fusion-instance/agents/<agent>/runs/<issue-id>-<rand>/` |
| Agent 生命周期 | `fusion-instance/agents/<agent>/lifecycle.json`（opt-in，0600） |
| 旧 Issue 产物 | `fusion-instance/issue-runs/`（历史） |
| 同步日志 | `fusion-instance/sync-journal.sqlite3` |
| 审批公钥 | `/home/ljw/.config/fusion/board_ed25519.pub.pem` |
| 服务单元 | `~/.config/systemd/user/fusion-server.service` |

### macOS

| 对象 | 路径 |
|---|---|
| 桌面 App | `衡鉴 · 点将台项目/desktop-app/build/Hengjian.app` |
| App 服务数据 | `~/Library/Application Support/Hengjian/instance/` |
| App discovery | `.../instance/discovery.json`（0600） |
| App 日志 | `.../Hengjian/service.log`、`app.log` |
| SSH 隧道 | `~/Library/LaunchAgents/com.ljw.fusion-linux-tunnel.plist` |
| DSH profile | `~/.dsh/profiles/desktop/cordis.patch.yml`（含 token，勿外传） |

---

## 3. 启停与健康检查

### 3.1 Linux 服务

```bash
ssh ljw-pc

# 状态
systemctl --user status fusion-server.service
systemctl --user is-active fusion-server.service

# 重启（改代码/策略后）
systemctl --user restart fusion-server.service

# 停止 / 启动
systemctl --user stop fusion-server.service
systemctl --user start fusion-server.service

# 实时日志
journalctl --user -u fusion-server.service -f -n 100
```

服务参数当前为：

```text
--worker --allow-unpriced --sync --issue-worker --issue-workers 2
```

### 3.2 健康检查

```bash
# Linux 本机
curl -s http://127.0.0.1:8793/api/health

# Mac 经隧道
curl -s http://127.0.0.1:8894/api/health
```

期望：

```json
{"ok": true, "service": "fusion"}
```

### 3.3 Mac 隧道

```bash
launchctl list | grep com.ljw.fusion-linux-tunnel
launchctl kickstart -k "gui/$(id -u)/com.ljw.fusion-linux-tunnel"
curl -s http://127.0.0.1:8894/api/health
```

隧道不通时，先确认 Linux 8793 正常，再检查 SSH/LaunchAgent。

### 3.4 桌面 App

```bash
cd "衡鉴 · 点将台项目/desktop-app"
./build.sh
open build/Hengjian.app
```

- 首次启动会进入初始化向导。
- 退出 App 会终止它自己启动的本地服务。
- **重建后先退出/杀掉旧实例再 open**；macOS `open` 可能复用旧进程，导致新 runtime 没生效。验证 `app.log` 里的 pid/port 是否变化。
- App 优先启动内置 `Contents/Resources/bin/fusion-server`；系统 Python 只作为开发回退。
- 当前分发包含自包含 sidecar，目标机不需要 Python；首次打开需 Gatekeeper 放行。
- 分发包：`desktop-app/dist/Hengjian-macOS-arm64.dmg` 或 `.zip`；挂载/解压后双击 App。

---

## 4. DSH 插件

### 4.1 当前模式

`~/.dsh/profiles/desktop/cordis.patch.yml` 中 `dutang` 配置：

```yaml
upstream: http://127.0.0.1:8894
autoAuth: true
token: "<TOKEN>"   # 不要打印或提交
```

修改后需要在 DSH 插件页重载该插件。

### 4.2 discovery 模式（桌面 App）

插件 0.5.5 源码支持：

```yaml
upstream: auto
```

它会读取 `~/Library/Application Support/Hengjian/instance/discovery.json`，自动获得端口和 token。

注意：**当前 DSH 进程仍加载 0.5.4**（HMR 报 `transactions cannot be nested`），要重启 DSH 后才会加载新工具参数。当前 profile 仍指向 Linux 实例，切 `auto` 前要先决定主数据源。

### 4.3 工具

当前可用：

- `dutang_list_cases` / `dutang_get_case`
- `dutang_submit_case`
- `dutang_merit`
- `dutang_list_artifacts` / `dutang_read_artifact`
- `dutang_list_agents`（0.5.3+）
- `dutang_list_issues` / `dutang_create_issue` / `dutang_comment_issue`（0.5.4+）

评审/签批没有模型工具，必须人类在面板/API 会话中操作。

---

## 5. 日常操作

### 5.1 呈交一条普通案卷

在 DSH 面板「呈交新案」或调用 `dutang_submit_case`。这是五席流水线任务，不是 Issue 队列。

### 5.2 建一条 Issue

优先用 DSH 工具：

```text
dutang_create_issue
  title: "生成周报"
  assignee: "gongbu"
  body: "写 weekly.md"
  reviewer: "menxia"
  approver: "<人类用户名>"
  acceptance: ["weekly.md 存在"]
```

或用 API（机器 token）：

```bash
curl -sS http://127.0.0.1:8894/api/issues \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "生成周报",
    "assignee": "gongbu",
    "body": "写 weekly.md",
    "reviewer": "menxia",
    "approver": "<人类用户名>",
    "acceptance": ["weekly.md 存在"],
    "checks": [
      {"type": "file_exists", "path": "weekly.md"}
    ],
    "max_tokens": 50000,
    "max_cost_usd": 0.05
  }'
```

### 5.3 查看队列

```bash
curl -sS "http://127.0.0.1:8894/api/issues?state=review" \
  -H "Authorization: Bearer <TOKEN>"
```

看板：Web UI「牌票」区域；Inbox 显示待你评审/签批的事项。

### 5.4 评审与签批（必须人类会话）

先登录：

```bash
curl -sS -c /tmp/fusion.cookies \
  -H "Content-Type: application/json" \
  -d '{"username":"<ADMIN_USER>","password":"<ADMIN_PASSWORD>"}' \
  http://127.0.0.1:8894/api/login
```

评审通过/驳回：

```bash
curl -sS -b /tmp/fusion.cookies \
  -H "Content-Type: application/json" \
  -d '{"verdict":"approve","comment":"checks 已过"}' \
  http://127.0.0.1:8894/api/issues/<ISSUE_ID>/review

curl -sS -b /tmp/fusion.cookies \
  -H "Content-Type: application/json" \
  -d '{"verdict":"reject","comment":"缺少验收文件"}' \
  http://127.0.0.1:8894/api/issues/<ISSUE_ID>/review
```

签批：

```bash
curl -sS -b /tmp/fusion.cookies \
  -H "Content-Type: application/json" -d '{}' \
  http://127.0.0.1:8894/api/issues/<ISSUE_ID>/approve
```

规则：

- 有 reviewer 时，必须先 `review` 通过，`approve` 才有效。
- reviewer/approver 名字必须与登录用户名一致；admin 可代审/代签。
- 机器 token 调 review/approve 会返回 403。

### 5.5 评论 / 阻塞 / 取消

```bash
# 评论
curl -sS http://127.0.0.1:8894/api/issues/<ID>/comment \
  -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
  -d '{"body":"补充一条说明"}'

# 阻塞
curl -sS http://127.0.0.1:8894/api/issues/<ID>/block \
  -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
  -d '{"blocker_id":"<BLOCKER_ID>"}'

# 取消
curl -sS http://127.0.0.1:8894/api/issues/<ID>/cancel \
  -H "Authorization: Bearer <TOKEN>" -H "Content-Type: application/json" \
  -d '{"reason":"不再需要"}'
```

---

## 6. 预算、策略与价格

### 6.1 四层预算

1. **单 Issue**：`max_tokens` / `max_calls`
2. **整棵委派树**：根 Issue 的 `max_tokens`；claim 时预留
3. **月度 token**：公司/角色
4. **成本**：单 Issue / 角色 / 公司月度

### 6.2 策略文件 / 设置页

```bash
cat ~/boss-lab/fusion-instance/issue-policy.json
```

设置抽屉「Issue 预算策略」可编辑，需 admin 会话。API：

```bash
curl -sS -b /tmp/fusion.cookies http://127.0.0.1:8894/api/issue-policy

curl -sS -b /tmp/fusion.cookies -X PUT \
  -H "Content-Type: application/json" \
  -d '{"max_tokens_per_issue":50000,"max_calls_per_issue":4,
       "monthly_tokens":2000000,
       "monthly_cost_usd":20,
       "role_max_tokens":{"gongbu":20000},
       "monthly_tokens_per_role":{"gongbu":500000}}' \
  http://127.0.0.1:8894/api/issue-policy
```

### 6.3 模型价格

只有配置了价格，成本估算才不是 `unpriced`：

```bash
cat ~/boss-lab/fusion-instance/pricing.json
```

设置抽屉「模型价格」或 API：

```bash
curl -sS -b /tmp/fusion.cookies -X PUT \
  -H "Content-Type: application/json" \
  -d '{"models":{"gpt-6.1-sol":{"input_per_million_usd":1,"output_per_million_usd":2}}}' \
  http://127.0.0.1:8894/api/pricing
```

注意：

- 设了 `max_cost_usd` 但模型未配置价格 → **执行前 blocked**。
- 本月已有 unpriced usage 且设了月度成本上限 → 新 claim fail-closed。
- 价格单位是 USD / 1M tokens。

### 6.4 常见“预算 blocked”

查看 Issue 详情 `result.error`：

- `root token budget exhausted`
- `monthly role budget exhausted`
- `monthly company budget exhausted`
- `monthly role cost budget exhausted`
- `cost cap requires configured pricing`
- `token budget exceeded`
- `cost budget exceeded`

处理顺序：补价格 → 调策略 → 拆小 Issue/重新建 Issue；不要直接改 DB。

---

## 7. Agent 清单与绑定

### 7.1 只读清单

```bash
curl -sS http://127.0.0.1:8894/api/agents/inventory \
  -H "Authorization: Bearer <TOKEN>"
```

DSH 工具：`dutang_list_agents`（默认只列已发现/已绑定）。

### 7.2 绑定角色

Web UI → 设置 →「角色绑定」：

- 应用预设
- 选 agent/model
- 保存自定义绑定

当前工部禁止绑定带工具的 CLI agent；API 通道（codex/hermes/pi/claude）不受影响。

### 7.3 受管 daemon 生命周期（opt-in）

CLI Agent 仍按 Issue 执行，不提供 start/stop。只有显式配置了生命周期命令的 Agent 才可受管：

```bash
# 需要 admin 会话；start/stop 必须绝对路径、直接 argv、无 shell
curl -sS -b /tmp/fusion.cookies -X PUT \
  -H "Content-Type: application/json" \
  -d '{"start":["/absolute/path/to/agent","serve"],"stop":["/absolute/path/to/agent","stop"]}' \
  http://127.0.0.1:8894/api/agents/<agent>/lifecycle

curl -sS -b /tmp/fusion.cookies \
  http://127.0.0.1:8894/api/agents/<agent>/lifecycle

curl -sS -b /tmp/fusion.cookies -X POST \
  http://127.0.0.1:8894/api/agents/<agent>/lifecycle/start
curl -sS -b /tmp/fusion.cookies -X POST \
  http://127.0.0.1:8894/api/agents/<agent>/lifecycle/stop
curl -sS -b /tmp/fusion.cookies -X POST \
  http://127.0.0.1:8894/api/agents/<agent>/lifecycle/restart
```

日志：`agents/<agent>/lifecycle.log`；pid：`lifecycle.pid`。未配置时 API 返回 400 `no lifecycle configured`。

自动重启：`{"restart":"always|on-failure","max_restarts":3,"restart_backoff_seconds":30,"health":["/abs/path/healthcheck"]}`；服务端每 15s 检查一次；人工 `stop` 置 paused，不会被自动拉起；`on-failure` 正常退出不重启。

重启耗尽/健康失败会写入 `alerts`，设置页「Agent 告警」可查看；Issue 预算耗尽、重试耗尽也会写入同一 Inbox；确认需要 human session。

UI：设置 → 本机 Agent →「查看」→ 生命周期；需要 admin 会话。

### 7.3 安装/更新 Agent

设置页「智能体接入」/ onboarding 页面；需要 admin 会话。安装有审计日志。

---

### 7.4 成员管理

设置 →「成员管理」（仅 admin）：

- 列出成员与角色
- 新增成员（默认 operator，可选 viewer/admin）
- 移除成员；不能移除自己或最后一个 admin
- 新成员在同一个登录页用自己的密码登录

API：

```bash
curl -sS -b /tmp/fusion.cookies http://127.0.0.1:8894/api/members
curl -sS -b /tmp/fusion.cookies -X POST \
  -H "Content-Type: application/json" \
  -d '{"username":"ljw","password":"<PASSWORD>","role":"operator"}' \
  http://127.0.0.1:8894/api/members
curl -sS -b /tmp/fusion.cookies -X DELETE \
  http://127.0.0.1:8894/api/members/<name>
```

自助注册仍不开放；第一个账号后，新增成员是 admin 操作。


### 7.5 公开注册

live 服务已启用：

```text
Environment=DUTANG_PUBLIC_REGISTRATION=1
```

行为：

- 第一个账号仍是 admin
- 之后任何能访问服务的人都可以在 `/login` 的「注册」页创建账号
- 新账号角色固定为 `operator`（可用 `DUTANG_REGISTRATION_ROLE=viewer` 改只读）
- 公开注册永远不会创建 admin
- IP 限流：每 5 分钟最多 5 次
- 用户总数上限 1000
- `DUTANG_DISABLE_REGISTRATION=1` 可随时关闭

安全提醒：

- 公开注册只适合你信任访问范围的环境；当前服务绑定 127.0.0.1，用户通过隧道/桌面 App 访问
- 若要暴露到互联网，必须在反向代理层加 TLS、限流、审计，并考虑邮箱验证/邀请
- `DUTANG_ALLOW_REMOTE_REGISTRATION=1` 才会接受非回环来源，默认不开

## 8. 同步（M0/M1）

### 8.1 只读投影（实例副本）

```bash
python3 -m fusion sync-project \
  --home /tmp/copy-instance \
  --journal /tmp/copy-instance/sync-journal.sqlite3
```

### 8.2 live 增量

服务已带 `--sync`，每 2 秒投影。

```bash
curl -sS "http://127.0.0.1:8894/api/sync/snapshot?workspace=w_local" \
  -H "Authorization: Bearer <TOKEN>"

curl -sS "http://127.0.0.1:8894/api/sync/events?after=0&limit=50" \
  -H "Authorization: Bearer <TOKEN>"
```

目前只有本地 journal；**没有 Hub/E2E/手机同步**。

---

## 9. 备份与恢复

### 9.1 在线备份

```bash
BACKUP=~/backups/fusion-$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP"

# 本机没有 sqlite3 CLI，用 Python 标准库在线备份
python3 - "$HOME/boss-lab/fusion-instance/fusion.sqlite3" "$BACKUP/fusion.sqlite3" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
    source.backup(target)
PY

cp -a ~/boss-lab/fusion-instance/auth "$BACKUP/auth"
cp -a ~/boss-lab/fusion-instance/tokens.json "$BACKUP/tokens.json"
cp -a ~/boss-lab/fusion-instance/roles.json "$BACKUP/roles.json"
cp -a ~/boss-lab/fusion-instance/issue-policy.json "$BACKUP/" 2>/dev/null || true
cp -a ~/boss-lab/fusion-instance/pricing.json "$BACKUP/" 2>/dev/null || true
cp -a ~/boss-lab/fusion-instance/sync-journal.sqlite3 "$BACKUP/" 2>/dev/null || true
cp -a ~/boss-lab/fusion-instance/agents "$BACKUP/agents" 2>/dev/null || true
cp -a ~/boss-lab/fusion-instance/issue-runs "$BACKUP/issue-runs" 2>/dev/null || true
chmod -R go-rwx "$BACKUP"
```

### 9.2 恢复

```bash
systemctl --user stop fusion-server.service
# 先备份当前目录，再覆盖
cp "$BACKUP/fusion.sqlite3" ~/boss-lab/fusion-instance/fusion.sqlite3
cp -a "$BACKUP/auth" ~/boss-lab/fusion-instance/
cp "$BACKUP/tokens.json" ~/boss-lab/fusion-instance/tokens.json
cp "$BACKUP/roles.json" ~/boss-lab/fusion-instance/roles.json
systemctl --user start fusion-server.service
```

注意：

- 恢复后确认 `tokens.json` / `auth` 权限为 0600/0700。
- 不要把 `tokens.json`、`auth`、`pricing.json` 提交到仓库。
- 当前没有自动迁移；跨版本恢复前先看 `LINUX-CANONICAL.md`。

---

## 10. 故障处理

| 现象 | 检查 | 处理 |
|---|---|---|
| DSH 面板 401 | token/隧道 | 检查 8894 健康；重载插件；不要打印 token |
| `/api/issues` 403 | 用了机器 token 调 review/approve | 用 human session 登录 |
| Issue 一直 queued | worker 是否启动；priority/retry_at | `systemctl status`、`journalctl`；看 `attempts`/`retry_at` |
| Issue running 卡住 | lease/worker | 等 recovery loop；查 `journalctl`；必要时取消重建 |
| budget blocked | result.error | 补价格/调策略/拆小任务 |
| 成本 `unpriced` | pricing.json | 配置价格；否则只能记录未知 |
| Agent 不可用 | `/api/agents/inventory` | 安装/登录/绑定；API 通道看 key |
| 桌面 App 卡住 | `app.log` / `service.log` | 确认 Python 3.12；重启 App |
| DSH HMR nested | 插件重载 | 重启 DSH；源码 0.5.5 才会生效 |
| 数据库 locked | 是否有多个服务/长事务 | 停服务、查进程；不要手动删 WAL |

---

## 11. 验证与 CI

```bash
ssh ljw-pc
cd ~/boss-lab/edict
bash fusion/ci.sh
```

期望：

```text
tests_status: PASS
acceptance_status: PASS
tests_passed: 692
acceptance_pass: 50
```

当前 live 冒烟：

- `ISS-6a58231566de49cc`：review，3 条 checks 全过
- 产物 `issue-verified.txt`，2 bytes，SHA256 `565339…12bb3`

### 2026-11-21 手册实测

- Linux 服务/健康/隧道/桌面 App：通过
- DSH `list_cases/list_agents/list_issues`：通过
- Issue 读取/创建（blocked）/取消：通过
- 机器 token 调 review/approve：按预期 403
- `/api/agents/inventory`：200；`/api/agents`、`/api/issue-policy`、`/api/pricing` 无 admin 会话 401
- sync snapshot cursor 347 / events 200
- 在线备份：quick_check ok；tasks 25 / issues 3；权限 700/600
- 非破坏性恢复演练：quick_check ok，计数一致
- Agent workspace：`agents/codex/workspace` 0700；inventory 返回 workspace/last_issue
- Agent 运行历史：`GET /api/agents/codex/runs` 200；设置页 Agent 行「查看」可打开 run 抽屉
- 新 run 真实链路：`ISS-d601f354baf6470b` → done，checks 2/2；`agents/codex/runs` 出现记录
- 失败链路：`ISS-54a9ea83ffe148d9` → blocked（sha256 失败），Inbox 收到带 ref 的 `issue_retry_exhausted`（旧代码；新代码会写 `issue_checks_failed`）

---

## 12. 当前明确的限制

- 单机、单用户为主；没有 Owner/Admin/Operator/Viewer 多人类角色。
- Agent 只读清单 + workspace/run 历史已落地；只有显式配置 lifecycle 的 daemon 才能 start/stop/restart，CLI Agent 仍 per-Issue。
- 每个 Agent 现在有稳定 workspace 与 recent-Issue 上下文，但不是完整模型 session/worktree，不能 resume 原会话。
- executor 只能写文件，不能执行命令、联网、调用 MCP/浏览器。
- 没有 watchdog/routines；只有 wake consumer 和 lease recovery。
- 没有移动端/Hub/E2E；sync 仅本地。
- 没有多公司/多租户/团队模板/eval/技能库。
- 桌面 App 仍依赖系统 Python，未签名/未公证、无自动更新。

---

## 13. 值得继续优化 / 借鉴（按优先级）

### P0 — 直接影响“Agent 能否真正干活”

1. **Agent 生命周期**（借 OpenAgents Launcher）
   - start/stop/restart、实时日志、就绪/错误状态、工作目录、后台运行。
2. **持久会话/工作区**（借 Paperclip adapters）
   - 每个 Agent 保留 session/workdir，支持 resume，而不是每 Issue 从零开始。
3. **工具与权限**（借 Paperclip Tool Gateway + OpenAgents shared browser）
   - MCP/命令/浏览器；Allowed/Ask first/Off；per-agent secret；沙箱。
4. **人类多角色 + Inbox/通知**（借两家）
   - Owner/Admin/Operator/Viewer；未读、审批、告警通知。
5. **Watchdog + Routines**（借 Paperclip heartbeat）
   - stall/loop 检测、自动重启、定时/事件触发；当前只有手动 wake。

### P1 — 组织与可运营性

6. **Org Chart / Manager / 任意角色**（Paperclip）
   - 五席降为默认模板，支持 manager 委派和汇报线。
7. **Project/Goal 层级**（Paperclip）
   - Issue 挂 project/goal，而不是只有 parent。
8. **Skills/评测**（Paperclip Skills Studio）
   - 角色 prompt 版本化、技能库、eval、失败样本。
9. **可观测性/成本面板**（两家）
   - 实时活动流、run timeline、本月成本、阈值告警。
10. **分发/安装**（OpenAgents Launcher / Paperclip npx）
    - 自包含运行时、签名、自动更新、一键安装。

### P2 — 多端与生态

11. **Hub/E2E/手机**（OpenAgents + Khala Sync 语义）
12. **Agent Marketplace / SDK / 团队模板**
13. **多公司/多租户**（仅当产品转向团队）

### 不建议现在做

- 完整 Paperclip 公司/四类人类角色/多租户。
- OpenAgents 的 PostgreSQL+Redis+S3 复杂度。
- 早期开放 Agent Marketplace 供应链。
- 在单机编排未稳定前做移动端。

---

## 14. 交接检查清单

- [ ] `systemctl --user is-active fusion-server.service` 为 active
- [ ] Linux `/api/health` 200
- [ ] Mac 8894 `/api/health` 200
- [ ] DSH 插件能列案卷/Issue
- [ ] `/api/agents/inventory` 能读到已绑定 Agent
- [ ] 最近一条 Issue 的 checks/审批状态符合预期
- [ ] `bash fusion/ci.sh` PASS
- [ ] 备份目录权限 0700、文件 0600
- [ ] 没有把 token/密码写进文档或聊天
