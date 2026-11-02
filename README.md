# Claude 与 IP 检测分流配置

供 Clash Verge / Mihomo 复用的个人规则：**Claude 和 IP 检测优先代理，国内及局域网直连，其余流量代理**。默认开启常见 STUN UDP 端口阻断。

## 规则顺序

| 顺序 | 流量 | 行为 |
|---|---|---|
| 1 | UDP 3478、5349、19302 | REJECT，保留 STUN 防护 |
| 2 | 6 个 Claude 域名后缀 | 指定代理组 |
| 3 | 31 个 IP、DNS、出口检测域名后缀 | 指定代理组，优先于国内直连 |
| 4 | 8 个回环、私有及链路本地网段 | DIRECT |
| 5 | `GEOSITE,cn` 国内域名 | DIRECT |
| 6 | `GEOIP,CN` 国内目标 IP | DIRECT，允许解析未命中域名集的请求 |
| 7 | 其余流量 | 指定代理组 |

共 51 条顶层规则，国内域名/IP 由已有地理数据库展开。**请保持规则模式**；全局模式绕过这张表，国内也会代理，STUN 拒绝规则也不执行。

## 在另一台设备复用

1. 先在 Clash Verge 导入自己的订阅，确认代理节点可用；按设备需要开启 TUN。仓库里的文件不含节点、订阅地址或凭据。
2. 确认目标代理组名称，默认是 `🤖 Claude`，并让该组选择实际代理节点。没有这个组时，用已有代理组名称生成：

   ```sh
   python3 scripts/generate.py --group '你的代理组'
   ```

3. 在 Clash Verge 的“订阅 → 全局扩展脚本”使用 `Script.js`，保存并“重新激活订阅”。如果已有自定义 `main(config)`，保留原逻辑，把 `applyOwnedRules()` 放在原逻辑返回配置之前调用；不要直接覆盖其他自定义功能。本机部署保留了已有脚本的非路由设置。
4. 选择“规则”模式。核对规则末尾依次为 `GEOSITE,cn,DIRECT`、`GEOIP,CN,DIRECT`、`MATCH,你的代理组`。
5. `dns.example.yaml` 是可选 DNS 片段，需通过 Clash Verge DNS 设置合并；它不是完整订阅。`rules.yaml` 是同一规则表的 YAML 形式，可供其他 Mihomo 配置合并。
6. 在内核连接记录中确认：百度/腾讯/哔哩哔哩直连，Claude/Ping0/cip.cc 代理。规则修改只决定新连接，已有长连接需重新连接或刷新页面。

需要可用的 `geosite.dat` 及国家 IP 数据库（默认 MMDB 模式通常为 `Country.mmdb`）。可继续使用客户端已有数据库；新设备缺失时，参考 [MetaCubeX 数据说明](https://github.com/MetaCubeX/meta-rules-dat) 下载标准数据。本仓库不附带或自动下载大型数据库。

## 文件与维护

- `owned-policy.json`：域名和本地网段的数据源。
- `scripts/generate.py`：仅依赖 Python 标准库，生成脚本、路由和 DNS 片段；不会改动系统或客户端配置。
- `Script.js` / `rules.yaml`：生成产物；替换完整旧路由表，防止旧的直连兜底混入。
- `dns.example.yaml`：业务 DoH 经代理；节点解析的引导 DNS 单独配置。国内站点的连接仍可直连，DNS 查询路径与网站连接路径分别控制。
- [来源与取舍](docs/SOURCES.md)、[验收记录](docs/VERIFICATION.md)、[脱敏分流证据](docs/routing-evidence.json)。

修改数据后重新运行生成器并检查：

```sh
python3 scripts/generate.py
python3 scripts/generate.py --check
```

更换代理组后，生成和检查时都使用相同的 `--group` 参数。默认 `--stun reject`；改为 `--stun proxy` 会把三个端口交给代理，需要节点真实支持 UDP，且需重新完成 STUN 实测。

本机 ClaudeSwitcher 定制版还会核对完整运行规则及用户/后台组件是否一致。若同时使用它，需要通过完整安装器同步其规则数据和校验组件；只更新 Clash 脚本会被旧版校验拒绝。

## 已知边界

Clash 按请求的目标域名/IP 分流，不能仅因请求来自 IP 检测网页就把其所有站外请求归为检测流量。检测页调用的未列入例外的国内端点仍会直连，所以综合“多出口检测”可能同时出现 CN 与 US；这与保留国内直连的目标一致。确需代理某个检测端点时，应核实其精确域名后单独加入。

三条端口规则覆盖常见 STUN，不代表阻断所有 WebRTC/TURN 变体。fake-IP 或 STUN 无响应本身不证明全部流量零泄漏。本机验证范围是 macOS + Clash Verge/Mihomo，其他客户端需自行验证。

此前“全部公网代理”的版本已被本次国内直连策略替代。不会通过关闭检查或删除历史记录来制造通过结果。

## macOS 持续监控与通知

[完整 ClaudeSwitcher 定制源码](claude-switcher/README.md)（`1.8.0-rc1.local3`）已补齐 VPN、节点及出口 IP 变化监控，并发送原生系统通知。保护对象是 `/Applications/Claude.app` 桌面版，未增加 Claude Code CLI 进程管理。

节点/VPN 每秒巡检；出口 IP 每 5 秒经明确代理探测，连续 20 秒无有效结果则停止桌面版 Claude。通知与进程停止分离，通知权限关闭不影响守护执行；停止失败会重试并拒绝恢复就绪。监控不会自动启动 Claude。

参见 [监控机制与安装说明](claude-switcher/docs/network/MONITOR.md)、[实际验收结果](docs/VERIFICATION.md) 和 [脱敏监控证据](docs/monitor-evidence.json)。原 51 条国内直连分流表保持不变。
