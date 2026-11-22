# P6-3 初始化向导

基线：BOS-42 的完整提交 `7771d79`，复用 `agents_probe.probe`、
`role_binding.save/apply_preset/preset_status` 和 `agent_install.installability`。
无需新增依赖。

## 启动

```bash
cd /home/ljw/boss-lab/BOS-43-worktree
python3 -m fusion.server --home /path/to/instance --port 8787
```

打开 `http://127.0.0.1:8787/`。实例没有 `roles.json` 时显示向导，否则显示工作台。
已有实例也可直接打开 `/onboarding.html` 重新配置；已有用户时必须管理员登录。
用户库沿用现有位置 `/path/to/instance/auth/users.json`，不创建第二套身份系统。

新实例无用户时可在回环连接上匿名读取/保存 onboarding 配置。一键安装继续要求
真实 admin 会话，匿名和机器 token 都不能安装。若需建立本地管理员，沿用 UserStore：

```bash
python3 -c 'from pathlib import Path; from getpass import getpass; from fusion.users import UserStore; UserStore(Path("/path/to/instance/auth")).add_user("admin", getpass("Admin password: "), "admin")'
```

之后在向导第二页出现的登录表单中登录，再重试安装。安装使用现有固定 npm argv 和
`~/.local` 用户前缀；确保运行服务的 PATH 包含 `~/.local/bin`。hermes 仅显示官方手动安装文档。
重新探测会强制更新本地探测缓存；不会调用模型。

## 行为

首屏仅有单/多 Agent 两个运行方式，优缺点照抄董事会文案。
单 Agent 按 `claude/codex/hermes/pi` 取第一个发现者高亮；所有发现者仍可改选，
页面明确说明“推荐只是推荐”。未列入排序的已探测 Agent 也可手动选择。
版本和安装日志通过 textContent 渲染，不解释成 HTML。

单模式用探测到的模型（若有），否则沿用该 Agent 的预设模型；没有具名预设模型时，
存储非空 `default` 标记以满足现有 single 绑定校验。它仅表示 CLI 默认模型偏好，
不是模型服务名或执行授权；本次没有让现有 API 执行器解析它。

多模式保持既有预设及 `missing` 的角色标识符语义：仅写入已发现者，缺失角色不写，
也不保留旧绑定。门下模型仍为空。第三页明确列出缺失角色；可进入工作台后补装并应用预设。
即使全部缺失，多模式也允许保存空绑定并显示完整缺失摘要，避免默默回填未探测 Agent。

`complete` 必须先有 roles.json，随后按已保存 mode 写 0600 onboarding.json，重复调用
保留首次 completed_at。single 同时保存完成回执；multi 在 complete 时写完成时间。
根页面判定严格只看 roles.json 是否存在，不由完成时间或用户存在情况替代。

鉴权只新增到四个 onboarding 端点：无用户时要求回环 peer 和本地 Host，存在 Origin 时
要求同源；不信任转发头。用户库存在用户时匿名、机器 token、viewer、operator 均 403，
admin 会话通过；损坏用户库失败关闭。业务和安装接口保持既有权限。

## 验证

```bash
PYTHONPATH="$PWD:$PWD/scripts" uv tool run --offline --from pytest pytest tests/ fusion/tests/ -q
node --check fusion/web/onboarding.js
node fusion/tests/onboarding_dom.cjs
git diff --check
```

新增 21 项测试，原 494 项测试的源码与断言不变。覆盖根页面切换、五角色写入、部分预设、
默认推荐和自由改选、所有引导端点权限、引导窗口关闭、幂等、0600、非法输入、
回环/来源限制、损坏用户库、前端空态/登录/安装轮询/缺失摘要、三主题变量与切换。
前端以实际 JS 控制器 + DOM double 验证，无真实浏览器截图验收。
测试离线使用假探测/假安装响应，不安装真实包、不调用付费模型。

硬约束核对：没有改 `ai_pipeline.py`、审批、预算、lease，也没有修改 BOS-42 接口语义。
新增 CSS 的颜色均来自已有主题变量。安全检查依照 security-review，前端表单和异步状态
参考 frontend-patterns；未引入新框架。
