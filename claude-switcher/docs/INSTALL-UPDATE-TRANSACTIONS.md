# 安装、更新与发布包

此仓库的主要发布目标为 macOS 13 及以上。Windows、Linux 目录保留参考实现，未作为本仓库已验收的安装产品发布。版本以 `VERSION` 为准；候选版本不能写成已完成真机验收的稳定版本。

## 完整 App 与源码包

在仓库的 `claude-switcher/` 目录执行：

```bash
bash scripts/gen-sums.sh
CLAUDE_SWITCHER_BUILD_ARCH=universal bash scripts/package-macos.sh
```

构建需要 macOS、Apple SDK/Swift 编译器与 Python 3。`CLAUDE_SWITCHER_BUILD_ARCH` 可选 `arm64`、`x86_64`、`universal`，默认使用当前机器架构。统一构建入口 `installer/install.py` 同时编译主 App 与 `NetworkNotifier.app` 通知助手；双架构包中的两个可执行文件都必须包含 arm64 与 x86_64。

产物位于 `build/`：

- `ClaudeSwitcher-<version>-macos-<arch>.zip` / `.dmg`：完整预编译 App，内含通知助手与校验过的首次安装组件。
- `ClaudeSwitcher-<version>-installer-kit.zip`：源码安装包，安装时编译 App。
- `ClaudeSwitcher-<version>-source.zip`：组件、文档和测试源码。

完整 App 的 `Contents/Resources/InstallerSource/` 保存源码安装组件，`BuildInfo.json` 记录版本和 `COMPONENTS.json` 的 SHA256。嵌入资源后，打包器先重新签名内部通知助手，再签名外层 App，并验证整体签名。下载 App 后先放入「应用程序」，首次打开使用 App 提供的安装入口完成组件安装。安装器验证预编译 App、版本与内置组件后复用编译结果，无需用户再次编译 Swift；仍需 Python 3、可用代理与安装所需的管理员授权。

默认签名为 ad-hoc，**不代表 Apple 公证**，首次打开可能被 macOS 拦截。若维护者已配置 Developer ID 与公证钥匙串，可设置 `CLAUDE_SWITCHER_SIGN_IDENTITY` 和 `CLAUDE_SWITCHER_NOTARY_PROFILE`；脚本提交公证、等待、装订并验证后才生成发行包。未配置这些凭据的 CI 产物应明确标为未经公证。

仅生成源码包无需 macOS：

```bash
python3 -B scripts/package-source.py
python3 -B scripts/package-source.py --kit
```

两类源码 ZIP 解压后，均可在其根目录执行 `python3 -B core/component-manager.py verify --source .`。打包只收录组件清单、已跟踪源码及指定文档/测试，不收录本机账号、浏览器配置或代理凭据。`--kit --directory <new-directory>` 使用相同规则生成内置安装目录。

## 安装事务与恢复

`installer/安装.command` 探测代理和目标地区后调用事务安装器。安装器先验证组件并准备有效 App，再在共享状态锁内提交用户组件与 root 组件。预编译 App 路径会验证代码签名及 `BuildInfo.json`；源码安装路径会先编译及验证 App。准备失败时不替换现有安装。

用户文件采用逐文件原子替换、持久备份与事务日志；root 组件具有固定目标目录和独立回滚日志。系统时区、地区与 IPv6 修改需要显式 `--configure-system`，代理托管规则需要 `--configure-proxy`。原值用于条件恢复，无法准确恢复的手工 IPv6 配置不会被自动改写。PF 不属于自动安装步骤。

停止守护服务前，安装器等待 Claude 退出；特权进程独立复核。root 工作使用 `/usr/bin/python3`，不运行用户自定义 Python。旧版 root 自动更新任务停用并保留为惰性备份，回滚不会重新启用 root 执行用户可写更新代码。

安装后的核心检查必须成功退出，返回 14 项互异结果且硬性检查通过。失败时回滚，不显示安装成功。若回滚授权被拒绝或中断，保留事务日志并明确报错。使用同一份经过验证的源码恢复：

```bash
python3 installer/install.py recover-install
```

安装事务不会删除账号快照。`installer/卸载.command` 保留账号及恢复资料；系统设置仅在仍等于本工具写入值时恢复。安装后被用户修改的代理配置不会整体覆盖，应保留并人工处理本工具的标记块。

## 更新契约

```bash
~/.local/bin/claude-switch.sh update-check
~/.local/bin/claude-switch.sh update
~/.local/bin/claude-switch.sh update --check --track main
```

默认稳定通道读取本仓库最新非预发布 Release，将标签解析为固定提交 SHA，并从同一 SHA 读取归档与组件清单。仓库根目录包含规则，安装源码位于 `claude-switcher/`；更新器必须从归档内的该目录定位完整组件。开发分支需显式选择 `--track main`。旧于已安装候选版本的稳定版本不会自动降级，不带完整清单的包会被拒绝。

用户态核心、版本与组件元数据一起提交和回滚。检查退出失败、返回截断/重复结果或硬项失败时整组恢复。更新器不会自行提权；App、root 助手、守护进程或安装器发生变化时，暂存完整校验包至 `~/.claude-switcher/pending-update/<commit>/`，保留当前安装并给出重装入口。

恢复中断更新：

```bash
python3 ~/.claude-switcher/component-manager.py recover
```

未恢复的账号或安装事务阻止更新写入；未恢复的更新事务阻止账号修改。组件更新不覆盖账号数据与检查偏好。

## CI 与验收边界

生效的工作流位于仓库根目录 `.github/workflows/`，执行安装源码命令时工作目录为 `claude-switcher/`。源码内的同名工作流随组件清单分发，修改后需同步根目录副本。

`checks` 验证规则生成结果、JavaScript 与 shell 语法、现有 Python 隔离测试、主 App/通知助手严格 Swift 编译、组件清单及安装目录。`candidate-artifacts` 依赖这些检查，构建 arm64、x86_64、universal 三种候选包。标签发布同样依赖检查，通过后上传 universal 完整 App、源码和 installer kit，并生成发行包 SHA256 清单。

跨架构编译、签名验证、ZIP 解压校验和上传成功，各自只证明对应步骤。它们不能替代 Intel/Apple Silicon 真机交互、首次安装、管理员授权、真实代理、系统地区修改或回滚验收。Windows/Linux 未在此工作流中做平台运行验证，也不生成声称已验证的发行资产。实际验收结果应单独记录，不能从构建成功推断。
