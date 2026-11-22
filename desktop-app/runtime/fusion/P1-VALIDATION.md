# P1-1 交付与验收证据

日期：2026-11-05。任务 BOS-17；执行者 Atlas。
代码库：`/home/ljw/boss-lab/edict`，分支 `fusion/p0-local-runtime`。
实际运行模型来自本次 Paperclip run 的 `usageJson.model`：`gpt-6.1-sol`；provider 字段为 `openai`。
没有新启动模型执行、招聘、采购、付费服务调用、外部人员联络或 Paperclip 配置变更。

## 已实现

- 先冻结设计，再写代码：设计独立提交 `4dabee6`，DESIGN.md SHA256 `f550efb84e7b49e4714fa6d866041233f914079a2a714f016b00a213d9f0c3b7`。实现提交在其后，可通过 `git log --oneline -3` 核验。
- `fusion/mutate.py::apply_mutation` 唯一写 tasks 表；创建、转换、方案/金额修改、运行产物写回均经同一服务。Store.create/move 只做委托。事件、任务 revision、approval 消费同事务提交。
- 旧入口选择 **(b) 硬拒绝**：旧 CLI 所有写命令 exit 2 / LEGACY_MUTATION_DISABLED；原版和 Fusion dashboard POST 405，审批没有 HTTP 写接口。原模块函数只保留上游离线 JSON 算法回归；Fusion 投影的 atomic_json_update 拒绝写入，JSON 永不反向导入 SQLite。
- 独立 SQLite decisions 表区分 review_decision 与 approval。门下只生成制度审核；人类批准由独立离线操作员签名并由控制器验签，不接受 actor="board" 字符串作为认证。批准与不可变 mutation_requests 绑定，不能重新声明发起人。
- 审批绑定业务 payload SHA256、revision、actor/run、created_at、expires_at、转换 scope；同 actor 或同 run 自审拒绝。金额/方案/产物或 revision 变动使审批失效；消费时和 PendingConfirm 确认时都重新检查有效期。
- 高风险 Review→Done 在入 PendingConfirm 前必须具有有效 board approval；原 confirm_by 确认仍保留。直接指定 PendingConfirm 被拒绝，伪造 pending_confirm 元数据不足以收口。门下 confirm 写 review_decision，不写 approval。
- CAS expected_revision + BEGIN IMMEDIATE 提供 409 冲突；同 approval_id、同请求精确重试返回首次结果，不追加事件或副作用；不同请求复用返回 409。最终确认后重试仍返回首次 PendingConfirm 结果，当前状态须读取 snapshot。
- 新 CLI `fusion.control` 提供本地创建/变更/冻结请求/导入签名批准；`fusion.board` 为独立人类终端工具，在 agent runtime 内拒绝执行。旧 P0 真实模型 demo 已停用，避免运行模型后才发现缺审批。
- `_VALID_TRANSITIONS`、AGENT_POLICY、agents.json 未削弱；P0 D1 防绕过测试保留并增强。没有删除或 skip 测试。两条既有确认测试增加独立 board fixture，仍保留原终态、防自审和防绕过断言。

## 已验证

最终全量：**105 passed in 7.74s**（原基线 79；净增 26；无 skip/xfail）。所有测试只使用本地 fixture 和故障注入，不调用真实模型。git diff --check 通过，control --help 冒烟通过。

运行方式：

```bash
cd /home/ljw/boss-lab/edict
PYTHONPATH=$PWD uv tool run --offline --from pytest pytest tests/ fusion/tests/ -q
git diff --check
git log --oneline -3
git status --short
python3 -m fusion.control --help
```

主要证据在 `fusion/tests/test_mutation.py`：

| 验收点 | 测试 |
| --- | --- |
| 老 CLI 状态/完成/确认/阻塞/创建全被拒绝，投影写函数也拒绝 | test_old_cli_hard_denied_and_direct_functions_cannot_write_projection（5 参数） |
| 原版 dashboard POST 405、权威状态不变 | test_original_dashboard_http_also_denies_post |
| 直接写 JSON 无法改权威状态，Fusion HTTP 读取并修复投影，POST 无效 | test_direct_json_tamper_never_changes_state_and_dashboard_repairs_it |
| 同 actor / 同 run AI 自审拒绝 | test_ai_self_review_rejected（2 参数） |
| 同 run 人类批准拒绝，agent 不能冒充 board，错误签名拒绝 | test_same_run_human_approval_rejected / test_agent_cannot_sign_as_board_even_with_valid_fixture_signature / test_unsigned_agent_cannot_create_approval |
| 金额/方案变化使旧审批失效 | test_payload_or_amount_change_invalidates_approval（2 参数） |
| 进入 PendingConfirm 后 payload 改动也拒绝确认 | test_pending_payload_edit_invalidates_confirmation_even_when_hash_recomputed |
| 审批过期/未来时间/无时区拒绝，确认时复查 | test_expiry_checked_on_consumption_and_confirm / test_invalid_future_or_expired_signed_approval_rejected（3 参数） |
| 两个 Store 并发同 revision：精确一个 200、一个 409，无重复事件 | test_concurrent_task_mutations_exactly_one_cas_winner |
| approval_id 幂等、改请求冲突、独立确认、重启持久化 | test_idempotent_approval_and_independent_confirmation |
| 发起 run、scope、reason 不可偷换 | test_scope_initiator_and_reason_are_immutable |
| 伪造 PendingConfirm 元数据无效；未配置 board key 失败关闭 | test_pending_metadata_without_approval_is_not_authority / test_no_board_key_fails_closed_and_wrong_scope_is_rejected |
| 本地 CLI 端到端与 409；agent runtime 无法运行 board 签名工具 | test_controller_cli_create_cas_and_board_signing_disabled_in_agent_runtime |
| run 产物通过统一服务写回并推进 revision | test_runtime_output_write_uses_revisioned_mutation_service |

P0 状态/权限/通信矩阵、D1 防 PendingConfirm 绕过、并发 checkout、失败/超时保持 Doing、无 OpenClaw dashboard 等既有测试继续通过。首次非离线 pytest 启动因 PyPI 索引连接超时失败，之后仅使用已有本机缓存离线执行。

## 未验证

- 没有签发真实董事会批准、没有新真实 LLM 任务；签名验收使用专属测试 fixture 密钥，不冒称真实人类授权。
- 没有生产多租户/不可信任务隔离、真实人类身份系统、跨机器并发或宿主攻击演练。
- 没有对 P0 现存运行账本做破坏性迁移/恢复；旧记录 revision 缺失按 0 处理，无审批元数据的旧 PendingConfirm 失败关闭。旧 JSON 不自动导入。

## 已知缺陷与边界

- 可信本地控制器模型：actor/run 必须由可信操作员提供，HMAC key 路径是可信配置。持有同 UID/管理员权限的人可直接改 SQLite、替换代码或读密钥；HMAC 和 CLI 环境检查不是 OS 安全边界。生产需独立凭证验证与进程/文件权限隔离，当前不能公开部署。
- 为保留上游算法回归，旧模块函数仍可操作与 Fusion 无关的离线 JSON 文件；它们不能写 SQLite，受支持的旧 CLI 与 HTTP 写入口已硬关闭。未声称统一仓库中独立 Postgres 后端的存储域。
- 原 HIGH_RISK_TRANSITIONS 的 Doing→Cancelled 受执行角色命令策略阻挡，Menxia→Cancelled 的旧 confirm_by=zhongshu 又无 confirm 权限。本次不扩宽 P0 权限，两个路径继续失败关闭；只对可达 Review→Done 证明完整合法成功链，不宣称所有取消路径可用。
- PendingConfirm 中审批过期或 payload 改动后确认拒绝；安全重提/撤回/修复尚无操作员流程。需要 P2 恢复设计，不能用手工修改状态绕过。
- 状态已提交但投影磁盘写失败时，调用方可能收到投影异常；SQLite 保持权威，可重新 project。只有 approval_id 请求提供持久化结果幂等回放，其他写请求须重读 revision；没有通用 outbox/恢复承诺。
- **预算硬停、lease/fencing、恢复演练、三省 AI 化仍全部是 P2，均未实现。**

## 最终处置

本次交付最终将 BOS-17 置为 **done**，Atlas 完成实现、测试、提交和证据归档；没有挂起问题、未决批准或等待人类回复的执行项。实际模型 **gpt-6.1-sol**。**不阻塞 P2 开始**；P2 必须继承上述安全/恢复边界，P1 完成不代表上线批准。
