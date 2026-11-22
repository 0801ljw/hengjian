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
