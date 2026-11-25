# 衡鉴 · 公网演示部署（hengjian.playxapi.com）

> 2026-11-22 建立。独立于私有 `fusion-instance`，不暴露任何现有案卷/Issue。

## 入口

```text
https://hengjian.playxapi.com
https://hengjian.playxapi.com/login
```

- 用户访问 `/login` 点「注册」
- 第一个 admin 已预置：`board`
- 之后注册的账号角色固定为 **operator**
- 公开注册已开启，IP 限流 5 次/5 分钟，用户上限 1000

管理员密码不在仓库/本文档中，存放在：

```text
/Users/ljw/.config/hengjian-public-admin.txt   (0600, macOS)
```

丢失后按重置流程重新生成，不要贴到聊天/工单。

## 主机与端口

| 对象 | 值 |
|---|---|
| 服务器 | `ljw-pc`（Debian） |
| 公网入口 | Cloudflare `hengjian.playxapi.com` |
| fusion-public | `127.0.0.1:8794` |
| 数据目录 | `/home/ljw/boss-lab/fusion-public` |
| 私有 fusion | `127.0.0.1:8793`（不受影响） |
| Playx | `127.0.0.1:3100`（不受影响） |

## 两个 user service

```bash
systemctl --user status fusion-public.service
systemctl --user status hengjian-tunnel.service

systemctl --user restart fusion-public.service
systemctl --user restart hengjian-tunnel.service

journalctl --user -u fusion-public.service -f
journalctl --user -u hengjian-tunnel.service -f
```

- `fusion-public.service`：独立数据目录 + 公开注册 + 可信代理 IP
- `hengjian-tunnel.service`：独立 Cloudflare tunnel，**没有**修改 `playx-cloudflared.service`
- Cloudflare tunnel 名：`hengjian`；只新增 `hengjian.playxapi.com` CNAME

## 环境变量

`fusion-public.service`：

```text
DUTANG_PUBLIC_REGISTRATION=1
DUTANG_ALLOW_REMOTE_REGISTRATION=1
DUTANG_TRUST_PROXY=1
```

- `DUTANG_TRUST_PROXY=1` 让注册限流使用 Cloudflare 的 `CF-Connecting-IP`，避免所有用户共享 `127.0.0.1` 的限流桶。
- 没有设置 `DUTANG_DISABLE_REGISTRATION`。

## 重要边界

- **公开演示实例不启动 worker**：可以注册/登录/看板/建 Issue，但不会自动调用模型。
- 所有注册 operator 共享这个公开实例的数据；不要在里面放私有案卷、token 或真实业务数据。
- 公开实例与私有实例数据完全分离。
- 没有邮箱验证、邀请码、Cloudflare Access；公开互联网可注册。
- 尚未做 Cloudflare WAF 自定义规则；只有应用内限流。
- App 仍是本机使用；公网是 Web 入口，不是桌面 App 的云同步。

## 回滚

只关闭本次新增的服务，不影响 Playx/私有 fusion：

```bash
systemctl --user stop hengjian-tunnel.service fusion-public.service
systemctl --user disable hengjian-tunnel.service fusion-public.service
```

Cloudflare DNS 记录 `hengjian.playxapi.com` 如需删除，在 Cloudflare 控制台删除对应 CNAME；不要动 `playxapi.com` 主站记录和 `playx-cloudflared.service`。

## 重新创建公开实例

```bash
mkdir -p ~/boss-lab/fusion-public
cd ~/boss-lab/edict
python3 - <<'PY'
from pathlib import Path
from fusion.users import UserStore
import secrets
home = Path('/home/ljw/boss-lab/fusion-public')
store = UserStore(home / 'auth')
pw = secrets.token_urlsafe(18)
store.add_user('board', pw, 'admin')
(home / 'roles.json').write_text('{"mode":"multi","bindings":{}}\n')
(home / 'roles.json').chmod(0o600)
print(pw)  # 只写到本地安全位置，不要贴聊天
PY
```

## Launcher 配对

- 云空间：设置 →「已连接 Launcher」→ 生成配对码
- 本机 App：设置 →「连接云空间」→ 输入云空间 URL + 配对码
- 配对后每 30s 心跳；云空间可查看/移除 Launcher
- 当前仅配对/心跳，未下发任务或同步文件

## 云任务下发（B 阶段）

- 创建 Issue 时传 `launcher_id`
- Launcher 调 `GET /api/launcher/tasks`
- 领取/完成/失败：`POST /api/launcher/tasks/<id>/claim|complete|fail`
- 当前没有创建 Issue UI；用 API/工具
- 没有自动回收 Launcher 离线任务；running 可能卡住

## 任务租约

- Launcher 领取后 600s lease，执行中每 30s 心跳续租
- 过期 running 由 recovery loop 退回 queued
- 当前云任务面板只读；自动执行需 `DUTANG_LAUNCHER_EXECUTE=1` 和本机模型配置

## 派发云任务

- 云空间设置 →「派发云任务」：标题、执行角色、Launcher、说明
- 创建后 state=queued，Launcher 拉取/领取
- 本机「云任务」面板只读；自动执行需 `DUTANG_LAUNCHER_EXECUTE=1`

## Workspace 隔离

- 新账号自动获得 `w_<hash>` workspace
- Issue/Launcher/配对码按 workspace 过滤
- 旧数据默认 `w_legacy`；机器 token 保持 legacy
- 公开实例 board 仍只有 legacy workspace；新用户看不到彼此数据

## 云账号登录

- 本机设置 →「连接云空间」→ 云账号/密码 → 自动创建 Launcher
- 令牌绑定该账号的 workspace；不再必须生成配对码
- 配对码仍保留为备用

## Workspace 成员/owner

- 设置 →「成员管理」查看当前 workspace 成员
- owner 不能被移除；跨 workspace 不可见
- 暂不支持切换/加入多个团队

## 自动备份

- systemd user timer：`fusion-public-backup.timer`
- 每天 03:30 左右执行（RandomizedDelaySec 15m）
- 输出：`/home/ljw/backups/fusion-public/fusion-YYYYmmdd-HHMMSS.tar.gz`
- 权限 0600，保留 14 天
- 内容：SQLite 在线备份 + auth + tokens + roles + issue-files + sync journal
- 手动：
  ```bash
  systemctl --user start fusion-public-backup.service
  systemctl --user list-timers fusion-public-backup.timer
  ```

恢复演练：

```bash
R=$(mktemp -d)
tar -xzf /home/ljw/backups/fusion-public/fusion-*.tar.gz -C "$R"
python3 - "$R/fusion.sqlite3" <<'PY'
import sqlite3,sys
print(sqlite3.connect(sys.argv[1]).execute("PRAGMA quick_check").fetchone()[0])
PY
rm -rf "$R"
```

备份包含 token/认证材料，不要上传到公开位置。
