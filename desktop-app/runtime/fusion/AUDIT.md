# P0 源码审计

审计对象：cft0808/edict，固定上游提交 `14a207557719c046af0f993a7bff1cc5a5015b33`（2026-05-06）。保留原 MIT 与作者署名。本目录是独立 P0 改造，不能称为融合完成。

## 第一轮：实现前代码证据

| README 主张 | 代码证据（相对仓库路径 / 函数） | 判定与边界 |
|---|---|---|
| stdlib 看板、实时任务和干预 | `dashboard/server.py`: `Handler`, `load_tasks`, `handle_task_action`, `handle_advance_state`；`dashboard/auth.py` | 有真实实现；看板启动与执行 agent 是两件事。无 OpenClaw 启动验证见验证报告。 |
| 状态机不可绕过 | `scripts/kanban_update.py`: `_load_canonical_transitions`, `_VALID_TRANSITIONS`, `cmd_state`；权威表在 `edict/backend/app/models/task.py`: `STATE_TRANSITIONS` | `cmd_state` 确有合法转换校验，但未知旧状态放行，终态在权威表缺键时也可能放行；`cmd_block`、`cmd_create`、`cmd_delegate_result` 直接写状态。README 的“不可绕过”过强。 |
| 严格权限矩阵 | `install.sh` 内 `AGENTS` 的 `subagents.allowAgents`；`agents.json`；`scripts/kanban_update.py`: `AGENT_POLICY`, `_check_permission` | 通信 ACL 由安装写入 OpenClaw，CLI 是命令级权限；身份由 env/cwd 推断，未知身份放行；直接调用 Python 函数不执行入口检查。不是本地进程安全边界。README 中中书→太子的矩阵与安装/agents.json 不一致。 |
| 强制门下审核、高风险确认 | `dashboard/server.py`: `handle_review_action`；CLI `HIGH_RISK_TRANSITIONS`, `CONFIRM_AUTHORITY`, `cmd_confirm` | 准奏/封驳已经存在，ROADMAP 仍列 Phase 2，存在文档滞后；CLI `cmd_confirm` 没核对 `confirm_by`。dashboard `handle_advance_state` 直接 Review→Done，未套 CLI 的 PendingConfirm。不可宣称全入口统一强制。 |
| 完整可审计 | CLI `_append_audit` 写 `data/audit_log.json`，原子更新但最多 5000 条，写失败只告警；`flow_log`, `progress_log` | 有真实日志，非不可变账本，状态/审计是两次写入；dashboard 自己的变更路径并非全走 `_append_audit`。数据库审计另属异步后端，不等于 stdlib 看板。 |
| 独立工作区、注册、热切模型 | `install.sh` 的 workspace、symlink、注册逻辑；`dashboard/server.py`: `wake_agent`, `dispatch_for_state`, `_check_gateway_alive`, `_resolve_openclaw_bin` | 配置/执行硬绑 `~/.openclaw` 和 CLI/gateway，未定义替代运行时接口。CLI 确实可以运行真实 agent，但本机无 OpenClaw 时无法完成这个默认执行链。 |
| 朝堂 LLM 讨论 | `dashboard/court_discuss.py`: `advance_discussion`, `_llm_discuss`, `_simulated_discuss` | 有 LLM 分支，也有静默规则模拟降级；会话在进程内 `_sessions`，不等于真实业务执行或持久化。 |
| 成本/资源 | CLI `cmd_progress(tokens, cost, elapsed)` | 有资源记录，默认 cost=0、只在 >0 时写，未定价与零成本难区分；没有公司/agent 双层预算准入。 |

## 硬依赖清单

不运行安装脚本：它会创建工作区、注册 agent、同步鉴权配置、重启 gateway。`wake_agent` 和 `dispatch_for_state` 执行 `openclaw agent`；状态检查读取 OpenClaw sessions；模型/统计/配置同步脚本也依赖 OpenClaw 文件格式。看板静态资源、JSON 读写、文件锁和本地登录模块本身不依赖 OpenClaw 服务。

原状态表与权限文件保持原样。新入口必须额外收紧未知身份、终态、确认主体和委派边，不借“兼容”扩大权限。原有漏洞作为审计发现保留，不在 P0 顺便重写上游。

完整下载、异步后端核对、独立启动与测试的最终证据见 `VALIDATION.md`。

## 完整源码补审（实现骨架后的证据校正）

- `edict/backend/app/services/task_service.py`: `TaskService.create_task`, `transition_state`, `request_dispatch` 确有 PostgreSQL 事务 outbox；`transition_state` 用 `SELECT FOR UPDATE` 串行化流转，并对缺少状态出边默认拒绝。这比 JSON CLI 的缺键放行严。
- `edict/backend/app/services/event_bus.py`: `EventBus.publish/consume/ack/claim_stale` 确有 Redis Streams，另有 pubsub；`workers/outbox_relay.py`: `OutboxRelay._relay_cycle` 确有轮询发布、重试和 dead_letter 分支。因此“没有事件驱动/数据库”是错误概括，正确说法是“stdlib 看板仍使用另一套文件/线程路径，异步后端未在本机验证”。
- Outbox relay 的 `publish` 为事件生成新 UUID，调用时没有传递原 outbox `event_id`；publish 后 DB commit 前崩溃可能重复发布，不能凭 README 声称 exactly-once。达到 MAX_ATTEMPTS 的事件未标终止，后续轮询仍可能再次尝试/重复 dead_letter。这是静态审计风险，未做故障注入。
- `edict/backend/app/workers/dispatch_worker.py`: `DispatchWorker._call_openclaw` 的真实子进程仍用 `settings.openclaw_bin`；`DispatchWorker.__init__/_get_bucket`（并发 semaphore 逻辑）是进程内资源限制，不等于跨进程任务 checkout/fencing。上下文拼装、重试、流式输出已有实现，不能说执行层完全没有定义；缺的是可替换的统一运行时契约。
- `edict/backend/app/models/audit.py`: `AuditLog` 是独立 SQLAlchemy 模型；对 `edict/backend/app` 搜索 `AuditLog(` 只命中类声明，`TaskService.transition_state` 写 flow_log/outbox，未写 AuditLog 行。README“所有状态变更自动写 audit.py”与实际调用证据不符。
- `scripts/sync_officials_stats.py`: `calc_cost` 有模型价格表，未知模型回退到 sonnet 价格而非标 unpriced；已有粗估不等于准确成本，更不等于双层预算治理。

## 阅读范围与未作推断

已核对 README/ROADMAP、install/start、全部 dashboard Python、核心 kanban/file_lock/utils/sync/model 脚本、后端 task/audit/outbox 模型与 task service/event bus/三种 worker、相关 API/测试及前端静态入口。图像/视频不是执行证据；未对全部前端交互、通知平台、Docker 镜像、OpenClaw gateway 或 PG/Redis 做运行验证。审计不等于逐行安全认证。
