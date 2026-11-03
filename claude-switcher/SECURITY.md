# SECURITY.md —— 安全策略

## 支持版本

默认自动更新跟踪稳定 Release，并固定到该 Release 的同一个 commit 下载源码和清单。`--track main` 是显式的开发选项。此分支版本为 `1.8.0-rc1`；原生平台验收和正式发布状态见发布记录。旧包缺少完整组件清单时拒绝自动升级，需要完整安装包迁移。

## 漏洞通报

- **首选**：GitHub Private Vulnerability Reporting（仓库 → Security 页 → "Report a vulnerability"），非公开渠道，我们会在修复发布前保密处理
- 不便使用上述渠道时：开 Discussion 或 Issue（**请不要在公开内容里附带可利用细节、你的出口 IP 或任何凭据**）

## 安全边界（诚实声明）

本项目把"防到什么程度"写在明处，不夸大：

- **Kill-Switch 是进程级急停，不是防火墙级 fail-closed**。保镖以 10 秒周期巡检"内核进程 × TUN 网卡"双因子，从隧道断开到进程被杀存在已知时间窗口（TOCTOU）；窗口内恰好发出的数据包不在防护范围内。此候选版提供默认关闭的实验性 PF 独立锚点，严格要求人工审核父规则、无既有状态和精确端点；尚需真机抓包验收，参见 [网络保护说明](docs/network/README.md)
- **自动更新的信任根是 GitHub 仓库本身**。每次更新前会对照固定 commit 下的 `COMPONENTS.json` 与 `SHA256SUMS` 校验完整组件（主脚本、helper、探针、App 源码、守护模板和安装器），可防传输层篡改/中间人换包；但如果仓库账号本身被攻陷，校验清单和工件会同时被替换——这是目前的信任边界。维护侧的发布纪律：`core/`、`daemons/`、`installer/` 任何改动提交前必须运行 `scripts/gen-sums.sh` 重新生成 SHA256SUMS，二者永远同提交
- **守护进程脚本 root 所有、用户态不可写**。保镖与时区看门狗安装在 `/Library/Application Support/claude-switcher/`（root:wheel, 755），launchd 以 root 执行这些脚本，普通用户进程无法篡改其内容——本地权限提升面已收敛
- **自动更新运行在用户态**（LaunchAgent），不再持有 root；需要 root 的守护脚本变更只提示用户运行安装器，绝不自动写系统目录

## 安全变更记录

### 1.8.0-rc1 候选修复

账号操作和组件更新串行化；账号日志可从崩溃边界恢复；快照导入保留已有恢复数据；公开启动入口自行完整审计；实际启动前重新审计；安装先编译并验证再提交；更新采用稳定版本和固定 commit，完整组件失败一起回滚；root 守护不再重定向写入用户日志。详见 [修复说明](docs/RELIABILITY-1.8.md)。

测试使用隔离目录、假网络服务和故障注入。没有 macOS/Windows 原生构建及真机测试证据的项目必须明确列为未验证，不能把 Linux 测试、mock PF 或交叉编译等同于实际系统验收。

### 2026-10-21（三项外部审查发现修复）

1. **本地权限提升修复**：launchd 系统服务原来以 root 执行 `~/.local/bin/` 下用户可写的脚本，任何能写该目录的进程都可提权。现守护脚本移入 root 所有的 `/Library/Application Support/claude-switcher/`，用户目录只保留 plist 副本
2. **供应链加固**：自动更新从 root 级 LaunchDaemon 改为用户态 LaunchAgent；新增 zip 工件逐项 SHA256 校验（对照仓库 `SHA256SUMS`），任一文件不匹配即中止
3. **Merge.yaml 覆盖修复**：安装器/一键修复原来 `cat >` 整文件覆盖 Clash Verge 全局扩展配置，会丢失用户已有配置。现改为带标记块的智能合并（幂等替换、保留用户规则、改前自动备份）；代理组探测到多个 select 组时改为让用户选择，不再静默取第一个
