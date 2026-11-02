# ClaudeSwitcher macOS 定制源码

版本 `1.8.0-rc1.local3`，基于原安装包 `1.8.0-rc1`，增加国内直连规则核验、节点/出口 IP 变化监控与系统通知。保留原项目 MIT 许可证。

这是完整组件源码，包含原版未修改的 Windows/Linux 组件以保持原组件清单完整；本次新增能力只适用于 macOS，实际验证平台为 Clash Verge/Mihomo。

## 构建安装

在本目录执行：

```sh
python3 core/component-manager.py verify --source .
python3 -m unittest discover -s tests -v
WEBRTC_POLICY=reject python3 installer/install.py install --source . --proxy-mode clash-verge --proxy-group '🤖 Claude' --country US --timezone America/New_York --locale en_US
```

运行前应已有可用代理、TUN 和本仓库的 51 条分流规则；代理组名称及目标地区应与实际配置一致。安装器会编译原生 App 并请求管理员身份验证。上述命令不变更时区/IPv6，也不重写 Clash 配置；若需这些动作，使用原安装器提供的显式选项。首次运行通知组件时允许系统通知。

详见 [持续监控说明](docs/network/MONITOR.md) 及仓库根目录的验收记录。

改动 core/、daemons/、installer/、app/ 等组件后执行 `bash scripts/gen-sums.sh`，同步提交 COMPONENTS.json 和 SHA256SUMS。不要手工替换半套用户/root 组件。
