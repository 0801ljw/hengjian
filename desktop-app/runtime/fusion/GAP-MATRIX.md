# Paperclip × edict 差距矩阵

工作量：S=1–2 工程日，M=3–5，L=1–3 周；单项粗估、非承诺，不含生产安全认证。Paperclip 能力取自本任务给定基线；不代表本次对 Paperclip 全代码/故障能力进行了验证。

| Paperclip 能力 | edict 代码现状 | 融合方案 / P0 边界 | 量 |
|---|---|---|---|
| 适配器抽象 | dashboard `dispatch_for_state`、异步 `dispatch_worker` 绑 OpenClaw；有执行逻辑，缺统一通道契约 | RuntimeAdapter 输入/结果/失败语义；P0 codex_local，P1 pi/process/能力验证 | M |
| 公司/agent 预算、warn/hard-stop、unknown | `cmd_progress` 和官员统计记录资源；无双层准入，缺失价格可能混为零 | dispatch 前原子预留，80% warn/100% stop；P0 cost=null+unpriced 显式准入，金额控制留 P1 | L |
| 人类招聘/花费/战略审批 | `handle_review_action`、`PendingConfirm` 已有，但非独立人类审批凭证，部分入口不一致 | hash 绑定审批队列叠加原审核；P0 不冒充人审，只保留门下确认语义 | L |
| 汇报树与扩编 | install/agent 配置、固定角色映射；可编辑配置但无组织治理 | agent registry+manager_id+角色能力，招聘提案审批后激活；P0 固定角色 | M |
| checkout/409/force-release/恢复 | JSON 文件锁防写竞争；调度重试/回滚；异步 worker 有资源锁，不等价任务独占 lease | P0 running 唯一索引，冲突；P1 lease+fencing+force-release 与活进程核验 | M |
| 结构化持久化+导入导出+恢复演练 | stdlib 路径 JSON；另有 SQLAlchemy/Postgres+outbox，不可说完全没有数据库 | P0 SQLite/WAL 运行账本+投影；P1 legacy 迁移、backup/export/import+恢复演练 | L |
| 按需唤醒 | stdlib run_loop/120s 巡检；另有 Redis Streams/event workers | P0 事件=显式 CLI 请求，无 agent 常驻；P1 DB outbox+按需消费者、幂等消费 | M |
| run/活动日志/artifact | flow/progress/审计+OpenClaw sessions；异步 audit 模型；无统一 artifact manifest 验收 | P0 run 日志、raw usage、SHA256 产物与确定性验收；P1 安全 HTTP 挂载+检索 | M |

优先 P1：统一唯一 mutation 服务和审批主体，再实现预算准入、lease/recovery；不可先扩编 agent 或新增模型花费。

---

# P1 排期（1.0 → 1.1 → 1.2）

> 依据：上表的差距项 + 当前实现盘点（2026-11-16，1.0.0）。
> 排期原则：**先保命、再保准、最后保快**。能靠制度挡住的，不先写代码。

## 现状盘点（决定起点）

| 能力 | 现状 | 结论 |
|---|---|---|
| 单一变更服务 | `mutate.py` 已收口所有写操作 | ✅ 已完成（P0） |
| 审批主体 | `approvals.py` + Ed25519 签名 | ✅ 已完成（P0） |
| 双层预算 | `budget.py`（reserve/commit + 公司/角色策略） | ✅ 主体完成，缺"拒绝路径的可观测性" |
| 任务独占 | `leases.py`（acquire/release + fencing token） | ✅ 主体完成，缺活进程核验 |
| 运行账本 | SQLite/WAL + `runs` / `mutation_requests` | ✅ 已完成（P0） |
| 产物 manifest | 落盘 + SHA-256 + 验收对账 | ✅ 已完成（P0） |
| 事件消费 | 显式 CLI/worker 轮询（`worker_loop`） | ⚠️ 无 outbox、无幂等消费表 |
| 适配器契约 | 每个 agent 走各自的 CLI 适配 | ⚠️ 无统一 RuntimeAdapter 接口 |
| 恢复演练 | `restore_drill.py` / `lease_drill.py` 存在 | ⚠️ 只是演练脚本，未接入 CI 门 |

---

## 1.1（目标：能扛住"跑一半断掉"）

| # | 项 | 为什么先做 | 依赖 | 验收 | 量 |
|---|---|---|---|---|---|
| 1.1-1 ✅ | **活进程核验 + 孤儿 run 回收** | 机器重启/进程被杀后，`running` 会永远卡住（本机断过 14 小时，正是这个场景） | `leases.py` | 已交付：`recovery.reconcile()` 启动回收队列遗留，标 `error/orphaned_after_restart`；租约/预算走既有 `leases.recover`（run 标 `orphaned`）。测试 `test_orphan_recovery.py`（5 例）；CLI `fusion recover --home <dir>` | M |
| 1.1-2 | **outbox 化事件消费 + 幂等表** | 现在 worker 轮询 + 内存态，重复消费只能靠运气 | 1.1-1 | 同一事件投递两次，只产生一次 run；注入崩溃后重放不重复扣预算 | M |
| 1.1-3 ✅ | **预算拒绝的可观测性** | 被预算挡下时只有终态，没有结构化原因 | `budget.py` | 已交付：`budget_denials` 表（append-only，事务外落库，回滚不丢）+ `/api/budget` 的 `denials` 字段；测试 `test_budget_denials.py`（限额/未定价/放行三例）。**前端提示未做**，见 1.1-3b | S |
| 1.1-4 ✅ | **恢复演练进 CI** | 备份能不能恢复，只有手动跑过 | `restore_drill.py` | 已交付：`verify_all.sh` E4 段每次 CI 跑完整演练（导出→删源→导入→逐项比对），实测 1s；失败即验收红 | S |
| 1.1-5 ✅ | **`running` 唯一性加固** | P0 有唯一索引，但 lease 过期边界未测 | `leases.py` | 已覆盖：`test_concurrent_executors_exactly_one_lease`（并发两执行者只有一个拿到租约）+ `checkout` 把 `sqlite3.IntegrityError` 映射为 `409: task already has a running execution` | S |

**已交付**：1.1-1（孤儿回收）、1.1-3（拒绝可观测，后端）、1.1-4（恢复演练进 CI）、1.1-5（running 唯一性，测试已覆盖）。
**剩余**：1.1-2（outbox + 幂等消费，M）、1.1-3b（拒绝原因前端可见，S）。启动路径见 `fusion/server.py` 的 reconcile 调用，手动入口 `fusion recover --home <dir>`（幂等，检测到活消费进程时拒绝动手）。

| 1.1-3b | **拒绝原因在界面上可见** | 现在只有 `/api/budget` 有数据，工作台还没展示 | 1.1-3 | 提交被预算挡下后，任务详情能看到 code（如 `BUDGET_COMPANY_EXCEEDED`）与限额/已用 | S |
**1.1 发布门**：以上 5 项各有自动化测试；故障注入（kill -9 / 断电模拟）后重启，系统**不出现永久 `running`**。

## 1.2（目标：能换实现而不换契约）

| # | 项 | 为什么 | 依赖 | 验收 | 量 |
|---|---|---|---|---|---|
| 1.2-1 | **RuntimeAdapter 统一契约** | 现在 8 个 agent 各有适配，新增一个就要改多处 | 1.1 全部 | 抽象 `prepare/invoke/parse/usage`；新增一个假适配器只需实现 4 个方法即可跑通全链路 | L |
| 1.2-2 | **artifact 检索与预览挂载** | 产物现在只能按 run 查 | 1.2-1 | 可按 issue/时间/SHA 检索；预览走安全 HTTP 挂载（已有 `/api/artifacts` 扩展） | M |
| 1.2-3 | **多 agent 汇报树（manager_id）** | 组织治理前提 | 1.2-1 | agent 可声明 manager；非法环被拒；招聘提案经审批后激活 | M |
| 1.2-4 | **legacy 导入导出闭环** | `transfer.py` 有，但未覆盖新表 | 1.2-1 | 导出→干净实例导入→行数与关键字段一致 | M |

**1.2 发布门**：能用一个"假适配器"在 CI 里跑通端到端；导出导入往返一致。

---

## 明确不做（每项都写理由）

| 项 | 决定 | 理由 |
|---|---|---|
| 多用户邀请 / SSO | 暂缓 | 当前用户量 1-2 人，`local_trusted` + 注册足够；引入 SSO 会扩大攻击面 |
| Kubernetes / 多机部署 | 不做 | 单机 mini PC 是实际部署形态；Docker 隔离已够（`design-docker` 归档） |
| 消息队列（Redis/Kafka） | 不做 | outbox + SQLite 在单机量级足够；引入外部依赖会让"一键可用"变难 |
| 移动端独立 App | 不做 | 响应式 Web 已覆盖；多维护一个客户端不值得 |
| 自研模型网关 | 不做 | 用户自管模型接入（11-13 已完成）+ 各 CLI 原生通道够用 |

---

## 排序理由（一句话版）

1. **先修"卡死"**（1.1-1/2/5）：断一次电就卡住的任务，比任何新功能都更伤信任。
2. **再修"看不见"**（1.1-3/4）：预算拒绝和备份恢复如果不写在账上，等于没有。
3. **最后修"扩展成本"**（1.2）：适配器契约和组织治理是"以后要不要长大"的问题，不是"现在能不能用"的问题。

## 跟踪方式

- 每个 P1 项对应一个 issue，标题前缀 `P1-`，验收条件写进 issue 描述（就是上表的"验收"列）
- 完成后在本表加 ✅ 并补 commit/测试名
- 每周末复核一次排期是否还成立（依赖是否变化、是否该降级/砍掉）
