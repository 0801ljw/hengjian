# 衡鉴·点将台 同步契约 v0（M0）

> 状态：M0 契约，个人优先。主节点是权威；Hub 是 E2E 中继/备份；手机是只读/呈案/签批伴侣。
> 配套：[SYNC-ARCHITECTURE.md](SYNC-ARCHITECTURE.md)。参考 OpenAgents 的 Khala Sync 契约与本地优先模型。

## 1. 范围与非目标

**范围（个人 workspace）**
- 一个账号一个个人 workspace；默认一个主节点（桌面）。
- 同步：案卷/运行/决策事件/审批/成物元数据/派生统计。
- 客户端：桌面主节点、手机伴侣、DSH 插件（薄客户端）。

**非目标**
- 手机执行 worker；手机持有主节点管理凭据。
- 同步私钥、模型 API key、token、原始工作区文件/PTY。
- 团队/RBAC/多主节点双写；首版一个主节点。

## 2. 标识与权威

| 对象 | 格式 | 权威 |
|---|---|---|
| `workspace_id` | `w_` + ULID | 主节点 |
| `device_id` | `d_` + ULID | 账号/设备注册 |
| `case_id` | `AI-` + 32 hex | 主节点 |
| `run_id` | 32 hex | 主节点 |
| `entity_id` | 各实体既有 id | 主节点 |
| `mutation_id` | UUIDv7 | 客户端生成 |
| `seq` | 单调整数 | 主节点分配 |

- 只有主节点能写业务状态机；手机/其他桌面只发命令。
- Hub 只追加密文记录，不改写、不解释业务。

## 3. 实体与字段

### 3.1 同步实体

| entity_type | 关键字段 | 说明 |
|---|---|---|
| `case` | id, state, title, revision, updated_at, tombstone | 案卷摘要；决策链走 event |
| `run` | id, case_id, status, phase, started_at, ended_at | 运行摘要 |
| `event` | seq, case_id, run_id, actor, action, payload_hash, at | 追加事件；不可改 |
| `approval` | id, case_id, scope, payload_sha256, signer_device, status | 签批记录 |
| `artifact` | path, sha256, bytes, mime, primary, content_ref | 元数据；内容按需 |
| `merit` | role,参与/完成/失败/返工 | 派生投影，可重算 |
| `device` | device_id, name, public_key, status | 设备注册/吊销 |

### 3.2 只在本地的状态
- 私钥、模型 key、token、供应商 session。
- 原始 provider SSE/JSONL、工作区文件内容、PTY。
- 窗口/主题/滚动/草稿等 presentation state。

## 4. 事件记录（本地 journal）

每条记录是不可变事实；`seq` 按 workspace 单调递增。

```json
{
  "seq": 1024,
  "workspace_id": "w_...",
  "device_id": "d_...",
  "entity_type": "case",
  "entity_id": "AI-...",
  "version": 7,
  "mutation_id": "019...",
  "op": "upsert",
  "payload": "{\"state\":\"Review\",...}",
  "prev_hash": "sha256:...",
  "hash": "sha256:..."
}
```

约束：
- `mutation_id` 全局唯一；重复提交返回原记录（幂等）。
- 同一 `(entity_type, entity_id)` 的 `version` 必须严格递增；`tombstone` 后不再 upsert。
- `hash = sha256(canonical(record without hash) + prev_hash)`；空 journal 的 `prev_hash = "GENESIS"`。
- `seq` 断档表示丢失，客户端必须 `must_refetch`。
- 本地 journal 是主节点事实；Hub 存储同一记录的密文副本。

## 5. Hub 协议

### 5.1 命令（客户端 → 主节点）

```json
{
  "workspace_id": "w_...",
  "device_id": "d_...",
  "mutation_id": "uuid",
  "command": "submit_case|approve|cancel|confirm",
  "payload_ciphertext": "base64",
  "nonce": "base64",
  "key_id": "k_...",
  "sent_at": "RFC3339"
}
```

- `mutation_id` 幂等；主节点返回 `accepted|rejected|duplicate|unknown_pending_reconcile`。
- 命令只入队；状态机仍由主节点执行。
- 送达（delivery）与生效（effective）分开记录；超时不能当成功。

### 5.2 事件拉取

- `GET /sync/events?workspace=...&after=<seq>&limit=...`
- 返回 `{events, next_cursor, has_more, retention_floor}`。
- `after < retention_floor` → `409 must_refetch`，客户端拉快照。
- `GET /sync/snapshot?workspace=...` → 每个实体最新版本 + `snapshot_cursor`。

### 5.3 相位

`connected | stale | reconnecting | must_refetch | revoked`。
客户端 UI 必须显示相位；缓存行不能显示为实时。

### 5.4 保留与压缩

- Hub 保留 N 天/事件数；主节点可 compact 为快照，但保留审计链。
- 快照包含 `snapshot_cursor`；客户端从该 cursor 继续增量。

## 6. E2E 加密

- 账号根密钥（仅客户端）派生 workspace 密钥；每设备有包装密钥。
- 记录载荷用 AEAD（XChaCha20-Poly1305 或 AES-GCM）；`nonce` 每密钥唯一。
- Hub 只见 `workspace_id/device_id/seq/entity_type/version` 等最小元数据。
- `key_id` 轮换：新设备加入用现有设备/恢复码授权；吊销设备后拒绝其 `key_id`。
- 私钥/签批密钥不进入 E2E 同步；签批签名单独上送。

## 7. 签批设备

- 每设备生成 Ed25519（Secure Enclave/Keystore）；注册公钥。
- 审批签名绑定：
  `account_id + device_id + case_id + revision + payload_sha256 + policy_version`。
- 默认策略：一台主签设备；手机可会签，高风险仍要求主签。
- 设备吊销立即失效；旧签名不能重放。

## 8. 错误码

| 错误 | 含义 |
|---|---|
| `duplicate` | mutation_id 已处理，返回原结果 |
| `conflict` | 版本/状态冲突，需刷新 |
| `must_refetch` | cursor 超出保留窗口 |
| `revoked` | 设备/密钥已吊销 |
| `unknown_pending_reconcile` | 传输超时，结果未知；不得显示成功 |
| `unauthorized` | 无有效设备/工作区 |

## 9. 验收 fixtures

- 两端重启：不重复、不丢事件。
- 重复 `mutation_id`：只生效一次。
- cursor 断档：`must_refetch` + 快照恢复。
- 设备吊销：旧签名/token 立即失败。
- 命令超时：`unknown_pending_reconcile`，不显示成功。
- 离线队列恢复：按序生效。
- tombstone：不复活。
- 哈希链：篡改任一记录，`verify_chain` 失败。
- 时钟偏移/网络抖动/主节点休眠。

## 10. 实现状态

- M0：契约 + 本地 journal（`72e6a61`）
- M1-a：只读投影（`fc2f61e`）
- M1-b：`--sync` 增量投影 + `/api/sync/snapshot` / `/api/sync/events`（`9e1def0`）

## 11. M0 实现顺序

1. `fusion/sync_journal.py`：本地 append-only journal + 幂等 + hash chain + cursor/snapshot（无网络、无加密）。
2. 测试：`test_sync_journal.py`。
3. 把现有 `events` 投影到 journal（只读镜像，不改状态机）——已实现 `fusion sync-project` / `fc2f61e`。
4. 再接 Hub/加密/手机；M0 不碰它们。
