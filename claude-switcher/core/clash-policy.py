#!/usr/bin/env python3
"""Runtime Mihomo policy verification; shared by desktop/Linux and root watchdog."""
"""Install and verify the Clash Verge global script without trusting Merge.yaml text."""
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit


PORTS = (3478, 5349, 19302)
LOCAL_CIDRS = {"127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
               "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10"}
SCRIPT_BEGIN = "// >>> claude-switcher managed rules >>>"
SCRIPT_END = "// <<< claude-switcher managed rules <<<"
MERGE_BEGIN = "# >>> claude-switcher pinned rules >>>"
MERGE_END = "# <<< claude-switcher pinned rules <<<"


def fail(message):
    print("FAIL:" + message, file=sys.stderr)
    raise SystemExit(1)


def owned_policy():
    # The root watchdog reads only its own root-owned, installed sibling.
    path = Path(__file__).resolve().with_name("owned-policy.json")
    if path.is_symlink() or path.stat().st_size > 32768:
        fail("自有规则文件不安全或过大")
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get("schema") != 1
            or set(value) != {"schema", "name", "claude_domains", "ip_check_domains", "local_cidrs", "china_direct"}
            or value.get("china_direct") is not True):
        fail("自有规则文件格式无效")
    domains = []
    for key in ("claude_domains", "ip_check_domains"):
        items = value[key]
        if not isinstance(items, list) or not 1 <= len(items) <= 128:
            fail("自有域名列表无效")
        for domain in items:
            if not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", domain):
                fail("自有规则包含无效域名")
        domains.extend(items)
    cidrs = value["local_cidrs"]
    if (len(set(domains)) != len(domains) or not isinstance(cidrs, list)
            or any(not isinstance(cidr, str) or cidr not in LOCAL_CIDRS for cidr in cidrs)
            or len(set(cidrs)) != len(cidrs)):
        fail("自有规则重复或包含非局域网直连范围")
    return domains, cidrs


def read_yaml_scalar(path, key):
    if not path.exists():
        return ""
    match = re.search(r"^" + re.escape(key) + r":[ \t]*(.*)$", read_text(path), re.M)
    if not match:
        return ""
    value = match.group(1).strip()
    if value.startswith('"'):
        return json.JSONDecoder().raw_decode(value)[0]
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
        if not match:
            fail("无法读取 Clash 控制接口配置")
        return match.group(1).replace("''", "'")
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if value in {"", "null", "~"}:
        return ""
    if value.startswith(("|", ">", "*", "&", "[", "{")):
        fail("Clash 控制接口需要普通标量配置")
    return value


def read_text(path):
    return path.read_bytes().decode("utf-8-sig")


def controller_config(clash_dir):
    for name in ("clash-verge.yaml", "config.yaml", "verge.yaml"):
        path = clash_dir / name
        unix_path = read_yaml_scalar(path, "external-controller-unix")
        secret = read_yaml_scalar(path, "secret")
        if "\r" in secret or "\n" in secret:
            fail("Clash 控制接口密钥格式无效")
        if unix_path and hasattr(socket, "AF_UNIX"):
            if not os.path.isabs(unix_path):
                fail("Clash Unix socket 路径必须为绝对路径")
            # 生成的配置可能记着已失效的临时 socket(Clash Verge 重启后路径会变);
            # 依次尝试配置路径与服务实际使用的用户级 socket,都存在才继续走 TCP 分支
            candidates = [unix_path]
            if hasattr(os, "getuid"):
                # A root daemon checks the account owner's Clash instance.
                # The service socket is keyed by that account, not daemon uid 0.
                service_uid = Path(clash_dir).stat().st_uid
                candidates.append(
                    "/var/run/clash-verge-service/users/%d/verge-mihomo.sock" % service_uid)
            for candidate in candidates:
                if os.path.isabs(candidate) and Path(candidate).exists():
                    return ("unix", candidate, None), secret
        endpoint = read_yaml_scalar(path, "external-controller")
        if endpoint:
            if not endpoint.startswith(("http://", "https://")):
                endpoint = "http://" + endpoint
            parsed = urlsplit(endpoint)
            host = parsed.hostname
            if host in {"0.0.0.0", "::"}:
                host = "127.0.0.1" if host == "0.0.0.0" else "::1"
            if host != "localhost":
                try:
                    local = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    local = False
                if not local:
                    fail("只允许验证本机 Clash 控制接口")
            if (parsed.scheme not in {"http", "https"} or not parsed.port
                    or parsed.username or parsed.password or parsed.path not in {"", "/"}
                    or parsed.query or parsed.fragment):
                fail("Clash 控制接口地址格式无效")
            return (parsed.scheme, host, parsed.port), secret
    fail("未找到本机 Clash 控制接口，无法验证内核规则")


def api_get(endpoint, secret, path):
    headers = {"Accept": "application/json"}
    if secret:
        headers["Authorization"] = "Bearer " + secret
    scheme, host, port = endpoint
    connection = None
    try:
        if scheme == "unix":
            connection = http.client.HTTPConnection("localhost", timeout=2)
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(2)
            connection.sock.connect(host)
        else:
            cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            connection = cls(host, port, timeout=2)
        # http.client connects directly; inherited proxy variables are never used.
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            fail("Clash 内核接口未返回成功状态")
        data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            fail("Clash 内核接口响应过大")
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict):
            fail("Clash 内核接口返回格式异常")
        return value
    except (http.client.HTTPException, OSError, ValueError) as exc:
        fail("Clash 内核接口不可用(" + exc.__class__.__name__ + ")")
    finally:
        if connection:
            connection.close()


def selected_proxy(proxies, name, seen=None):
    seen = set() if seen is None else seen
    if name in seen:
        fail("代理组存在循环引用")
    seen.add(name)
    item = proxies.get(name)
    if not isinstance(item, dict):
        fail("代理组不存在:" + name)
    if "all" in item or "now" in item:
        current = item.get("now")
        if not current or current == name:
            fail("代理组没有可用的当前节点:" + name)
        return selected_proxy(proxies, current, seen)
    return item


def validate_group(state, group, policy):
    proxies = state.get("proxies", {}).get("proxies", {})
    if not isinstance(proxies, dict) or group in {"DIRECT", "REJECT", "GLOBAL", "PASS"}:
        fail("请选择实际的代理组")
    if not isinstance(proxies.get(group), dict) or "all" not in proxies[group]:
        fail("配置的代理组不存在")
    selected = selected_proxy(proxies, group)
    selected_type = str(selected.get("type", "")).lower()
    if not selected_type or selected_type in {"direct", "reject", "block", "pass"}:
        fail("代理组当前节点为直连或拒绝，请选择代理节点")
    if policy == "proxy" and selected.get("udp") is not True:
        fail("当前节点未确认支持 UDP，请更换节点或选择 reject 策略")


def norm(value):
    return re.sub(r"[\s()_-]+", "", str(value).upper()).replace("&&", ",")


def expected_rules():
    domains, cidrs = owned_policy()
    rules = [("port", p) for p in PORTS]
    rules.extend(("domain", d) for d in domains)
    rules.extend(("cidr", c) for c in cidrs)
    rules.extend((("geosite", "cn"), ("geoip", "cn")))
    rules.append(("match", ""))
    return rules


def verify_runtime(clash_dir, group, policy, allow_plain_ports=False, require_node=True):
    base, secret = controller_config(clash_dir)
    state = {
        "configs": api_get(base, secret, "/configs"),
        "rules": api_get(base, secret, "/rules"),
        "proxies": api_get(base, secret, "/proxies"),
    }
    if str(state["configs"].get("mode", "")).lower() != "rule":
        fail("请切换到规则模式；国内直连、Claude/IP 检测优先代理，全局模式会绕过分流和 STUN 防护")
    if require_node:
        validate_group(state, group, policy)
    rules = state["rules"].get("rules")
    if not isinstance(rules, list):
        fail("Clash /rules 返回格式异常")
    expected = expected_rules()
    if len(rules) != len(expected):
        fail("运行规则数量与自有规则不一致，可能混入旧分流规则")
    for index, (kind, value) in enumerate(expected):
        if kind == "port":
            rule = rules[index]
            target = "REJECT" if policy == "reject" else group
            payload = norm(rule.get("payload", "")) if isinstance(rule, dict) else ""
            kind_value = str(rule.get("type", "")).upper().replace("_", "-") if isinstance(rule, dict) else ""
            matches = kind_value == "AND" and payload == "NETWORK,UDP,DSTPORT," + str(value)
            if allow_plain_ports and kind_value in {"DST-PORT", "DSTPORT", "DESTPORT"} and payload == str(value):
                matches = True  # FlClash's stored destination-port rule covers UDP and TCP.
            if (not isinstance(rule, dict) or not matches
                    or rule.get("proxy") != target
                    or (rule.get("extra") or {}).get("disabled") is True):
                fail("运行规则未按策略置顶:UDP/" + str(value))
        elif kind == "domain":
            rule = rules[index]
            if (not isinstance(rule, dict)
                    or str(rule.get("type", "")).upper().replace("_", "-") not in {"DOMAIN-SUFFIX", "DOMAINSUFFIX"}
                    or str(rule.get("payload", "")).strip().lower() != value
                    or rule.get("proxy") != group
                    or (rule.get("extra") or {}).get("disabled") is True):
                fail("运行规则未按策略置顶:" + value)
        elif kind in {"geosite", "geoip"}:
            rule = rules[index]
            if (not isinstance(rule, dict) or norm(rule.get("type", "")) != kind.upper()
                    or str(rule.get("payload", "")).lower() != value
                    or rule.get("proxy") != "DIRECT"
                    or (rule.get("extra") or {}).get("disabled") is True):
                fail("国内直连规则缺失或顺序不正确:" + kind)
        else:
            rule = rules[index]
            allowed_types = {"MATCH"} if kind == "match" else ({"IPCIDR6", "IPCIDR"} if ":" in value else {"IPCIDR"})
            if (not isinstance(rule, dict) or norm(rule.get("type", "")) not in allowed_types
                    or str(rule.get("payload", "")) != value
                    or rule.get("proxy") != (group if kind == "match" else "DIRECT")
                    or (rule.get("extra") or {}).get("disabled") is True):
                fail("运行规则与自有规则不一致:局域网/最终代理")
    return state


def rule_strings(group, policy):
    domains, cidrs = owned_policy()
    target = "REJECT" if policy == "reject" else group
    return ([f"AND,((NETWORK,UDP),(DST-PORT,{port})),{target}" for port in PORTS] +
            [f"DOMAIN-SUFFIX,{domain},{group}" for domain in domains] +
            [f"{'IP-CIDR6' if ':' in cidr else 'IP-CIDR'},{cidr},DIRECT,no-resolve" for cidr in cidrs] +
            ["GEOSITE,cn,DIRECT", "GEOIP,CN,DIRECT"] +
            [f"MATCH,{group}"])


def environment_block(group, options):
    # This code runs after all subscription/global merges, so it also works when
    # the next subscription does not provide the installed group name.
    keys = {"proxy_group", "browser_port", "configure_dns", "auto_group", "enable_browser_proxy"}
    if (not isinstance(options, dict) or set(options) != keys or options["proxy_group"] != group
            or type(options["browser_port"]) is not int or not 1024 <= options["browser_port"] <= 65535
            or any(type(options[k]) is not bool for k in ("configure_dns", "auto_group", "enable_browser_proxy"))):
        fail("环境配置格式无效")
    encoded = json.dumps(options, ensure_ascii=False)
    return """
    const env = %s;
    result["claude-switcher-environment"] = JSON.stringify(env);
    const groups = result["proxy-groups"] || (result["proxy-groups"] = []);
    if (!Array.isArray(groups)) throw new Error("claude-switcher: invalid proxy groups");
    if (!groups.some(g => g && g.name === group) && env.auto_group) {
      const nodes = (result.proxies || []).filter(p => p && p.name !== group && !["direct","reject","pass"].includes(String(p.type).toLowerCase())).map(p => p.name);
      const providers = Object.keys(result["proxy-providers"] || {});
      const added = {name:group,type:"select",proxies:["REJECT", ...nodes]};
      if (providers.length) added.use = providers;
      groups.push(added);
    }
    const listeners = result.listeners || [];
    if (!Array.isArray(listeners)) throw new Error("claude-switcher: invalid listeners");
    const own = l => l && l.name === "adspower-claude-local" && l.type === "http" && l.listen === "127.0.0.1" && l.proxy === group;
    if (listeners.some(l => l && (l.name === "adspower-claude-local" || (env.enable_browser_proxy && Number(l.port) === env.browser_port)) && !own(l))) throw new Error("claude-switcher: browser listener conflicts with an existing listener");
    for (const key of ["port","mixed-port","socks-port","redir-port","tproxy-port"]) {
      if (env.enable_browser_proxy && Number(result[key]) === env.browser_port) throw new Error("claude-switcher: browser port is already used");
    }
    for (const address of [result["external-controller"], result["external-controller-tls"], (result.dns || {}).listen]) {
      const match = typeof address === "string" ? address.match(/:([0-9]+)$/) : null;
      if (env.enable_browser_proxy && match && Number(match[1]) === env.browser_port) throw new Error("claude-switcher: browser port conflicts with DNS/controller");
    }
    result.listeners = listeners.filter(l => !own(l));
    if (env.enable_browser_proxy) result.listeners.push({name:"adspower-claude-local",type:"http",listen:"127.0.0.1",port:env.browser_port,proxy:group});
    if (env.configure_dns) {
      const dns = result.dns || {};
      if (typeof dns !== "object" || Array.isArray(dns)) throw new Error("claude-switcher: invalid DNS");
      result.dns = {...dns, enable:true, "enhanced-mode":"fake-ip", "respect-rules":true,
        nameserver:["https://1.1.1.1/dns-query#" + group], "nameserver-policy":{}, fallback:[],
        "direct-nameserver":[], "proxy-server-nameserver": dns["proxy-server-nameserver"] && dns["proxy-server-nameserver"].length ? dns["proxy-server-nameserver"] : ["https://1.1.1.1/dns-query"]};
    }
    """ % encoded


def managed_block(group, policy, options=None):
    encoded = json.dumps(rule_strings(group, policy), ensure_ascii=False)
    group_json = json.dumps(group, ensure_ascii=False)
    environment = environment_block(group, options) if options is not None else ""
    return f"""{SCRIPT_BEGIN}
;(function () {{
  const originalMain = main;
  const pinnedRules = {encoded};
  const group = {group_json};
  main = function (config, ...args) {{
    const result = originalMain(config, ...args);
    if (!result || typeof result !== \"object\" || Array.isArray(result)) throw new Error(\"claude-switcher: original main must return a config object\");
    {environment}
    if (!Array.isArray(result[\"proxy-groups\"]) || !result[\"proxy-groups\"].some((item) => item && item.name === group)) throw new Error(\"claude-switcher: configured proxy group missing\");
    if (result.rules !== undefined && !Array.isArray(result.rules)) throw new Error(\"claude-switcher: rules must be an array\");
    result.rules = pinnedRules.slice();
    result.mode = "rule";
    return result;
  }};
}})();
{SCRIPT_END}"""


def script_text(path, group, policy, options=None):
    old = read_text(path) if path.exists() else ""
    block = managed_block(group, policy, options)
    if SCRIPT_BEGIN in old:
        if old.count(SCRIPT_BEGIN) != 1 or old.count(SCRIPT_END) != 1:
            fail("Script.js 中存在未闭合的 claude-switcher 管理块")
        pattern = re.compile(re.escape(SCRIPT_BEGIN) + r".*?" + re.escape(SCRIPT_END), re.S)
        if old.index(SCRIPT_END) < old.index(SCRIPT_BEGIN):
            fail("Script.js 中的管理块顺序无效")
        return pattern.sub(lambda _: block, old, count=1)
    if SCRIPT_END in old:
        fail("Script.js 中存在孤立的 claude-switcher 管理块")
    if not old.strip():
        old = "function main(config) { return config; }\n"
    match = re.search(r"function\s+main\s*\(", old)
    if not match:
        fail("现有 Script.js 没有可组合的 main(config)，已停止以免覆盖用户脚本")
    return old + ("" if old.endswith("\n") else "\n") + block + "\n"


def migrate_merge(path):
    if not path.exists():
        return None
    old = read_text(path)
    if MERGE_BEGIN not in old:
        if MERGE_END in old:
            fail("Merge.yaml 中存在孤立的 claude-switcher 管理块")
        return old
    if old.count(MERGE_BEGIN) != 1 or old.count(MERGE_END) != 1 or old.index(MERGE_END) < old.index(MERGE_BEGIN):
        fail("Merge.yaml 中存在未闭合的 claude-switcher 管理块")
    pattern = re.compile(r"(?ms)^[ \t]*" + re.escape(MERGE_BEGIN) + r"[ \t]*\r?\n.*?^[ \t]*" + re.escape(MERGE_END) + r"[ \t]*\r?\n?")
    new = pattern.sub("", old, count=1)
    return new


def write_changes(changes):
    changes = {path: data.encode("utf-8") for path, data in changes.items()
               if not path.exists() or path.read_bytes() != data.encode("utf-8")}
    originals, staged, replaced = {}, {}, []
    try:
        for path, data in changes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            originals[path] = path.read_bytes() if path.exists() else None
            if path.exists():
                shutil.copy2(path, path.with_name(path.name + ".bak." + str(time.time_ns())))
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".claude-switcher-", delete=False) as handle:
                staged[path] = Path(handle.name)
                handle.write(data)
            if path.exists():
                staged[path].chmod(path.stat().st_mode & 0o777)
        for path, temp in staged.items():
            os.replace(temp, path)
            replaced.append(path)
    except OSError:
        for path in reversed(replaced):
            if originals[path] is None:
                path.unlink()
            else:
                path.write_bytes(originals[path])
        raise
    finally:
        for temp in staged.values():
            if temp.exists():
                temp.unlink()


def assess_probe(policy, expected_ip, output, exit_code):
    if exit_code != "0":
        return "fail:STUN 探测执行失败，未确认 UDP 出口"
    try:
        expected = {ipaddress.ip_address(value) for value in expected_ip.split(",")}
    except ValueError:
        return "fail:未获取有效的出口地址，无法比对 STUN 结果"
    if output == "RESULT:NONE":
        if policy == "reject":
            return "ok:常见 STUN UDP 拒绝规则已验证，探测无响应"
        return "fail:STUN 无响应，无法确认 UDP 是否经过代理"
    if not output.startswith("RESULT:"):
        return "fail:STUN 探测结果格式异常"
    try:
        addresses = [ipaddress.ip_address(value) for value in output[7:].split(",")]
    except ValueError:
        return "fail:STUN 探测结果格式异常"
    if policy == "reject":
        return "fail:拒绝策略下仍收到 STUN 响应，请检查运行规则"
    if any(address not in expected for address in addresses):
        return "fail:STUN 暴露了非代理出口地址"
    if {address.version for address in addresses} != {address.version for address in expected}:
        return "fail:某个已启用的IP协议族没有STUN响应，无法验证出口"
    return "ok:已响应的STUN节点仅返回已知出口(不等同完整浏览器ICE审计)"


def main():
    if len(sys.argv) < 5:
        fail("内部参数不足")
    action, clash_dir, base_dir, group = sys.argv[1:5]
    clash_dir = Path(clash_dir)
    base_dir = Path(base_dir)
    policy_path = base_dir / "webrtc-policy"
    policy = sys.argv[5] if len(sys.argv) > 5 else ""
    policy = policy or (read_text(policy_path).strip() if policy_path.exists() else "proxy")
    if policy not in {"proxy", "reject"}:
        fail("WEBRTC_POLICY 只能是 proxy 或 reject")
    if action == "policy":
        print(policy)
        return 0
    if action == "assess" and len(sys.argv) == 9:
        print(assess_probe(policy, *sys.argv[6:9]))
        return 0
    if action == "mode":
        base, secret = controller_config(clash_dir)
        print(str(api_get(base, secret, "/configs").get("mode", "")).lower())
        return 0
    if not group or any(char in group for char in ",\r\n"):
        fail("代理组名为空或包含规则分隔符")
    if action in {"verify-egress", "guard"}:
        base, secret = controller_config(clash_dir)
        mode = str(api_get(base, secret, "/configs").get("mode", "")).lower()
        if mode == "global":
            fail("全局模式绕过国内直连分流和 STUN 防护；请使用规则模式")
        else:
            if action == "guard":
                rows = api_get(base, secret, "/rules").get("rules", [])
                if len(rows) < 4 or not isinstance(rows[3], dict):
                    fail("无法从运行规则确定代理组")
                group = rows[3].get("proxy", "")
            verify_runtime(clash_dir, group, policy, allow_plain_ports=action == "guard")
            print("OK:运行内核置顶规则及代理节点已验证")
        return 0
    if action == "verify":
        verify_runtime(clash_dir, group, policy)
        print("OK:运行时规则已验证(" + policy + ")")
        return 0
    if action != "write":
        fail("未知内部动作")

    base, secret = controller_config(clash_dir)
    state = {"configs": api_get(base, secret, "/configs"),
             "proxies": api_get(base, secret, "/proxies")}
    if str(state["configs"].get("mode", "")).lower() != "rule":
        fail("Clash 当前不是 rule 模式")
    validate_group(state, group, policy)
    profiles = clash_dir / "profiles"
    script_path = profiles / "Script.js"
    merge_path = profiles / "Merge.yaml"
    settings_path = base_dir / "environment-settings.json"
    options = json.loads(read_text(settings_path)) if settings_path.exists() else None
    if options is not None and options.get("proxy_group") != group:
        fail("环境配置代理组与安装设置不一致，请重新配置")
    new_script = script_text(script_path, group, policy, options)
    new_merge = migrate_merge(merge_path)
    changes = {script_path: new_script, policy_path: policy + "\n"}
    if new_merge is not None:
        changes[merge_path] = new_merge
    write_changes(changes)
    print("OK:全局脚本已写入(" + policy + ")")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, AttributeError):
        fail("规则配置处理失败，未确认修复；请检查配置格式与权限")
