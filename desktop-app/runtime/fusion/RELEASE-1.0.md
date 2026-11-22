# 衡鉴 · 点将台 1.0 发布门（Release Gate）

> 本文件是 **1.0 的准入标准**，不是愿望清单。
> 规则：下面每一条都必须有**可执行的验证**（测试或验收脚本）通过，缺一条就不发 1.0。

版本：`1.0.0`
验收入口：`bash fusion/ci.sh`（单元测试 + 端到端验收，一条命令）

---

## A. 核心闭环（必须全绿）

| # | 要求 | 验证方式 |
|---|---|---|
| A1 | 安装 → 初始化 → 提交 → 执行 → 产物 → 审计，端到端可跑 | `verify_all.sh` 的「端到端」组 |
| A2 | 产物 manifest 落盘，SHA-256 与实际文件一致 | 同上：`产物 SHA-256 与 manifest 一致` |
| A3 | 每个决定都有留痕（时间线 5 步、审计落盘） | 同上：`时间线渲染 5 步`、`审计落盘` |
| A4 | 失败/取消路径不吞错（有终态） | `test_cancel_paths.py`、`test_migrate_legacy.py` |

## B. 身份与权限（必须全绿）

| # | 要求 | 验证方式 |
|---|---|---|
| B1 | 无凭据访问受保护接口 → 401 | 验收「无 token → 401」 |
| B2 | 角色隔离：viewer 只读、写 → 403 | 验收「viewer 只读（写 403）」 |
| B3 | 登出后凭据立即失效 | 验收「登出后 401」 |
| B4 | 密码不落明文、token 文件无明文 | 验收「密码不落明文」「token 文件无明文」 |
| B5 | **浏览器会话用 httpOnly cookie**，JS 读不到 token | 验收「会话 cookie 是 HttpOnly」+ `test_session_cookie.py` |
| B6 | **cookie 会话的状态变更必须带 CSRF 头**，缺头 → 403 | `test_session_cookie.py::test_cookie_write_requires_csrf_header` |
| B7 | 机器客户端（Bearer）不受 CSRF 影响 | `test_session_cookie.py::test_bearer_path_still_works_without_csrf` |

## C. 预算与成本（必须全绿）

| # | 要求 | 验证方式 |
|---|---|---|
| C1 | 每次运行有金额或显式 unknown，不混成 0 | 单元测试 `budget` 组 |
| C2 | 未定价策略可配置（allow/block），block 时拒绝起跑 | 单元测试 + `--allow-unpriced` 守卫 |
| C3 | 双层预算（公司 + 角色）都生效 | `budget` 相关测试 |

## D. 隔离与安全（必须全绿）

| # | 要求 | 验证方式 |
|---|---|---|
| D1 | 子进程不继承密钥 | 验收「子进程不继承密钥」 |
| D2 | 窄授权：gongbu 只有两条权限、无通用 state | 验收「窄授权仅两条」「gongbu 无通用 state」 |
| D3 | 只绑 loopback；对外暴露需显式配置 | `server.py` 默认 host + `test_http_server.py` |
| D4 | 注册默认本机可用，远程需显式开关 | `_registration_allowed` 测试 |

## E. 可运维（必须全绿）

| # | 要求 | 验证方式 |
|---|---|---|
| E1 | `install.sh` 空目录可跑通、`launcher` 任意目录可用、`uninstall.sh` 清理干净 | 验收安装组 |
| E2 | `--version` 与 `VERSION` 单一来源一致 | `test_packaging.py::test_version_single_source` |
| E3 | 升级失败可回滚，数据不丢 | `test_packaging.py` 回滚组 |
| E4 | 页面版本号与 `VERSION` 一致（防漂移） | 验收「页脚版本与 VERSION 一致」 |
| E5 | 文档齐全：README / USER-GUIDE / ISOLATION / DESIGN / 本文件 | 验收 E3 文档组 |

## F. 质量门（数字指标）

| 指标 | 1.0 要求 | 当前 |
|---|---|---|
| 单元测试 | 全部通过，且**只增不删** | 637 passed |
| 端到端验收 | 全部通过 | 44 passed |
| 静态检查 | 无语法错误（`node --check` / `ast.parse`） | 通过 |
| 已知漏洞 | 无「为了过测试而绕过安全约束」的实现 | window.name 已移除 |

---

## 发版动作清单

```bash
# 1) 版本身份
echo 1.0.0 > VERSION
python3 - <<'PY'
from pathlib import Path
p = Path("fusion/web/index.html"); s = p.read_text(encoding="utf-8")
import re
p.write_text(re.sub(r"v\d+\.\d+\.\d+", "v1.0.0", s), encoding="utf-8")
PY

# 2) 全量验证（必须 0 fail）
bash fusion/ci.sh

# 3) 打包与安装演练（干净前缀）
bash fusion/install.sh --prefix /tmp/fusion-1.0-check
/tmp/fusion-1.0-check/fusion --version
/tmp/fusion-1.0-check/fusion uninstall --prefix /tmp/fusion-1.0-check

# 4) 打标签
git tag -a v1.0.0 -m "衡鉴 · 点将台 1.0.0：核心闭环 + cookie 会话 + 发布门"
```

## 1.0 之后（不在本门内）

- GAP-MATRIX 的 P1 项（adapter 统一契约、lease/fencing、artifact 检索、outbox 幂等）→ 1.1
- 多用户邀请与组织治理 → 1.2

---

*任何一条 gate 无法用命令验证，就说明它不该出现在这张表里——请删掉或补上验证。*
