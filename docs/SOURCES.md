# 来源与取舍

查询日期：2026-11-02。精选规则固定在本仓库中维护，不自动执行或整包加载上游脚本。

| 用途 | 来源 | 取舍 |
|---|---|---|
| Claude 核心网络依赖 | [Claude Desktop 官方说明](https://code.claude.com/docs/en/desktop#network-access-requirements)、[Claude Code 网络说明](https://code.claude.com/docs/en/network-config#network-access-requirements) | 保留 `anthropic.com`、`claude.ai`、`claude.com`、`claude.app`、`claudeusercontent.com`、`claudemcpcontent.com` |
| IP 检测分类 | [v2fly 固定版本](https://github.com/v2fly/domain-list-community/blob/01add61fbaf719b55673d969615cd328e80f214c/data/category-ip-geo-detect) | 筛选常用站点，包含国内检测站 `cip.cc`、`ip138.com`、`ip.cn`、`ipip.net`，以及 `ip2location.com`、`browserscan.net`、`whoer.net`、`ip.skk.moe` |
| IPv6 检测 | [v2fly test-ipv6](https://github.com/v2fly/domain-list-community/blob/8f1acdf69923fde68e1cb6e8a58fb6e501fdaf2f/data/test-ipv6) | 保留 `test-ipv6.com` |
| 实际检测目标 | [Ping0 环境检测](https://ping0.cc/env)、[IPPure 出口检测说明](https://ippure.com/IP-Outbound-Detect) 与切换器检查器 | 补充 `ipapi.is`、`geojs.io`、`bigdatacloud.net`、`www.cloudflare.com`、`speed.cloudflare.com`；不把整个 `cloudflare.com` 作为检测站 |
| 国内域名与 IP 数据 | [MetaCubeX README 固定版本](https://github.com/MetaCubeX/meta-rules-dat/blob/4178770badecb1b349fbcd62c737e0d7a2079729/README.md) | 使用客户端现有 `cn` 集与国家 IP 数据；Meta 的 `cn` 来自 blackmatrix7 ChinaMax_Domain，GeoIP 沿用 Loyalsoldier 系列来源，并非原样 v2fly `cn` |
| 规则语义 | [Mihomo 官方规则文档](https://wiki.metacubex.one/config/rules/) | 从上到下匹配；检测例外置于国内直连之前；`GEOIP,CN` 不加 `no-resolve`，允许未入域名表的国内 IP 正常直连 |

额外交叉对照：[blackmatrix7 Claude 规则](https://github.com/blackmatrix7/ios_rule_script/blob/0fa60782abfe70f58da510e90d9086136cf0d855/rule/Clash/Claude/Claude.yaml)、[MetaCubeX anthropic 生成列表](https://github.com/MetaCubeX/meta-rules-dat/blob/dc60743738b8c81d45b61773d585b97d5c3f428d/geo/geosite/anthropic.yaml)。未复制其整包文件；v3 按用户提供的 Net.Coffee 列表补充共享遥测与客服依赖；这不代表这些域名只被 Claude 使用。

保留 v2fly 的 [MIT 许可证](../licenses/v2fly-MIT.txt)。MetaCubeX、blackmatrix7 等外部数据库各自遵循其许可证，本仓库只引用数据来源，不重新打包这些数据库。


## v3 更新（2026-11-02）

用户提供的 [Net.Coffee Claude 域名说明](https://ip.net.coffee/claude/site.html) 是本次增量来源，作为社区整理清单使用，不把其风控推断当作官方承诺。原有 51 条扩展为 69 条，保持国内及局域网直连、其他目标代理的原则。

- 补充两个核心后缀 `clau.de`、`claudemcpclient.com`，四个遥测/客服后缀，六个精确认证/CDN/统计域名，以及 `datadog`、`sentry`、`sift` 三个关键词。原有 `anthropic.com` 后缀已覆盖其 CDN、API、MCP、Console、Workbench 子域，不重复添加。
- 把 `net.coffee` 加入检测站例外，覆盖其检测子域。
- IP 兜底采用 [Anthropic 官方入站范围](https://platform.claude.com/docs/en/api/ip-addresses)：IPv4 `160.79.104.0/23`、IPv6 `2607:6bc0::/48`。社区页面给出的 IPv4 `/21` 在官方文档属于服务向外发起请求的范围，IPv6 `/32` 也不是该页公布的入站范围。IP 兜底不是完整的 CDN 地址清单。
- 不增加 `IP-ASN,399358`：当前规则已有全球非 CN 代理兜底，新增 ASN 数据库依赖的收益有限；也不为了规则文件强制下载数据库。域名分类 `GEOSITE,anthropic` 同理不作为额外运行依赖。
- 不增加 `GEOSITE,category-ntp`。NTP 时间戳基于 UTC，并不携带客户端时区；也没有从 Claude 官方资料确认“Claude Code 通过 NTP 返回时区进行检测”。参考 [IETF RFC 5905](https://www.rfc-editor.org/rfc/rfc5905.html)。系统时区仍由显式环境设置处理，不改变系统时间同步流量的既有分流。

关键词匹配可能使其他网站的同名服务流量经过代理，这是 v3 的可见副作用；单个关键词也不能覆盖所有未来新服务。IP 规则的 `no-resolve` 意味着不为匹配它额外触发域名解析，并非反向 DNS。规则命中、传输可达和账号可用是不同结论；本项目不承诺避免封号或通过全部第三方评分。
