# Fusion P2-0：Ed25519 离线审批与可信本地 mutation 控制器

P3-1 真实 AI 决策、封驳与可复现证据见 [P3-1-VALIDATION.md](P3-1-VALIDATION.md)。
P3-2 公司迁移/真实删除恢复/逻辑隔离见 [DESIGN.md](DESIGN.md) 的 P3-2 节。
正式模块入口 `python3 -m fusion run --help`；模型仅提建议，既有审批/预算/lease 不变。

上游：https://github.com/cft0808/edict，MIT / cft0808；保留原 LICENSE，不复制或修改 Paperclip 实现。

先读 DESIGN.md 的 P2-0 节；历史验证见 VALIDATION.md / P1-VALIDATION.md，本次结论见 P2-VALIDATION.md。
旧 CLI 写命令已移除（exit 2 / LEGACY_MUTATION_DISABLED），原版和 Fusion dashboard 的 HTTP POST 一律 405。
JSON 只是可重建投影，不接收导入，不是事实源。上游模块函数仅保留离线算法回归；不能修改 Fusion SQLite 状态。

```bash
cd /home/ljw/boss-lab/edict
PYTHONPATH=$PWD uv tool run --offline --from pytest pytest tests/ fusion/tests/ -q
python3 -m fusion.control --help
python3 -m fusion.control --home fusion/var snapshot
python3 -m fusion.dashboard --home fusion/var --port 7891
```

迁移与多公司（不需要安装包或调用模型）：

```bash
export PATH="$PWD/scripts:$PATH"
fusion snapshot --home /path/to/instance --company alpha
fusion export --home /path/to/instance --company alpha --out /path/to/company.tar.gz
fusion import --bundle /path/to/company.tar.gz --home /path/to/new-empty-instance \
  --company alpha --board-key-file /trusted/board-public.pem
python3 -m fusion.restore_drill --output /path/to/new-drill-evidence
```

也可用 `python3 -m fusion` 代替 `fusion`。省略 company 为兼容旧布局的 `default`。
导入整个目标实例必须为空；不复制密钥和外部工件；活跃执行必须先按既有规则完结/回收。
这是可信本地控制器的公司数据隔离，不是多用户身份认证或同 UID 进程沙箱。

## P4-1：三省 AI 正式 run 入口

从仓库根目录运行（Python 3.11+）：

```bash
python3 -m fusion run "把这份会议记录整理成规范：会议决定每周五复盘" \
  --home ./instance --company alpha --allow-unpriced
python3 -m fusion run --request "生成本地交接文档" --home ./instance --json
python3 -m fusion run --help
```

请求必须为 1..12000 字符；位置参数与 `--request` 二选一。`--home` 必填，
`--company` 默认 `default`，沿用既有公司隔离路径。每次运行使用唯一任务 ID 和审计目录，
不复用旧 demo 的调用额度。非聊天请求会实际调用已配置模型；上述命令不是免费演练，
执行前需取得对应调用授权。不会自动选择服务商、采购或修改配置。

`--allow-unpriced` 仅显式允许未定价执行，不能绕过已签预算上限或 lease。
未传时仍由既有预算准入拒绝未定价工部执行（此前的三省模型调用仍可能发生）。
所有模型仅返回提议，实际写入仍经原有 Pipeline/apply_mutation、预算准入和 fencing lease。
成功产出停在 `Review` / `awaiting_board`，只生成 `board-request.json`，不签名、不代替签批处关闭。

普通模式逐步打印到 stdout 并即时 flush；`--json` 将逐步进度写入 stderr，stdout 为单个
JSON 结果，包含 status、exit_code、elapsed_sec、total_tokens、cost_usd、pricing，
成功时还包含 workspace、files、artifact 和 board_request。未知 token/成本在 JSON 中为
`null`、普通模式为 `unknown`，不以 0 冒充。审计文件位于选定公司目录的 `ai/<task-id>/`。

以下为本机实际离线假模型运行输出（真实 Pipeline/SQLite/文件产物；不是付费模型能力证明）：

```text
[太子] 请求决策…
[太子] task：concrete file task
[中书] 请求决策…
[中书] 第1版方案：write report.md
[门下] 请求决策…
[门下] 第1次 reject 理由：unsafe；需修改：remove network access
[中书] 请求决策…
[中书] 第2版方案：write report.md
[门下] 请求决策…
[门下] 第2次 approve 理由：bounded and testable
[尚书] 请求决策…
[尚书] 调度提议：write
[工部] 请求决策…
[工部] 文件提议 1 个；等待可信写入
[工部] 产出 /home/ljw/boss-lab/BOS-37-offline-example/companies/alpha/runs/c1169f4d6eeb45cda3bf0cee457d42c0/report.md
结果：awaiting_board；耗时 0.135s
token：105；成本 USD：unknown
产物路径：/home/ljw/boss-lab/BOS-37-offline-example/companies/alpha/runs/c1169f4d6eeb45cda3bf0cee457d42c0
等待签批处独立审批；未自动关闭任务。
审计路径：/home/ljw/boss-lab/BOS-37-offline-example/companies/alpha/ai/AI-7065d2d848b14973aa5f9e7b8ad94759
```

退出码（仅新 `run` 命令；旧 controller 命令语义不变）：

- `0`：成功产出待签批处审批，或识别为聊天而未创建任务。
- `2`：门下最终封驳；最多初稿 + 两次重拟，共三次审核后终止。
- `3`：参数、策略/预算/lease、配置、模型传输或文件错误。

离线复现：`PYTHONPATH=$PWD uv tool run --offline --from pytest pytest fusion/tests/test_ai_run.py -q`。
测试注入固定响应，不接外部服务，覆盖真实链路和逐步输出。

仓库 `scripts/fusion` 与安装启动器现在均经过 `fusion/__main__.py`，支持 `run`、
`agents`、`bind`，原有 controller 参数和退出语义保持不变。

## P6-1：本机探测与角色绑定

以下命令仅本地探测/写配置，不执行模型，不请求外部 API：

```bash
python3 -m fusion agents --instance ./instance
python3 -m fusion agents --instance ./instance --refresh
python3 -m fusion bind --instance ./instance --show
python3 -m fusion bind --instance ./instance --mode single --agent codex:gpt-6.1-sol
python3 -m fusion bind --instance ./instance --auto
python3 -m fusion bind --instance ./instance --auto --set menxia=claude:your-model
python3 -m fusion bind --instance ./instance --mode multi \
  --set taizi=pi:deepseek-flash --set zhongshu=codex:gpt-6.1-sol \
  --set menxia=claude:your-model --set shangshu=codex:gpt-6.1-sol \
  --set gongbu=codex:gpt-6.1-sol
```

`--home` 是 `--instance` 的别名，默认 `./instance`；应与 `fusion run --home` 一致。
`roles.json` 是实例级配置，命名公司也使用该实例根目录的配置。新建 multi 必须列全五角色；
已有配置时 `--set` 仅覆盖指定角色。未知/未安装 agent、空 model、缺角色或不一致的 single
模式返回非零，且不改原配置。配置原子写入，权限 0600，可编辑或再次 bind。

`agents` 同时显示结构化事实：CLI 路径、版本、配置目录、Codex 配置里的 provider 名称和
model、Hermes profile 名称、环境变量是否非空。Pi 只检查目录，不解析内部格式。
API 通道来自 Codex `model_providers`，有配置且对应 `env_key` 非空时，`api` 可参与绑定；
不显示环境变量的值，不读取 auth 文件，不验证登录或模型可用性。
Codex 配置默认 `~/.codex/config.toml`（尊重 `CODEX_HOME`）。

所有版本检查并发执行，每个默认 5 秒，超时杀掉进程组并记 `timeout`；CLI 不存在则
`found=false`。`found=true` 只表示可执行文件存在，版本超时不是能力或不可用结论。
版本/目录/model 事实缓存于 `<instance>/agents-cache.json`，按 mtime 保留 24 小时；
`--refresh` 强制重测。环境变量存在性每次重新读取，坏缓存自动重建。

建议表是**启发式，可用 --set 覆盖**，不是对本机 agent 能力或实时价格的判断。
优先 Pi/classify、Codex/plan-code-review、Claude/review-write、Hermes/plan；
门下尽量使用不同 agent。只检测到 API 时全部建议 api。模型优先采用可读取的配置值；
否则 Pi 建议 `deepseek-flash`、其他建议当前默认 MODEL，并明确标记“非探测事实”。
未检测到任何通道时不会编造绑定。只有 `bind --auto` 才写建议，并逐角色打印理由；
`agents` 不改角色配置。单 agent 全角色会警告，门下/中书同 agent+model 必定提示
「审查独立性弱」。

此阶段绑定中的 `agent` 是来源标识，运行接入的是逐角色 `model`，**不是 CLI 执行后端**。
现有 StreamTransport 仍只使用已配置的 penguinapi，不自动切换 provider、不启动代理工具。
因此设置的 model 必须能由既有通道提供；探测和 bind 成功不代表该 model 可调用。
没有 `roles.json` 时默认五角色仍用原 MODEL，审批、预算、lease、Pipeline 均不变。

只读看板：http://127.0.0.1:7891；/api/fusion/status 展示 tasks、runs、events、decisions。
/api/live-status 请求重新投影，手改 JSON 不能改变响应中的权威状态。
fusion.demo 的历史真实模型演示已停用，启动即拒绝，避免先运行模型再卡在缺失审批的收口步骤。

## 本地写入协议

命令：`python3 -m fusion.control --home /absolute/home mutate request.json`。
请求 JSON 示例（新任务，无模型调用）：

```json
{
  "task_id": "LOCAL-001", "actor": "taizi", "command": "create", "target": "Taizi",
  "reason": "本地合成任务", "payload": {"title": "本地合成任务"},
  "payload_sha256": "先使用 fusion.mutate.payload_hash 对 payload 计算",
  "expected_revision": 0, "run_id": "local-human-controller-session"
}
```

状态请求 command=state / done / confirm，不附 payload；payload_sha256 来自当前任务 payload，
expected_revision 来自当前 snapshot。方案/金额更新 command=payload，target 保持当前状态，
payload 为完整新业务对象，hash 仍校验旧对象。服务推进 revision，旧 approval 因绑定不匹配而失效。
CLI 返回码：0 成功、2 策略/输入拒绝、3 revision/幂等冲突（JSON status=409）。
Store.create/move 仅委托同一服务；显式调用应提交已读 revision、hash、真实 actor/run。

## 独立 board 审批

1. 控制器使用 prepare request.json 固定请求（task_id、actor、target、reason、command、recipient、run_id、expected_revision、payload_sha256），保存返回 JSON 为冻结请求。此步骤不改变任务状态。
2. 签批处在 Mac 上检查完整任务 payload 和冻结请求，在独立人类终端使用下方签名命令，保存输出 envelope。Ed25519 私钥仅留 Mac，绝不拷到主机、仓库或 agent 环境。此命令在 agent runtime 内拒绝运行。
3. 人工拷回 envelope，本地主机控制器使用默认公钥 `/home/ljw/.config/fusion/board_ed25519.pub.pem` 验签入库；可信操作员可用 `--board-key-file PUBLIC.pem` 显式配置公钥。公钥缺失、算法不符或签名错误一律拒绝。HTTP 不提供审批写接口。
4. 对同一冻结请求执行 mutate，添加 approval_id。高风险转换才可使用 approval_id；进入 PendingConfirm 后仍需原 confirm_by 角色独立 confirm，并再次检查有效期、载荷和 revision。menxia 只能生成 review_decision，不能签发 approval。
5. 同 approval_id 的完全相同请求返回第一次结果，无重复事件/副作用；任何字段改变后复用返回 409。返回的是第一次 PendingConfirm 快照，读取当前状态请用 snapshot。

```bash
# 仅在签批处 Mac 的独立人类终端运行（需要代码 checkout）
python3 -m fusion.board --private-key /offline/board-private.pem --request frozen-request.json \
  --actor board:<human-id> --run-id <human-session> --expires-at <timezone-ISO>

# 或只拷 fusion/board-sign.py 到 Mac，对已核对的 document JSON 签名
python3 board-sign.py --document approval-document.json --private-key /offline/board-private.pem

# 人工将输出保存为 envelope.json 并拷回主机后
python3 -m fusion.control --home fusion/var import-approval envelope.json
```

上述尖括号为待替换参数，不可原样复制到 shell。工具需要 Python 3 和支持 Ed25519 `pkeyutl -rawin` 的 OpenSSL；无口令 PEM 才受支持，加密 PEM 暂不支持。本任务未签发真实人类批准。
envelope 显式包含 `alg=ed25519` 和 128 位小写十六进制签名，canonical JSON 不变。旧 HMAC 审批拒绝，未完成操作须人工重新审批；保留旧审计记录，不自动重签。
此版本依赖可信本地控制器与操作员。Ed25519 只消除主机持有真实签名私钥的需要；同 UID 仍可改 SQLite/换代码/换公钥，需宿主隔离。
未声称实现生产人类认证或宿主级多租户安全。后续已交付的 lease/fencing、三省 AI 化、恢复演练分别见对应 P2/P3 验证文档。

## P2-1 预算准入

唯一 mutation 服务现在统一执行公司 + 工部两层整数分预算、UTC 自然月预留/结算、未定价策略和启动孤儿回收。
默认未定价拒绝；策略调整和一次性特批只接受离线 Ed25519 签批处签章，普通任务审批不能绕过预算。
运行与完整配置格式见 [P2-1-VALIDATION.md](P2-1-VALIDATION.md)，边界见 [DESIGN.md](DESIGN.md) 的 P2-1 节。

## 文档导航

| 想了解 | 看这里 |
|---|---|
| 怎么用（安装、初始化、日常操作） | [USER-GUIDE.md](USER-GUIDE.md) |
| 设计取舍与边界 | [DESIGN.md](DESIGN.md) |
| 隔离与安全（同 UID / 容器） | [ISOLATION.md](ISOLATION.md) |
| **1.0 发布门**（哪些必须过、怎么验） | [RELEASE-1.0.md](RELEASE-1.0.md) |
| **与 Paperclip 的差距、P1 排期** | [GAP-MATRIX.md](GAP-MATRIX.md) |
| 能力边界与执行器约束 | [EXECUTOR-CAPABILITIES.md](EXECUTOR-CAPABILITIES.md) |
| CI 与验收怎么跑 | [CI.md](CI.md) |
| 角色设定（衡鉴 · 点将台） | [../docs/衡鉴点将台-底层角色设定.md](../docs/衡鉴点将台-底层角色设定.md) |

历史文档（2026-05 三省六部时期）已移到 [`../docs/archive/`](../docs/archive/)，不再维护。
