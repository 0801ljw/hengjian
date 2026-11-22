# 进程隔离说明（P5-4）

## 结论速查

| 层 | 状态 | 说明 |
|---|---|---|
| 环境变量白名单 | ✅ 已实现 | 默认拒绝，密钥不外泄 |
| 子进程 HOME 隔离 | ✅ 已实现 | `HOME`/`TMPDIR` 指向工作区内目录 |
| 进程组隔离 | ✅ 已实现 | `start_new_session=True`，超时 `killpg` |
| 容器/独立用户隔离 | ⬜ 提供方案，未启用 | 见下 |
| 同 UID 防护 | ❌ **不提供** | 同 UID 仍可直接改 SQLite |

`CodexLocal.capabilities` 如实声明：`isolated: False, env_allowlisted: True`。

## 已实现的边界

运行 runtime 子进程前，`fusion.runtime.child_env()` 构造环境：

- **允许**：`PATH`、`LANG`、`LANGUAGE`、`TERM`、`TMPDIR`、`TZ`、`SHELL`、
  `SSL_CERT_FILE`、`SSL_CERT_DIR`、`REQUESTS_CA_BUNDLE`、`LC_*`
- **显式放行**：`FUSION_RUNTIME_ENV_ALLOW="NAME1,NAME2"`（可信运维设置）
- **永不透传**（即使写进放行名单）：
  `PAPERCLIP_*`、`CODEX_*`、`AWS_*`、`SSH_*`、`PENGUIN_*`、`GITHUB_*`、
  `AZURE_*`、`GOOGLE_*`、`OPENAI_*`、`ANTHROPIC_*`，
  以及任何 `*_API_KEY`、`*_TOKEN`、`*_SECRET`、`*_PASSWORD`、`*_CREDENTIALS`
- `HOME` → `<workspace>/home`，`TMPDIR` → `<workspace>/home/tmp`

有 5 条测试覆盖（`fusion/tests/test_isolation.py`），包括"放行名单也不能走私密钥"。

## 未实现：真隔离

需要真隔离时，用容器：

```bash
docker build -t fusion .
docker run --rm -it \
  --network none \
  -v "$PWD/instance:/app/instance" \
  -v "$PWD/workspace:/app/workspace" \
  fusion python3 -m fusion run "..." --home /app/instance
```

- 容器内是独立用户与文件系统命名空间，`isolated` 才能记为 True
- `--network none` 会同时切断模型调用；需要模型时改为放行特定域名
- **这条路径尚未在本仓库实测**，标记为"提供方案，未验证"

## 剩余风险（必须知道）

1. **同 UID**：持有同一系统账号的人可直接读写实例 SQLite、替换代码、读公钥。
   环境白名单挡不住这个。
2. **prompt 注入**：agent 执行的命令仍在其工作区内有写权限；需要更强约束时用容器 + 只读挂载。
3. **不可信输入**：当前设计假定操作者是可信的，不适合直接暴露到公网。
