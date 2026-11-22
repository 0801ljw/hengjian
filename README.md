# 衡鉴 · 点将台

本机多 Agent 编排与可信执行台。当前是 macOS 早期测试版：**下载、注册、登录即可用**；v0.1.4 起支持 Launcher 与云空间配对、云 Issue 下发/派发 UI、任务租约心跳和云任务面板。

## 下载 macOS App

**[下载最新 Release](https://github.com/0801ljw/hengjian/releases/latest)**

- Apple Silicon（arm64）
- 解压/挂载后双击 `Hengjian.app`
- 已内置 Python sidecar，**目标机不需要安装 Python**
- 首次注册的账号是 admin，之后是 operator
- 公开注册已开启

如果 Gatekeeper 拦截：

```bash
xattr -dr com.apple.quarantine /Applications/Hengjian.app
open /Applications/Hengjian.app
```

也可以右键 App →「打开」。

## 公网演示

- Web：https://hengjian.playxapi.com
- 注册：https://hengjian.playxapi.com/login
- 独立公开实例，不含私有案卷；不启动 worker

## 它是什么

- 本机 Agent 清单/工作区/运行历史
- Agent Issue 队列、checks、评审/签批
- opt-in daemon 生命周期、健康探针、自动重启、Inbox 告警
- 成员管理、公开注册、预算/成本策略
- 桌面 App 与 DSH 插件共享同一个本地 fusion 服务

## 当前边界

- 只有 macOS arm64 包，未签名/未公证，内部测试优先
- App 默认不启动模型 worker；可注册/登录/看板，但不会自动调用模型
- 公开注册=任何能访问本机 App 的人都能创建 operator 账号
- 还没有互联网部署/多端同步/一键模型配置
- 公开注册、成员管理、Agent 生命周期均为早期实现

## 从源码构建

```bash
cd desktop-app
./build-sidecar.sh
./build.sh
open build/Hengjian.app
```

## DSH 插件

DSH 侧栏/工具适配器在独立仓库：

**[0801ljw/dutang-dsh-plugin](https://github.com/0801ljw/dutang-dsh-plugin)**

```bash
git clone https://github.com/0801ljw/dutang-dsh-plugin.git
cd dutang-dsh-plugin
./scripts/install.sh desktop   # DSH Desktop
# 或
./scripts/install.sh web       # DSH Web
```

或在 DSH 的 **Plugins → Add plugin** 选择本地插件目录。插件需要可达的 fusion 服务（桌面 App 或 Linux 实例）。

## 文档

- [操作执行手册](OPERATIONS-RUNBOOK.md)
- [桌面 App 构建说明](desktop-app/README.md)

## License

MIT
