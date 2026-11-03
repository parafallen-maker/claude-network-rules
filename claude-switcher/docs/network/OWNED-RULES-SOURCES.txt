# 来源与取舍

查询日期：2026-11-01。精选规则固定在本仓库中维护，不自动执行或整包加载上游脚本。

| 用途 | 来源 | 取舍 |
|---|---|---|
| Claude 核心网络依赖 | [Claude Desktop 官方说明](https://code.claude.com/docs/en/desktop#network-access-requirements)、[Claude Code 网络说明](https://code.claude.com/docs/en/network-config#network-access-requirements) | 保留 `anthropic.com`、`claude.ai`、`claude.com`、`claude.app`、`claudeusercontent.com`、`claudemcpcontent.com` |
| IP 检测分类 | [v2fly 固定版本](https://github.com/v2fly/domain-list-community/blob/01add61fbaf719b55673d969615cd328e80f214c/data/category-ip-geo-detect) | 筛选常用站点，包含国内检测站 `cip.cc`、`ip138.com`、`ip.cn`、`ipip.net`，以及 `ip2location.com`、`browserscan.net`、`whoer.net`、`ip.skk.moe` |
| IPv6 检测 | [v2fly test-ipv6](https://github.com/v2fly/domain-list-community/blob/8f1acdf69923fde68e1cb6e8a58fb6e501fdaf2f/data/test-ipv6) | 保留 `test-ipv6.com` |
| 实际检测目标 | [Ping0 环境检测](https://ping0.cc/env)、[IPPure 出口检测说明](https://ippure.com/IP-Outbound-Detect) 与切换器检查器 | 补充 `ipapi.is`、`geojs.io`、`bigdatacloud.net`、`www.cloudflare.com`、`speed.cloudflare.com`；不把整个 `cloudflare.com` 作为检测站 |
| 国内域名与 IP 数据 | [MetaCubeX README 固定版本](https://github.com/MetaCubeX/meta-rules-dat/blob/4178770badecb1b349fbcd62c737e0d7a2079729/README.md) | 使用客户端现有 `cn` 集与国家 IP 数据；Meta 的 `cn` 来自 blackmatrix7 ChinaMax_Domain，GeoIP 沿用 Loyalsoldier 系列来源，并非原样 v2fly `cn` |
| 规则语义 | [Mihomo 官方规则文档](https://wiki.metacubex.one/config/rules/) | 从上到下匹配；检测例外置于国内直连之前；`GEOIP,CN` 不加 `no-resolve`，允许未入域名表的国内 IP 正常直连 |

额外交叉对照：[blackmatrix7 Claude 规则](https://github.com/blackmatrix7/ios_rule_script/blob/0fa60782abfe70f58da510e90d9086136cf0d855/rule/Clash/Claude/Claude.yaml)、[MetaCubeX anthropic 生成列表](https://github.com/MetaCubeX/meta-rules-dat/blob/dc60743738b8c81d45b61773d585b97d5c3f428d/geo/geosite/anthropic.yaml)。未复制其整包文件；通用统计、客服、支付域不作为 Claude 专属条目重复添加。

保留 v2fly 的 [MIT 许可证](../licenses/v2fly-MIT.txt)。MetaCubeX、blackmatrix7 等外部数据库各自遵循其许可证，本仓库只引用数据来源，不重新打包这些数据库。
