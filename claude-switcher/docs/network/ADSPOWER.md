# AdsPower 浏览器代理配置

`core/browser-proxy.py` 为明确选定的 AdsPower 环境配置本机 HTTP 代理，并保留恢复原设置所需的最小记录。它不启动、关闭或创建浏览器环境，不修改账号、Cookie、User-Agent、Canvas 或其他指纹字段，也不为用户创建 API 密钥。

使用前应启动 AdsPower、开启 Local API，并确保账号具有相应 API 权限。默认地址为 `http://127.0.0.1:50325`，仅接受本机 HTTP 地址；`localhost` 固定解析为 `127.0.0.1`。API 密钥只通过标准输入传入，使用完即丢弃，不进入命令参数、日志或恢复文件。程序不使用环境代理，不跟随 HTTP 重定向。

## CLI 合同

```text
python3 core/browser-proxy.py list|status|configure|restore
```

标准输入是 JSON 对象，标准输出是脱敏 JSON。成功退出 0，失败退出 1。可接受字段如下：

| 字段 | 含义 |
| --- | --- |
| `endpoint` | 可选，本机 API 地址，默认 `http://127.0.0.1:50325` |
| `api_key` | 可选，仅在 AdsPower 开启 API 安全验证时提供 |
| `profile_id` | `status`、`configure`、`restore` 必填；使用列表返回的环境 ID |
| `port` | 可选，本机 HTTP 代理端口，默认 `7898`；不会修改代理软件监听端口 |
| `page` | 可选，`list` 的页码，默认 1；每页最多 100 个环境 |

`list` 仅返回环境 ID 与名称，以及 `page`、`has_more`。`status` 返回本机代理是否匹配、可读取的 WebRTC 模式、环境运行状态及是否有恢复记录，不输出原代理服务器、用户名或密码。

`configure` 要求环境已关闭，把代理设为 `http://127.0.0.1:<port>`，并通过查询接口读回验证。设置已经匹配时保持不变。存在不同目标或尚未恢复的记录时，需先执行 `restore`。

如果 API 能读取 `fingerprint_config.webrtc`，工具只把这一字段设为 `disabled`，保留其原值用于恢复。**官方 V2 环境列表目前未承诺返回这个字段**；未返回时，工具只配置代理，输出 `complete: false` 和 `manual_required`，要求用户在 AdsPower 指纹设置中手动选择 WebRTC → Disabled。不能把无法读取的设置当成已验证，也不能为了完成状态盲写不可恢复的指纹字段。禁用 WebRTC 会影响依赖它的音视频通话。

`ok: true` 表示本次操作成功；`complete: true` 仅表示本机代理及 WebRTC 托管目标均已读回匹配。它不代表实际出口国家正确、网络无泄漏或全部浏览器指纹一致。浏览器环境需要另行打开检测页验收；工具本身不会打开它。

## 恢复与失败处理

恢复记录保存在 `~/.claude-switcher/browser-proxy/`，目录权限 `0700`，文件权限 `0600`，文件名不含环境名称。记录只包含环境 ID、本机 API 地址、原/目标代理字段与可托管的 WebRTC 值；恢复原代理需要时，原代理用户名和密码会保存在该私有文件内。不得将该目录加入日志、诊断包或公开仓库。

修改前先持有共享 `state.lock`、确认没有待恢复的安装/更新、确认浏览器环境已关闭，再备份原设置。写入后查询不一致时尝试恢复；不能确认恢复成功时保留记录并返回 `recovery_required: true`。`restore` 只恢复本工具托管的值；发现代理或 WebRTC 已被其他操作改为不同值时拒绝覆盖。API 没有条件更新机制，因此操作期间应保持该环境关闭，避免同时从其他客户端编辑。

环境列表、状态查询均为只读。已有原代理以 AdsPower 保存的代理 ID 绑定时，恢复使用原 `proxyid`。不支持或无法完整恢复的代理字段会阻止修改。

## 验证与来源

隔离测试使用临时目录和本机假 API，不读取真实 AdsPower 环境、不操作已安装切换器：

```text
python3 -m unittest discover -s tests -p test_browser_proxy.py -v
```

测试覆盖读回验证、恢复、部分写入回滚、外部修改冲突、运行中拒改、共享锁、权限、隐私及不跟随重定向。真实 AdsPower 的 API 授权、版本兼容性和浏览器出口仍需实际环境验收。

- [AdsPower Local API 概述](https://localapi-doc-en.adspower.com/docs/Rdw7Iu)
- [查询环境 V2：代理配置与代理 ID](https://localapi-doc-en.adspower.com/docs/Query-Profile-V2)
- [更新环境 V1：可省略未修改的字段](https://localapi-doc-en.adspower.com/docs/GMvym2)
- [代理配置字段](https://localapi-doc-en.adspower.com/docs/Lb8pOg)
- [浏览器运行状态 V2](https://localapi-doc-en.adspower.com/docs/Check-Browser-Status-V2)
- [编辑前关闭环境](https://help.adspower.com/docs/editing)
- [WebRTC 模式说明](https://help.adspower.com/docs/How-to-choose-WebRTC-mode)
