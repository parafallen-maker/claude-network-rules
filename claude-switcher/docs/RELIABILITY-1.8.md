# 1.8.0-rc1：可靠性与网络检查修复

这是基于 `f37cb2651a781d9cf2765f53814c4d2ff5914cd0` 的候选版本。`rc1` 表示待原生平台验收的发布候选；它不是上游已发布的稳定版本。安装、更新和构建的具体命令见 [安装更新说明](INSTALL-UPDATE-TRANSACTIONS.md)。

## 账号数据

macOS 与 Linux 共用 `core/account_state.py`。保留现有 `accounts/A` 到 `accounts/D` 目录格式；新事务使用唯一暂存目录、持久化日志、原子账号标记和同一把跨进程锁。复制失败、目录换位失败、标记写入失败或进程突然退出时，后续操作先恢复事务，避免把 B 的实时数据归档为 A。

恢复只清理当前事务能证明归属的文件。旧版留下的 `.new`、`.previous`、`.tmp`、`.old` 或 `keep-stage` 可能是唯一副本，因此保留并要求核对；它们不能被当作普通缓存自动删除。无效快照、错误密码或导入失败不能删除这些既有恢复数据。跨身份保留只接收经过路径和链接检查的相对路径。

Windows 使用同样的事务原则，维护账号换位日志和完整组件更新日志，并用跨登录会话的独占文件锁串行化切换、快照与更新。Windows 的 `.csnap` 仍与 macOS 的加密 DMG 不兼容。

恢复命令不会启动 Claude，也不需要先通过网络审计：

```bash
# macOS
~/.local/bin/claude-switch.sh recover
# Linux
claude-switch recover
```

```powershell
# Windows，使用已安装核心脚本的完整路径
powershell -NoProfile -File "$env:USERPROFILE\.claude-switcher\claude-switch.ps1" recover
```

GUI 的 `pending=1` 状态表示存在中断的账号事务，不能把显示的账号理解为已经完成切换。恢复路径会先确认应用已退出，再触碰实时目录。Linux 仅自动退出同一用户、显式使用受管 `--user-data-dir` 的浏览器；无法确认配置目录归属的普通浏览器要求手动退出，不会批量结束所有 Chrome/Chromium。

## 启动和检查

公开的 `switch` 与 `open-safe` 自行执行启动审计，不依赖 GUI 预先调用检查。账号切换在修改数据前和真正启动前分别复核；复制期间网络改变时，准备好的账号会保留，但不会报告启动成功。应用启动命令失败会返回失败状态。

门禁必须收到全部 14 个不重复的检查结果。代理、TUN、出口国家、时区、地区、IPv6、保镖、规则这八项必须为 `ok`。六项可选检查只接受正常结果、明确的警告，或用户配置允许的 `skip`；`skip` 不代表已验证。硬性检查的 `warn` 或 `skip` 不再被界面视为通过。

- TUN 检查验证活动接口和公共目标的实际选路。`198.18.0.0/15` 地址不能单独证明 TUN 生效。
- macOS/Linux 的 IPv6 检查检查所有活动接口和公共 IPv6 路由。关闭 IPv6与由隧道承载 IPv6是两种不同的结果，界面详情会说明。
- fake-IP DNS 结果只说明解析行为，不证明 DNS 传输路径或上游递归解析器可信。
- STUN 校验响应类型、magic cookie、事务号、长度、属性边界、来源地址与端口，并解析 IPv4/IPv6 地址。无响应表示没有取得证据，不表示零暴露。
- Clash/Mihomo 规则需核对运行中的控制接口。无法验证其他客户端的真实路由策略时，会明确拒绝硬性规则检查；选择客户端并不保证所有版本和所有配置均受支持。

## 网络层保护的范围

默认保镖仍是进程级巡检，存在检查周期和启动时刻之间的时间窗口。它不能提供逐包阻断保证。

macOS 增加显式启用的 PF 模块。安装器不会自动启用它。它只管理独立锚点和本项目的 PF 引用，要求用户明确列出隧道接口、物理接口及代理端点。启用前要核对父规则、PF 状态表、接口和路由；规则加载失败或已启用配置漂移时，保留阻断状态并报告故障。

该模式限制整机出站网络，要求严格、单一且人工审阅的父锚点规则；默认系统规则或已有其他防火墙规则可能不满足前提。它不会自动改写全局 PF 配置、清空全局状态表或关闭其他软件的防火墙。`plan` 用于预览，实际启用步骤与验收清单见网络保护文档。此候选版的模拟命令测试验证规则操作和失败处理，**没有替代 macOS 真机抓包、断网、重连、休眠唤醒和代理切换验收**。Windows/Linux 没有在此版本新增同等防火墙能力。

## 安装、更新和版本一致性

macOS 安装先编译、验证签名、准备所有组件，再修改已安装文件；旧 App 不会在编译前被删除。安装后完整审计失败会回滚。系统设置和代理托管规则分别需要显式选项；卸载仅在系统值仍等于本工具写入值时恢复原值，保留用户后来主动修改的配置。

默认更新跟踪稳定 Release，并固定到同一个 commit 下载源码与清单；开发分支需要显式 `--track main`。`VERSION`、`COMPONENTS.json`、`SHA256SUMS` 覆盖整套组件。用户态更新事务包括主脚本、所有 Python helper、STUN 和版本元数据；GUI 或 root 组件发生变化时，暂存经过验证的完整安装包，保持当前安装不变，并提示运行安装器。

Windows 更新同时处理核心、界面、保镖、RuntimeSafety 和版本文件。每个文件原子替换；所有备份校验通过后才能开始恢复；更新后需要完整审计才能提交。未恢复的组件更新会阻止账号操作，避免混用新旧组件。

SHA256 只能验证工件与清单一致，信任根仍然是仓库及发布账户。默认本地 macOS 打包使用 ad-hoc 签名；Developer ID 与公证需要维护者已有的签名身份和凭据。脚本支持这些步骤，但没有证书时不能产生已公证的发行包。

## 验证和交付

可在隔离开发目录运行：

```bash
python3 -B -m unittest discover -s tests -v
bash scripts/gen-sums.sh
python3 core/component-manager.py verify --source .
python3 scripts/package-source.py
python3 scripts/package-source.py --kit
```

Windows 的 CI 在 Windows PowerShell 5.1 和 PowerShell 7 中运行账号、加密快照和 `windows-runtime-safety.ps1` 三组隔离回归。PowerShell 7 Linux 测试只能证明其中可移植的文件、加密、跨进程锁和协议行为；它不能替代 Windows WPF、Appx、真实 robocopy、计划任务和 UAC 的验收。macOS 的 SwiftUI 模型测试仅在 macOS 执行，Linux 会明确跳过。

候选构建工作流提供 arm64、x86_64、universal App 包，以及完整源码与源码安装包。源码 ZIP 内的 `SOURCE-INFO.json` 标记 `native_build: false`，不能作为已编译的 macOS App 交付。任何原生 App 产物都必须以实际成功的 macOS 构建记录为依据。
