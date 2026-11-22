# Fusion P0 架构冻结 v1

## P3-2：公司迁移、真实恢复与租户命名空间（2026-11-06）

本节替代历史章节中“导入导出/恢复演练未实现”的说明；不改变状态机、预算、lease 或审批策略。

### 隔离模型

一个 `--home` 是实例根目录，可信本地控制器用 `--company` 选择作用域：
`default` 保留旧 `<home>/fusion.sqlite3`；其他公司为 `<home>/companies/<company>/fusion.sqlite3`。
每个 Store 在构造时固定 company，所有查询、事务、预算、租约、审批、事件与投影只使用这一数据库。
因此同 task/run/approval ID 不依靠全局唯一性隔离；没有跨公司联表、默认回退或全实例扫描接口。
company 只允许 1–64 个 ASCII 字母数字/下划线/连字符（首字符必须字母数字），
拒绝穿越路径和命名空间/数据库符号链接。默认公司查询不会扫描 `companies/`。
原 `_held` capability 已以数据库路径和进程 ID 分键，无需修改 lease 策略。

`fusion`、`fusion.control`、dashboard、receipt verifier 和 AI demo 的 Store 入口支持 `--company`。
双 dashboard 的状态/投影 GET 直接绑定各自 Store，不能使用上游共享 DATA 全局变量读取另一公司。
AI demo 仍使用独立新回合目录；其外部模型原始 transcript 不是数据库审计，见下方导出边界。
离线签名工具不选择数据库，company 不作为新的签名字段，原签名协议保持不变。

### 导出边界和格式

`fusion export --home INSTANCE --company alpha --out /outside/company.tar.gz`：
SQLite 只读事务取一致逻辑快照，不执行 startup recovery，不修改源状态。
manifest 声明 `fusion-company`、schema_version=1、company、数据库结构 SHA256、
state.json SHA256 以及逐表记录数。12 张表全部保留：tasks、events、runs、mutation_requests、
decisions、approval_uses、budget_policy、budget_reservations、budget_events、budget_overrides、
leases、execution_commits。包括原始签名、已消费审批/特批、已结算/孤儿费用和 fencing 历史。

不导出：其他公司、SQLite WAL/SHM、临时文件、进程能力与 heartbeat、密钥、可重建 JSON 投影、
数据库以外的工件/模型 transcript/任意文件。数据库中的 artifact 路径/哈希作为历史记录原样保留，
不代表对应文件已经迁移；需要工件归档时必须另行复制并验 hash。私钥永不进入迁移包。
存在 owner 非空 lease、running run 或 reserved 预算即拒绝导出；不静默丢弃或改写活跃状态。
先正常完结或按既有 dead-owner recovery 规则回收，再重试。目标包必须在公司目录之外，防止覆盖状态源。
正式迁移切换应先停源控制器，导出后不再让旧实例继续写；导出本身只是时间点备份，不持有永久停写锁。

### 导入与失败语义

`fusion import --bundle FILE --home NEW_EMPTY_INSTANCE --company alpha --board-key-file TRUSTED_PUBLIC.pem`。
整个目标实例必须不存在或为空，不能合并进已有多公司实例；不支持公司重命名。
仅读取两个固定普通 tar 成员，不 extract 路径，不加载备份 SQL/数据库 schema；
拒绝重复成员、链接、设备、未知路径、重复 JSON key/非有限数字，单成员上限 128 MiB。
在目标的同级私有临时目录用当前代码创建空 schema，验证版本、schema 哈希、state 哈希、表名/列、
记录数、完整插入结果、foreign_key_check / integrity_check 和静止条件。
再验证归档 approval/预算策略/历史策略/override 的 Ed25519 签名、冻结 request 绑定、
审批消费与 PendingConfirm/confirm 的引用，以及任务事件 revision/状态链、run/预算账本一致性。
公钥从可信操作员配置取得，绝不相信 bundle 自带的 key。
旧审批过期不妨碍归档恢复，但没有续期；实际 mutation/confirm 仍执行原有效期与完整绑定检查。

所有检查和投影生成完成后 checkpoint，再以同文件系统目录 rename 一次发布；此前失败只清理暂存目录，
不留下半个目标。拒绝非空目标时不改其中任何文件。源与目标应在操作员独占的本地目录下操作，
不允许并发初始化同一个 import 目标或让不可信同 UID 进程替换路径。

### 真实演练与剩余风险

`python3 -m fusion.restore_drill --output /absolute/new-evidence-dir`（也支持直接执行脚本）：
独立真实 seed 进程创建 3 个任务/已结算预算/审批消费/已释放 lease/执行提交；export 子进程导出；
驱动程序真实 `shutil.rmtree(original-store)` 并记录 exists_after=false；import 子进程恢复；
独立 inspect 子进程对照每张表数量、全量状态哈希、状态、账本、预算余额与审批链，任一失败 exit 1。
证据为 drill.log + result.json；只生成 TEST-ONLY 临时签名密钥，seed 后删除测试私钥，不使用真实签批处私钥。

此模型隔离可信控制器的逻辑数据，不是身份认证、OS 沙箱或恶意宿主隔离。持有任意 `--home`/`--company`
选择权的本地操作员可主动选择其他公司；同 UID/管理员仍可直接读写数据库。生产服务必须由可信路由
将认证身份绑定 company，且使用独立 OS 权限/公钥信任域；共用 board key 的公司共享签名权威，
不能宣称签名本身绑定 company。当前未开放网络写入/新增身份系统。
SHA256 防损坏但不证明备份来源；归档来自可信备份介质，不能对抗攻击者重写全部未签名历史再重新计算哈希。
不支持在线热迁移、旧未知 schema 自动升级、跨主机运行中 lease 迁移或外部工件自动重定位。

## P2-0：Ed25519 与私钥离机（2026-11-06）

本节替代下方 P1-1 的 HMAC 签名方案，其余状态机、权限矩阵、审批绑定与独立确认规则不变。

```text
主机可信控制器 prepare → 冻结请求 + 完整 payload → 签批处 Mac 人工核对
                                                     │
                           私钥（仅 Mac）→ board-sign.py / fusion.board
                                                     │ Ed25519 envelope
主机公钥（只验签）← 人工拷回 envelope ←───────────────┘
       │
import_approval → SQLite → mutation / confirm 时重新验签、有效期与绑定
```

主机控制器 CLI 默认只使用 `/home/ljw/.config/fusion/board_ed25519.pub.pem`；
`--board-key-file` 保留为可信操作员的公钥配置入口，不能来自 envelope。
Store 的显式公钥参数用于测试/本地嵌入；未配置仍 fail closed。
验签路径只执行 OpenSSL `pkey -pubin` 和 `pkeyutl -verify -pubin -rawin`，不加载签名工具，
不读取私钥。严格验证 Ed25519 SPKI 类型，私钥 PEM、RSA 公钥、旧 HMAC key、未知算法、
缺少算法和畸形 envelope 均拒绝，无兼容降级。公钥 DER SHA256：
`6505337d3e5dd574df7a9ffb1292897fe50ec0e88dc0872cf985092642d475e9`（与签批处提供前缀一致）。

envelope 为 `{ "alg": "ed25519", "document": {...}, "signature": "128位小写hex" }`。
签名输入仍为原 canonical JSON document 的 UTF-8 字节：sort_keys、紧凑分隔、ensure_ascii=False、
allow_nan=False。算法标识在 envelope 外层，验签端只接受唯一的 ed25519 值，不参与算法协商。
document 字段集合不变，逐字段覆盖；时效、发起 actor/run、request_id、scope、revision 和 payload
绑定仍在导入/消费/确认时执行。旧 HMAC approval 不再有效，未完成操作需人工重新审批；
不自动重签、不删除审计记录、不改现有数据库。

`fusion/board-sign.py` 可单文件拷到 Mac，只有 Python stdlib 与支持 Ed25519 `-rawin` 的 OpenSSL
依赖，无网络安装。输入 document JSON 与显式私钥路径，输出 envelope；`fusion.board` 先生成并
校验 document 后调用同一签名端逻辑。二者都是离线人工工具，控制器/HTTP/runtime 不调用；
检测 PAPERCLIP_/CODEX_ 环境即拒绝。该检查只是误用防护，不能视作对恶意 agent 的隔离。
当前工具只支持无口令 PEM（`-passin pass:` 避免交互阻塞）；加密私钥/Keychain/硬件密钥不在本次范围。

威胁模型：在验签代码与公钥配置未被替换的前提下，读取主机公钥、请求、SQLite 审批记录的进程，
无法生成新签批处签章或篡改已签 document；拒绝错公钥、过期、载荷变更与自审。
**Ed25519 只解决“签名密钥不在主机”这一点，不是宿主权限隔离。** 同 UID/管理员仍可改 SQLite、
替换公钥/代码/OpenSSL/PATH、伪造时间或跳过校验；主机仍有通用计算能力，不能把“无真实签批处签章能力”
说成“不能运行任何签名程序”。需要 OS/服务账户隔离、可信分发、只读公钥部署才能进一步收紧这些边界。
审批内容真实性、Mac 私钥保管、人工核对与传递、主机时钟/代码可信、多租户身份认证均不能由本次测试证明。
仅生成并销毁临时测试密钥；没有获取、复制或使用真实签批处私钥，也没有签发真实批准。

## P2-1：双层预算准入与恢复（2026-11-06）

本节覆盖历史预算展望；不改变 `_VALID_TRANSITIONS` / `AGENT_POLICY`。
预算入口是 **`apply_mutation(command="dispatch")`**，不是 adapter 或 UI。
`Store.checkout` 仅作兼容委托。dispatch 校验尚书身份、Doing 状态、payload hash、revision、run 排重后，
在同一个 `BEGIN IMMEDIATE` 事务内执行公司及工部两层预算检查、写 run、预留与审计事件；
dispatch 使用 savepoint，内部连接调用方即使捕获异常并提交，也不会保留部分 run/预留。
不把预算附在 Doing 状态转换上：进入 Doing 不是派发，每次实际派发必须独立通过准入。
不提供 HTTP 派发旁路，真实运行仍须 controller 在 checkout 成功后调用 adapter。

每个 Store home 是一个公司账本，运行 agent 固定为现有 `gongbu`。公司上限和 `agents.gongbu` 上限同时生效，
缺任一额度时 priced 派发报 `BUDGET_NOT_CONFIGURED`。金额只接受非负、非 bool、SQLite 64 位整数 cents；
计算使用 Python 整数，绝不把历史 `cost_usd` 浮点字段用于预算。月键由 UTC 时钟生成 `YYYY-MM`，
窗口固定 `calendar_month_utc`；结算归属预留时月份，跨月不能把旧运行费用转嫁到新月份。
每层准入计算为 `known_spend_cents + reserved_cents + estimated_cents <= cap`。
当前已占用达到 100% 时，即使新估算为 0 也 hard stop；恰好填满剩余额度允许，后续派发拒绝。
拒绝码为 `BUDGET_COMPANY_EXCEEDED` / `BUDGET_AGENT_EXCEEDED`，与任务审批无关。
预计占用达到 80% 时记 `budget_events.kind=warn` incident，不阻断。

provider `penguinapi` 固定视为 `unpriced`，传入伪造的 0 报价也不能改为 priced；
其他 provider 缺估算同样 unpriced。这里只消费可信本地控制器提供的估算上界，不引入真实价格服务，
不声称任意字符串 provider + 自报价格就是可信的生产价格表。当前 CodexLocal 默认路径仍是 penguinapi。
unpriced 默认 `block`，错误码 `UNPRICED_BLOCKED`；`allow` 每次成功派发写一条 unpriced 事件。
为兼容 P0 明确授权参数，无已安装策略时 `checkout(..., True)` 等价于本次显式 allow；
一旦签批处签署策略，其 block 优先，旧 flag 不能覆盖。unpriced 仍受已配置且已触顶的两层额度约束。
未定价的估算/实际金额始终为 null、pricing=unpriced、unknown_spend=true，绝不记 0。
汇总只加已知金额，因此 `known_spend_cents=0, unknown_spend=true` 是“已知子集为空”，绝非总费用为零。

预留、结算、回收使用 `budget_reservations`；append-only 应用审计为 `budget_events`。
`Store.finish` 在运行完结及 output mutation 的同一事务内按 `actual_cents` 结算，释放整个预留，
将实际金额计入月支出（即退款 estimate-actual）；超估算如实计入并记 `estimate_exceeded` incident，
不能截断费用伪装未超额。缺实际计量释放预留但保留 unknown；unpriced 即使 adapter 报 0 也保持 null。
业务产物写回失败则 run 完结和结算一起回滚；重复 finish 拒绝，不重复扣账。

每次 Store 启动巡检 reserved：记录 Linux PID + boot ID + `/proc` start ticks，
只回收已退出/僵尸/进程身份变化的 owner，标 reservation 和 run 为 orphaned、unknown_spend=true，
清除预算占用和 running 排他索引，写 orphaned 事件，不自动重新派发任务。
新 Store/并发连接不回收仍存活 owner。此为单机 Linux 同步 controller 进程所有权，
不是多机 lease/fencing；不能把一次性 CLI 派发返回后另行启动的无监管进程当作受管运行。
owner 死亡只证明控制器结束，不能证明外部 provider 没有产生费用：回收是释放预留，不是宣称费用为零。

审批边界：普通任务 board approval 不能用于 dispatch。调整额度或 unpriced 策略须导入
Ed25519 签署的 `budget_policy`（完整 schema 见 `P2-1-VALIDATION.md`），revision 单调递增，
过期/未来/篡改签名拒绝；已导入策略持续生效，expires_at 是导入授权有效期，不是额度自动撤销时间。
政策历史存入预算事件；旧 revision 不可回放覆盖新额度。未配置公钥时无法导入。
“特批一次”只接受单独签署的 `budget_override`，绑定 task_id、run_id、UTC 月、估算额，
为两层分别提供显式增加额度，仍重新执行两层预算不等式；不接受无账本的直接放行。
override 记录与 run/预留同事务写入 `budget_overrides` 和预算事件，ID/运行唯一，不能重用，不能豁免 unpriced。
所有签名测试仅使用临时 fixture 密钥，无真实签批处批准或模型调用；不改变既有同 UID/管理员威胁边界。

## P2-2：单机 lease、fencing 与真实 kill-restart（2026-11-06）

本节覆盖历史章节中“尚无 lease/fencing”的边界；不改 `_VALID_TRANSITIONS`、`AGENT_POLICY`。
`leases(task_id, owner_id, fencing_token, acquired_at, expires_at, heartbeat_at,
owner_pid, process_identity)` 常驻保存每个任务的最新 fence，正常释放也不删除行。
owner_id 是唯一 run ID；process_identity 为 Linux boot ID + start ticks。
所有获取/续约/释放/接管在 `BEGIN IMMEDIATE` 中串行 CAS。获取分配更大的正整数 token；
续约保留 token，每 10 秒后台 heartbeat 将有效期延长到当前时间 + 30 秒。
`expires_at < now` 才算过期，等号仍持有。token 不要求连续（撤销也消耗一个 token）。

```mermaid
sequenceDiagram
    participant A as Executor A / token 1
    participant DB as SQLite controller
    participant B as Executor B / token 3
    A->>DB: BEGIN IMMEDIATE; acquire lease + run + reserve budget
    DB-->>A: COMMIT, token 1
    loop every 10s
        A->>DB: renew(owner, token 1), TTL 30s
    end
    Note over A: external SIGKILL; process reaped
    B->>DB: startup: confirm old process gone
    DB->>DB: fence 2; orphan old attempt; release reservation, unknown=true
    B->>DB: BEGIN IMMEDIATE; acquire token 3 + new run + reserve
    DB-->>B: COMMIT
    A->>DB: simulated late write with token 1
    DB-->>A: STALE_FENCING_TOKEN (transaction rollback)
    B->>DB: finish(token 3) + output + budget settle + unique execution_commit
    DB->>DB: release lease, preserve token 3
    DB-->>B: COMMIT (one committed execution)
```

`apply_mutation` 的任务状态/payload/output 写入在 revision、审批消费及任何 mutation 前校验 token；
未有租约的流程阶段使用 bootstrap token 0，已有租约必须显式匹配当前 token。
比要求的 `token >= current` 更严格：未来伪造 token 也拒绝；None/bool/浮点/字符串都不是有效 token。
状态和事件携带 token，`Store.finish` 在 run 写入/预算结算/产物写回之前验证 token、owner 和有效期。
运行结果不能覆盖准入 token。续约过期、owner 不符或旧 token 都失败，不静默接受。
正常释放仅允许当前持有者，保留当前 fence；后续流程使用该 fence，下一轮准入必须递增。
`Store.move` 从本 Store 曾取得的内存能力填 token，`Store.finish` 仅可从当前进程成功准入的能力填 token；
新进程不能从 DB 自动恢复旧写权限。跨进程调用方须传明确 token，且始终受当前租约校验。
直接 `apply_mutation(..., command='dispatch')` 的常规独立事务入口也自动启动 heartbeat；
内部 conn 入口成功返回后由事务所有者提交并调用 `leases.remember`，不能在提交前开始执行。
新 controller 在已有 fence 的任务上做后续流程时，必须明确提交当前 token；
不能只重建 Store 后靠旧的无 token 请求获得写权限。低层 reserve/settle 也自行校验 token。

dispatch 的 SAVEPOINT 包含租约、run、预算预留/override 消费及事件，外层事务为 BEGIN IMMEDIATE。
任一步失败整体回滚，包括“过期接管后预算不足”还原旧租约、旧 running 与旧预留。
startup 回收只处理已死亡/僵尸/身份已变的旧 owner，不因心跳陈旧误回收活 owner；
撤销先增 fence，再标旧 run/reservation 为 orphaned，记录撤销 token、unknown spend。
force-release 额外严格检查 `os.kill(pid, 0)`：PID 存在或权限不明都报 OWNER_ALIVE，
即便已过期也不能 force；只有 PID 不存在且 CAS 匹配才允许。
普通过期接管不声称杀死旧执行者；旧执行者可能仍在计算，但所有受管写入被 fence 拒绝。
无无条件抢占入口，私有撤销函数也复核死亡或真实过期条件。

`execution_commits(task_id PRIMARY KEY, run_id UNIQUE, fencing_token, artifact)` 是受管副作用的
任务级幂等提交记录，与成功 finish/output/预算结算在同一事务。已有提交的任务禁止再次 dispatch。
失败/孤儿可以重试；一次崩溃加恢复保留 **两条尝试 runs（orphaned + succeeded）**，
但只有一条 committed execution、一次 run_finished 和一次产物写回；不删除审计历史伪装没发生崩溃。
已提交的 output 也不能用当前 token 再次覆盖，避免从直接 mutation 入口绕过单次提交。
这不是任意外部副作用的 exactly-once：外部 API、文件、子进程需自行接受 fencing/idempotency key，
否则旧进程在 lease 失效后仍可能进行不受管 I/O。这里的真实演练只提交 SQLite 内的模拟结果，
不调用模型，不调用付费服务；不声称 provider 费用为零或外部业务自动安全重放。

复现入口：`python -m fusion.lease_drill --output /home/ljw/boss-lab/BOS-29-drill`，
每次新建独立 kill-restart-* 目录，绝不复用/覆盖现有运行 DB；输出 evidence.json、output.log。
脚本父进程等第一执行者成功获取 lease 后，实际 `os.kill(pid, SIGKILL)`（即 kill -9），
等待回收，再启动第二执行者；验证 token 递增、两种复活写入失败、重复派发失败、单次提交。
`python -m fusion.control --home <演练目录> lease-status` 可查 lease 和提交记录。
Store 启动本身会做安全死亡回收，查询 CLI 因此不是绝对无写入工具。

剩余边界：单机 Linux + SQLite，不是分布式锁；主机时间回拨/跃迁会影响过期判断，
租约失效只撤销受管写权限，不保证立即终止全部计算/外部 I/O；PID 存活检测不是跨主机证明。
heartbeat DB 失败会停止续约，后续写入仍受有效期约束；仅在当前租约仍有效时可提交。
同 UID/管理员仍可直接改 DB/代码、读取 token 或绕过 API，token 不是安全身份凭证。
已有 P2-1 数据库按 CREATE IF NOT EXISTS 加表；旧无 lease 的孤儿走原恢复分支，
活的旧 running 仍被唯一索引挡住，升级应先停旧 controller；不无条件接管旧活任务。
不新增后台自动重派发/模型调用，不修改 Paperclip 实例或现有生产数据。

## P1-1 冻结：唯一 mutation 与审批主体 v1（2026-11-05）

本节在 P1 代码之前落盘并单独提交；对本任务覆盖的行为优先于下方历史 P0/P1 展望。P1-1 不运行真实模型。

事实源仅为 SQLite。`fusion.mutate.apply_mutation` 是任务创建、状态转换、业务 payload 修改的唯一服务；`Store.create/move` 仅作兼容委托。请求必须带 actor、command、target、reason、payload_sha256、expected_revision、run_id；旧 Store 调用可在委托前读取 revision，显式并发调用必须提交同一个已读 revision。revision 单调递增，BEGIN IMMEDIATE 内比较并更新，冲突抛 Conflict（status_code=409）。事件、状态、审批消费同事务。运行结果写回 output 也经过该服务（与 run 完结同事务），使审批失效。JSON 不导入，始终只是投影；修改 JSON 不能修改权威状态，重新投影覆盖篡改。

旧入口选择 (b)：`scripts/kanban_update.py` 可执行 CLI 所有写命令一律退出 2（LEGACY_MUTATION_DISABLED），原 dashboard 的所有 HTTP POST 一律 405；无允许写入的环境开关。保留旧模块函数供上游离线 JSON 算法回归，不是受支持的任务入口，不读写 SQLite；Fusion 投影的 atomic_json_update 也硬拒绝。HTTP 读取权威 SQLite 快照/重新生成投影，审批只读。旧 JSON 任务不静默导入，正式迁移与恢复演练仍属 P2。

主体分离：menxia 只能签发 `review_decision`，board 只能签发 `approval`。SQLite 独立 `decisions` 表记录 id、kind、task_id、payload_sha256、revision、actor、run_id、created_at、expires_at、scope、request_actor、request_run_id 和 decision。scope 精确包含原状态、目标状态、command。`mutation_requests` 保存不可变发起主体，批准内容不能重新声明发起人；同 actor 或同 run 发起和审核/批准一律拒绝。制度审核与人类批准分别存储、互不替代。

board 不可通过 actor 字符串冒充：审批导入须校验独立操作员保管的 HMAC-SHA256 密钥签名（覆盖全部记录字段），密钥路径由 Store 的可信本地配置指定，不存进数据库/投影；menxia 接口拒绝产出 approval。离线 board 工具不在 agent runtime 签发；测试用独立 fixture 密钥只证明验签边界，不代表真实签批处批准。此版本仍是可信本地控制器，不防同 UID/管理员读取密钥、直接写 SQLite 或替换代码；生产多租户身份认证及 OS 权限隔离未验证，不能声称防御宿主恶意进程。board 密钥未配置时高风险操作 fail closed。

审批绑定 canonical JSON（sort_keys、紧凑分隔、UTF-8、禁止 NaN）的 SHA256，payload 包含全部业务方案/金额；与任务 revision、发起 actor/run 和转换 scope 同时绑定。修改 payload、金额、output 或 revision 后旧记录保留供审计但不再有效。created_at/expires_at 必须是带时区时间，created_at ≤ 当前时间 < expires_at，消费时重新验证，不只检查导入时间。

高风险转换保留 `_VALID_TRANSITIONS`、`AGENT_POLICY`、`agents.json` 和 P0 D1：禁止直接指定 PendingConfirm。原 HIGH_RISK_TRANSITIONS 必须先有有效 board approval 才能进入 PendingConfirm；保留原 confirm_by 权限方完成二次确认，并再次验证绑定、有效期及发起人隔离。PendingConfirm 内只接受其绑定目标，不能靠手填元数据完成；Review→Done 的 menxia confirm 生成 review_decision，不生成 approval。审批消费发生在进入 PendingConfirm 时；同 approval_id 和完全相同请求重复提交返回原结果、不追加事件、不再次变更；不同请求复用则 409。后续 confirm 使用保存的审批引用重新校验，过期或 payload 变化拒绝。普通 Menxia 转换同样记录制度审核且不允许与上一次提交方案的 actor/run 相同。

预算硬停、lease/fencing、恢复演练、三省 AI 化仍是 P2；P1-1 完成后不阻塞这些工作开始，但不代表它们已实现或获上线授权。

冻结日：2026-11-05。范围：stdlib dashboard + 单机真实 Codex 执行 + SQLite 运行账本。上游提交见 AUDIT.md。冻结意味着实现按本文验收；P1 的新增方案须另立版本，不表示本文已获产品上线批准。

## 保留制度，替换执行依赖

P0 不改上游 `_VALID_TRANSITIONS`、`AGENT_POLICY`、`agents.json`、MIT LICENSE。新增可信本地控制器，读取权威规则，拒绝未知身份、未知状态、终态出边；执行命令权限与跨 agent 消息 ACL 分别核验。六部仅能回报 Review，不能自行收口 Done。Review→Done 保留 PendingConfirm，且只允许 `menxia` 确认。门下的准奏/封驳有明确 actor 记录。

演示中的太子分拣、中书方案、门下审核、尚书派发是预先写明的操作员固定用例，不声称调用了多个 AI 角色或完成自主规划。唯一真实 LLM 运行在工部执行步骤。演示验收器由控制器对照固定期望检查文件计算结果，验收后控制器以 menxia 角色确认；这是测试 fixture 的角色事件，不是独立主体审核或人类审批凭证。

`python3 -m fusion.dashboard` 导入原 stdlib dashboard，使用 SQLite 的 JSON 只读投影。保留原看板 UI，P0 拦截所有 POST（405），防止旧 mutation API 绕过新策略或触发 OpenClaw；真实任务由本地 CLI 触发。`/api/fusion/status` 暴露账本，`/api/live-status` 暴露原 UI 可消费的数据。无 Redis、Docker、OpenClaw，不启动新闻抓取/外部通知或心跳循环。原版 `python3 dashboard/server.py` 仍可单独验证，功能无改动。

## 适配器契约

`RuntimeAdapter` 的 `capabilities` 声明 `filesystem`, `commands`, `usage_events`，`isolated=false`：CLI 工作目录不是安全沙箱的证明。接口 `execute(RunRequest)->RunResult`。

输入：`run_id`、`task_id`、`agent_id`、绝对 `workspace`、UTF-8 `prompt`、超时秒数。运行目录需控制器创建且位于本地数据根目录。模型使用当前本机配置 `penguinapi / gpt-6.1-sol`，不得失败后切换；契约不接受从 HTTP 传入任意 executable、环境变量、provider 或模型。只支持固定 `codex exec`，argv 调用、无 shell 拼接；stdin 传 prompt。

输出：`status` = succeeded / failed / timeout / unavailable，`exit_code` 可空，`usage` 保存原始 thread usage，`cost_usd=null`（未定价），stdout/stderr 日志路径和返回耗时。CLI 返回 0 只表示运行成功，业务成功还必须通过控制器对照固定期望的产物检查。不存在/错误产物必须失败。超时终止整个子进程组，保留日志，不自动重复执行。外部进程 crash / 父进程 crash 不等价业务失败；恢复时标记 interrupted，禁止无条件重放。

子进程剥离 `PAPERCLIP_*` 控制面凭证，保留用户已经配置的 CLI 通道。P0 工作区输入为合成 CSV，不接触客户，不采购、不新增服务、不变更 Paperclip。P0 单 run 硬超时 180 秒，CLI sandbox=workspace-write；实际 sandbox 能力取决于宿主环境，失败如实记录，不自动降级到无沙箱。

## 持久化与迁移

选择 SQLite（stdlib sqlite3, WAL, foreign keys, BEGIN IMMEDIATE），P0 表：tasks（状态+JSON）、runs（独占任务执行记录、状态、usage、cost=null）、events（append-only 应用接口）。同一任务只允许一个 running run（部分唯一索引），冲突视为 409 语义。状态和事件同事务，JSON 只作 dashboard 投影，不反向写回数据库。DB 是新入口唯一事实源；旧入口保留独立，不能共写同一任务。

P1 迁移步骤：停写旧任务文件→备份+SHA256→校验 ID、已知状态、关系→单事务导入并记录 legacy_id→逐任务对比数量/状态/flow→生成只读投影→切入口→失败则恢复旧快照。已有异步 SQLAlchemy/Postgres 后端作为后续多机候选，不在 P0 强装 PostgreSQL/Redis；不能同时运行两套状态源。

P1 SQLite backup API 导出一致性 DB + artifact manifest，import 到新空目录后做 integrity_check、哈希校验和状态计数比对；恢复演练成功才可声称可恢复。P0 仅使用 SQLite 和重建投影，尚不交付正式导入/导出或灾难恢复。

## 预算与审批位置

预算属于控制器 dispatch admission 层，任何适配器之前统一执行。P1：公司+agent 两层同事务预留估算上界，warn 80%，hard-stop 100%；运行结束计量并结算预留；长运行定期检查/取消，不保证已发生费用可逆。unknown ≠ 0：无价格表/缺 usage 的 run 标 unpriced，默认拒绝新派发，只有明确范围授权可放行；预算审批不能由同一运行自动签发。P0 保留 `cost_usd=null` 和必须显式开启的 `--allow-unpriced`，没有金额预算实现，不能宣称实现双层 hard-stop。

P1 三种人类闸门：招聘在 agent proposed→active 前；花费在 approved plan→dispatch 前；战略在 Menxia→Assigned 前。审批绑定 payload hash、revision、actor 与有效期，方案或金额变化即失效；人类审批叠加门下省的制度审核，不能替代它。取消高风险操作与最终收口同样保留原确认要求。当前 P0 没有真实人类审批队列，demo 的流程确认只是 fixture。

## 运行、恢复与观测

P0 按 CLI 请求执行，无常驻 agent 心跳；dashboard 无上游自动巡检和 startup re-dispatch。run 保存开始/结束、CLI 状态、usage、日志、验收结果和 artifact SHA256。执行期间用 SQLite running 唯一索引排重；异常留下 interrupted/failed，业务状态停留 Doing，明确人工恢复，绝不假成功。P1 引入有 fencing token 的 lease、force-release（必须先确认旧进程死掉）、事务 outbox/按需 worker、幂等键与恢复演练。

P0 UI 为只读投影，artifact 在本机日志目录中由报告挂载路径，不宣称有安全 HTTP 下载/多租户隔离。P1 需实现鉴权、artifact 只读服务、路径约束与进程隔离，再允许 HTTP 执行任务。

## 验收边界

必须通过：原上游关键测试不改规则；新入口非法转换/未知 actor/越权委派/错误确认/并发冲突测试；原 dashboard 无 OpenClaw 启动；融合 dashboard 200；一次真实运行读输入、写文件、运行命令、生成 JSON，控制器对照固定期望 + 产物哈希自复算与日志可查。失败就标失败，不换模型，也不把 mock 结果算真实通过。

`verify_receipt` 从产物文件重新计算 SHA256，并对照固定期望检查 JSON 内容；任务状态、流转、run 状态、usage、成本及记录的哈希均来自同一控制器账本（经 HTTP 投影），不是外部独立来源，不能据此宣称独立验证了账本真实性或执行来源。
