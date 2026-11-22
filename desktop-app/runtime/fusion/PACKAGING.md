# Fusion 分发、安装与升级

## 版本与边界

`VERSION` 是产品版本唯一来源；`fusion.__version__` 在运行时读取它。
采用 SemVer：patch 修复、minor 兼容功能、major 不兼容变更。
`fusion --version` 显示版本和 12 位 commit。git checkout 直接读取 HEAD；
源码归档必须带 `COMMIT`（由构建时 `git rev-parse --short=12 HEAD` 生成）。
安装后不再依赖原始 checkout，移动或删除原 checkout 不影响已安装版本。

本版本支持 Linux、Python 3.10+（含 venv/ensurepip）、Bash、支持 Ed25519 的
OpenSSL。干净安装验证还需要 curl、tar、mktemp、find、常规 coreutils。
Python/OpenSSL 是系统前置条件；安装器不会 sudo 或自动安装系统软件。

## 依赖锁定

`requirements.lock` 锁定 Fusion **运行时**依赖集。本版为空：Fusion 及其所用
的三个共享脚本只有标准库依赖，不应伪造第三方依赖或把开发机 pip freeze
当作产品依赖。根 `requirements.txt` 的 numpy 是上游可选 AgentRec 推荐器，
不属于 Fusion 安装；Playwright、Node 和上游后端同样不在本安装范围。

安装器创建独立 venv，始终执行：

```sh
python -I -m pip --isolated install --no-index --no-deps \
  --disable-pip-version-check --no-cache-dir -r requirements.lock
```

不访问包索引、不升级 pip、不自动补依赖；venv 自带的 pip 来自系统 Python
ensurepip。未来添加第三方库时，所有直接与传递依赖都必须精确 `name==version`，
随分发包提供校验过的 wheelhouse 并显式接入安装流程，否则安装会失败而非联网。

## 安装与卸载

在可信源码 checkout 根运行（路径请使用绝对路径，下面仅为例子）：

```sh
bash fusion/install.sh --home /srv/fusion --prefix /srv/fusion-bin
/srv/fusion-bin/fusion --version
/srv/fusion-bin/fusion doctor --home /srv/fusion
/srv/fusion-bin/fusion serve --home /srv/fusion/db --port 8787
# 停止服务后卸载；会删除所有数据、私钥、备份和已安装版本，需自行先备份。
bash fusion/uninstall.sh --home /srv/fusion --prefix /srv/fusion-bin --yes
```

无参数默认 `$HOME/.fusion`、`$HOME/.local/bin`；兼容 FUSION_HOME/PREFIX。
初次安装生成 board 密钥（`--no-key` 可跳过），私钥权限 0600，须移到人类
签名设备。安装不会创建模型凭证、运行模型、安装 agent 或联系外部服务。
已有安装必须走升级，不可用 install 覆盖。前缀可为 `<home>/bin`。

实例布局：

```text
<home>/
  db/                         # 服务 --home；角色、用户、数据库、审计等业务数据
  schema.json                 # 打包层 schema_version（当前 1）
  releases/<version>-<id>/    # 自包含代码、VERSION、COMMIT、锁文件、独立 .venv
  backup-<UTC timestamp>/     # 升级前快照，含旧代码/venv及外置启动器内容
```

既有实例可直接在 `<home>` 保存业务文件，升级保留整个布局，不强制迁移到 db/。
备份不递归包含旧 backup-*，避免指数增长；backup-* 和 .packaging.lock 为保留名。
启动器固定绝对 Python 与 release，使用 `-I`，不借用调用方 PYTHONPATH。

## 升级与自动回滚

1. 停掉该实例 **所有** server、worker、CLI 写入及外部数据库写入者。
   `--stopped` 是操作员确认；安装器另检查 Linux /proc 的常见 Fusion 进程。
   它不是任意第三方数据库写入者的通用检测器，不能替代停机操作。
2. 解包可信新版到另一目录；不要改写运行中的 release。
3. 从新版根运行：

```sh
bash fusion/upgrade.sh --home /srv/fusion --prefix /srv/fusion-bin --stopped
/srv/fusion-bin/fusion --version
/srv/fusion-bin/fusion doctor --home /srv/fusion
```

升级持有实例打包锁，先备份完整实例到 `<home>/backup-<时间戳>/instance/`，
记录每个文件的 SHA256/权限、符号链接和旧启动器，再装入新 release。
新版自身执行 version、doctor 后才原子切换启动器。roles.json、users.json、
数据库（包括停机 WAL）、审计日志、密钥、产物均原样保留。

schema.json 缺失按历史 schema 1 处理；存在时必须是整数 1。
SQLite user_version 允许历史 0 或 1；检查核心表存在和 quick_check。
数据库检查在临时副本执行，避免只读连接改变原 SHM。未知/未来版本直接拒绝，
不擅自迁移或覆写版本。本版没有数据格式变更。

任何安装/新版检查异常会真正删除失败版本及其新增文件，复制备份恢复数据、
旧 release 和启动器，再按摘要/权限逐项验证，打印 `ROLLBACK VERIFIED`，
并以非零退出码报告升级失败。成功升级保留备份，不自动清理。
备份含凭证和用户数据，目录 0700，请按实例同等保护。

## 手动回滚

先停掉所有写入者。选择升级日志打印的确切备份目录：

```sh
bash fusion/upgrade.sh --home /srv/fusion --prefix /srv/fusion-bin --stopped \
  --rollback /srv/fusion/backup-20261107T120000000000Z
/srv/fusion-bin/fusion --version
/srv/fusion-bin/fusion doctor --home /srv/fusion
```

使用创建备份时的同一 home/prefix；恢复前先验证备份摘要，不匹配则拒绝破坏性
操作。回滚会舍弃备份之后的业务写入，请停机并另做快照再执行。
恢复路径包含绝对 venv 路径，不能把备份搬到另一实例后直接使用。
自动回滚针对可捕获的执行失败；掉电、SIGKILL、磁盘损坏不保证自动完成，
需使用完整备份和上述手动恢复流程。服务重启由操作员负责。

## 验证命令

```sh
# slow：env -i、全新临时源码目录与 HOME、独立 venv、离线安装、HTTP 200、卸载
bash fusion/tests/test_clean_install.sh
# 使用现有 pytest 环境；绝对 PYTHONPATH 保证历史改变 cwd 的子进程测试也能找到源码
PYTHONPATH="$PWD:$PWD/scripts" python3 -m pytest tests/ fusion/tests/ -q
# 有已缓存 pytest 的开发环境可替代上一条；不会联网
PYTHONPATH="$PWD:$PWD/scripts" uv tool run --offline --from pytest pytest tests/ fusion/tests/ -q
```

脚本成败由退出码表达；成功打印 `PASS clean install...`，trap 在所有退出路径
关闭并回收测试服务，删除临时目录。测试只用 loopback health，不运行 worker。
它清空继承环境，只使用显式 PATH；缺依赖时清楚报错，不读取开发机配置。
`test_upgrade_rollback_on_failure` 在新版 doctor 内实际改坏 roles、删除 users、
改写数据库版本、创建多余文件，然后断言数据/权限/启动器完整恢复。
