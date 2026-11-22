# Fusion 使用手册

三省六部制度 + Paperclip 的"能真干活"能力融合后的本地编排系统。
一句话：**用自然语言下任务，太子分拣 → 中书规划 → 门下审（可封驳）→ 尚书派发 → 工部真执行**，
全过程有预算、租约、审批与审计。

---

## Worker 模式的必需参数

Web 提交只会入队；处理队列需要启动 worker，并预先配置所绑定通道的凭据：

```sh
python -m fusion.server --home /srv/fusion/instance --worker --allow-unpriced
```

`--home` 指定实例，`--worker` 启动可信消费者。目前 Pipeline 的执行报价为 unknown，
没有已安装签批处预算策略时，必须显式传 `--allow-unpriced` 才能通过未定价准入。
这不是零成本或无限预算授权；已签名安装的预算策略优先，命令行参数不能覆盖其拒绝。
凭据只配置在服务进程侧，不写进请求、提示词或产物。

未启用未定价用量且策略阻止时，worker 启动警告：
`worker running with unpriced usage disabled: queued runs will fail`。
run 详情 `/api/runs/<id>` 和 `/api/ai-runs/<id>` 的 `error` 包含
`error_type`、安全分类 `message`、`at`，不包含原始异常文本、路径或堆栈。
连续三次同类异常记录一条 `worker failing repeatedly: <类型>`，每组三次一条，
成功处理会重置计数。失败队列项不会自动重放，检查配置与既有执行记录后再决定是否重新提交。

工部的能力及 runtime 时间约定见 [EXECUTOR-CAPABILITIES.md](EXECUTOR-CAPABILITIES.md)。

## 1. 安装

```bash
git clone <repo> && cd edict
bash fusion/install.sh
```

脚本会：
- 检查 Python ≥3.10 与支持 Ed25519 的 openssl
- 创建实例目录 `~/.fusion`
- 生成 board 密钥对（`board-private.pem` / `board-public.pem`）
- 安装启动器 `~/.local/bin/fusion`
- 自检

> **私钥必须移到你自己的设备**（例如 Mac），主机只保留公钥。
> 这样"批准"这件事无法被主机上的任何程序伪造。

自定义位置：

```bash
FUSION_HOME=/srv/fusion PREFIX=/usr/local/bin bash fusion/install.sh
```

## 2. 跑第一个任务（命令行）

```bash
export PENGUIN_GPT_API_KEY=<你的 key>
fusion run "在本地生成一份 hello.md，用中文说明这是什么项目" \
  --home ~/.fusion/instance --allow-unpriced
```

你会看到逐步输出：

```
[太子] 判定为任务
[中书] 计划 1 个子任务
[门下] approve
[尚书] 派发 1 个任务
[工部] 产出 hello.md
```

退出码：`0` 成功；`2` 门下最终封驳；`3` 参数/策略错误。

需要 AI 读输入文件时（例如让它依据某份文档写作），显式声明来源：

```bash
fusion run "根据给定的源文档写一份一页说明" --home ~/.fusion/instance --allow-unpriced \
  --input-root ~/docs --input ~/docs/a.md --input ~/docs/b.md
```

约束：最多 8 份、每份 ≤64KB、合计 ≤256KB，且必须落在 `--home`、当前目录或
`--input-root` 指定的目录内；被拒绝的路径会打印原因，不会静默跳过。

### 2b. 收尾：签批处签章后再完结

`fusion run` 走到 `Review` 会停下并打印 `awaiting_board`，把审批请求写到
`<run>/board-request.json`。这是设计如此——**高风险完结必须有人签名**。

1. 取回请求，在**你自己的设备**上用私钥签名（私钥永不上主机）：

   ```bash
   scp 主机:<run>/board-request.json .
   # 先在主机上生成待签文档：
   #   python3 -m fusion... （见 fusion/board-sign.py 的用法说明）
   python3 fusion/board-sign.py --document approval-document.json \
     --private-key ~/board_ed25519.pem > signed-envelope.json
   ```

2. 回传签名：

   ```bash
   fusion approve --request  board-request.json \
                  --envelope signed-envelope.json \
                  --home ~/.fusion/instance \
                  --board-key-file ~/.fusion/board-public.pem
   ```

   导入签名后停在 `PendingConfirm`，命令会告诉你等谁确认。

3. 由指定第二方单独执行确认：

   ```bash
   fusion confirm --home ~/.fusion/instance \
                  --board-key-file ~/.fusion/board-public.pem \
                  --task <task_id> --run-id <operator-session>
   ```

**绑定字段由冻结的请求提供，调用者不需要（也不能）自己填**，这样不会出现
"批准了一份、执行了另一份"。

## 3. 启动 Web 界面

```bash
fusion --home ~/.fusion/instance snapshot         # 初始化实例
python3 -m fusion.server --home ~/.fusion/instance --port 8787
```

打开 <http://127.0.0.1:8787>。

### 建第一个人类账号

```bash
python3 - <<'PY'
from pathlib import Path
from fusion.users import UserStore
u = UserStore(Path.home()/".fusion/instance/auth")
u.add_user("alice", "换成一个长密码", "admin")
PY
```

登录后：`/api/me` 会返回你的名字与角色。

| 角色 | 能做什么 |
|---|---|
| `admin` | 全部（含建用户、发机器 token） |
| `operator` | 读 + 提交任务 |
| `viewer` | 只读 |

## 4. 调用 API

### 机器调用（推荐给脚本）

```bash
python3 - <<'PY'
from pathlib import Path
from fusion.httpapi import API
from fusion.core import Store
api = API(Store(Path.home()/".fusion/instance"))
api.add_token("ci-bot", "<32..512 位高熵字符串>")
PY
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8787/api/tasks
```

### 人类会话

```bash
curl -s -X POST http://127.0.0.1:8787/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"..."}'
curl -H "Authorization: Bearer <session>" http://127.0.0.1:8787/api/me
```

### 端点一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 探活（公开） |
| POST | `/api/login` / `/api/logout` | 登录 / 登出 |
| GET | `/api/me` | 当前身份 |
| GET | `/api/tasks` | 任务列表 |
| GET | `/api/ai-runs` | 三省决策时间线列表 |
| GET | `/api/ai-runs/<id>` | 单次运行完整决策链 |

**每次请求都会写审计**（`instance/api-audit.jsonl`），含谁、做了什么、状态、时间。

## 5. 安全模型（请务必读）

| 边界 | 状态 |
|---|---|
| 模型只提议、确定性代码裁决 | ✅ |
| board 私钥离机 + Ed25519 验签 | ✅ |
| API token 哈希存储（不存明文） | ✅ |
| 密码 scrypt 哈希 + 会话 TTL | ✅ |
| 子进程环境白名单（密钥不外泄） | ✅ |
| 同 UID 隔离 | ❌ **不提供** |
| 公网暴露 | ❌ **不建议** |

**同 UID 的人仍可直接改实例数据库、替换代码。** 需要更强隔离请用容器，见 `fusion/ISOLATION.md`。

## 6. 已知限制

- 默认只绑 `127.0.0.1`，不要直接暴露到公网
- 模型调用需要 `PENGUIN_GPT_API_KEY`；未定价用量记 `unknown`，**不记 0**
- 单机设计：多机并发、跨机迁移未验证
- 真实会议/业务数据验证尚未进行（只在合成夹具上验证过）

## 7. 卸载

```bash
bash fusion/uninstall.sh          # 会先确认
bash fusion/uninstall.sh --yes    # 无人值守
```

实例目录与启动器会被删除；仓库目录不动。

## 8. 排错

| 现象 | 处理 |
|---|---|
| `openssl` 不支持 Ed25519 | macOS 自带 LibreSSL 常见，装 Homebrew openssl |
| `fusion: command not found` | 把 `~/.local/bin` 加进 `PATH` |
| API 一直 401 | token 是否 ≥32 位？是否 `Bearer ` 前缀？ |
| 登录后 403 | 该角色无此操作权限（viewer 只能读） |
| `spawn E2BIG`（Hermes 通道） | 适配器用 argv 传 prompt，历史过长会超 128KB |

## 9. 卡住任务巡检（`fusion stuck`）

流水线中断后不会自动重放，先巡检再决定，不要直接改数据库。

```bash
fusion stuck --home ~/.fusion/instance                 # 只读，默认闲置阈值 2 小时
fusion stuck --home ~/.fusion/instance --json          # 机器可读
fusion stuck --home ~/.fusion/instance --hours 8       # 自定义阈值
fusion stuck --home ~/.fusion/instance --apply-block   # 仅标记 Assigned/Doing → Blocked
```

| kind | 含义 | 建议处置 |
|---|---|---|
| `awaiting_board` | Review / PendingConfirm，等人工批准的高危转换 | 签批处离线签名后 `fusion approve`，再由指定方 `fusion confirm` |
| `stalled_menxia` | 中书院已交计划、门下无决议（断点） | 退回中书院重拟，或人工撤销 |
| `stalled_assigned` | 门下已放行但未派发（断点） | `--apply-block` 标记 Blocked |
| `stalled_doing` | 已派发但无回报（worker 中断） | `--apply-block` 标记 Blocked（走 gongbu 的 `block` 命令） |
| `failed_run` | 执行失败且停在 Doing | 失败 run 会**自动**把任务标 Blocked；也可 `--apply-block` 手动处理 |
| `blocked` | 已 Blocked，等人工决定 | 人工判断 |
| `pipeline_head` | 停在 Taizi / Zhongshu / Pending | 人工决定重投或取消 |

约束：默认只读；`--apply-block` 只做状态机本就允许、且权限表已授予的 `Assigned/Next/Doing → Blocked`（状态属主身份、
经 `apply_mutation` 落审计；Doing 用 gongbu 的 `block` 命令）。执行失败（run status=failed）会自动 block，
不再把任务留在 Doing；失败详情在 run 行与 `events` 里。**不会替人签名、不会绕过审批门、不会自动重投**——重投要花钱，
工具只打印对应的 `fusion run ... --allow-unpriced` 命令，由人决定。

### 定时巡检（systemd user timer）

仓库模板：`fusion/ops/fusion-stuck-check.{service,timer}`；本机已安装为 `fusion-stuck-check.timer`，
每日 **09:00** 跑一次（`Persistent=true`，关机错过会开机补跑，主机 02:00 关机的时段不影响）。

```bash
cp fusion/ops/fusion-stuck-check.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fusion-stuck-check.timer
systemctl --user start fusion-stuck-check.service          # 立即跑一次
systemctl --user list-timers | grep fusion-stuck           # 看下次时间
```

- **只读**：不 block、不重投、不签名，只报告；
- 结果原子写到 `~/boss-lab/fusion-stuck-status.json`（时间、阈值、非终态数、stuck 数、逐条明细）；
- 有 stuck → 退出码 **2**，journal 打 `WARN:` + 明细，`systemctl --user list-units --failed` 可见；
- 无 stuck → 退出 **0**；审计本身失败 → 退出 **1** 并写 error JSON；
- 可用 `FUSION_HOME` / `FUSION_STUCK_STATUS` / `FUSION_STUCK_HOURS` 覆盖默认值。



## 10. 功过簿（只读）

工作台「点将台」页面新增「功过簿」面板；数据全部来自既有账本（tasks / events / runs），
**不写库、不调模型、不改任何状态**。同一数据也可走 API：

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8793/api/merit
```

按角色统计：

| 角色 | 指标 |
|---|---|
| 太子 | 分拣案数 |
| 中书 | 计划数、被门下跌回数、返工率 = 退回 / 计划 |
| 门下 | 审议数（准 / 驳）、驳率、平均审议耗时（中书交稿 → 门下裁决） |
| 尚书 | 派发数、确认数、平均派发耗时（门下放行 → 派发） |
| 工部 | 执行数（成 / 败）、成功率、平均耗时、失败后 Blocked 次数 |

判词阈值（写进返回的 `reason`，可手工复算）：

- 工部：样本 ≥ 3 且成功率 ≥ 90% → `能臣`；成功率 < 70% → `需要训练`；其余 `待观察`；
- 中书：样本 ≥ 3 且封驳退回率 ≤ 20% → `能臣`；≥ 50% → `需要训练`；其余 `待观察`；
- 太子 / 门下 / 尚书：没有单一质量口径，只给 `待观察` 或 `样本不足`，**不硬评**；
- 任何角色样本 < 3 → `样本不足`，不参与评比（避免小样本噪声）。

返回还含 `overall`（全司任务数 / 办结率）与 `recent`（近 7 日完成数、失败 run 数）。
