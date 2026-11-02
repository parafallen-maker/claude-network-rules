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


DOMAINS = (
    "claude.ai", "claude.com", "anthropic.com", "claudeusercontent.com",
    "challenges.cloudflare.com", "datadoghq.com", "segment.io", "stripe.com",
    "stripe.network", "stripecdn.com", "intercom.io", "intercomcdn.com",
    "intercomassets.com",
)
PORTS = (3478, 5349, 19302)
SCRIPT_BEGIN = "// >>> claude-switcher managed rules >>>"
SCRIPT_END = "// <<< claude-switcher managed rules <<<"
MERGE_BEGIN = "# >>> claude-switcher pinned rules >>>"
MERGE_END = "# <<< claude-switcher pinned rules <<<"


def fail(message):
    print("FAIL:" + message, file=sys.stderr)
    raise SystemExit(1)


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
                candidates.append(
                    "/var/run/clash-verge-service/users/%d/verge-mihomo.sock" % os.getuid())
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
    if selected_type in {"direct", "reject", "block"}:
        fail("代理组当前节点为直连或拒绝，请选择代理节点")
    if policy == "proxy" and selected.get("udp") is not True:
        fail("当前节点未确认支持 UDP，请更换节点或选择 reject 策略")


def norm(value):
    return re.sub(r"[\s()_-]+", "", str(value).upper()).replace("&&", ",")


def expected_rules():
    rules = [("port", p) for p in PORTS]
    rules.extend(("domain", d) for d in DOMAINS)
    return rules


def verify_runtime(clash_dir, group, policy, allow_plain_ports=False):
    base, secret = controller_config(clash_dir)
    state = {
        "configs": api_get(base, secret, "/configs"),
        "rules": api_get(base, secret, "/rules"),
        "proxies": api_get(base, secret, "/proxies"),
    }
    if str(state["configs"].get("mode", "")).lower() != "rule":
        fail("Clash 当前不是 rule 模式")
    validate_group(state, group, policy)
    rules = state["rules"].get("rules")
    if not isinstance(rules, list):
        fail("Clash /rules 返回格式异常")
    expected = expected_rules()
    if len(rules) < len(expected):
        fail("运行中的规则数量不足")
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
        else:
            rule = rules[index]
            if (not isinstance(rule, dict)
                    or str(rule.get("type", "")).upper().replace("_", "-") not in {"DOMAIN-SUFFIX", "DOMAINSUFFIX"}
                    or str(rule.get("payload", "")).strip().lower() != value
                    or rule.get("proxy") != group
                    or (rule.get("extra") or {}).get("disabled") is True):
                fail("运行规则未按策略置顶:" + value)
    return state


def rule_strings(group, policy):
    target = "REJECT" if policy == "reject" else group
    return ([f"AND,((NETWORK,UDP),(DST-PORT,{port})),{target}" for port in PORTS] +
            [f"DOMAIN-SUFFIX,{domain},{group}" for domain in DOMAINS])


def managed_block(group, policy):
    encoded = json.dumps(rule_strings(group, policy), ensure_ascii=False)
    group_json = json.dumps(group, ensure_ascii=False)
    return f"""{SCRIPT_BEGIN}
;(function () {{
  const originalMain = main;
  const pinnedRules = {encoded};
  const group = {group_json};
  main = function (config, ...args) {{
    const result = originalMain(config, ...args);
    if (!result || typeof result !== \"object\" || Array.isArray(result)) throw new Error(\"claude-switcher: original main must return a config object\");
    if (!Array.isArray(result[\"proxy-groups\"]) || !result[\"proxy-groups\"].some((item) => item && item.name === group)) throw new Error(\"claude-switcher: configured proxy group missing\");
    if (result.rules !== undefined && !Array.isArray(result.rules)) throw new Error(\"claude-switcher: rules must be an array\");
    result.rules = pinnedRules.concat((result.rules || []).filter((rule) => !pinnedRules.includes(rule)));
    return result;
  }};
}})();
{SCRIPT_END}"""


def script_text(path, group, policy):
    old = read_text(path) if path.exists() else ""
    block = managed_block(group, policy)
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
            proxies = api_get(base, secret, "/proxies").get("proxies", {})
            selected = selected_proxy(proxies, "GLOBAL")
            kind = str(selected.get("type", "")).lower()
            if not kind or kind in {"direct", "reject", "block", "pass"}:
                fail("全局模式当前出口并非已确认的代理节点")
            if policy == "proxy" and selected.get("udp") is not True:
                fail("全局代理节点未确认支持UDP")
            print("OK:全局模式及当前代理节点已验证")
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
    new_script = script_text(script_path, group, policy)
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
