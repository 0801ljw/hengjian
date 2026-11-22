# P0 验证与诚实结论

日期：2026-11-05；执行者 Atlas。结论：**P0 审计、8 项矩阵、冻结设计、单机真实执行骨架交付；不等于全量融合完成，不建议上线或承载不可信业务。**

## 取得上游与改动边界

GitHub smart HTTP 完整 clone 接收部分后停滞，浅 clone 分别遇到连接超时和 TLS 中断。没有把失败说成成功：改用 GitHub 官方 API 固定提交 tarball，完成解压，并按 API recursive tree 校验全部 **223 个 blob**、Git tree `7b44f37128938137493fe07be85aad99408fb54a`、原始签名 commit bytes 的 Git SHA1 `14a207557719c046af0f993a7bff1cc5a5015b33`。恢复为 depth-1 Git checkout，origin 指向原仓库，再开 `fusion/p0-local-runtime` 分支。恢复脚本：`/home/ljw/boss-lab/recover-edict-clone.py`，元数据 `edict-tree.json` / `edict-commit.json`。上游 main 对象完整匹配；不是伪造新作者提交。

原 LICENSE、`agents.json`、`install.sh`、`scripts/kanban_update.py`、权威 `models/task.py` 均无 diff。生产改造全部在新增 `fusion/`；另修正两份上游测试文件，见下。未安装/启动 OpenClaw、Redis、Docker，未改 Paperclip 仓库/服务/配置或公司配置，未招聘、采购、外联客户。只使用已授权本机模型通道；pytest 为公开开源测试依赖，无付费服务采购。

## 已实现

- 代码证据审计：`AUDIT.md`；完整八项矩阵：`GAP-MATRIX.md`。
- 初次冻结 `DESIGN.md` SHA256：`eaad77d589531a0a629c47548f5d7512c3726c23bc3eb945a11a9052ef6c63b6`（历史版本；本次口径更正后已变化，不作为当前文件校验值）。
- `RuntimeAdapter`/真实 `CodexLocal`；固定 argv、stdin prompt、180s 超时、进程组终止；无 mock fallback，无模型切换。
- SQLite 任务/事件/run、独占 running 唯一索引（409 语义）、unknown-cost 显式准入、JSON 只读看板投影。
- 新控制器使用原规则并收紧未知身份/未知状态/终态/确认主体；工部只能回报 Review，最终收口必须经过 PendingConfirm。
- 原 UI 的只读封装：屏蔽旧 POST 和可能检查 OpenClaw/迁移配置的 API GET；本机 CLI 发起任务。

## 已验证

环境：Python **3.13.5**；Codex **0.166.0**，已配置 **penguinapi / gpt-6.1-sol**。pi **1.5.0** 仅查版本，未调用。`command -v openclaw` 无结果。

### 真实 run

- task：`FUSION-P0-001`；run：`53d98212a37844db806f2a5ea05cffc1`。
- 真实运行目录：`/home/ljw/boss-lab/edict/fusion/var/runs/53d98212a37844db806f2a5ea05cffc1/`。
- 耗时 **127.617s**，CLI exit 0。`runtime.jsonl` 有 3 条成功命令完成事件、1 个文件变更事件内含 2 个 add（result.json/worklog.txt）；读取 input.csv、执行 Python CSV 聚合、写 result.json/worklog.txt，再运行验证命令。
- 控制器对照固定预期：`{"totals":{"gongbu":22,"hubu":32},"row_count":4}`；输入未修改，worklog 非空，命令事件退出 0。`verify_receipt` 自复算产物哈希，其余状态与运行元数据来自控制器自己的账本，不构成独立审计。
- 产物：`result.json`，55 bytes，SHA256 **540ebc9174053aca9eca15329e1cee4c05b9da2d387859df7a100889ed1ac9ca**。
- DB/receipt 显示 `Taizi → Zhongshu → Menxia → Assigned → Doing → Review → PendingConfirm → Done`。三省动作是确定性测试 fixture，不是多人类审批或多个真实 LLM agent；工部是唯一真实运行时调用。
- raw usage：input_tokens **80,553**，cached_input_tokens **71,936**，output_tokens **642**，reasoning_output_tokens **20**。保留供应商原义；即便保守把 input+cached 相加也是 **152,489**，不接近 1.2M。金额未知，`cost_usd=null`，没有冒充 $0；没有切换模型重试。

### 看板与测试

- 原 `dashboard/server.py` 通过 bounded 子进程启动测试，在无 OpenClaw 下 `/` 返回 HTTP 200，随后退出，证明“可启动看板”不等于“默认派发可用”。
- `fusion.verify_receipt` 起真实本地 HTTPServer，`/`、`/api/live-status`、`/api/fusion/status` 都返回 200，HTTP 查询到同一真实 run 的 Done 状态与产物 hash；检查后关闭，无遗留后台服务。日志 `/home/ljw/boss-lab/BOS-14-dashboard-smoke.log`。
- 原始上游基线：**61 passed / 7 failed**，完整失败日志 `/home/ljw/boss-lab/BOS-14-upstream-tests.log`。失败不是适配器引入：两条 E2E 过时地要求 Zhongshu 直接 `done`→Done，与上游 `cmd_done` 的 Review 规则矛盾；五条 symlink 测试只 patch Path.home、没 patch import 时缓存的 OPENCLAW_HOME，导致测试路径错位。
- 保留全部原测试，没有 skip/xfail/删除断言。仅把旧 E2E 改为走完整流转，并新增 Review/PendingConfirm 断言；fixture 增加 OPENCLAW_HOME 的临时目录 patch。未改生产状态/权限规则来迁就测试。
- 最终 **78 passed in 3.44s**：68 条上游测试（含前述修正）+ 10 条新增测试。原状态机一致性测试原封不动通过；新增全表组合检查状态/命令权限/通信 ACL、非法确认、未知主体、终态、未完成 todo、四线程争抢仅一成功、unpriced 拦截、CLI 缺失/超时故障注入、原/融合看板启动。
- `python3 -m compileall -q fusion` 与 `git diff --check` 通过。pytest 无覆盖率百分比报告；不据此声称 100% 代码覆盖。

## 未验证 / 未完成

### 2026-11-05 修补复验（BOS-16）

- D1 采用拒绝外部直接进入 PendingConfirm：仅高风险转换可生成该状态与确认元数据，未放宽状态机或权限矩阵。新增回归检查拒绝后任务/事件不变，且正常 Done→PendingConfirm→menxia confirm 路径仍可用。
- D2 的 `test_sync_agent_config.py` fixture 显式隔离 OPENCLAW_HOME、部署映射、配置及输出目录，独立配置 scripts 导入路径，消除测试收集顺序依赖；保留真实部署逻辑和原断言。
- 清理前逐项确认真实 `~/.openclaw` 下 256 项均为内容匹配的 SOUL、脚本链接、已知临时测试链接或空目录；无真实配置，随后删除这些测试产物，保留根目录。清理记录：`/home/ljw/boss-lab/BOS-16-cleanup.log`。
- 单文件 **1 passed**；反向收集相关测试 **24 passed**；全量 **79 passed in 4.55s**（原 78 + 新增 1；没有 skip/xfail/删除）。全量期间 inotify 变更事件 **0 bytes**，结束目录残留 **0 项**，根目录 mtime/ctime 不变。`compileall`、`git diff --check` 均通过。
- D3/D4 已更正：控制器固定预期检查、产物哈希自复算不等于独立账本审计；1 个文件变更事件含 2 个 add，不是 2 个事件。
- 完整复现/验收：`/home/ljw/boss-lab/BOS-16-REPORT.md`。本次实际模型为 Codex / penguinapi / gpt-6.1-sol；无新增模型执行、外部服务、采购或实例配置变更。D1–D4 无未完成项；不包含既存手工损坏数据库的自动迁移，也未授权启动 P1。下列原 P0 限制继续有效。

- PG/Redis 异步后端、通知平台、Docker、OpenClaw 真实执行链未运行；静态阅读不能当成可靠性验证。
- pi 适配器、多 agent 自主规划与审议、真实人类审批、双层金额预算/长运行中途 hard-stop、可扩编组织模型未实现。
- force-release、lease/fencing、父进程 kill 后自动修复、正式导入/导出/恢复演练未实现；SQLite 可重开与独占冲突测试不等于灾难恢复。
- 原 UI 所有面板/交互未做浏览器测试。P0 禁止旧写 API，部分面板返回 404，不能从看板按钮启动任务。没有生产级 artifact HTTP 服务。
- 本次没有对 Paperclip 全代码做逐行审计或导出实现；矩阵的 Paperclip 列使用任务给定对照基线，融合是接口/治理模式借鉴，不是复制其实例。

## 已知缺陷 / 接受限制

- 当前本地 CLI 角色身份是可信控制器参数，不是加密认证的独立 agent 身份；工作目录与 sandbox 参数不证明宿主强隔离。只适用于合成内部用例，不允许公开部署或不可信 prompt。
- Codex 沿用已有用户配置；首次运行 stderr 记录一次 `chatgpt.com/backend-api/ps/mcp` 连接失败（CLI 自身可选连接初始化），但业务任务正常完成。未调用业务外部服务工具；不能声称子进程具备断网隔离。P1 必须隔离可选 MCP/插件与环境变量后再验收安全边界。
- 未把 CLI 0 直接当业务成功，但控制器本身仍是可信单机代码，调用者若直接写 DB/调用底层 `finish` 可伪造元数据；不是多租户防篡改系统。
- 运行失败停在 Doing，留失败 run 和原因，需操作员决策；不具备持久化自动恢复。父进程被 SIGKILL 可能留下 running，不能盲目 force-release 后重放。
- 通信 ACL 读的是 `agents.json`（与安装矩阵核对），角色固定工部；不是可配置组织注册中心。上游关于统一审核/全审计的缺陷保留在 AUDIT.md，没有在 P0 偷换制度。
- Git smart HTTP clone 失败，交付为经全对象 hash 验证的官方 API 镜像恢复；这一取得方式与直接 clone 的差异明确保留。

## 最终处置与 P1 建议

P0 本 issue 可置 **done**：没有需要董事会在本 issue 决定的挂起项，也没有后台服务等待。所有运行窗口均已结束。下一步仅建议，未自动创建或批准 P1：

1. 先统一唯一 mutation 服务，修正上游入口审核/确认权限/审计不一致，引入真正人类审批记录。
2. 实现公司+agent 双层预算预留与 unknown-cost 默认拒绝，隔离 CLI 的可选 MCP、凭证和执行权限。
3. 做 lease/fencing、force-release 与 kill/restart 恢复演练，再做导入导出和多机事件消费。

复现命令见 `README.md`。本次执行通道 **Codex / penguinapi / gpt-6.1-sol**；没有换模型凑成功。金额仍 unknown，不作精确成本承诺。
