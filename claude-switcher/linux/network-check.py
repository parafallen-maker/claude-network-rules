#!/usr/bin/env python3
"""Read-only interface/route evidence. Never configures a network or sends probes.

`tunnel` checks the kernel routes for multiple public IPv4 destinations, not a
magic subnet. `ipv6` inspects every active interface and IPv6 route. These are
local observations, not proof of a proxy's upstream routing policy.
"""
import argparse
import ipaddress
import json
import os
import platform
import re
import subprocess
import sys


V4_TARGETS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
V6_TARGETS = ("2606:4700:4700::1111", "2001:4860:4860::8888")
VIRTUAL = re.compile(r"^(?:utun|tun|tap|wg|ppp|ipsec|tailscale)[A-Za-z0-9_.-]*$")


def run(command):
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=4,
                                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"})
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


class EvidenceError(ValueError):
    """A command failure is unknown evidence, never a proven absent route."""


def route_output(command, runner=run):
    if runner is not run:
        output = runner(command)
        if output is None:
            return None  # Explicit absence supplied by a test/platform adapter.
        if not output.strip():
            raise EvidenceError("路由命令输出为空")
        return output
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=4,
                                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        raise EvidenceError("路由命令不可用或超时")
    if result.returncode:
        # Only an explicit kernel no-route result counts as absence. Permission
        # errors, missing binaries and malformed responses remain unknown.
        absence = ("network is unreachable", "no route to host", "not in table", "route has not been found")
        if any(message in (result.stderr + result.stdout).lower() for message in absence):
            return None
        raise EvidenceError("路由查询失败")
    if not result.stdout.strip():
        raise EvidenceError("路由查询返回空结果")
    return result.stdout


def parse_ifconfig(text):
    interfaces = {}
    current = None
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+):\s+flags=\d+<([^>]*)>", line)
        if match:
            name, flags = match.groups()
            flags = flags.split(",")
            current = {"name": name, "up": "UP" in flags, "active": "UP" in flags,
                       "loopback": "LOOPBACK" in flags, "virtual": bool(VIRTUAL.fullmatch(name)),
                       "addresses": []}
            interfaces[name] = current
        elif current is not None:
            if line.strip() == "status: inactive":
                current["active"] = False
            match = re.match(r"\s+inet6?\s+(\S+)", line)
            if match:
                try:
                    current["addresses"].append(str(ipaddress.ip_address(match.group(1).split("%")[0])))
                except ValueError:
                    pass
    return interfaces


def parse_linux_interfaces(text):
    interfaces = {}
    for item in json.loads(text or "[]"):
        name = item["ifname"]
        flags = item.get("flags", [])
        interfaces[name] = {
            "name": name, "up": "UP" in flags,
            "active": "UP" in flags and item.get("operstate", "UNKNOWN") != "DOWN",
            "loopback": "LOOPBACK" in flags, "virtual": bool(VIRTUAL.fullmatch(name)),
            "addresses": [entry["local"] for entry in item.get("addr_info", []) if "local" in entry],
        }
    return interfaces


def inventory(system=None, runner=run):
    system = system or platform.system()
    if system == "Darwin":
        return parse_ifconfig(runner(["/sbin/ifconfig", "-a"]))
    if system == "Linux":
        return parse_linux_interfaces(runner(["/usr/sbin/ip", "-j", "address", "show"]) or
                                      runner(["/sbin/ip", "-j", "address", "show"]) or
                                      runner(["/usr/bin/ip", "-j", "address", "show"]) or
                                      runner(["/bin/ip", "-j", "address", "show"]))
    return {}


def route_interface(address, system=None, runner=run):
    address = ipaddress.ip_address(address)
    system = system or platform.system()
    if system == "Darwin":
        output = route_output(["/sbin/route", "-n", "get", "-inet6" if address.version == 6 else "-inet", str(address)], runner)
        if output is None:
            return None
        match = re.search(r"(?m)^\s*interface:\s*([A-Za-z0-9_.-]+)\s*$", output)
        # Reject routes explicitly reported as unreachable/reject/blackhole.
        if re.search(r"\b(?:REJECT|BLACKHOLE)\b", output):
            return None
        if not match:
            raise EvidenceError("无法识别Darwin路由接口")
        return match.group(1)
    if system == "Linux":
        candidates = ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip", "/bin/ip")
        path = next((name for name in candidates if os.path.isfile(name)), candidates[0])
        output = route_output([path, "-j", "-6" if address.version == 6 else "-4", "route", "get", str(address)], runner)
        if output is None:
            return None
        try:
            rows = json.loads(output)
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                raise EvidenceError("无法识别Linux路由结果")
            if rows[0].get("type") in ("blackhole", "unreachable", "prohibit"):
                return None
            name = rows[0].get("dev")
            if not isinstance(name, str) or not name:
                raise EvidenceError("Linux路由缺少接口")
            return name
        except (ValueError, TypeError, KeyError):
            raise EvidenceError("无法识别Linux路由接口")
    raise EvidenceError("不支持此平台的路由证据")


def tunnel_evidence(interfaces, route, targets=V4_TARGETS):
    if not interfaces:
        return False, "无法读取网络接口，未确认隧道"
    used = set()
    for address in targets:
        name = route(address)
        item = interfaces.get(name, {})
        if not item.get("active") or not item.get("virtual") or item.get("loopback"):
            return False, "公共目标路由未由活动隧道接管:" + (name or "无路由")
        used.add(name)
    return True, "活动隧道与公共目标路由:" + ",".join(sorted(used))


def ipv6_evidence(interfaces, route):
    if not interfaces:
        return False, "无法读取网络接口，IPv6状态未知"
    external = []
    for name, item in interfaces.items():
        if not item.get("active") or item.get("loopback"):
            continue
        for raw in item.get("addresses", []):
            address = ipaddress.ip_address(raw)
            if address.version == 6 and not (address.is_loopback or address.is_link_local or address.is_unspecified):
                external.append(name)
    routes = [route(address) for address in V6_TARGETS]
    if not any(routes) and not external:
        return True, "全部活动接口未发现非链路本地IPv6地址或公网IPv6路由"
    for name in routes:
        item = interfaces.get(name, {})
        if not item.get("active") or not item.get("virtual"):
            return False, "存在未确认由隧道接管的IPv6路径:" + (name or ",".join(sorted(set(external))) or "未知")
    return True, "IPv6公共目标路由由活动隧道接管(非关闭):" + ",".join(sorted(set(routes)))


def health(system=None, runner=run):
    interfaces = inventory(system, runner)
    # Capture each route once: validation and reported evidence must describe
    # the same observations even when routing changes during this audit.
    routes = {address: route_interface(address, system, runner)
              for address in V4_TARGETS + V6_TARGETS}
    route = routes.__getitem__
    ok4, detail4 = tunnel_evidence(interfaces, route)
    ok6, detail6 = ipv6_evidence(interfaces, route)
    return {"ok": ok4 and ok6, "tunnel": {"ok": ok4, "detail": detail4},
            "ipv6": {"ok": ok6, "detail": detail6}, "interfaces": interfaces,
            "routes": routes, "upstream_policy_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("tunnel", "ipv6", "health", "inventory", "route"))
    parser.add_argument("addresses", nargs="*")
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            value = inventory()
            print(json.dumps(value, ensure_ascii=False, sort_keys=True))
            return 0 if value else 1
        if args.command == "route":
            if not args.addresses:
                parser.error("route requires literal IP addresses")
            interfaces = inventory()
            ok, detail = tunnel_evidence(interfaces, route_interface, args.addresses)
        elif args.command == "health":
            value = health()
            print(json.dumps(value, ensure_ascii=False, sort_keys=True))
            return 0 if value["ok"] else 1
        else:
            interfaces = inventory()
            check = tunnel_evidence if args.command == "tunnel" else ipv6_evidence
            ok, detail = check(interfaces, route_interface)
        print(("ok:" if ok else "fail:") + detail)
        return 0 if ok else 1
    except (ValueError, TypeError, KeyError, OSError):
        print("fail:网络证据读取失败，未确认安全状态")
        return 1


if __name__ == "__main__":
    sys.exit(main())
