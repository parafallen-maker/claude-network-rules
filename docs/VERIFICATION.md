# 分流验收

验证日期：2026-11-01。环境：macOS、Clash Verge、Mihomo v1.19.32。

配套 ClaudeSwitcher `1.8.0-rc1.local2` 已完成安装及完整验收：14 项检查中 12 项通过、2 项保留提示，无失败或跳过。用户与后台的规则数据和校验组件一致，3 个后台服务均已注册，受保护组件为 root 所有且普通用户不可写，无待恢复安装事务。保留的提示分别是历史出口变更与本地 fake-IP 无法单独证明上游 DNS 链路，没有放宽检查条件。

- Mihomo 原生配置校验通过；`cn` 域名数据成功加载。
- 内核实际运行 `rule` 模式，51 条规则的数量、顺序、类型、目标全部与自有配置一致。
- 配套规则校验器的 11 项隔离回归测试通过，包括拒绝错误国内目标、国内规则提前覆盖 Claude、错误兜底、错误 STUN 策略、额外直连规则以及 global 模式。
- 本仓库生成的路由与本机定制配置相同；JavaScript 检查确认保留无关配置，并在代理组不存在时拒绝应用。
- 以下为对实际代理端口发起新连接后，从内核运行连接表读取的结果；没有仅凭配置文本推断。

| 目标 | 实际行为 | 命中规则 |
|---|---|---|
| `www.baidu.com` | DIRECT | GeoSite cn |
| `www.qq.com` | DIRECT | GeoSite cn |
| `www.bilibili.com` | DIRECT | GeoSite cn |
| `claude.ai` | 代理 | DomainSuffix claude.ai |
| `ping0.cc` | 代理 | DomainSuffix ping0.cc |
| `cip.cc` | 代理 | DomainSuffix cip.cc，优先于国内规则 |
| `github.com` | 代理 | Match |

机器可读结果见 [routing-evidence.json](routing-evidence.json)，仅保留目标、规则和是否通过，不含出口 IP、节点名或凭据。

这些连接测试证明分流选择，不等同于 Claude 账号鉴权、对话可用性或全系统绝对零泄漏。STUN 的实际无响应也必须结合已加载的拒绝规则判断。

国内直连恢复后，不再把综合检测页所有站外目标都显示美国作为验收条件；该条件会与国内直连需求冲突。Claude/IP 检测列表中的目标必须命中代理，普通国内站点必须命中直连。
