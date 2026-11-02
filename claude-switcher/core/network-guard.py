#!/usr/bin/env python3
"""Opt-in macOS PF guard. No install, global ruleset reload, or global state flush.

This guard deliberately REFUSES unknown parent rulesets. Before enabling it an
administrator must review/install the documented first, quick anchor call. A
fresh packet-capture acceptance test is still required on the actual Mac.
"""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time


ANCHOR = "com.apple/claude-switcher"
CONFIG = Path("/Library/Application Support/claude-switcher/network-guard.json")
STATE = Path("/var/run/claude-switcher-network-guard.json")
RECEIPT = Path("/var/run/claude-switcher-network-guard-status.json")
LOCK = Path("/var/run/claude-switcher-network-guard.lock")
PARENT_RULE = 'anchor "com.apple/claude-switcher" quick all'
RECEIPT_MAX_AGE = 25


class GuardError(Exception):
    pass


def load_module():
    path = Path(__file__).with_name("network-check.py")
    spec = importlib.util.spec_from_file_location("switcher_network_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_config(value):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise GuardError("PF配置必须包含version=1")
    allowed = {"version", "acknowledge_machine_wide_filter", "physical_interfaces", "tunnel_interfaces", "proxy_endpoints"}
    if set(value) - allowed:
        raise GuardError("PF配置包含未知字段")
    for key in ("physical_interfaces", "tunnel_interfaces"):
        names = value.get(key)
        if (not isinstance(names, list) or not 1 <= len(names) <= 16 or
                any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,31}", name) for name in names) or
                len(set(names)) != len(names)):
            raise GuardError("PF接口列表无效")
    if set(value["physical_interfaces"]) & set(value["tunnel_interfaces"]):
        raise GuardError("物理接口与隧道接口不能重叠")
    if any(not re.fullmatch(r"(?:utun|tun|tap|wg|ppp|ipsec)[0-9]+", name) for name in value["tunnel_interfaces"]):
        raise GuardError("只允许明确命名的隧道接口")
    if any(name.startswith(("lo", "utun", "tun", "wg")) for name in value["physical_interfaces"]):
        raise GuardError("物理接口列表包含回环或隧道")
    endpoints = value.get("proxy_endpoints")
    if not isinstance(endpoints, list) or not 1 <= len(endpoints) <= 64:
        raise GuardError("必须显式列出代理服务器IP、端口、协议和物理接口")
    for item in endpoints:
        if not isinstance(item, dict) or set(item) != {"address", "port", "protocol", "interface"}:
            raise GuardError("代理端点字段不完整")
        try:
            address = ipaddress.ip_address(item["address"])
        except (ValueError, TypeError):
            raise GuardError("代理端点必须是IP字面量，不能是域名/网段")
        if address.is_unspecified or address.is_multicast or address.is_loopback:
            raise GuardError("代理端点地址无效")
        if type(item["port"]) is not int or not 1 <= item["port"] <= 65535:
            raise GuardError("代理端点端口无效")
        if item["protocol"] not in ("tcp", "udp") or item["interface"] not in value["physical_interfaces"]:
            raise GuardError("代理端点协议/接口无效")
    return value


def render_rules(config, lockdown=False):
    validate_config(config)
    lines = ['# Claude Switcher: opt-in machine-wide outgoing restriction.',
             'pass out quick on lo0 all no state label "cs-loopback"']
    if not lockdown:
        for name in config["tunnel_interfaces"]:
            lines.append('pass out quick on %s all no state label "cs-tunnel"' % name)
        for item in config["proxy_endpoints"]:
            family = "inet6" if ipaddress.ip_address(item["address"]).version == 6 else "inet"
            lines.append('pass out quick on %s %s proto %s from any to %s port %d no state label "cs-proxy"' %
                         (item["interface"], family, item["protocol"], item["address"], item["port"]))
    # Both IPv4 and IPv6; UDP, TCP and all other protocols. New/unlisted
    # interfaces stay blocked. No states are created by any allow rule.
    lines.append('block drop out quick all label "cs-default-block"')
    return "\n".join(lines) + "\n"


def parent_is_supported(text):
    rules = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    # A wildcard alone is NOT sufficient. No other parent/sibling rule can
    # pre-empt, override, or create states behind this anchor.
    return len(rules) == 1 and bool(re.fullmatch(
        r'anchor "com\.apple/claude-switcher" (?:quick all|all quick)', rules[0]))


def secure_path(path, must_exist=True):
    path = Path(path)
    if not path.is_absolute():
        raise GuardError("特权路径必须是绝对路径")
    # macOS /var is a root-owned symlink to /private/var. Resolve the fixed
    # trusted root prefix, never a user-controlled final component.
    if path.parts[:3] == ("/", "var", "run"):
        path = Path("/private/var/run").joinpath(*path.parts[3:]) if platform.system() == "Darwin" else path
    for component in [*reversed(path.parents), path]:
        try:
            info = component.lstat()
        except FileNotFoundError:
            if component == path and not must_exist:
                break
            if component == path:
                raise FileNotFoundError(str(path))
            raise GuardError("特权路径缺失")
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise GuardError("特权路径必须由root控制且不可为符号链接")
    return path


def read_json(path, privileged=True):
    if privileged:
        path = secure_path(path)
    data = Path(path).read_bytes()
    if len(data) > 65536:
        raise GuardError("配置/状态文件过大")
    return json.loads(data)


def atomic_json(path, value, mode=0o600):
    path = secure_path(path, must_exist=False)
    descriptor, temporary = tempfile.mkstemp(prefix=".cs-pf-", dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def command(arguments):
    try:
        result = subprocess.run(["/sbin/pfctl"] + arguments, text=True, capture_output=True,
                                timeout=8, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        raise GuardError("pfctl不可用或超时")
    if result.returncode:
        # Do not expose configured proxy addresses or secrets in diagnostics.
        raise GuardError("pfctl操作失败，未确认防护")
    return result.stdout, result.stderr


class Guard:
    def __init__(self, config, config_path=CONFIG, state_path=STATE, receipt_path=RECEIPT,
                 runner=command, writer=atomic_json, reader=read_json, health=None):
        self.config = validate_config(config)
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.receipt_path = Path(receipt_path)
        self.run = runner
        self.write = writer
        self.read = reader
        self.health = health or load_module().health

    def config_digest(self):
        return digest(json.dumps(self.config, sort_keys=True, separators=(",", ":")))

    def preflight(self):
        if not parent_is_supported(self.run(["-sr"])[0]):
            raise GuardError("PF父规则未知：需人工审核的独立quick锚点，不能仅依赖com.apple/*")
        interfaces = self.run(["-v", "-s", "Interfaces"])[0]
        if re.search(r"\bskip\b", interfaces, re.I):
            raise GuardError("PF存在skip接口，无法确认所有出站路径受控")
        if self.run(["-ss"])[0].strip():
            raise GuardError("存在既有PF状态；请退出联网应用等待状态清空，本工具绝不全局清除状态")

    def current_health(self):
        evidence = self.health()
        if not evidence.get("ok"):
            raise GuardError("接口或IPv4/IPv6路由漂移")
        active = {name for name, item in evidence.get("interfaces", {}).items() if item.get("active")}
        if not set(self.config["tunnel_interfaces"]).issubset(active):
            raise GuardError("配置中的隧道接口未全部活动")
        for address, name in evidence.get("routes", {}).items():
            if name and name not in self.config["tunnel_interfaces"]:
                raise GuardError("实际公共目标路由未使用显式允许的隧道")
        return evidence

    def load_rules(self, rules):
        # No shell, global -f, -F, -d, or state-kill operation is ever used.
        descriptor, name = tempfile.mkstemp(prefix="claude-switcher-pf-", suffix=".conf")
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(rules)
            self.run(["-n", "-a", ANCHOR, "-f", name])
            self.run(["-a", ANCHOR, "-f", name])
            return digest(self.run(["-a", ANCHOR, "-sr"])[0])
        finally:
            os.unlink(name)

    def publish(self, state, active, reason):
        receipt = {"version": 1, "active": bool(active), "mode": state.get("mode", "disabled"),
                   "checked_at": time.time(), "reason": reason, "anchor": ANCHOR,
                   "assurance": "rule_installation_and_local_routes_only", "packet_capture_verified": False,
                   "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        self.write(self.receipt_path, receipt, 0o644)
        return receipt

    def existing(self):
        try:
            return self.read(self.state_path)
        except FileNotFoundError:
            return None

    def lockdown(self, state=None, reason="explicit lockdown"):
        state = state or self.existing() or {"version": 1, "config_path": str(self.config_path)}
        state["config"] = self.config
        try:
            state["runtime_hash"] = self.load_rules(render_rules(self.config, lockdown=True))
            state["mode"] = "blocked"
        except GuardError:
            state["mode"] = "unsafe"
        state["config_hash"] = self.config_digest()
        self.write(self.state_path, state)
        self.publish(state, False, reason)
        return state

    def lockdown_if_enabled(self):
        state = self.existing()
        if not state or state.get("mode") == "disabled":
            return self.publish(state or {"mode": "disabled"}, False, "PF保持显式禁用，不安装拒绝规则")
        self.lockdown(state, "已启用PF的代理健康检查失败")
        return self.status()

    def enable(self):
        if self.config.get("acknowledge_machine_wide_filter") is not True:
            raise GuardError("必须显式确认PF会限制整机出站网络")
        self.preflight()
        self.current_health()
        existing = self.existing()
        state = existing or {"version": 1, "config_path": str(self.config_path), "pf_token": None}
        self.lockdown(state, "准备显式启用，先安装拒绝规则")
        if state.get("mode") != "blocked":
            raise GuardError("无法安装拒绝规则，未尝试放行规则")
        try:
            info = self.run(["-s", "info"])[0]
            if not re.search(r"(?m)^Status:\s*Enabled\b", info):
                output, errors = self.run(["-E"])
                match = re.search(r"Token\s*:\s*(\d+)", output + errors, re.I)
                if not match:
                    raise GuardError("PF启用令牌无法验证；保留拒绝规则")
                state["pf_token"] = match.group(1)
            state["runtime_hash"] = self.load_rules(render_rules(self.config))
            state["mode"] = "active"
            state["config_hash"] = self.config_digest()
            state["helper_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            self.write(self.state_path, state)
            return self.status()
        except (GuardError, OSError, ValueError):
            self.lockdown(state, "启用/更新失败，保留拒绝规则，需要人工恢复")
            raise

    def status(self):
        state = self.existing()
        if not state:
            return self.publish({"mode": "disabled"}, False, "未显式启用PF")
        if state.get("mode") != "active":
            return self.publish(state, False, "PF未处于活动模式；需显式更新恢复")
        self.preflight()
        self.current_health()
        if state.get("config_hash") != self.config_digest():
            raise GuardError("PF配置已变化，需显式更新")
        if state.get("helper_sha256") != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
            raise GuardError("PF helper已变化，需显式更新重新验收")
        if not re.search(r"(?m)^Status:\s*Enabled\b", self.run(["-s", "info"])[0]):
            raise GuardError("PF已被停用")
        if state.get("runtime_hash") != digest(self.run(["-a", ANCHOR, "-sr"])[0]):
            raise GuardError("PF锚点规则发生漂移")
        return self.publish(state, True, "规则安装和本地路径检查通过；未代替真机抓包验收")

    def watch(self):
        try:
            return self.status()
        except (GuardError, OSError, ValueError) as error:
            self.lockdown(reason=str(error))
            raise

    def disable(self):
        state = self.existing()
        if not state:
            return self.publish({"mode": "disabled"}, False, "未显式启用PF")
        # Empty only our anchor, leaving every global/other anchor rule intact.
        self.load_rules("")
        token = state.get("pf_token")
        if token:
            if not re.fullmatch(r"\d+", str(token)):
                raise GuardError("PF令牌无效，未改变全局PF状态")
            self.run(["-X", str(token)])
        state["mode"] = "disabled"
        state["pf_token"] = None
        self.write(self.state_path, state)
        return self.publish(state, False, "已清空本项目锚点并释放本项目PF引用")


def read_receipt():
    receipt = read_json(RECEIPT)
    age = time.time() - float(receipt.get("checked_at", 0))
    if age < 0 or age > RECEIPT_MAX_AGE:
        raise GuardError("PF守护收据过期，未确认当前状态")
    if receipt.get("helper_sha256") != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise GuardError("PF守护组件版本不一致，请重新授权安装")
    return receipt


@contextlib.contextmanager
def privileged_lock():
    path = secure_path(LOCK, must_exist=False)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise GuardError("另一PF操作正在执行")
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "enable", "update", "status", "watch", "lockdown", "disable"))
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--require-active", action="store_true")
    parser.add_argument("--if-enabled", action="store_true", help="lockdown is a no-op if PF was not explicitly enabled")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            config = validate_config(read_json(args.config, privileged=False))
            print(json.dumps({"rules": render_rules(config), "lockdown_rules": render_rules(config, True),
                              "required_parent_filter": PARENT_RULE,
                              "scope": "machine-wide outgoing traffic, all interfaces, IPv4 and IPv6",
                              "requires_empty_state_table": True, "default_enabled": False,
                              "packet_capture_verified": False}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "status" and os.geteuid() != 0:
            receipt = read_receipt()
        else:
            if platform.system() != "Darwin" or os.geteuid() != 0:
                raise GuardError("该动作仅允许在macOS由root显式执行")
            # An unconfigured guard is a read-only no-op. Do not create a PF
            # lock merely to report it disabled: stock macOS /var/run is
            # root:daemon 0775 and intentionally fails secure_path's strict
            # write boundary. Configured/active PF operations still require
            # the existing privileged lock and all ownership checks.
            if not args.config.exists() and not STATE.exists() and (args.command in ("status", "watch") or args.command == "lockdown" and args.if_enabled):
                receipt = {"mode": "disabled", "active": False, "packet_capture_verified": False}
                print(json.dumps(receipt, ensure_ascii=False))
                return 1 if args.require_active else 0
            with privileged_lock():
                if not args.config.exists() and not STATE.exists() and (args.command in ("status", "watch") or args.command == "lockdown" and args.if_enabled):
                    receipt = {"mode": "disabled", "active": False, "packet_capture_verified": False}
                else:
                    try:
                        config = validate_config(read_json(args.config))
                    except (GuardError, OSError, ValueError, TypeError):
                        prior = read_json(STATE)
                        if prior.get("mode") == "disabled":
                            if args.command in ("watch", "status", "disable") or args.command == "lockdown" and args.if_enabled:
                                receipt = {"mode": "disabled", "active": False, "packet_capture_verified": False}
                                print(json.dumps(receipt, ensure_ascii=False))
                                return 1 if args.require_active else 0
                            raise GuardError("配置缺失或损坏，PF保持禁用")
                        config = validate_config(prior.get("config"))
                        recovery = Guard(config, args.config)
                        if args.command == "disable":
                            receipt = recovery.disable()
                            print(json.dumps(receipt, ensure_ascii=False))
                            return 0
                        recovery.lockdown(prior, "配置缺失或损坏，保留拒绝规则")
                        raise GuardError("配置缺失或损坏，已尝试收紧原PF锚点")
                    guard = Guard(config, args.config)
                    operation = "enable" if args.command == "update" else args.command
                    if operation == "lockdown":
                        if args.if_enabled:
                            receipt = guard.lockdown_if_enabled()
                        else:
                            guard.lockdown()
                            receipt = guard.status()
                    else:
                        receipt = getattr(guard, operation)()
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        if args.command == "watch" and receipt.get("mode") != "disabled" and not receipt.get("active"):
            return 1
        return 1 if args.require_active and not receipt.get("active") else 0
    except (GuardError, OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"active": False, "mode": "unverified", "reason": str(error),
                          "packet_capture_verified": False}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
