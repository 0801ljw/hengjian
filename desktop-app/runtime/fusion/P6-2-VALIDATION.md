# P6-2：默认预设与缺失 agent 安装

董事会预设保持原分工：太子 hermes/gpt-6.1-terra，中书与工部
codex/gpt-6.1-sol，门下 claude/空模型，尚书 pi/deepseek-flash。
`preset_status(facts)` 返回五角色事实；`apply_preset(facts, instance)`
原子替换该实例的 roles.json（0600），仅保存探测到的角色并返回 missing。
省略 instance 时只生成结果，不落盘。不会保留缺失角色的旧绑定。

## 使用

```bash
python3 -m fusion.server --home /path/to/instance --port 8787
```

使用该实例已有 admin 账号登录工作台，在呈交区下方查看「角色绑定」。
缺失 codex/claude/pi 可点击「一键安装」，hermes 仅提供官方手工安装文档。
安装结束后自动刷新探测；点击「应用预设」保存并显示新绑定。
operator/viewer 无权调用这组接口；机器 token 不替代 admin 会话。
新实例账号可通过已有本地 `fusion.users.UserStore(...).add_user(...)` 管理，
不要将密码提交到源码或报告。

接口使用已有 `Authorization: Bearer <session>`：

- `GET /api/agents`：agents / preset / installability / binding。
- `POST /api/agents/install`：严格只接受 `{"agent":"codex"}` 等白名单名称；返回 202 与 job。
- `GET /api/agents/install/<job>`：status / tail / exit_code。
- `POST /api/agents/bind`：重新探测并应用预设，返回 bindings / missing。

## 安全与运维

三个 npm 包名及 argv 固定，不接受命令参数、不经过 shell。
npm 前缀固定为当前服务用户的 `~/.local`，确保 `~/.local/bin` 在启动服务的 PATH 中。
不使用 sudo；不读取用户或全局 npmrc，不继承 API/npm token 环境变量。
私有 registry 认证不在本次自动安装范围内。后台进程使用新 session，
300 秒超时杀死整个进程组；正常退出也清理残留后代。
stdout/stderr 合并，内存与状态文件只留最后 8192 字节，按环境中的密钥值脱敏。

每个实例的 `agent-installs/` 保存 job JSON、开始即落盘并在结束时更新的
`<job>.audit.json`，以及每次拒绝/已结束尝试各一行的 `audit.jsonl`。
包含 who / agent / argv / started_at / exit_code / duration_sec，权限为 0600。
每个 agent 的文件锁阻止同实例并发重复安装；已发现或正在安装返回 409。
服务退出不会自动重试安装。若安装中服务被强制终止，需由管理员核查
操作系统进程和 job 记录，避免在未知结果下重跑。

角色绑定仍是配置偏好，探测到 CLI 不代表该 agent 已获得执行或切换 provider 的权限。
本次不扩展现有 API 决策执行器：其非空 model 校验和固定 provider 语义保持原样。
门下空模型按董事会要求原样保存，供 agent 默认模型使用；现有 API 执行器不解析
CLI 默认模型，直接消费此空模型会拒绝运行。没有伪装为已接通跨厂商执行或擅自
回退到中书同源模型。审批、预算、lease、ai_pipeline.py 均未变更。

## 离线验证

```bash
PYTHONPATH="$PWD:$PWD/scripts" uv tool run --offline --from pytest pytest tests/ fusion/tests/ -q
```

新增测试使用假 npm，不下载包、不调用模型、不产生外部费用。
覆盖精确预设、缺失角色/旧绑定清除、空模型、白名单、重复安装、真实 HTTP
admin/operator/viewer/机器鉴权、超时与后代清理、进程启动失败、非零退出、
8KB 输出边界、密钥不继承与脱敏、审计，以及真实 JS 控制器的安装轮询、
应用预设、错误态、退出清理和三主题切换。既有主题变量测试约束所有新增配色。
JS 使用 DOM double 验证行为，未声称完成真实浏览器像素级截图验收。
