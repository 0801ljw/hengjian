# P2-2 交付报告

工程交付完成，等待董事会验收关闭。任务：[BOS-29](/BOS/issues/BOS-29)。

## 实现

- 代码目录：`/home/ljw/boss-lab/edict/fusion/`。
- `leases.py`：单机 SQLite lease，owner/run/PID/进程身份，30 秒有效期、10 秒后台续约；
  `BEGIN IMMEDIATE` 内获取、续约、释放、撤销；fencing_token 单调递增，释放不删除 fence。
- `mutate.py`：lease 挂在唯一 dispatch admission 的预算旁边；lease、run、预算预留、
  override、事件在同一事务/savepoint，异常全部回滚。任务写入先校验 token，再执行 CAS/审批。
- `core.py` / `budget.py`：finish 和低层 reserve/settle 校验 token；过期/错误 owner 拒绝；
  runtime 不能篡改准入 token；startup 只回收死亡 owner，先增 fence 再撤销旧 run 与预留。
- force-release 必须 `os.kill(pid, 0)` 明确确认 PID 不存在；活 PID、权限不明都拒绝，
  过期也不能绕过。普通过期接管允许新 fence，旧 holder 不能再进行受管状态写入。
- `execution_commits`：任务级唯一成功执行提交，与 output、finish、预算结算同事务；
  已提交任务不允许再次 dispatch，已提交 output 不能直接覆盖。
- `control.py lease-status` 可检查租约与成功提交。`DESIGN.md` P2-2 节含时序图、API 使用边界、
  事务及恢复语义。未修改 `_VALID_TRANSITIONS` / `AGENT_POLICY` / `agents.json`。

## 测试

在 `/home/ljw/boss-lab/edict` 运行，使用已有离线缓存：

```bash
/home/ljw/.local/bin/uv run --offline --no-project --with pytest python -m pytest fusion/tests tests -q
/home/ljw/.local/bin/uv run --offline --no-project --with pytest python -m pytest fusion/tests/test_leases.py -q
git diff --check
git status --short
git log -1 --oneline
```

基线 `a57287f`：178 passed in 18.86s。最终：**209 passed in 20.29s**，原有 178 条全绿，
新增 31 条专项。`git diff --check` 通过。实现、脚本、测试、设计与本报告同一 git 提交；
具体提交 hash 见交付评论 / `git log -1`，提交后工作区应为空并在交付前核验。

覆盖：正常获取/续约/释放、未过期竞争拒绝、过期接管 token 递增、六类任务 mutation 的 stale
拒绝、finish/renew/release/低层结算 stale 拒绝、缺失/未来/类型错误 token 拒绝；
活 PID 即使过期也拒绝 force、真实死亡 PID 允许 force、真实 SIGKILL 后 startup 回收；
预算失败和内部捕获异常后提交均无部分状态、接管后预算失败恢复原租约和预留、
预留成功后故障回滚、线程及真实双进程竞争只有一个成功、真实后台 heartbeat、CLI 查账、
单次成功提交及重复 output/dispatch 拒绝。

Python patterns/testing 与 verification-loop 技能用于明确 token 类型、真实 SQLite 事务、
异常路径、进程清理和全量回归，不引入新依赖或通用覆盖率门槛。

## 真实 kill -9 演练

脚本：`/home/ljw/boss-lab/edict/fusion/lease_drill.py`。每次创建新的独立目录，不覆盖现有库。

```bash
cd /home/ljw/boss-lab/edict
/home/ljw/.local/bin/uv run --offline --no-project --with pytest python -m fusion.lease_drill --output /home/ljw/boss-lab/BOS-29-drill
```

最终版本证据目录：`/home/ljw/boss-lab/BOS-29-drill/kill-restart-zlxmqcxa/`。
其中 `output.log` 为演练输出，`evidence.json` 保存快照/租约/提交/事件，`fusion.sqlite3` 可查询。
查询命令（Store 初始化会做安全死亡回收）：

```bash
/home/ljw/.local/bin/uv run --offline --no-project --with pytest python -m fusion.control --home /home/ljw/boss-lab/BOS-29-drill/kill-restart-zlxmqcxa lease-status
```

实际记录：

```text
first_admitted: PID=236803, run=a3e5cd187f7948878cac235deeb3a462, token=1
live_force_release_refused: OWNER_ALIVE for PID 236803
external_SIGKILL: PID=236803, returncode=-9
restarted_and_completed: PID=236807, run=25cc693a55e241f5b06fe7fc53b9c515, token=3
resurrected finish: STALE_FENCING_TOKEN supplied=1 current=3
resurrected task_mutation: STALE_FENCING_TOKEN supplied=1 current=3
duplicate_dispatch_refused
verified: committed_executions=1, successful_runs=1, run_finished_events=1,
          attempts=2, duplicate_side_effects=0
```

两条 runs 是保留的尝试审计（orphaned + succeeded），不是两次成功执行。
任务的成功执行提交记录、成功 run、结果完结事件均只有一条；不删除崩溃审计来伪装单次尝试。
演练真实启动两执行者、父进程外部发送 SIGKILL 并 wait，再启动恢复者；不是 mock。
模拟副作用仅为 SQLite 内的结果提交，不调用模型/外部服务。

## 剩余边界与成本

仅单机 Linux SQLite，不是多机锁。lease/fencing 保证受管写入排他，不保证旧计算立刻停止；
外部 API/文件副作用还需下游支持 fencing 或幂等键，不能声称任意外部 exactly-once。
主机时钟跳变影响过期；同 UID/管理员仍可改 DB/代码或读取 token，fence 不是身份认证。
升级已有无 lease 的活 controller 应先停旧进程，不无条件抢占；原预算历史保持可审计。
未知费用仍标 unknown，不把回收预留当作实际免费。业务任务仍遵守原状态流转及审批，
本演练的 succeeded 不冒充业务 Done/董事会批准。

没有采购、没有付费服务调用、没有新增模型调用、没有联系外部对象。
仅修改本地 Fusion 项目和新增演练目录；未改 Paperclip 实例/配置/生产数据库。
只按任务要求向本 issue 发布交付评论，不调用 issue 状态/关闭 API。

## 最终处置

交付完成。下一状态应为 **done**，等待董事会核对提交及上述证据后关闭；Atlas 不等待新的技术输入，
也不另建需要人类答复的挂起项。按本任务明确要求，不自行通过 Paperclip API 改状态或收口。

需董事会关闭
