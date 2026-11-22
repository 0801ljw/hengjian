# P2-0 Ed25519 验收记录（2026-11-06）

## 已实现

- `approvals.py` 删除 HMAC 签名能力，只用系统 OpenSSL 验证 Ed25519。document 的原 canonical JSON 字节不变。
- envelope 显式 `alg=ed25519`；签名为 64 字节的小写 hex（128 字符）。缺算法、旧 HMAC、未知算法、异常格式均 fail closed。
- 控制器默认公钥：`/home/ljw/.config/fusion/board_ed25519.pub.pem`。验签端显式限制 PUBLIC KEY PEM 和 Ed25519 SPKI，拒绝私钥 PEM/非 Ed25519 公钥；公钥配置不由 envelope 决定。
- `board-sign.py` 为独立单文件人工签名工具；`board.py` 生成 document 后复用其逻辑。没有接入 controller、dashboard 或 runtime 自动流程。两者拒绝已识别的 agent 环境。
- 保留有效期、不可变请求、payload/revision/scope/actor/run 绑定、独立确认和幂等行为。未改状态机、权限矩阵、agents.json；未删除或跳过测试。
- DESIGN.md P2-0 节和 README 说明密钥分工、迁移方式、人工运行方法与剩余边界。旧 HMAC 审批须由董事会重新审批，不自动重签，不修改现存数据库。

## 已验证

环境：Linux，系统 OpenSSL `3.5.7`，Python + 已缓存 pytest，无联网安装。

```bash
cd /home/ljw/boss-lab/edict
PYTHONPATH=$PWD uv tool run --offline --from pytest pytest tests/ fusion/tests/ -q
git diff --check
```

迁移前基线 **105 passed in 8.66s**；迁移后 **148 passed in 12.61s**，无 skip（原 105 条保留，新增 43 条）。`git diff --check` 无错误。

| 行为 | 证据 |
| --- | --- |
| 有效签名通过；只留测试公钥也能验证 | `test_valid_signature_public_key_only_and_canonical_order`（签后删除测试私钥） |
| 全部 15 个 document 顶层字段逐一篡改拒绝 | `test_every_document_field_is_signed`（15 个参数用例） |
| 错公钥拒绝且无写入 | `test_wrong_public_key_rejected_without_writes` |
| 旧 HMAC/未知或缺算法/异常签名/NaN 拒绝 | `test_bad_envelope_fails_closed` |
| 私钥 PEM、RSA、旧对称 key、丢失/损坏公钥拒绝 | `test_verifier_rejects_non_ed25519_public_files` |
| OpenSSL 丢失或超时关闭审批路径 | `test_openssl_unavailable_or_timeout_fails_closed` |
| 签名有效但过期/同 run 自审/载荷改变/转换 scope 改变仍拒绝 | `test_signed_invalid_decision_still_rejected` |
| 消费与 confirm 时再次检查过期/载荷绑定 | 原 `test_expiry_checked_on_consumption_and_confirm`、`test_pending_payload_edit_invalidates_confirmation_even_when_hash_recomputed` |
| actor 自审/AI 冒充/请求主体隔离保留 | 原 `test_ai_self_review_rejected`、`test_agent_cannot_sign_as_board_even_with_valid_fixture_signature`、`test_same_run_human_approval_rejected` |
| SQLite 内签名记录被改后消费拒绝 | `test_stored_signature_is_rechecked_on_consumption` |
| 两个人工工具临时测试密钥签名→实际导入成功 | `test_manual_tools_test_keys_round_trip`（standalone/board） |
| 单文件拷离仓库仍可运行，canonical 字节与控制器完全一致 | 同上 standalone 用例（含中文、缩进/字段顺序变化、确定性签名比对） |
| agent 环境拒绝两个人工工具 | `test_standalone_signer_refuses_agent_environment_before_reading_key` 及原 controller/board subprocess 用例 |
| 控制器默认指定主机公钥 | `test_controller_defaults_to_host_public_key` |

人工工具测试只在隔离子进程中模拟离线人类环境，使用临时生成且明确 TEST-ONLY 的密钥；没有获取或使用真实董事会私钥，没有产生真实批准。

实际主机公钥只做公开材料检查：

```bash
openssl pkey -pubin -in /home/ljw/.config/fusion/board_ed25519.pub.pem -outform DER | sha256sum
```

结果：`6505337d3e5dd574df7a9ffb1292897fe50ec0e88dc0872cf985092642d475e9`，与董事会给出的 SHA256 前缀一致。

## 未验证

- 未在董事会 Mac 上运行；未使用真实私钥验证往返；未证明 Mac 私钥保管与人工传递过程安全。
- 未部署或改动实际运行数据库；未读取旧 HMAC 密钥或扫描私钥。没有撤销、删除真实 key 或重签旧记录。
- 未验证其他 OpenSSL/macOS 版本。工具要求支持 Ed25519 `pkeyutl -rawin` 的 OpenSSL，不保证系统 LibreSSL 兼容。
- 未做 OS 账户/文件权限隔离、硬件密钥、多租户身份认证、密钥轮换与可信时钟部署验证。
- 未调用真实模型、外部付费服务、外部联系人；未改 Paperclip 实例配置/实现。只使用当前 issue 的正常交付和状态 API。

## 已知缺陷与边界

- 同 UID/管理员仍可改 SQLite、换公钥/代码/OpenSSL/PATH 或篡改时间。Ed25519 **只解决真实签名密钥不在主机**，不等于主机隔离或完整的人类身份认证。
- 环境变量检测只是误用防护，恶意进程可清除它；真实安全边界是董事会私钥离机及可信验签端。
- 人工工具目前只支持无口令 Ed25519 PEM，不支持加密 PEM/Keychain/硬件密钥；没有自动密钥分发、吊销/轮换机制。
- 本机 OpenSSL 实测 `pkey -pubin` 仍接受测试私钥 PEM；已通过显式 PUBLIC KEY PEM 约束修复，不能仅依赖命令行参数表达的意图。
- 每次验证调用本机 OpenSSL，异常/10 秒超时拒绝；没有签名算法降级路径。临时验签文件受 TemporaryDirectory 的私有目录权限保护并自动清理，不含私钥。
- 不自动升级旧 HMAC envelope；历史记录保留，待执行操作需要董事会离线重批。预算硬停、lease/fencing、恢复演练不属于本次实现。

## 人工运行与交付

工作目录：`/home/ljw/boss-lab/edict`，分支 `fusion/p0-local-runtime`。

仅在董事会 Mac（不要在主机放真实私钥）：

```bash
python3 board-sign.py --document approval-document.json --private-key /offline/board-private.pem
```

输出为 envelope JSON；人工保存并拷回主机，再执行：

```bash
python3 -m fusion.control --home fusion/var import-approval envelope.json
```

冻结请求签名入口见 README 的 `python3 -m fusion.board` 用法。真实签名不是本次完成门槛，也未创建需要董事会答复的挂起项。
