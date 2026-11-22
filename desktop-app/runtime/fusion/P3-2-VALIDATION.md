# P3-2 验证报告（2026-11-06）

交付：导入导出 CLI、真实删除恢复脚本、按公司独立数据库/预算/租约/审批/审计、33 条专项测试。
安全审查促成数据成员白名单、不执行备份 SQL、公钥外部配置、审批消费链验证和跨 dashboard 全局数据隔离。
边界及剩余风险详见 [DESIGN.md](DESIGN.md) 的 P3-2 节。

## 运行

```bash
cd /home/ljw/boss-lab/edict
export PATH="$PWD/scripts:$PATH"
fusion export --home /path/to/instance --company alpha --out /outside/company.tar.gz
fusion import --bundle /outside/company.tar.gz --home /path/to/new-empty-instance \
  --company alpha --board-key-file /trusted/board-public.pem
python3 -m fusion.restore_drill --output /home/ljw/boss-lab/new-restore-evidence
PYTHONPATH=$PWD uv tool run --offline --from pytest pytest tests/ fusion/tests/ -q
```

`new-restore-evidence` 必须不存在；脚本只删除其自己创建的 original-store。
CLI 亦可使用 `python3 -m fusion`。正式迁移需停源控制器，密钥和外部工件另行管理。

## 真实进程演练

本次落盘运行：`/home/ljw/boss-lab/BOS-36-verified-drill/`。
仓库收录 `evidence/p3-2/drill.log`、`result.json`、测试公钥及 `tests.log`。
seed/export/import/inspect 都是真实子进程，无 mock。
驱动程序在 import 之前真实删除 original-store，日志明确记录 `exists_after=false`。
演练退出 0；result.status=passed，before==after，包括全量数据库状态 SHA256。

| 表 | 导出前 | 恢复后 |
| --- | ---: | ---: |
| tasks | 3 | 3 |
| events | 24 | 24 |
| runs | 1 | 1 |
| mutation_requests | 2 | 2 |
| decisions | 5 | 5 |
| approval_uses | 2 | 2 |
| budget_policy | 1 | 1 |
| budget_reservations | 1 | 1 |
| budget_events | 3 | 3 |
| budget_overrides | 0 | 0 |
| leases | 1 | 1 |
| execution_commits | 1 | 1 |
| 合计 | 44 | 44 |

任务状态为 T=Done、PENDING=PendingConfirm、OPEN=Taizi；恢复前后完全一致。
公司/工部预算上限 1000/500 分，已知支出 30 分、预留 0、unknown_spend=false，余额 970/470 分不变。
2 份 board approval、2 份冻结请求、2 次消费、3 份制度 review_decision 均保留；
已释放 lease 的 fencing token 和成功执行提交保留。外部工件文件不在公司迁移包范围。
所有签名仅使用临时 TEST-ONLY 测试密钥，seed 结束即删除私钥，不代表真实董事会批准。

## 测试覆盖

原 247 条回归不删改；新增专项覆盖：全表往返、default 旧布局兼容、
导入非空拒绝/空目录成功、损坏哈希/版本/计数/schema/外键/签名/审批消费链/伪造终态/列/公司标识/截断包拒绝，
失败无目标半状态且清理暂存、发布 rename 失败回滚、恶意 tar 路径不提取、导出不能覆盖数据库、
缺少可信公钥拒绝、静止条件检查、真实子进程删除恢复、公司路径穿越/符号链接拒绝、
至少三条查询路径隔离（get、snapshot、budget ledger）、跨公司审批/预算/运行引用拒绝、
相同 task/run ID 的独立租约/预算、双 dashboard 同进程状态隔离、CLI 参数前后位置及单公司导出。

最终全量结果：**280 passed in 31.25s**（原 247 + 新增 33），退出码 0。

不修改 `_VALID_TRANSITIONS`、预算/lease/审批安全策略及 mutation 服务。
未触碰 Paperclip 实例实现/配置；只通过任务控制 API 提交证据和最终处置。
未采购、未联系外部对象、无新增模型调用。

## 验收界限

这是可信本地控制器的逻辑多公司隔离，非生产多用户认证或宿主级安全隔离。
仅支持当前逻辑 schema v1、静止迁移及全新空目标，不支持运行中 lease 迁移、公司重命名或合并已有实例。
哈希用于损坏检测而非备份来源认证；共用公钥不提供公司级签名授权域。
不迁移私钥、运行进程、外部 artifact 和模型原始 transcript，不重签/续期审批。
完整测试数字见同目录 tests.log；git 提交见 `git log -1`，提交后工作区须为空。
