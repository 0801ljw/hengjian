# P2-1 预算准入交付

交付目录：`/home/ljw/boss-lab/edict/fusion/`。仅修改本地 Fusion 项目，未调用 Paperclip API、
未改实例、未调用模型、未采购、未联系外部对象。所有签名测试使用临时 TEST-ONLY 密钥。

## 运行与验证

在 `/home/ljw/boss-lab/edict` 执行（使用已有缓存，禁止联网安装）：

```bash
/home/ljw/.local/bin/uv run --offline --no-project --with pytest python -m pytest fusion/tests tests -q
/home/ljw/.local/bin/uv run --offline --no-project --with pytest python -m pytest fusion/tests/test_budget.py -q
git diff --check
git status --short
```

原始提交 `5f3814d` 基线：148 passed。新增预算专项 30 条，覆盖 UTC 月边界事务内一致性。
最终验收记录：上述全量命令 **178 passed in 17.02s**；`git diff --check` 通过。
Python patterns/testing 技能用于整数金额校验、真实 SQLite 事务回归、真实进程崩溃及并发测试设计。
测试调用本地 Python 子进程和 OpenSSL，不运行 Codex 或其他模型。

代码入口：`budget.py`（预算策略/预留/结算/恢复/审计）、`mutate.py`（唯一 dispatch mutation）、
`core.py`（checkout 委托与 finish 事务）、`control.py`（策略导入与只读查询）。

## 准入、查账与策略配置

默认 provider=penguinapi，未定价且默认 block。真实调用方必须先成功 checkout，才能执行 adapter：

```python
from pathlib import Path
from fusion.core import Store

store = Store(Path("/absolute/path/to/company-data"), Path("/absolute/path/to/board-public.pem"))
# penguinapi 无法定价，须董事会策略允许；以下不会发起模型调用。
run_id = store.checkout("EXISTING-DOING-TASK")
# 对有可信报价的未来适配器，用整数分预留；provider 名称不自动查询/证明价格。
run_id = store.checkout("EXISTING-DOING-TASK", provider="trusted-priced-provider", estimated_cents=50)
# 真实执行完成后，不论成功失败都交付真实计量；计量缺失省略 actual_cents，绝不编造 0。
store.finish(run_id, {"status": "failed", "actual_cents": 20})
print(store.budget_status())
```

上述两次 checkout 是互斥场景示例，不应对同一个正在运行的任务连续执行。
成功结果还须提供现有 `artifact`，并由同一事务调用 output mutation。

CLI 使用可用 Python 解释器（本环境可通过同一 `uv run --offline --no-project --with pytest python` 前缀运行）：

```bash
python3 -m fusion.control --home /absolute/path/to/company-data budget-status
python3 -m fusion.control --home /absolute/path/to/company-data budget-ledger
python3 -m fusion.control --home /absolute/path/to/company-data --board-key-file /absolute/path/to/board-public.pem import-budget-policy /absolute/path/to/signed-policy.json
```

`budget-ledger` 返回当月 status、全部 reservation、按序排列的预算事件（含签署策略、override 原文）。
查未知支出必须同时看 `unknown_spend`，不能只看 `known_spend_cents`。
只读 CLI 初始化 Store 会执行启动恢复；不对现有运行中的生产库做无授权演练。

### 离线预算策略 document

与 P2-0 相同的 Ed25519 envelope：`{"alg":"ed25519","document":{...},"signature":"128 hex chars"}`。
董事会在独立人工终端核对、签署 document，运行主机仅配置公钥；不得把真实私钥复制到控制器。
现有 `board-sign.py --document ... --private-key ...` 可签任意经人工核对的 JSON document，
其 agent runtime 禁用规则保持不变，本次没有使用该工具签发真实批准。

document 完整 schema（以下为未签署模板，不构成授权；时间需人工填写有效范围）：

```json
{
  "id": "policy-unique-id",
  "kind": "budget_policy",
  "principal": "board",
  "actor": "board:identified-human",
  "created_at": "2026-11-06T00:00:00+00:00",
  "expires_at": "2026-11-07T00:00:00+00:00",
  "revision": 1,
  "window": "calendar_month_utc",
  "company_cents": 10000,
  "agents": {"gongbu": 3000},
  "unpriced": "block"
}
```

调整额度或改为 allow，必须新签署、递增 revision；完全相同 envelope 重试幂等。
导入授权过期不让已安装策略消失。当前只支持现有工部运行主体，单 Store home 对应单公司。
无持久策略时保留历史显式 `checkout(task, True)` 作为当次 allow 配置；持久 block 不接受此 flag 绕过。

### 一次性 override document

不是普通任务 approval。需单独签署以下完整字段，作为 checkout 的 `budget_override=envelope`，
并显式传 `run_id`；绑定月、任务、run、估算额，过期重验。两个 extra 都是整数分，不省略任何一层。

```json
{
  "id": "override-unique-id",
  "kind": "budget_override",
  "principal": "board",
  "actor": "board:identified-human",
  "created_at": "2026-11-06T00:00:00+00:00",
  "expires_at": "2026-11-07T00:00:00+00:00",
  "run_id": "preselected-run-id",
  "task_id": "EXISTING-DOING-TASK",
  "month": "2026-11",
  "estimated_cents": 120,
  "company_extra_cents": 20,
  "agent_extra_cents": 20
}
```

有效上限为该次 base cap + extra；仍须满足两层预算不等式。任一层不足，整笔 override 消费与 run 写入回滚。
只作用一次，不改变持久上限；实际结算照常累计，后续派发没有自动豁免。不能用于未定价运行。

## 验收覆盖

- 双层额度四种组合：只公司足、只 agent 足、两者不足均拒；两者足准入。
- 80% 双层 warn 事件；恰好填满余额允许，当前 100% 后新派发即使报价 0 也拒。
- penguinapi + 0 报价仍为 unpriced；默认 block；allow 每次记录 unknown；已安装 block 优先于旧 flag。
- unpriced allow 不绕过已触顶额度；汇总只含已知费用，显式 unknown。
- 真实 SQLite 并发两任务各预留 60、总额 100：恰好一笔准入。
- 预留 90、实际 30 释放差额；超估算照实计入并产生 incident；缺计量保留 unknown；重复结算拒绝。
- output 失败后 run 完结/结算整体回滚；直接 mutation/内部连接异常也无部分写入。
- 真实子进程 `os._exit(23)` 后重启：orphaned、释放额度、保持 unknown、可再准入；存活子进程不被误回收。
- UTC 月切换与跨月结算归属；负数/浮点/bool/字符串/溢出拒绝。
- 签名篡改拒绝、策略 revision 防回放；一次性 override 正常记录、绑定错拒绝、两层任一不足拒绝、run 重用拒绝。
- CLI 可查持久账本；adapter 不能篡改准入元数据或把 unpriced 费用写成 0。

恢复边界详见 DESIGN P2-1：Linux 单机 controller 生命周期，不是多机 lease/fencing。
孤儿运行不自动重放；释放占用不意味着 provider 未收费。任意宿主同 UID/管理员可篡改代码/数据库的既有风险未改变。

## 最终处置

工程交付完成后建议下一状态：`done`，由董事会验收并关闭。
按本任务明确禁令未调用 Paperclip API，因此没有写入 issue comment、上传 work product 或修改 issue 状态；
不得据此宣称平台已收口。**无法收口，需董事会关闭。** 董事会等待本地测试、提交与路径凭证；不需要追加采购或外部授权。

最终测试/提交凭证见本次最终回复；提交使用要求的 Paperclip co-author trailer。
