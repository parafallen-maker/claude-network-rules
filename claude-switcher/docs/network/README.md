# 网络检查与可选 PF 防护

本版本把检测结果与强制阻断分开表示。接口存在、fake-IP DNS 回答、STUN 无响应、语言、ASN 都不能单独证明安全；通过检查也不能保证账号不被服务端限制。

## 默认工作方式

- `network-check.py` 只读系统接口和路由，不发测试包、不修改配置。活动 TUN 必须实际承接多个公共 IPv4 目标路由，地址不再写死为 `198.18.*`。
- IPv6 检查覆盖所有活动接口。没有公网 IPv6 路由且只有链路本地地址可通过；存在公网/ULA 地址但路径未知会失败；IPv6 确实由活动隧道接管时允许保持启用。
- macOS 的 GUI、CLI 切换与一次性浏览器共用完整启动审计。没有环境变量跳过生产门禁。
- macOS/Linux 的 Clash Verge、FlClash（macOS）、Mihomo（Linux）读取运行控制接口的实际模式、代理组、UDP 能力及置顶规则。全局模式也确认当前选中的是代理节点，不能仅因为字符串为 global 就放行。
- 其他客户端仍可运行只读诊断，但未实现运行策略适配时会明确阻断受保护启动；fake-IP 回答不作为规则验证的替代。macOS 显式 PF 防护的有效 root 收据可提供额外网络层证据。
- DNS 检查只报告解析信号，不声称已验证上游加密或没有 DNS 泄漏。
- STUN 校验来源、事务 ID、magic cookie、报文长度和属性边界，支持 IPv4/IPv6 XOR-MAPPED-ADDRESS。无响应表示未知；只有已验证的拒绝规则可将无响应作为预期拒绝结果。STUN 仍不等同浏览器 ICE 全面审计。
- 系统守护使用 root 所有的 Python 组件，只写系统日志。进程、IPv4/IPv6 路由、可读取的运行分流规则发生异常时终止受保护进程。日志不再通过 root 重定向到用户目录。
- 一次性浏览器新增运行期检查，失败后请求终止该临时 profile 的进程；它仍存在进程巡检窗口。

## PF 的状态与边界

PF 默认关闭。安装、更新、普通检查都不会自动开启 PF。它是一个需显式启用的实验性网络防护组件，覆盖整机出站流量，不是单个应用的沙箱。

`network-guard.py plan --config <JSON>` 在任何平台仅生成待审规则；不会调用 pfctl。

启用、更新、监测、禁用只允许在 macOS 由 root 执行。安装到：

- `/Library/Application Support/claude-switcher/network-guard.py`
- 配置：`/Library/Application Support/claude-switcher/network-guard.json`
- 私有状态：`/var/run/claude-switcher-network-guard.json`（root 0600）
- 非敏感状态收据：`/var/run/claude-switcher-network-guard-status.json`（root 0644，25 秒有效）

### 有意限制的启用前提

1. 当前主过滤规则必须仅包含本项目明确的 quick 锚点调用：

   ```pf
   anchor "com.apple/claude-switcher" quick all
   ```

   默认 `com.apple/*` 通配锚点不足以证明调用顺序。存在其他父规则、兄弟锚点、早先 quick 放行或未知结构时拒绝启用。此工具不会修改 `/etc/pf.conf`，不会代用户重载全局规则。**不要为启用本工具而删除系统或其他产品的 PF 规则；已有复杂防火墙应继续使用原管理器，或在专用测试机上设计适配。**

2. 不允许 PF skip 接口；状态表必须为空。既有 PF state 能绕过新规则，因此工具不会把“锚点已经加载”当作强制阻断完成。工具不调用全局状态清除，需退出联网应用并由管理员检查状态来源。

3. 显式配置允许的 TUN 接口及代理服务器 IP/端口/协议/物理接口。只能使用 IP 字面量，不能使用域名或 `0.0.0.0/0`。

4. `acknowledge_machine_wide_filter` 必须明确为 `true`。

### 规则与失败行为

- 仅变更 `com.apple/claude-switcher` 独立锚点，先 `pfctl -n` 校验，后原子加载该锚点；不执行全局 `-f`、`-F`、`-d` 或 state kill。
- 允许回环、指定 TUN，以及物理接口上精确列出的代理端点。其余出站统一拒绝，包括新出现的接口、IPv4/IPv6、TCP/UDP 和其他协议。
- 所有允许规则为 `no state`，不会生成能在之后收紧规则时绕过限制的新 state。
- 启用/更新先安装拒绝规则；允许规则加载失败保留拒绝规则。
- 路由、配置、父规则或运行锚点发生漂移时记录未验证状态、尝试收紧本锚点并停止受保护应用。不会自动恢复放行，需管理员检查后显式 `update`。
- `disable` 只清空本锚点、释放本工具取得的 PF 引用；不关闭别人启用的 PF。
- root/admin 主动修改 PF、卸载规则、创建其他过滤旁路不属于可完全约束的威胁模型；异常时状态必须失败，不能显示受保护。
- 代理服务器换 IP、端口或新网络接口出现，会阻断，需要显式更新端点配置。这是默认拒绝的预期行为。

## 示例（只生成计划）

```bash
python3 core/network-guard.py plan --config docs/network/pf-example.json
```

示例使用文档保留 IP，不能直接用于联网。根据真实网络修改接口和精确端点后再审核；代理 IP 不要提交到公开仓库。

macOS 管理员审核后可执行：

```bash
sudo /usr/bin/python3 '/Library/Application Support/claude-switcher/network-guard.py' enable
sudo /usr/bin/python3 '/Library/Application Support/claude-switcher/network-guard.py' status --require-active
sudo /usr/bin/python3 '/Library/Application Support/claude-switcher/network-guard.py' update
sudo /usr/bin/python3 '/Library/Application Support/claude-switcher/network-guard.py' disable
```

## 测试完成与未完成范围

自动化行为测试使用假接口、路由、STUN 报文与 FakePF，覆盖启用前提、缺失确认、未知父规则、既有 state、skip 接口、失败回退、路由/规则漂移、独立禁用、错误事务/来源/报文长度以及 IPv6 接口漏检。未运行真实 pfctl 或修改当前机器网络。

**本环境不能进行 macOS 真机 PF 语法/加载及抓包验收。** 状态输出固定包含 `packet_capture_verified=false`，不能把 mock 测试当作物理零泄漏证明。发布使用前，需在可恢复的 Mac 上检查以下场景：

- 正常连接时，物理接口只出现显式代理端点流量；IPv4/IPv6 同时观察。
- 已建立 TCP/UDP 连接、停代理、接口消失、路由变更、规则变 DIRECT、睡眠唤醒、Wi-Fi/有线切换。
- 新的物理接口不能绕过默认拒绝；代理端点换 IP 时正确拒绝并能人工恢复。
- 故意使规则加载失败，确认拒绝锚点保留；重复 disable 不改变其他 PF 用户的状态。
- 对比启用前后的全局规则、其他锚点及系统网络配置，确认本工具没有覆盖其他配置。

Linux 当前仅实现相同的检测/运行规则门禁与用户态进程守护，没有实现 nftables 网络级强制阻断。不要把 macOS PF 的能力迁移宣称到 Linux。
