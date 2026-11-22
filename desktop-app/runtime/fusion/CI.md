# 离线回归 CI

## 手动运行

```bash
cd /home/ljw/boss-lab/edict
bash fusion/ci.sh
```

依次运行完整 `pytest tests/ fusion/tests/` 和 `bash fusion/verify_all.sh`。
验收脚本自己也会执行 pytest，保留其完整行为，不传 `--quick`。
需要 Python 3、git（提交标识）、bash、openssl、node、which，以及本机
Python 的 pytest 或 uv 已缓存的 pytest。与验收脚本一致：有 uv 时优先使用
其离线缓存，缓存缺失则 SKIP；没有 uv 才使用 Python 模块。uv 强制离线，不安装或下载依赖。
PATH 自动加入 `~/.local/bin`、`~/.local/node/bin`，适配用户 systemd 环境。
仅本机回环 HTTP 用于原有测试/验收，不访问外部服务，不发通知。

## 结果与排障

默认输出到 `/home/ljw/boss-lab/`：

- `ci-status.json`：最近一次结果，原子替换；包含 time（UTC）、commit、
  tests_passed/tests_failed、acceptance_pass/acceptance_fail、ok。
- 附加字段 tests_status/acceptance_status 为 PASS、FAIL 或 SKIP；
  tests_skipped/acceptance_skip、reasons、failure_items 保留跳过与失败信息。
- `ci-tests.log`、`ci-acceptance.log`：最近一次实际运行的详细输出。
- `ci-failures.log`：每次非成功结果追加一行 JSON，含时间、提交、失败项及原因。
  只落盘，不发送外部服务。可用 `tail -n 5` 查看。

退出码 0=完整通过，1=测试/验收失败，2=SKIP（缺依赖、测试跳过或已有运行）。
SKIP 永远不会令 ok=true；依赖缺失时两个阶段均跳过，计数为 0，并写明原因。
无 Python 时 shell 生成 SKIP JSON 并退出 2（该后备路径需 flock/date/mv）。
应检查时间戳，不能把旧文件当作本次结果。运行持有 ci.lock，重复运行退出 2，不覆盖正在生成的结果。
git 缺失记录 commit=unknown；其他依赖缺失不自动修复，由操作者离线准备。
先查看 reasons 与 failure_items，再看详细日志；缺汇总、无测试或异常退出不算通过。
SKIP 时详细日志可能属于上一次实际运行，请以 status 时间与阶段为准。

可用 `bash fusion/ci.sh --output-dir /tmp/fusion-ci-check` 隔离演练输出。

## 定时器（需签批处自行启用）

模板位于 `fusion/ci/fusion-ci.{service,timer}`，已复制到
`~/.config/systemd/user/`，没有 enable/start/daemon-reload。
service 为 Type=oneshot；每天系统本地时间 04:00 触发，随机延迟为 0。
模板固定本机仓库路径；迁移机器时先修改 WorkingDirectory/ExecStart。
关闭用户会话时能否执行取决于用户 manager 生命周期；不自动设置 linger。
不配置补跑：错过 04:00 等下次计划，避免启用时意外触发。

以下命令仅供签批处决定后执行：

```bash
systemctl --user daemon-reload
systemctl --user enable --now fusion-ci.timer
systemctl --user list-timers fusion-ci.timer
journalctl --user -u fusion-ci.service
```

停用：`systemctl --user disable --now fusion-ci.timer`。
