# 安全说明（当前版本）

## 已实现

- 人类账号密码 scrypt + per-user salt，明文不落盘
- 会话 token 仅存 SHA-256；cookie HttpOnly / SameSite=Strict
- HTTPS（Cloudflare）下自动加 `Secure` cookie
- Cookie 会话的写请求要求 `X-Fusion-Request` 自定义头，降低 CSRF 风险
- 机器 token 只读/受限，不能评审、签批、管理用户
- 公开注册：IP 限流 5 次/5 分钟，用户上限 1000，新账号固定 operator
- Workspace 隔离：Issue / Launcher / 文件 / 日志按 workspace 过滤
- Workspace owner 不可被移除；最后一个 admin 不可被移除
- 文件路径拒绝绝对路径、`..`、反斜杠、控制字符
- 下载文件名做安全清洗
- 日志上传前对 token/secret/password/api_key/bearer 等做启发式脱敏
- 单文件 10MB、单 Issue 50MB 配额
- Launcher token 只返回一次，服务端存 hash

## 仍存在的风险

- 公开注册没有邮箱验证/邀请强制，任何能访问实例的人可创建 operator
- 公开实例的 operator 共享同一 Workspace 的数据；不要放私有内容
- 日志脱敏是正则/字段名启发式，不能保证覆盖所有秘密
- 文件内容未做病毒扫描/内容安全检测
- 文件存本地磁盘，未加密、无对象存储/版本/回收站
- Cloudflare 后的真实客户端 IP 依赖 `CF-Connecting-IP`；仅当 `DUTANG_TRUST_PROXY=1` 时信任
- 全局 role 与 Workspace role 仍并存
- macOS App 未签名/未公证；下载后需要 Gatekeeper 放行
- 没有自动化安全扫描/依赖审计

## 生产部署建议

- 服务只绑定 `127.0.0.1`，通过 Cloudflare Tunnel/反向代理暴露
- 公网入口强制 HTTPS；确认 `Set-Cookie` 含 Secure
- Cloudflare WAF/限流 + 应用内限流双层
- 定期备份 `fusion.sqlite3`、`issue-files/`、`auth/`、`tokens.json`
- 不要把 `tokens.json`、`auth/`、`paperclip.env` 放进仓库
- 定期轮换机器 token 和 Launcher token
- 对公开实例设置独立数据目录，不复用私有实例
