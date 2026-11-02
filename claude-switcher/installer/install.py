#!/usr/bin/env python3
"""Staged macOS installation and reversible system changes.

The public installer compiles before replacing anything. The privileged worker
has a fixed destination allowlist and never enables the optional PF guard.
"""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

SOURCE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("component_manager", SOURCE / "core/component-manager.py")
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)
SYSTEM = Path("/Library/Application Support/claude-switcher")
DAEMONS = Path("/Library/LaunchDaemons")
SYSTEM_JOURNAL = SYSTEM / ".install-rollback"
LABELS = ("local.claude-bodyguard", "local.tz-watchdog")
ROOT_FILES = ("claude-bodyguard.sh", "tz-watchdog.sh", "network-check.py", "network-guard.py", "network-monitor.py", "clash-policy.py", "owned-policy.json", "components.json")
NOTIFIER_LABEL = "local.claude-network-notifier"


def run(args, allow_failure=False, **kwargs):
    interactive = Path(str(args[0])).name == "sudo"
    result = subprocess.run([str(x) for x in args], stdout=None if interactive else subprocess.PIPE,
                            stderr=None if interactive else subprocess.PIPE, **kwargs)
    if result.returncode and not allow_failure:
        raise cm.ComponentError("命令失败: " + Path(str(args[0])).name)
    return result


def read_text(args):
    r = run(args, allow_failure=True)
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else None


def desktop_running(uid):
    result = run(["/usr/bin/pgrep", "-u", str(uid), "-f", "/Claude[.]app/Contents/"], allow_failure=True)
    if result.returncode not in (0, 1):
        raise cm.ComponentError("无法确认 Claude 进程状态，守护服务未停止")
    return result.returncode == 0


def stop_protected_desktop():
    uid = os.getuid()
    if not desktop_running(uid):
        return
    run(["/usr/bin/osascript", "-e", 'tell application "Claude" to quit'], timeout=20)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not desktop_running(uid):
            return
        time.sleep(0.2)
    raise cm.ComponentError("Claude 尚未退出，请保存工作并手动退出；安装/恢复未继续")


def assert_desktop_stopped(uid):
    if desktop_running(uid):
        raise cm.ComponentError("Claude 仍在运行，拒绝停止守护服务")


def disable_legacy_root_updater():
    legacy = DAEMONS / "local.claude-update.plist"
    if legacy.is_symlink():
        raise cm.ComponentError("旧自动更新服务路径异常")
    if legacy.exists():
        # This obsolete job executes user-writable code as root. Never re-enable
        # that privilege boundary during rollback; retain its plist for inspection.
        cm.atomic_write(SYSTEM / "legacy-root-updater.disabled.plist", legacy.read_bytes(), 0o600)
        run(["/bin/launchctl", "bootout", "system/local.claude-update"], allow_failure=True)
        legacy.unlink()


def target_paths():
    return [SYSTEM / x for x in ROOT_FILES] + [DAEMONS / (x + ".plist") for x in LABELS]


def running_as_root():
    return os.geteuid() == 0


def system_snapshot(configure):
    state = {"timezone": None, "ipv6": {}}
    if not configure:
        return state
    tz = read_text(["/usr/sbin/systemsetup", "-gettimezone"])
    if not tz or not tz.startswith("Time Zone: "):
        raise cm.ComponentError("无法备份原系统时区")
    state["timezone"] = tz.split(": ", 1)[1]
    services = read_text(["/usr/sbin/networksetup", "-listallnetworkservices"])
    if services is None:
        raise cm.ComponentError("无法备份网络服务")
    for service in services.splitlines()[1:]:
        if service.startswith("*") or not service:
            continue
        info = read_text(["/usr/sbin/networksetup", "-getinfo", service]) or ""
        match = re.search(r"^IPv6: (.+)$", info, re.M)
        if match:
            mode = match[1].strip()
            if mode not in ("Off", "Automatic", "Link-local only"):
                raise cm.ComponentError("检测到不能自动恢复的手动 IPv6 配置；请不带 --configure-system 安装并手动处理网络")
            state["ipv6"][service] = mode
    return state


def restore_system(state, applied=None):
    # Only restore settings still equal to our applied value. Preserve later user edits.
    if state.get("timezone"):
        now = read_text(["/usr/sbin/systemsetup", "-gettimezone"])
        if applied is None or now == "Time Zone: " + applied.get("timezone", ""):
            run(["/usr/sbin/systemsetup", "-settimezone", state["timezone"]])
    modes = {"Off": "-setv6off", "Automatic": "-setv6automatic", "Link-local only": "-setv6linklocal"}
    for service, mode in state.get("ipv6", {}).items():
        if mode not in modes:
            raise cm.ComponentError("原 IPv6 配置记录无效")
        now = read_text(["/usr/sbin/networksetup", "-getinfo", service]) or ""
        if applied is None or re.search(r"^IPv6: Off$", now, re.M):
            run(["/usr/sbin/networksetup", modes[mode], service])


def root_apply(args):
    if os.geteuid() != 0:
        raise cm.ComponentError("系统安装需要管理员授权")
    if SYSTEM_JOURNAL.exists():
        raise cm.ComponentError("存在未完成系统安装，请先运行 system-rollback")
    source = args.source.resolve()
    manifest = cm.load_bundle(source)
    config = json.loads(args.config.read_text())
    if set(config) != {"home", "proxy_mode", "proxy_group", "country", "timezone", "configure_system"}:
        raise cm.ComponentError("系统安装参数无效")
    if not isinstance(config["proxy_group"], str) or not 1 <= len(config["proxy_group"]) <= 256 or any(ord(c) < 32 for c in config["proxy_group"]):
        raise cm.ComponentError("代理组参数无效")
    if not re.fullmatch(r"[A-Z]{2}", config["country"]):
        raise cm.ComponentError("出口国家参数无效")
    if not re.fullmatch(r"[A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?", config["timezone"]):
        raise cm.ComponentError("时区参数无效")
    if config["proxy_mode"] not in ("clash-verge", "flclash", "shadowrocket", "sing-box", "clashx", "surge", "auto"):
        raise cm.ComponentError("代理模式无效")
    home = Path(config["home"])
    if not home.is_absolute() or any(x in str(home) for x in ("\n", "\r", "\x00")):
        raise cm.ComponentError("用户目录无效")
    owner_uid = home.stat().st_uid
    assert_desktop_stopped(owner_uid)
    original = system_snapshot(config["configure_system"])
    SYSTEM.mkdir(parents=True, exist_ok=True, mode=0o755)
    if SYSTEM.is_symlink():
        raise cm.ComponentError("系统组件目录不得为符号链接")
    os.chown(SYSTEM, 0, 0)
    os.chmod(SYSTEM, 0o755)
    disable_legacy_root_updater()
    SYSTEM_JOURNAL.mkdir(mode=0o700)
    entries = []
    for i, path in enumerate(target_paths() + [SYSTEM / "system-original.json"]):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise cm.ComponentError("系统目标不是普通文件")
        item = {"path": str(path), "existed": path.exists(), "backup": str(i), "mode": 0o755 if path.suffix in (".sh", ".py") else 0o644}
        if path.exists():
            shutil.copyfile(path, SYSTEM_JOURNAL / str(i))
            os.chmod(SYSTEM_JOURNAL / str(i), 0o600)
            with (SYSTEM_JOURNAL / str(i)).open("rb") as backup:
                os.fsync(backup.fileno())
            item["backup_sha256"] = cm.digest(SYSTEM_JOURNAL / str(i))
        entries.append(item)
    active = [label for label in LABELS if run(["/bin/launchctl", "print", "system/" + label], allow_failure=True).returncode == 0]
    record = {"schema": 1, "phase": "prepared", "owner_uid": owner_uid, "entries": entries, "settings": original, "active": active}
    cm.write_json(SYSTEM_JOURNAL / "state.json", record)
    try:
        for label in LABELS:
            run(["/bin/launchctl", "bootout", "system/" + label], allow_failure=True)
        watchdog_tz = config["timezone"]
        if not config["configure_system"] and "local.tz-watchdog" in active:
            current_tz = read_text(["/usr/sbin/systemsetup", "-gettimezone"])
            if not current_tz or not current_tz.startswith("Time Zone: "):
                raise cm.ComponentError("无法保留已安装时区看门狗的当前目标")
            watchdog_tz = current_tz.split(": ", 1)[1]
        for name in ("claude-bodyguard.sh", "tz-watchdog.sh"):
            content = (source / "daemons" / name).read_text()
            # Shell-safe placeholders: templates contain a quoted HOME and scalar target.
            escaped_home = str(home).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
            content = content.replace("__HOME__", escaped_home).replace("__UID__", str(home.stat().st_uid)).replace("__PROXY_MODE__", config["proxy_mode"]).replace("__TZ_TARGET__", watchdog_tz)
            escaped_group = config["proxy_group"].replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
            content = content.replace("__PROXY_GROUP__", escaped_group).replace("__COUNTRY_TARGET__", config["country"])
            cm.atomic_write(SYSTEM / name, content.encode(), 0o755)
        for name in ("network-check.py", "network-guard.py", "network-monitor.py", "clash-policy.py", "owned-policy.json"):
            if "core/" + name in manifest["files"]:
                cm.atomic_write(SYSTEM / name, (source / "core" / name).read_bytes(), 0o644 if name.endswith(".json") else 0o755)
        cm.write_json(SYSTEM / "components.json", dict(manifest, owner_uid=owner_uid))
        for label in LABELS:
            cm.atomic_write(DAEMONS / (label + ".plist"), (source / "daemons" / (label + ".plist")).read_bytes(), 0o644)
        if config["configure_system"]:
            baseline = SYSTEM / "system-original.json"
            if baseline.exists():
                saved = json.loads(baseline.read_text())
                for service, value in original["ipv6"].items():
                    saved.setdefault("original", {}).setdefault("ipv6", {}).setdefault(service, value)
                if not saved["original"].get("timezone"):
                    saved["original"]["timezone"] = original["timezone"]
            else:
                saved = {"schema": 1, "original": original}
            saved["applied"] = {"timezone": config["timezone"], "ipv6": "Off"}
            cm.write_json(baseline, saved)
            run(["/usr/sbin/systemsetup", "-settimezone", config["timezone"]])
            for service in original["ipv6"]:
                run(["/usr/sbin/networksetup", "-setv6off", service])
        run(["/bin/launchctl", "bootstrap", "system", DAEMONS / "local.claude-bodyguard.plist"])
        # A timezone watchdog would change global settings: start only with opt-in.
        if config["configure_system"] or "local.tz-watchdog" in active:
            run(["/bin/launchctl", "bootstrap", "system", DAEMONS / "local.tz-watchdog.plist"])
    except BaseException:
        root_rollback()
        raise


def root_rollback():
    if os.geteuid() != 0:
        raise cm.ComponentError("系统回滚需要管理员授权")
    if not SYSTEM_JOURNAL.exists():
        return
    record = json.loads((SYSTEM_JOURNAL / "state.json").read_text())
    allowed = {str(x) for x in target_paths() + [SYSTEM / "system-original.json"]}
    if record.get("schema") != 1 or any(x.get("path") not in allowed or not re.fullmatch(r"\d+", x.get("backup", "")) for x in record["entries"]):
        raise cm.ComponentError("系统回滚记录异常，停止写入")
    if type(record.get("owner_uid")) is not int or record["owner_uid"] < 0:
        raise cm.ComponentError("系统回滚用户记录异常")
    # Validate every backup before stopping services or replacing the first file.
    # A missing/corrupt later backup must not leave a partly restored guard set.
    for entry in record["entries"]:
        backup = SYSTEM_JOURNAL / entry["backup"]
        if entry["existed"] and (backup.is_symlink() or not backup.is_file()
                                 or cm.digest(backup) != entry.get("backup_sha256")):
            raise cm.ComponentError("系统回滚备份校验失败；保留服务、日志与备份供恢复")
    assert_desktop_stopped(record["owner_uid"])
    for label in LABELS:
        run(["/bin/launchctl", "bootout", "system/" + label], allow_failure=True)
    for entry in record["entries"]:
        path = Path(entry["path"])
        if entry["existed"]:
            cm.atomic_write(path, (SYSTEM_JOURNAL / entry["backup"]).read_bytes(), entry["mode"])
        else:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
    restore_system(record["settings"])
    for label in record["active"]:
        run(["/bin/launchctl", "bootstrap", "system", DAEMONS / (label + ".plist")])
    shutil.rmtree(SYSTEM_JOURNAL)


def compile_app(source, output, version):
    output = Path(output)
    (output / "Contents/MacOS").mkdir(parents=True)
    (output / "Contents/Resources").mkdir()
    arch = read_text(["/usr/bin/uname", "-m"])
    if arch not in ("arm64", "x86_64"):
        raise cm.ComponentError("不支持的 macOS 架构")
    run(["/usr/bin/xcrun", "swiftc", "-O", "-parse-as-library", "-warnings-as-errors", "-target", arch + "-apple-macos13.0",
         source / "app/main.swift", "-o", output / "Contents/MacOS/ClaudeSwitcher", "-framework", "SwiftUI", "-framework", "AppKit"])
    with tempfile.TemporaryDirectory() as d:
        icons = Path(d) / "AppIcon.iconset"
        icons.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = "icon_{}x{}{}.png".format(size, size, "@2x" if scale == 2 else "")
                run(["/usr/bin/sips", "-z", str(size * scale), str(size * scale), source / "assets/icon-2048.png", "--out", icons / name])
        run(["/usr/bin/iconutil", "-c", "icns", icons, "-o", output / "Contents/Resources/AppIcon.icns"])
    info = {"CFBundleExecutable": "ClaudeSwitcher", "CFBundleIdentifier": "local.claude-switcher", "CFBundleName": "Claude切换器",
            "CFBundleDisplayName": "Claude切换器", "CFBundlePackageType": "APPL", "CFBundleShortVersionString": version.split("-")[0],
            "CFBundleVersion": version.split("-")[0], "ClaudeSwitcherVersion": version, "LSMinimumSystemVersion": "13.0",
            "CFBundleIconFile": "AppIcon", "NSHighResolutionCapable": True, "LSUIElement": True}
    (output / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    notifier = output / "Contents/Helpers/NetworkNotifier.app"
    (notifier / "Contents/MacOS").mkdir(parents=True)
    run(["/usr/bin/xcrun", "swiftc", "-O", "-warnings-as-errors", "-target", arch + "-apple-macos13.0",
         source / "app/network-notifier.swift", "-o", notifier / "Contents/MacOS/NetworkNotifier",
         "-framework", "AppKit", "-framework", "UserNotifications"])
    notifier_info = {"CFBundleExecutable": "NetworkNotifier", "CFBundleIdentifier": "local.claude-switcher.network-notifier",
                     "CFBundleName": "Claude 网络防护", "CFBundleDisplayName": "Claude 网络防护",
                     "CFBundlePackageType": "APPL", "CFBundleVersion": version.split("-")[0],
                     "CFBundleShortVersionString": version.split("-")[0], "LSMinimumSystemVersion": "13.0", "LSUIElement": True}
    (notifier / "Contents/Info.plist").write_bytes(plistlib.dumps(notifier_info))
    run(["/usr/bin/codesign", "--force", "-s", "-", notifier])
    run(["/usr/bin/codesign", "--force", "--deep", "-s", "-", output])
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", output])


def stage_user_files(source, home, config, manifest):
    base = home / ".claude-switcher"
    entries = {}
    for relative in manifest["files"]:
        if relative == "core/claude-switch.sh":
            entries[home / ".local/bin/claude-switch.sh"] = (cm.inject_configs((source / relative).read_text(), config).encode(), 0o700)
        elif relative.startswith("core/") and Path(relative).parent == Path("core") and (relative.endswith(".py") or relative == "core/owned-policy.json"):
            entries[base / Path(relative).name] = ((source / relative).read_bytes(), 0o600)
    entries[base / "components.json"] = ((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(), 0o600)
    entries[base / "version.txt"] = ((manifest["version"] + "\n").encode(), 0o600)
    agent = plistlib.loads((source / "daemons/local.claude-update.plist").read_bytes())
    agent["ProgramArguments"] = [value.replace("__HOME__", str(home)) for value in agent["ProgramArguments"]]
    entries[home / "Library/LaunchAgents/local.claude-update.plist"] = (plistlib.dumps(agent), 0o600)
    notifier = plistlib.loads((source / ("daemons/" + NOTIFIER_LABEL + ".plist")).read_bytes())
    notifier["ProgramArguments"] = [value.replace("__HOME__", str(home)) for value in notifier["ProgramArguments"]]
    entries[home / ("Library/LaunchAgents/" + NOTIFIER_LABEL + ".plist")] = (plistlib.dumps(notifier), 0o600)
    return entries


def notifier_agent(home, start=False):
    domain = "gui/" + str(os.getuid())
    run(["/bin/launchctl", "bootout", domain + "/" + NOTIFIER_LABEL], allow_failure=True)
    if start:
        bundle = home / "Desktop/Claude切换器.app/Contents/Helpers/NetworkNotifier.app"
        register = Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister")
        run([register, "-f", bundle])
        run(["/bin/launchctl", "bootstrap", domain, home / ("Library/LaunchAgents/" + NOTIFIER_LABEL + ".plist")])


def wait_for_monitor(home, mode, group, country):
    if mode not in ("clash-verge", "flclash"):
        return
    deadline = time.monotonic() + 25
    started = time.time()
    while time.monotonic() < deadline:
        result = run(["/usr/bin/python3", SYSTEM / "network-monitor.py", "status", "--owner-uid", str(home.stat().st_uid),
                      "--proxy-mode", mode, "--proxy-group", group, "--expected-country", country,
                      "--min-heartbeat", str(started)], allow_failure=True)
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise cm.ComponentError("持续网络监控未就绪，安装验收中止")


def install(args):
    if sys.platform != "darwin" or running_as_root():
        raise cm.ComponentError("请以普通用户在 macOS 运行安装器；系统阶段单独请求管理员授权")
    source, home = args.source.resolve(), args.home.resolve()
    manifest = cm.load_bundle(source)
    base, binary = home / ".claude-switcher", home / ".local/bin/claude-switch.sh"
    config = {"PROXY_MODE": args.proxy_mode, "PROXY_GROUP": args.proxy_group, "COUNTRY_TARGET": args.country,
              "TZ_TARGET": args.timezone, "LOCALE_TARGET": args.locale}
    app = home / "Desktop/Claude切换器.app"
    if app.is_symlink():
        raise cm.ComponentError("App 目标不得为符号链接")
    if (base / "install-journal.json").exists():
        raise cm.ComponentError("存在待恢复安装记录，请先检查备份后使用 recover-install")
    if SYSTEM_JOURNAL.exists():
        raise cm.ComponentError("存在待恢复系统安装；请先运行 recover-install 或授权 system-rollback")
    with tempfile.TemporaryDirectory(prefix="claude-switcher-install-") as work:
        work = Path(work)
        print("正在暂存编译；现有安装尚未修改。", flush=True)
        staged = work / "Claude切换器.app"
        compile_app(source, staged, manifest["version"])
        with cm.state_lock(base):
            # Another installer may have left recovery state while this one compiled.
            if (base / "install-journal.json").exists() or SYSTEM_JOURNAL.exists():
                raise cm.ComponentError("存在待恢复安装记录，请先使用 recover-install；现有组件未修改")
            if (base / "account-transaction.json").exists():
                raise cm.ComponentError("存在待恢复账号事务，请先执行 claude-switch.sh recover")
            cm.recover_update(base, binary.parent)
            stop_protected_desktop()
            entries = stage_user_files(source, home, config, manifest)
            state_file = base / "installation-state.json"
            entries[state_file] = (state_file.read_bytes() if state_file.exists() else b'{"schema":1,"proxy":[]}\n', 0o600)
            root_config = {"home": str(home), "proxy_mode": args.proxy_mode, "proxy_group": args.proxy_group,
                           "country": args.country, "timezone": args.timezone,
                           "configure_system": args.configure_system}
            cm.write_json(work / "system.json", root_config)
            original_locale = read_text(["/usr/bin/defaults", "read", "-g", "AppleLocale"])
            # Include proxy config in the rollback transaction when explicitly requested.
            proxy = home / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles"
            proxy_paths = [proxy / "Script.js", proxy / "Merge.yaml", base / "webrtc-policy"] if args.configure_proxy and args.proxy_mode == "clash-verge" else []
            all_destinations = list(entries) + proxy_paths
            previous = app.with_name(app.name + ".previous-" + uuid.uuid4().hex)
            app_staging = app.with_name(app.name + ".new-" + uuid.uuid4().hex)
            journal = {"schema": 1, "phase": "prepared", "app": str(app), "previous": str(previous), "staging": str(app_staging),
                       "app_existed": app.exists(), "original_locale": original_locale, "applied_locale": args.locale,
                       "locale_changed": args.configure_system, "system_applied": False,
                       "notifier_active": run(["/bin/launchctl", "print", "gui/" + str(os.getuid()) + "/" + NOTIFIER_LABEL], allow_failure=True).returncode == 0,
                       "agent_active": run(["/bin/launchctl", "print", "gui/" + str(os.getuid()) + "/local.claude-update"], allow_failure=True).returncode == 0}
            txn = None
            try:
                # Prepare the entire App before creating a user-file transaction.
                # Its recovery requires the installer journal's extra path scope,
                # so that journal must exist before update-journal can be written.
                app.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(staged, app_staging)
                cm.write_json(base / "install-journal.json", journal)
                txn = cm.FileTransaction(base, all_destinations)
                for dest, (data, mode) in entries.items():
                    txn.replace(dest, data, mode)
                if args.configure_system:
                    run(["/usr/bin/defaults", "write", "-g", "AppleLocale", args.locale])
                print("需要管理员授权安装守护进程。PF 网络防护保持关闭。", flush=True)
                journal["system_applied"] = True
                cm.write_json(base / "install-journal.json", journal)
                run(["/usr/bin/sudo", "/usr/bin/python3", source / "installer/install.py", "system-apply", "--source", source, "--config", work / "system.json"])
                if proxy_paths:
                    # Public mutation commands deliberately reject this pending
                    # installation. Use the verified helper inside our existing
                    # lock and rollback transaction, with all three outputs saved.
                    helper = base / "clash-policy.py"
                    if helper.is_symlink() or cm.digest(helper) != manifest["files"]["core/clash-policy.py"]:
                        raise cm.ComponentError("代理策略 helper 与已校验安装包不一致")
                    run([sys.executable, helper, "write", proxy.parent, base, args.proxy_group, os.environ.get("WEBRTC_POLICY", "")])
                    run(["/usr/bin/osascript", "-e", 'tell application "Clash Verge" to quit'], allow_failure=True)
                    run(["/usr/bin/open", "-a", "Clash Verge"])
                wait_for_monitor(home, args.proxy_mode, args.proxy_group, args.country)
                cm.check_installed(binary, base)
                # Old UI remains available through compilation and audit. Stop only at commit.
                run(["/usr/bin/pkill", "-x", "ClaudeSwitcher"], allow_failure=True)
                notifier_agent(home)
                if app.exists():
                    os.replace(app, previous)
                os.replace(app_staging, app)
                agent = home / "Library/LaunchAgents/local.claude-update.plist"
                run(["/bin/launchctl", "bootout", "gui/" + str(os.getuid()) + "/local.claude-update"], allow_failure=True)
                run(["/bin/launchctl", "bootstrap", "gui/" + str(os.getuid()), agent])
                notifier_agent(home, start=True)
                install_state = base / "installation-state.json"
                old_state = json.loads(install_state.read_text()) if install_state.exists() else {"schema": 1, "proxy": []}
                if args.configure_system and "original_locale" not in old_state:
                    old_state["original_locale"] = original_locale
                if args.configure_system:
                    old_state["applied_locale"] = args.locale
                for path in proxy_paths:
                    old = next(e for e in txn.record["entries"] if e["path"] == str(path))
                    saved = next((e for e in old_state["proxy"] if e["path"] == str(path)), None)
                    if saved is None:
                        saved = {"path": str(path), "original": Path(old["backup"]).read_text() if old["existed"] else None}
                        old_state["proxy"].append(saved)
                    elif old.get("backup_sha256") != saved.get("installed_sha256"):
                        # Reinstallation must not erase user edits made since an
                        # earlier install when a later uninstall restores defaults.
                        saved["preserve_user_changes"] = True
                    saved["installed_sha256"] = cm.digest(path) if path.exists() else None
                txn.replace(install_state, (json.dumps(old_state, ensure_ascii=False, indent=2) + "\n").encode())
                # Durable commit intent before discarding either privilege domain's backups.
                txn.record["phase"] = "committed"
                cm.write_json(txn.journal, txn.record)
                run(["/usr/bin/sudo", "/usr/bin/python3", source / "installer/install.py", "system-commit", "--source", source])
                journal["phase"] = "committed"
                cm.write_json(base / "install-journal.json", journal)
                txn.finish_commit()
                finished = run(["/usr/bin/sudo", "/usr/bin/python3", source / "installer/install.py", "system-finish", "--source", source], allow_failure=True)
                if finished.returncode == 0:
                    with contextlib.suppress(OSError):
                        if previous.exists():
                            shutil.rmtree(previous)
                        (base / "install-journal.json").unlink()
                print("OK:安装和完整环境验收通过 " + manifest["version"] + "；稳定版更新通道已启用。")
            except BaseException:
                if txn is not None:
                    notifier_agent(home)
                rollback_error = False
                if journal["system_applied"]:
                    try:
                        run(["/usr/bin/sudo", "/usr/bin/python3", source / "installer/install.py", "system-rollback", "--source", source])
                    except cm.ComponentError:
                        rollback_error = True
                if previous.exists():
                    if app.exists():
                        shutil.rmtree(app)
                    os.replace(previous, app)
                elif not journal["app_existed"] and app.exists():
                    shutil.rmtree(app)
                if app_staging.exists():
                    shutil.rmtree(app_staging)
                if txn is None:
                    # No live component/configuration has changed. If constructor
                    # failure left a durable update journal, keep the matching
                    # install journal so recover-install can validate every path.
                    if not (base / "update-journal.json").exists():
                        with contextlib.suppress(FileNotFoundError):
                            (base / "install-journal.json").unlink()
                    raise
                if args.configure_system:
                    command = ["/usr/bin/defaults", "delete", "-g", "AppleLocale"] if original_locale is None else ["/usr/bin/defaults", "write", "-g", "AppleLocale", original_locale]
                    run(command, allow_failure=True)
                txn.rollback()
                if journal.get("notifier_active"):
                    notifier_agent(home, start=True)
                run(["/bin/launchctl", "bootout", "gui/" + str(os.getuid()) + "/local.claude-update"], allow_failure=True)
                if journal["agent_active"]:
                    run(["/bin/launchctl", "bootstrap", "gui/" + str(os.getuid()), home / "Library/LaunchAgents/local.claude-update.plist"], allow_failure=True)
                if rollback_error:
                    raise cm.ComponentError("安装未通过；用户组件已回滚，系统回滚未获成功。保留 install-journal 和 root 备份，请授权运行 system-rollback")
                (base / "install-journal.json").unlink()
                raise


def recover_install(args):
    home, source = args.home.resolve(), args.source.resolve()
    base = home / ".claude-switcher"
    with cm.state_lock(base):
        path = base / "install-journal.json"
        if not path.exists():
            print("OK:没有待恢复安装")
            return
        stop_protected_desktop()
        record = json.loads(path.read_text())
        app = home / "Desktop/Claude切换器.app"
        previous, staged = Path(record["previous"]), Path(record["staging"])
        if record.get("schema") != 1 or record.get("app") != str(app) or previous.parent != app.parent or staged.parent != app.parent:
            raise cm.ComponentError("安装恢复路径无效")
        if not re.fullmatch(re.escape(app.name) + r"\.previous-[a-f0-9]{32}", previous.name) or not re.fullmatch(re.escape(app.name) + r"\.new-[a-f0-9]{32}", staged.name):
            raise cm.ComponentError("安装恢复路径无效")
        for target in (app, previous, staged):
            if target.is_symlink():
                raise cm.ComponentError("安装恢复目标不得为符号链接")
        extra = [home / "Library/LaunchAgents", home / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles"]
        if record.get("phase") == "committed":
            run(["/usr/bin/sudo", "/usr/bin/python3", source / "installer/install.py", "system-finish", "--source", source])
            cm.recover_update(base, home / ".local/bin", extra)
            for target in (previous, staged):
                if target.exists():
                    shutil.rmtree(target)
            path.unlink()
            print("OK:已完成上次成功安装的备份清理")
            return
        run(["/usr/bin/sudo", "/usr/bin/python3", source / "installer/install.py", "system-rollback", "--source", source])
        notifier_agent(home)
        if previous.exists():
            if app.exists():
                shutil.rmtree(app)
            os.replace(previous, app)
        elif not record["app_existed"] and app.exists():
            shutil.rmtree(app)
        if staged.exists():
            shutil.rmtree(staged)
        if record["locale_changed"]:
            old = record["original_locale"]
            run(["/usr/bin/defaults", "delete", "-g", "AppleLocale"] if old is None else ["/usr/bin/defaults", "write", "-g", "AppleLocale", old], allow_failure=True)
        # An install commit marker alone is not enough: root commit may not have completed.
        update_path = base / "update-journal.json"
        if update_path.exists():
            update_record = json.loads(update_path.read_text())
            update_record["phase"] = "prepared"
            cm.write_json(update_path, update_record)
        cm.recover_update(base, home / ".local/bin", extra)
        if record.get("notifier_active"):
            notifier_agent(home, start=True)
        run(["/bin/launchctl", "bootout", "gui/" + str(os.getuid()) + "/local.claude-update"], allow_failure=True)
        if record.get("agent_active"):
            run(["/bin/launchctl", "bootstrap", "gui/" + str(os.getuid()), home / "Library/LaunchAgents/local.claude-update.plist"])
        path.unlink()
        print("OK:已恢复上次安装前的组件与配置")


def root_uninstall():
    if os.geteuid() != 0:
        raise cm.ComponentError("卸载系统组件需要管理员授权")
    if SYSTEM.exists():
        disable_legacy_root_updater()
    installed = SYSTEM / "components.json"
    if installed.exists():
        owner = json.loads(installed.read_text()).get("owner_uid")
        if type(owner) is not int or owner < 0:
            raise cm.ComponentError("无法确认守护服务目标用户，请人工检查")
        assert_desktop_stopped(owner)
    if SYSTEM_JOURNAL.exists():
        pending = json.loads((SYSTEM_JOURNAL / "state.json").read_text())
        if pending.get("phase") == "committed":
            shutil.rmtree(SYSTEM_JOURNAL)
        else:
            root_rollback()
    guard = SYSTEM / "network-guard.py"
    # Never remove a live PF guard's controlling files. Ask its helper to disable only its anchor.
    if guard.exists() and (SYSTEM / "network-guard.json").exists():
        run([sys.executable, guard, "disable"])
    for label in LABELS:
        run(["/bin/launchctl", "bootout", "system/" + label], allow_failure=True)
    original = SYSTEM / "system-original.json"
    if original.exists():
        saved = json.loads(original.read_text())
        restore_system(saved["original"], saved.get("applied"))
    for path in target_paths() + [original]:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    # Preserve unrecognized files and PF configuration; never recursively delete the directory.
    with contextlib.suppress(OSError):
        SYSTEM.rmdir()


def uninstall(args):
    home, source = args.home.resolve(), args.source.resolve()
    base = home / ".claude-switcher"
    with cm.state_lock(base):
        stop_protected_desktop()
        run(["/usr/bin/sudo", "/usr/bin/python3", source / "installer/install.py", "system-uninstall", "--source", source])
        state_file = base / "installation-state.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        if "original_locale" in state and read_text(["/usr/bin/defaults", "read", "-g", "AppleLocale"]) == state.get("applied_locale"):
            old = state["original_locale"]
            run(["/usr/bin/defaults", "delete", "-g", "AppleLocale"] if old is None else ["/usr/bin/defaults", "write", "-g", "AppleLocale", old], allow_failure=True)
        for entry in state.get("proxy", []):
            path = Path(entry["path"])
            expected_parent = home / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles"
            known_proxy = path.parent == expected_parent and path.name in ("Script.js", "Merge.yaml")
            if (not known_proxy and path != base / "webrtc-policy") or path.is_symlink():
                raise cm.ComponentError("代理恢复记录无效")
            if entry.get("preserve_user_changes"):
                print("INFO:代理配置曾在安装之间修改，保留用户修改；请仅移除 Claude Switcher 托管块。")
                continue
            if path.exists() and cm.digest(path) == entry.get("installed_sha256"):
                if entry["original"] is None:
                    path.unlink()
                else:
                    cm.atomic_write(path, entry["original"].encode(), 0o600)
            elif path.exists():
                print("INFO:代理配置已在安装后修改，保留用户修改；请仅移除 Claude Switcher 托管块。")
        run(["/bin/launchctl", "bootout", "gui/" + str(os.getuid()) + "/local.claude-update"], allow_failure=True)
        notifier_agent(home)
        for path in (home / "Library/LaunchAgents/local.claude-update.plist", home / ("Library/LaunchAgents/" + NOTIFIER_LABEL + ".plist"), home / ".local/bin/claude-switch.sh"):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        app = home / "Desktop/Claude切换器.app"
        if app.is_dir() and not app.is_symlink():
            run(["/usr/bin/pkill", "-x", "ClaudeSwitcher"], allow_failure=True)
            shutil.rmtree(app)
        manifest = cm.installed_manifest(base)
        for relative in manifest.get("files", {}):
            if relative.startswith("core/") and Path(relative).parent == Path("core") and (relative.endswith(".py") or relative == "core/owned-policy.json"):
                with contextlib.suppress(FileNotFoundError):
                    (base / Path(relative).name).unlink()
        print("OK:已卸载；账号快照、恢复记录与用户数据保留。仅恢复本版本记录且其后未被用户改动的系统设置。")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("install", "uninstall", "recover-install", "system-apply", "system-rollback", "system-commit", "system-finish", "system-uninstall"))
    p.add_argument("--source", type=Path, default=SOURCE)
    p.add_argument("--home", type=Path, default=Path.home())
    p.add_argument("--config", type=Path)
    p.add_argument("--proxy-mode", default="auto")
    p.add_argument("--proxy-group", default="GLOBAL")
    p.add_argument("--country", default="US")
    p.add_argument("--timezone", default="America/New_York")
    p.add_argument("--locale", default="en_US")
    p.add_argument("--configure-system", action="store_true")
    p.add_argument("--configure-proxy", action="store_true")
    args = p.parse_args()
    try:
        if args.action == "install":
            install(args)
        elif args.action == "uninstall":
            uninstall(args)
        elif args.action == "recover-install":
            recover_install(args)
        elif args.action == "system-apply":
            root_apply(args)
        elif args.action == "system-rollback":
            root_rollback()
        elif args.action == "system-uninstall":
            root_uninstall()
        elif args.action == "system-commit":
            if os.geteuid() != 0:
                raise cm.ComponentError("需要管理员授权")
            if SYSTEM_JOURNAL.exists():
                state = json.loads((SYSTEM_JOURNAL / "state.json").read_text())
                state["phase"] = "committed"
                cm.write_json(SYSTEM_JOURNAL / "state.json", state)
        elif args.action == "system-finish":
            if os.geteuid() != 0:
                raise cm.ComponentError("需要管理员授权")
            if SYSTEM_JOURNAL.exists():
                state = json.loads((SYSTEM_JOURNAL / "state.json").read_text())
                if state.get("phase") != "committed":
                    raise cm.ComponentError("系统安装尚未提交，不能清理备份")
                shutil.rmtree(SYSTEM_JOURNAL)
    except (cm.ComponentError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print("FAIL:" + (str(exc) if isinstance(exc, cm.ComponentError) else "安装操作失败；请保留事务备份以便恢复"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
