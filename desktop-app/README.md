# 衡鉴·点将台 · macOS 桌面 App（原型）

原生 Swift + WKWebView 外壳，内嵌/启动本地 fusion 服务，加载与 DSH 面板相同的 Web UI。

## 结构

```text
desktop-app/
  Sources/HengjianApp.swift   # AppKit 窗口 + WKWebView + 服务生命周期
  Info.plist
  build.sh                    # swiftc + .app 打包（会嵌入 dist/fusion-server）
  build-sidecar.sh            # PyInstaller 构建自包含 fusion-server
  sidecar_entry.py            # sidecar 入口
  runtime/                    # 从 Linux 准绳同步的 fusion 源码 + scripts（开发回退）
  dist/fusion-server          # 自包含 sidecar
  dist/Hengjian-macOS-arm64.dmg # 分发 DMG
  dist/Hengjian-macOS-arm64.zip # 分发 zip
  build/Hengjian.app          # 生成物（不提交）
```

## 下载后直接运行

下载/复制：

```text
desktop-app/dist/Hengjian-macOS-arm64.dmg
```

挂载 DMG（或解压 zip）后双击 `Hengjian.app`。目标机 **不需要安装 Python**，App 内置
`Contents/Resources/bin/fusion-server`。

如果 macOS Gatekeeper 拦截：

```bash
xattr -dr com.apple.quarantine /path/to/Hengjian.app
open /path/to/Hengjian.app
```

或右键 App →「打开」。

首次进入初始化/注册页；公开注册已开启，新账号为 operator，第一个账号为 admin。

## 从源码构建

```bash
./build-sidecar.sh   # 需要 Python 3.12 + PyInstaller；生成 dist/fusion-server
./build.sh           # 生成 build/Hengjian.app，并嵌入 sidecar
open build/Hengjian.app
```

Python 只用于**构建 sidecar**，不是运行目标机的依赖。若 `dist/fusion-server`
不存在，`build.sh` 仍会生成一个回退到系统 Python 的 App。

## 服务发现

App 启动内置 sidecar：

```bash
Hengjian.app/Contents/Resources/bin/fusion-server \
  --home ~/Library/Application\ Support/Hengjian/instance \
  --port 0 \
  --discovery-file ~/Library/Application\ Support/Hengjian/instance/discovery.json \
  --sync
```

若 sidecar 缺失，才回退到系统 Python。

发现文件为 **0600**，包含 `port / token / pid / home / started_at`。App 读取它并加载
`http://127.0.0.1:<port>/`，再自动注入 token。若发现文件已存在且 `/api/health`
200，App 直接复用，不重复起服务。

## 已验证

- App 日志记录 `didFinish http://127.0.0.1:<port>/`，WKWebView 确实加载了页面
- 服务日志 `fusion server: http://127.0.0.1:<port>`
- discovery 0600，`/api/health` 200
- 首次启动进入初始化向导

## 当前能力

- 双击启动、单窗口、退出时终止自己启动的服务
- 首次启动进入初始化向导（与 Web 版一致）
- 复用现有 Web UI/CSS/JS，不维护第二套界面
- 服务发现 + 本地客户端 token

## 当前限制

- 未签名/未公证；本机 ad-hoc 签名可运行，下载后需要 Gatekeeper 放行
- 尚未启动 `--worker`（因为没有内置模型密钥）；下载后可以注册/登录/看板，但不会自动调用模型
- 只有 arm64 包
- DSH 插件还未改成读 discovery；目前仍走 profile 里的 upstream/token
- 没有自动更新、托盘、崩溃恢复

## 目标形态

```text
DSH 插件 ─┐
          ├─ 同一个本地 fusion 服务（discovery/token）
桌面 App ─┘
```

DSH 插件 0.5.5 源码支持 `upstream: auto` 读 discovery；当前 profile 仍指向 Linux 实例，尚未把插件切到桌面 App 的新实例（两边数据不同，需要先选定主数据源/迁移）。
