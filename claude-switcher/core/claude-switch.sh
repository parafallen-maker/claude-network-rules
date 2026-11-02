#!/bin/bash
# Claude 账号切换器 - 核心脚本 (通用版)
# 用法: claude-switch.sh check | status | switch A|B|C|D | checkdetail | fix | set-region | update [--check|--auto] | diagnose-proxy
set -u
export LC_ALL=en_US.UTF-8

if [ "${1:-}" = "_support" ]; then
    shift
    exec "${CLAUDE_SWITCHER_PYTHON:-python3}" - "$@" <<'PY_SUPPORT'
import datetime
import json
import os
from pathlib import Path
import platform
import re
import sys
import tempfile
import zipfile


OPTIONAL = ("IP一致", "DNS", "WebRTC", "客户端语言", "ASN属性", "生态可达性")
ALL_CHECKS = ("代理内核", "TUN网卡", "出口IP", "IP一致", "时区", "地区", "IPv6", "DNS",
              "WebRTC", "保镖", "规则置顶", "客户端语言", "ASN属性", "生态可达性")
DEFAULTS = {"disabled": [], "allowOptionalFailures": False, "logging": True}
LOG_LIMIT = 512 * 1024


def fail():
    print("配置或诊断参数无效", file=sys.stderr)
    raise SystemExit(2)


def paths():
    home = Path.home()
    return home / ".claude-switcher", home / "Library/Logs/claude-switcher/client.log"


def validate(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        fail()
    result = dict(DEFAULTS)
    disabled = value.get("disabled", [])
    if (not isinstance(disabled, list)
            or any(not isinstance(item, str) or item not in OPTIONAL for item in disabled)
            or len(disabled) != len(set(disabled))):
        fail()
    result["disabled"] = [name for name in OPTIONAL if name in disabled]
    for key in ("allowOptionalFailures", "logging"):
        if key in value:
            if type(value[key]) is not bool:
                fail()
            result[key] = value[key]
    return result


def load_config():
    base, _ = paths()
    path = base / "checks.json"
    if not path.exists():
        return dict(DEFAULTS)
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        fail()
    return validate(value)


def app_version():
    value = os.environ.get("CLAUDE_SWITCHER_VERSION", "").strip()
    if value and re.fullmatch(r"[A-Za-z0-9._-]{1,40}", value):
        return value
    base, _ = paths()
    try:
        value = (base / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    return value if re.fullmatch(r"[A-Za-z0-9._-]{1,40}", value) else "unknown"


def write_log(args):
    config = load_config()
    if not config["logging"]:
        return
    if len(args) != 4 or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", args[0]):
        fail()
    if not re.fullmatch(r"\d{1,3}", args[1]) or not 0 <= int(args[1]) <= 255:
        fail()
    if not re.fullmatch(r"\d{1,12}", args[2]):
        fail()
    tokens = args[3].split()
    checks = {}
    for token in tokens:
        match = re.fullmatch(r"([A-Za-z0-9\u4e00-\u9fff]+)=(ok|warn|fail|skip)", token)
        if not match or match.group(1) not in ALL_CHECKS or match.group(1) in checks:
            fail()
        checks[match.group(1)] = match.group(2)
    summary = " ".join(name + "=" + checks[name] for name in ALL_CHECKS if name in checks)
    line = (datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            + " version=" + app_version() + " command=" + args[0]
            + " exit=" + str(int(args[1])) + " duration_ms=" + str(int(args[2]))
            + ((" " + summary) if summary else "") + "\n")
    _, log_path = paths()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(log_path.parent, 0o700)
        if log_path.exists() and log_path.stat().st_size + len(line.encode("utf-8")) > LOG_LIMIT:
            os.replace(log_path, log_path.with_name("client.log.1"))
        with log_path.open("a", encoding="utf-8") as handle:
            os.chmod(log_path, 0o600)
            handle.write(line)
    except OSError:
        fail()


def recent_status():
    base, _ = paths()
    result = {}
    try:
        lines = (base / "last-check.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        match = re.fullmatch(r"([^=]+)=(ok|warn|fail|skip)(?::.*)?", line)
        if match and match.group(1) in ALL_CHECKS and match.group(1) not in result:
            result[match.group(1)] = match.group(2)
    return result


def safe_log_lines():
    _, log_path = paths()
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    allowed = re.compile(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 version=[A-Za-z0-9._-]+ "
        r"command=[a-z][a-z0-9-]{0,31} exit=\d{1,3} duration_ms=\d{1,12}"
        r"(?: (?:[A-Za-z0-9\u4e00-\u9fff]+=(?:ok|warn|fail|skip) ?)*)?")
    return [line for line in lines[-2000:] if allowed.fullmatch(line)]


def export_diagnostics(args):
    if len(args) != 1 or not args[0] or "\x00" in args[0]:
        fail()
    destination = Path(args[0]).expanduser()
    base, _ = paths()
    environment = {"schema_version": 1, "platform": platform.system(),
                   "app_version": app_version(),
                   "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in (("environment.json", environment),
                                ("recent-status.json", {"checks": recent_status()}),
                                ("checks.json", load_config())):
                archive.writestr(name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            archive.writestr("client.log", "\n".join(safe_log_lines()) + "\n")
        os.chmod(destination, 0o600)
    except OSError:
        fail()
    print("OK:诊断日志已导出（不含 IP、凭据、账号或代理配置）")


def main(args):
    if not args:
        fail()
    action = args[0]
    if action == "config" and len(args) == 1:
        config = load_config()
        print("disabled=" + ",".join(config["disabled"]))
        print("allowOptionalFailures=" + str(int(config["allowOptionalFailures"])))
        print("logging=" + str(int(config["logging"])))
    elif action == "config-get" and len(args) == 1:
        print(json.dumps(load_config(), ensure_ascii=False))
    elif action == "config-set" and len(args) == 2:
        raw = args[1].encode("utf-8")
        if len(raw) > 16384:
            fail()
        try:
            config = validate(json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeError):
            fail()
        base, _ = paths()
        try:
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=".checks-", dir=base)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, base / "checks.json")
        except OSError:
            fail()
    elif action == "log":
        write_log(args[1:])
    elif action == "export-diagnostics":
        export_diagnostics(args[1:])
    else:
        fail()


if __name__ == "__main__":
    main(sys.argv[1:])
PY_SUPPORT
fi

# 随现有单脚本更新分发;在任何账号状态读写、审计/修复/启动之前分派。
if [ "${1:-}" = "diagnose-proxy" ]; then
    shift
    exec "${CLAUDE_SWITCHER_PYTHON:-python3}" - "$@" <<'PY_PROXY_DIAGNOSE'
"""Read-only transport diagnostics for an explicitly selected HTTP/SOCKS proxy."""
import argparse
import concurrent.futures
import ipaddress
import json
import math
import os
import subprocess
from urllib.parse import urlsplit


TARGETS = (
    ("anthropic_api", "https://api.anthropic.com/v1/models"),
    ("claude_web", "https://claude.ai/"),
)


def select_proxy(args, environ):
    if args.proxy is not None:
        value, source = args.proxy, "argument"
    elif args.config is not None:
        with open(args.config, encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            raise ValueError("invalid config")
        value, source = config.get("proxy_url"), "config"
    else:
        value, source = environ.get("CLAUDE_SWITCHER_PROXY_URL"), "environment"
    if not isinstance(value, str) or not value.strip():
        raise ValueError("proxy required")
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid proxy")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https", "socks5", "socks5h")
            or not parsed.hostname or parsed.port is None
            or not 0 < parsed.port < 65536
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("invalid proxy")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    # Do not include proxy addresses, credentials, or private config paths in reports.
    public = {"scheme": parsed.scheme, "port": parsed.port,
              "host_scope": "loopback" if loopback else "remote",
              "authentication_configured": parsed.username is not None,
              "source": source}
    return value, public


def probe(name, url, proxy, environ):
    # -q ignores curlrc. Pass proxy credentials over stdin, never in argv/reports.
    quoted = proxy.replace("\\", "\\\\").replace('"', '\\"')
    config = 'proxy = "' + quoted + '"\n'
    args = ["curl", "-q", "--config", "-", "-sS", "-o", os.devnull,
            "--proto", "=https", "--noproxy", "", "--connect-timeout", "6",
            "--max-time", "12", "-w",
            "%{http_code}\t%{http_connect}\t%{ssl_verify_result}\t%{time_appconnect}\t%{time_total}",
            url]
    keep = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "SYSTEMROOT"}
    clean_env = {k: v for k, v in environ.items() if k in keep}
    result = {"name": name, "url": url, "transport_reached": False,
              "access": "unknown"}
    try:
        process = subprocess.run(args, input=config, env=clean_env, capture_output=True,
                                 text=True, timeout=15)
        fields = process.stdout.strip().split()
        status, connect, verify = map(int, fields[:3])
        tls, elapsed = map(float, fields[3:5])
        if (not 0 <= status <= 599 or not 0 <= connect <= 599 or verify < 0
                or not math.isfinite(tls) or not math.isfinite(elapsed)
                or tls < 0 or elapsed < 0):
            raise ValueError("invalid probe metrics")
        reached = process.returncode == 0 and status > 0 and verify == 0 and tls > 0
        access = ("unauthenticated" if status == 401 else
                  "access_restricted" if status in (403, 407) else
                  "server_error" if status >= 500 else "http_response")
        result.update(curl_exit=process.returncode, http_status=status,
                      proxy_connect_status=connect, tls_verified=reached,
                      elapsed_seconds=elapsed, transport_reached=reached,
                      access=access if reached else "unknown")
    except subprocess.TimeoutExpired:
        result["error"] = "probe_timeout"
    except OSError:
        result["error"] = "curl_unavailable"
    except (ValueError, IndexError):
        result["error"] = "invalid_probe_output"
    return result


class DiagnosticParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo private URLs or paths from argv.
        raise ValueError("invalid arguments")


def main(argv=None, environ=None):
    parser = DiagnosticParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--proxy", help="explicit proxy URL including a port")
    choice.add_argument("--config", help="private JSON file containing proxy_url")
    environ = os.environ if environ is None else environ
    report = {"schema_version": 1, "mode": "proxy_diagnostics",
              "scope": "unauthenticated_transport_only",
              "account_status": "not_evaluated", "leak_protection": "not_evaluated",
              "launch_allowed": None, "application_started": False,
              "system_modified": False}
    try:
        args = parser.parse_args(argv)
        proxy, public = select_proxy(args, environ)
    except (OSError, ValueError, TypeError):
        report.update(state="configuration_error", error="invalid_or_missing_proxy",
                      hint="Use --proxy, --config, or CLAUDE_SWITCHER_PROXY_URL; an explicit port is required.")
        print(json.dumps(report, ensure_ascii=False))
        return 2
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(probe, name, url, proxy, environ) for name, url in TARGETS]
        checks = [f.result() for f in futures]
    reached = all(check["transport_reached"] for check in checks)
    report.update(proxy=public, checks=checks,
                  state="transport_reached" if reached else "transport_failed")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if reached else 1


if __name__ == "__main__":
    raise SystemExit(main())
PY_PROXY_DIAGNOSE
fi

if [ "${1:-}" = "flclash-repair" ]; then
    shift
    exec "${CLAUDE_SWITCHER_PYTHON:-python3}" - "$@" <<'PY_FLCLASH_REPAIR'
import datetime
import json
import re
import secrets
import shutil
import sqlite3
import sys
from pathlib import Path


DOMAINS = (
    "claude.ai", "claude.com", "anthropic.com", "claudeusercontent.com",
    "challenges.cloudflare.com", "datadoghq.com", "segment.io", "stripe.com",
    "stripe.network", "stripecdn.com", "intercom.io", "intercomcdn.com",
    "intercomassets.com",
)
PORTS = (3478, 5349, 19302)
REQUIRED_RULE_COLUMNS = {
    "id", "profile_id", "rule_action", "content", "rule_target",
    "rule_provider", "sub_rule", "no_resolve", "src", "order",
}


def fail(message):
    print(f"FAIL:{message}", file=sys.stderr)
    raise SystemExit(1)


def clean_scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    if not value or re.search(r"[,\r\n\x00-\x1f]", value):
        fail("代理组名无效")
    return value


def select_group(config, configured):
    groups = []
    in_groups = False
    current = None
    for line in config.splitlines():
        if re.match(r"^proxy-groups:\s*(?:#.*)?$", line):
            in_groups = True
            continue
        if in_groups and re.match(r"^[A-Za-z_]", line):
            break
        if not in_groups:
            continue
        name = re.match(r"^\s*-\s*name:\s*(.+?)\s*$", line)
        if name:
            current = clean_scalar(name.group(1))
            continue
        if re.match(r"^\s*type:\s*select\s*$", line) and current:
            groups.append(current)
            current = None
    if configured in groups:
        return configured
    return groups[0] if groups else "GLOBAL"


def current_profile(home):
    path = home / "shared_preferences.json"
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        profile_id = values.get("flutter.currentProfileId")
        if type(profile_id) is not int or profile_id <= 0:
            fail("无法识别当前 Profile，已保留原配置")
        return profile_id
    except (OSError, ValueError, TypeError):
        fail("无法读取当前 Profile，已保留原配置")


def repair(home, configured):
    if not home.is_dir():
        fail("FlClash 配置目录不存在")
    database = home / "database.sqlite"
    config_path = home / "config.yaml"
    if not database.is_file() or not config_path.is_file():
        fail("缺少 FlClash 数据库或运行配置")
    profile_id = current_profile(home)
    config = config_path.read_text(encoding="utf-8")
    if not re.search(r"enhanced-mode:\s*fake-ip\b|fake-ip:\s*true\b", config, re.I):
        fail("请先在 FlClash 中启用 fake-IP，再执行修复")
    target = select_group(config, configured)
    backup = None
    try:
        with sqlite3.connect(database, timeout=5) as db:
            db.execute("PRAGMA busy_timeout=5000")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < 17 or version > 21:
                fail("FlClash 数据库版本不受支持，已保留原配置")
            columns = {row[1] for row in db.execute("PRAGMA table_info(rules)")}
            if not REQUIRED_RULE_COLUMNS.issubset(columns):
                fail("FlClash 规则表结构不受支持，已保留原配置")
            if not db.execute("SELECT 1 FROM profiles WHERE id=?", (profile_id,)).fetchone():
                fail("当前 Profile 不在数据库中，已保留原配置")
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = home / f".claude-switcher-backup-{stamp}-{secrets.token_hex(4)}.sqlite"
            db.execute("VACUUM INTO ?", (str(backup),))
        with sqlite3.connect(database, timeout=5) as db:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE")
            try:
                for action, content in [("DOMAIN_SUFFIX", value) for value in DOMAINS] + [("DST_PORT", str(value)) for value in PORTS]:
                    exists = db.execute(
                        "SELECT 1 FROM rules WHERE profile_id=? AND rule_action=? AND content=? AND rule_target=?",
                        (profile_id, action, content, target),
                    ).fetchone()
                    if exists:
                        continue
                    while True:
                        rule_id = secrets.randbits(62)
                        if not db.execute("SELECT 1 FROM rules WHERE id=?", (rule_id,)).fetchone():
                            break
                    db.execute(
                        'INSERT INTO rules (id, profile_id, rule_action, content, rule_target, rule_provider, sub_rule, no_resolve, src, "order") '
                        "VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, 0, NULL)",
                        (rule_id, profile_id, action, content, target),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        print(f"BACKUP={backup}")
    except SystemExit:
        raise
    except Exception as error:
        if backup and backup.exists():
            shutil.copyfile(backup, database)
        fail(f"写入 FlClash 规则失败:{type(error).__name__}")


def rollback(home, backup_name):
    backup = Path(backup_name).resolve()
    if backup.parent != home.resolve() or not backup.name.startswith(".claude-switcher-backup-") or backup.suffix != ".sqlite" or not backup.is_file():
        fail("回滚备份无效")
    database = home / "database.sqlite"
    try:
        for suffix in ("-wal", "-shm"):
            (home / f"database.sqlite{suffix}").unlink(missing_ok=True)
        shutil.copyfile(backup, database)
        print("OK:已回滚 FlClash 配置")
    except OSError as error:
        fail(f"回滚失败:{type(error).__name__}")


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "repair":
        repair(Path(sys.argv[2]), sys.argv[3])
        return
    if len(sys.argv) == 4 and sys.argv[1] == "rollback":
        rollback(Path(sys.argv[2]), sys.argv[3])
        return
    fail("用法: flclash-repair repair <配置目录> <代理组> | rollback <配置目录> <备份>")


if __name__ == "__main__":
    main()
PY_FLCLASH_REPAIR
fi

if [ "${1:-}" = "clash-rules" ]; then
    shift
    POLICY_HELPER="$HOME/.claude-switcher/clash-policy.py"
    [ -f "$POLICY_HELPER" ] || POLICY_HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/clash-policy.py"
    [ -f "$POLICY_HELPER" ] || { echo 'FAIL:缺少clash-policy.py，请重新安装完整组件'; exit 1; }
    exec "${CLAUDE_SWITCHER_PYTHON:-python3}" "$POLICY_HELPER" "$@"
fi

# ============ 安装时可配置项(安装程序会自动改写) ============
PROXY_MODE="clash-verge"       # 代理客户端: clash-verge|flclash|shadowrocket|sing-box|clashx|surge|auto
PROXY_GROUP="IpEqual"          # Clash 代理组名(安装时自动探测)
COUNTRY_TARGET="US"            # 目标出口国家(安装时按出口自动识别)
TZ_TARGET="America/New_York"   # 锁定时区,应与出口IP所在城市一致
LOCALE_TARGET="en_US"          # 锁定地区格式
# ============================================================

DATA="$HOME/Library/Application Support/Claude"
BASE="$HOME/.claude-switcher"
LOG="$HOME/Library/Logs/claude-switch.log"
CLASH_DIR="$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
ROUTE_MISSED=""
KEEP_STAGED=0
mkdir -p "$BASE/accounts"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >>"$LOG"; }

# Account/update commands share one inherited, verified filesystem lock.
account_state_helper() {
    local here candidate
    here="$(cd "$(dirname "$0")" && pwd)"
    for candidate in "$here/account_state.py" "$here/../core/account_state.py" "$BASE/account_state.py"; do
        if [ -f "$candidate" ]; then printf '%s\n' "$candidate"; return 0; fi
    done
    echo "FAIL:缺少账号事务组件，请重新运行安装器" >&2
    return 1
}
account_state_call() {
    local helper command="$1"
    shift
    helper=$(account_state_helper) || return 1
    python3 "$helper" "$command" --base "$BASE" --data "$DATA" "$@"
}
account_lock_reexec() {
    local helper
    helper=$(account_state_helper) || return 1
    if [ -n "${CLAUDE_SWITCHER_STATE_LOCK_FD:-}" ]; then
        python3 "$helper" assert-lock --base "$BASE" || return 1
    else
        exec python3 "$helper" lock-exec --base "$BASE" -- /bin/bash "$0" "$@"
    fi
}

# Configuration writes serialize with account switches and component updates.
configuration_write_begin() {
    account_lock_reexec "$@" || return 1
    account_state_call config-ready
}

CHECKS_DISABLED=""
ALLOW_OPTIONAL_FAILURES=0
load_check_config() {
    local config key value
    config=$("$0" _support config 2>/dev/null) || return 1
    while IFS='=' read -r key value; do
        case "$key" in
            disabled) CHECKS_DISABLED="$value" ;;
            allowOptionalFailures) ALLOW_OPTIONAL_FAILURES="$value" ;;
        esac
    done <<< "$config"
}
check_disabled() { case ",$CHECKS_DISABLED," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
optional_failure() {
    [ "$ALLOW_OPTIONAL_FAILURES" = "1" ] || fails="$fails;$1"
}
optional_result() {
    local name="$1" state="$2" detail="${3:-}"
    if check_disabled "$name"; then
        echo "$name=skip:用户已禁用"
    elif [ "$state" = "fail" ] && [ "$ALLOW_OPTIONAL_FAILURES" = "1" ]; then
        echo "$name=warn:非关键失败已忽略${detail:+($detail)}"
    else
        echo "$name=$state${detail:+:$detail}"
    fi
}
log_result() {
    local started="$1" command="$2" rc="$3" summary="${4:-}"
    "$0" _support log "$command" "$rc" "$(( ($(date +%s) - started) * 1000 ))" "$summary" >/dev/null 2>&1 || true
}

# Read-only helpers: a fake-IP subnet is not evidence of an active tunnel.
network_check() {
    local helper="$BASE/network-check.py"
    [ -f "$helper" ] || helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/network-check.py"
    [ -f "$helper" ] || { echo 'fail:缺少network-check.py，请重新安装完整组件'; return 1; }
    "${CLAUDE_SWITCHER_PYTHON:-python3}" "$helper" "$@"
}
tun_up() { network_check tunnel >/dev/null 2>&1; }
ipv6_status() { network_check ipv6; }

network_guard_status() {
    local helper="$BASE/network-guard.py"
    [ -f "$helper" ] || helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/network-guard.py"
    [ -f "$helper" ] || return 1
    "${CLAUDE_SWITCHER_PYTHON:-python3}" "$helper" status --require-active >/dev/null 2>&1
}

# A single gate used by GUI/CLI switch and temporary browsers. There is no
# production environment-variable bypass. Tests inject fake command functions.
require_launch_audit() {
    local result
    if ! result=$(check); then
        printf '%s\n' "$result"
        return 1
    fi
    case "$result" in
        OK:*) return 0 ;;
        *) echo 'FAIL:环境审计输出不完整，拒绝启动'; return 1 ;;
    esac
}

# 代理客户端显示名(供检查细节输出)
proxy_name() {
    case "$PROXY_MODE" in
        clash-verge)  echo "Clash Verge" ;;
        flclash)      echo "FlClash" ;;
        shadowrocket) echo "Shadowrocket" ;;
        sing-box)     echo "sing-box" ;;
        clashx)       echo "ClashX" ;;
        surge)        echo "Surge" ;;
        auto)         echo "代理客户端" ;;
        *)            echo "$PROXY_MODE" ;;
    esac
}

# 可 open -a 启动的客户端 App 名(sing-box/auto 无标准 App,返回空)
proxy_app_name() {
    case "$PROXY_MODE" in
        clash-verge)  echo "Clash Verge" ;;
        flclash)      echo "FlClash" ;;
        shadowrocket) echo "Shadowrocket" ;;
        clashx)       echo "ClashX" ;;
        surge)        echo "Surge" ;;
        *)            echo "" ;;
    esac
}

# Shadowrocket 在 Mac 上表现为 VPN 配置;launchd/非图形环境下 scutil 可能查不到该行,此时以 TUN 判定为准(避免误杀)
shadowrocket_up() {
    nc=$(scutil --nc list 2>/dev/null)
    if echo "$nc" | grep -q 'com.liguangming.Shadowrocket'; then
        echo "$nc" | grep 'com.liguangming.Shadowrocket' | grep -q '(Connected)'
    else
        tun_up
    fi
}

# 代理内核在线判定:按安装时识别的客户端分派;auto 时任一命中即活
proxy_alive() {
    case "$PROXY_MODE" in
        clash-verge)  pgrep -f verge-mihomo >/dev/null 2>&1 ;;
        flclash)      pgrep -f FlClashCore >/dev/null 2>&1 || pgrep -x FlClash >/dev/null 2>&1 ;;
        shadowrocket) shadowrocket_up ;;
        sing-box)     pgrep -f sing-box >/dev/null 2>&1 ;;
        clashx)       pgrep -x ClashX >/dev/null 2>&1 || pgrep -f clash >/dev/null 2>&1 ;;
        surge)        pgrep -f Surge >/dev/null 2>&1 ;;
        auto)         pgrep -f verge-mihomo >/dev/null 2>&1 || shadowrocket_up \
                      || pgrep -f sing-box >/dev/null 2>&1 \
                      || pgrep -x ClashX >/dev/null 2>&1 || pgrep -f clash >/dev/null 2>&1 \
                      || pgrep -f Surge >/dev/null 2>&1 ;;
        *)            pgrep -f verge-mihomo >/dev/null 2>&1 ;;
    esac
}

# 单文件内嵌 Python 的隔离入口;策略从 WEBRTC_POLICY 或持久化文件读取。
clash_rules() {
    local action="$1" rules_dir="$CLASH_DIR"
    [ "$PROXY_MODE" = "flclash" ] && rules_dir="$HOME/Library/Application Support/com.follow.clash"
    shift
    "${BASH:-/bin/bash}" "$0" clash-rules "$action" "$rules_dir" "$BASE" "$PROXY_GROUP" "${WEBRTC_POLICY:-}" "$@"
}

# 只接受运行中内核加载的规则，不再用配置文本或 fake-ip 代替。
merge_ok() { clash_rules verify >/dev/null 2>&1; }
stun_ok() { merge_ok; }
# 当前 Clash 模式(rule/global/direct);读取失败返回空
clash_mode() { clash_rules mode 2>/dev/null; }

# Only a running controller policy (or a current root PF receipt) can confirm
# routing protection. Synthetic DNS answers are deliberately not used here.
proxy_rules_status() {
    local result action=verify-egress
    case "$PROXY_MODE" in
        clash-verge|flclash)
            [ "$PROXY_MODE" = "flclash" ] && action=guard
            if result=$(clash_rules "$action" 2>&1); then
                echo "ok:${result#OK:}"
                return 0
            fi
            echo "fail:${result#FAIL:}"
            return 1 ;;
        *)
            if network_guard_status; then
                echo 'ok:显式PF限制已安装且守护收据有效(抓包验收仍需真机完成)'
                return 0
            fi
            echo 'fail:该客户端运行分流规则无法验证；fake-IP和TUN路由不等同出口保证，请使用受支持控制接口或显式PF防护'
            return 1 ;;
    esac
}
route_ok() {
    local result
    result=$(proxy_rules_status)
    case "$result" in
        ok:*) ROUTE_MISSED=""; return 0 ;;
        *) ROUTE_MISSED="${result#fail:}"; return 1 ;;
    esac
}

# 国家代码 → 默认 IANA 时区(cloudflare trace 等来源不带时区时使用)
default_tz_for() {
    case "$1" in
        US) echo America/New_York ;; JP) echo Asia/Tokyo ;; TW) echo Asia/Taipei ;;
        HK) echo Asia/Hong_Kong ;; SG) echo Asia/Singapore ;; GB) echo Europe/London ;;
        DE) echo Europe/Berlin ;; FR) echo Europe/Paris ;; KR) echo Asia/Seoul ;;
        AU) echo Australia/Sydney ;; CA) echo America/Toronto ;;
        *) echo America/New_York ;;
    esac
}

# 出口探测辅助:严格校验两位大写国家码与 IANA 时区格式
_geo_valid_country() {
    case "$1" in [A-Z][A-Z]) return 0 ;; *) return 1 ;; esac
}
_geo_valid_tz() {
    [[ "$1" =~ ^[A-Za-z_]+/[A-Za-z_]+(/[A-Za-z_]+)?$ ]]
}
_geo_json_field() {
    printf '%s\n' "$1" | sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
}
_geo_diag_add() {
    GEO_DIAG="${GEO_DIAG:+$GEO_DIAG; }$1"
    if [ -n "${GEO_DIAG_FILE:-}" ]; then
        printf '%s\n' "$1" >>"$GEO_DIAG_FILE"
    fi
}
# 取一个源;失败把简短原因(HTTP 码/超时)记进诊断,不打印
_geo_fetch() {
    local out rc code
    _GEO_BODY=""
    out=$(curl -s --max-time 8 -w '\n%{http_code}' "$1" 2>/dev/null); rc=$?
    if [ "$rc" -ne 0 ]; then
        _geo_diag_add "$2 超时/连接失败"
        return 1
    fi
    if [ -z "$out" ]; then
        _geo_diag_add "$2 无响应"
        return 1
    fi
    code=$(printf '%s\n' "$out" | tail -n 1)
    case "$code" in
        200)
            _GEO_BODY=$(printf '%s\n' "$out" | sed '$d')
            return 0 ;;
        [0-9][0-9][0-9])
            _geo_diag_add "$2 HTTP $code"
            return 1 ;;
        *)
            # 没有 -w 状态行时按成功处理(兼容 curl 替身/中间层)
            _GEO_BODY="$out"
            return 0 ;;
    esac
}
# 记录一个成功源;首个成功源优先,后续来源国家不一致只告警
_geo_consider() {
    [ -z "$1" ] && return 0
    if [ -z "$_GEO_CC" ]; then
        _GEO_CC="$1"; _GEO_TZ="$2"; _GEO_SRC="$3"
    elif [ "$1" != "$_GEO_CC" ]; then
        printf 'WARN:出口国家不一致(%s=%s %s=%s),采用优先级更高的 %s\n' \
            "$_GEO_SRC" "$_GEO_CC" "$3" "$1" "$_GEO_SRC" >&2
    fi
}

# 多源出口探测:ipinfo → ipapi → ifconfig → cloudflare trace,成功源按此优先级取首个;
# 全部失败输出 "||",各源失败原因写入 GEO_DIAG_FILE(命令替换会丢子 shell 变量)。
geo_lookup() {
    local cc tz loc
    local _GEO_BODY="" _GEO_CC="" _GEO_TZ="" _GEO_SRC=""
    GEO_DIAG=""

    if _geo_fetch "https://ipinfo.io/json" ipinfo; then
        cc=$(_geo_json_field "$_GEO_BODY" country)
        tz=$(_geo_json_field "$_GEO_BODY" timezone)
        if _geo_valid_country "$cc"; then
            _geo_valid_tz "$tz" || tz=$(default_tz_for "$cc")
            _geo_consider "$cc" "$tz" ipinfo
        else
            _geo_diag_add "ipinfo 国家字段无效"
        fi
    fi

    if _geo_fetch "https://ipapi.co/json/" ipapi; then
        cc=$(_geo_json_field "$_GEO_BODY" country_code)
        tz=$(_geo_json_field "$_GEO_BODY" timezone)
        if _geo_valid_country "$cc"; then
            _geo_valid_tz "$tz" || tz=$(default_tz_for "$cc")
            _geo_consider "$cc" "$tz" ipapi
        else
            _geo_diag_add "ipapi 国家字段无效"
        fi
    fi

    if _geo_fetch "https://ifconfig.co/json" ifconfig; then
        cc=$(_geo_json_field "$_GEO_BODY" country_iso)
        tz=$(_geo_json_field "$_GEO_BODY" timezone)
        if _geo_valid_country "$cc"; then
            _geo_valid_tz "$tz" || tz=$(default_tz_for "$cc")
            _geo_consider "$cc" "$tz" ifconfig
        else
            _geo_diag_add "ifconfig 国家字段无效"
        fi
    fi

    if _geo_fetch "https://www.cloudflare.com/cdn-cgi/trace" cloudflare; then
        loc=$(printf '%s\n' "$_GEO_BODY" | tr -d '\r' | sed -n 's/^loc=\([A-Za-z][A-Za-z]\)$/\1/p' | head -n 1 | tr 'a-z' 'A-Z')
        if _geo_valid_country "$loc"; then
            _geo_consider "$loc" "$(default_tz_for "$loc")" cloudflare
        else
            _geo_diag_add "cloudflare loc 无效"
        fi
    fi

    if [ -z "$_GEO_CC" ]; then
        printf '||\n'
        return 1
    fi
    printf '%s|%s|%s\n' "$_GEO_CC" "$_GEO_TZ" "$_GEO_SRC"
    return 0
}

# 国家代码 → 地区格式映射(未收录的国家回退 en_US)
locale_for() {
    case "$1" in
        US) echo en_US ;; JP) echo ja_JP ;; TW) echo zh_TW ;; HK) echo zh_HK ;;
        SG) echo en_SG ;; GB) echo en_GB ;; DE) echo de_DE ;; FR) echo fr_FR ;;
        KR) echo ko_KR ;; AU) echo en_AU ;; CA) echo en_CA ;; *) echo en_US ;;
    esac
}

# IP 审查:记录出口IP并统计最近48h内不同IP个数(账号画像风险指标)
iphist_count() {
    [ -z "$1" ] && { echo 0; return; }
    local HIST="$BASE/iphistory.log"
    local now=$(date +%s)
    echo "$1 $now" >>"$HIST"
    # 裁掉48小时(172800秒)以前的记录
    awk -v cutoff=$((now-172800)) '$2>=cutoff' "$HIST" > "$HIST.tmp" && mv "$HIST.tmp" "$HIST"
    # 统计48h内不同IP个数
    awk '{print $1}' "$HIST" | sort -u | wc -l | tr -d ' '
}

# ---------- 安全检查 ----------
audit_validate() {
    local helper="$BASE/audit-contract.py"
    [ -f "$helper" ] || helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/audit-contract.py"
    [ -f "$helper" ] || { echo 'FAIL:缺少audit-contract.py，拒绝启动'; return 1; }
    "${CLAUDE_SWITCHER_PYTHON:-python3}" "$helper" --config "$BASE/checks.json"
}

# One source of truth: checkdetail supplies all fourteen results; the strict
# contract rejects missing/duplicate/unknown output and hard warnings/skips.
check() {
    local report verdict
    if ! report=$(checkdetail); then
        printf '%s\n' "$report"
        echo 'FAIL:环境审计未完整执行'
        return 1
    fi
    if ! verdict=$(printf '%s\n' "$report" | audit_validate); then
        printf '%s\n' "$report"
        printf '%s\n' "$verdict"
        return 1
    fi
    printf '%s\n' "$verdict"
}

# WebRTC/STUN 探测:返回 RESULT:ip,ip 或 RESULT:NONE 或空
webrtc_test() {
    "${CLAUDE_SWITCHER_PYTHON:-python3}" "$BASE/stun-probe.py" --family "${1:-ipv4}" 2>/dev/null
}

webrtc_status() {
    local verified result probe_rc assessment mode expected v6state v6trace v6ip family policy
    if ! proxy_alive || ! tun_up; then
        echo 'fail:代理或目标路由未就绪，未发送STUN探测'
        return
    fi
    v6state=$(ipv6_status)
    case "$v6state" in ok:*) ;; *) echo 'fail:IPv6路径未确认，未发送STUN探测'; return ;; esac
    if ! verified=$(proxy_rules_status); then
        echo "fail:未确认出口保护:${verified#fail:}"
        return
    fi
    expected="$1"; family=ipv4
    case "$v6state" in
        ok:IPv6公共目标路由*)
            v6trace=$(curl -6 -s --max-time 5 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)
            v6ip=$(printf '%s\n' "$v6trace" | sed -n 's/^ip=//p')
            [ -n "$v6ip" ] || { echo 'fail:IPv6已启用但未取得可比对出口'; return; }
            expected="${expected:+$expected,}$v6ip"; family=both ;;
    esac
    policy=proxy
    case "$PROXY_MODE" in
        clash-verge|flclash)
            mode=$(clash_mode)
            # No response only proves an installed reject rule when rule mode
            # was actually verified. Global mode is never an implicit reject.
            [ "$mode" = "rule" ] && policy=$(clash_rules policy) ;;
    esac
    result=$(webrtc_test "$family"); probe_rc=$?
    assessment=$(WEBRTC_POLICY="$policy" clash_rules assess "$expected" "$result" "$probe_rc" 2>/dev/null)
    case "$assessment" in
        ok:*|warn:*|fail:*) echo "$assessment" ;;
        *) echo 'fail:无法评估STUN结果' ;;
    esac
}

# 保镖脚本由 root 安装，MODE 在安装时写死。核心切客户端后必须检测模式一致性，
# 否则旧保镖可能按错误的代理进程状态关闭 Claude，单看 launchctl 在线会误报成功。
bodyguard_status() {
    local launch_info root_dir guard guard_mode part installed local_copy last_exit monitor_pid
    if ! launch_info=$(launchctl print system/local.claude-bodyguard 2>/dev/null); then
        echo 'fail:未安装'
        return
    fi
    root_dir="/Library/Application Support/claude-switcher"
    guard="$root_dir/claude-bodyguard.sh"
    [ -r "$guard" ] || { echo 'fail:守护脚本不可读或缺失(请重新运行安装器)'; return; }
    guard_mode=$(sed -n 's/^MODE="\([^"]*\)".*/\1/p' "$guard" | head -1)
    if [ "$guard_mode" != "$PROXY_MODE" ]; then
        echo "fail:客户端不一致(核心=$PROXY_MODE,保镖=${guard_mode:-未知};请重新运行安装器同步)"
        return
    fi
    for part in network-check.py network-guard.py network-monitor.py clash-policy.py owned-policy.json; do
        installed="$root_dir/$part"; local_copy="$BASE/$part"
        if [ ! -r "$installed" ] || [ ! -r "$local_copy" ] || ! cmp -s "$installed" "$local_copy"; then
            echo 'fail:特权守护组件缺失或版本不一致，请重新授权安装完整组件'
            return
        fi
    done
    if ! "${CLAUDE_SWITCHER_PYTHON:-python3}" - "$root_dir" <<'PY_GUARD_FILES'
import os, pathlib, stat, sys
root = pathlib.Path(sys.argv[1])
for path in [root, root / 'claude-bodyguard.sh', root / 'network-check.py', root / 'network-guard.py', root / 'network-monitor.py', root / 'clash-policy.py', root / 'owned-policy.json']:
    try:
        info = path.lstat()
    except OSError:
        raise SystemExit(1)
    if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit(1)
PY_GUARD_FILES
    then
        echo 'fail:守护文件权限不安全，请重新授权安装'
        return
    fi
    case "$PROXY_MODE" in
        clash-verge|flclash)
            monitor_pid=$(printf '%s\n' "$launch_info" | sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\).*/\1/p' | head -1)
            if [ -z "$monitor_pid" ] || ! "${CLAUDE_SWITCHER_PYTHON:-python3}" "$root_dir/network-monitor.py" status \
                --owner-uid "$(id -u)" --expected-pid "$monitor_pid" --proxy-mode "$PROXY_MODE" \
                --proxy-group "$PROXY_GROUP" --expected-country "$COUNTRY_TARGET" 2>/dev/null; then
                echo 'fail:持续监控未就绪、配置不一致或证据过期，请检查后台服务或完整重装同步'
            fi
            return ;;
    esac
    last_exit=$(printf '%s\n' "$launch_info" | sed -n 's/.*last exit code = \([0-9][0-9]*\).*/\1/p' | head -1)
    if [ -n "$last_exit" ] && [ "$last_exit" != 0 ]; then
        echo "fail:守护上次退出异常($last_exit)，请检查系统日志"
        return
    fi
    echo 'ok:已注册且特权组件一致；进程级巡检不代表防火墙级零泄漏'
}

# 逐项输出检查结果(供UI使用): 名称=ok|fail:细节
checkdetail() {
    load_check_config || { echo "检查配置无效，请恢复默认设置"; return 1; }
    if proxy_alive; then
        case "$PROXY_MODE" in
            shadowrocket) echo '代理内核=ok:Shadowrocket 已连接' ;;
            *) echo "代理内核=ok:$(proxy_name) 运行中" ;;
        esac
    else
        echo "代理内核=fail:$(proxy_name) 未运行"
    fi
    if tun_up; then echo 'TUN网卡=ok'; else echo 'TUN网卡=fail:不在线'; fi
    trace=$(curl -4 -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)
    ip=$(echo "$trace" | grep '^ip=' | cut -d= -f2)
    loc=$(echo "$trace" | grep '^loc=' | cut -d= -f2)
    if [ "$loc" = "$COUNTRY_TARGET" ]; then echo "出口IP=ok:$ip/$loc"; else echo "出口IP=fail:${ip:-无}/${loc:-未知}(期望$COUNTRY_TARGET)"; fi
    if check_disabled "IP一致"; then
        optional_result "IP一致" ok
    else
        lastip=""; [ -f "$BASE/lastip" ] && lastip=$(cat "$BASE/lastip")
        ipst=""; ipdt=""
        if [ -z "$ip" ]; then ipst="fail"; ipdt="未获取到出口IP"
        elif [ -z "$lastip" ]; then ipst="ok"; ipdt="首次记录"
        elif [ "$ip" = "$lastip" ]; then ipst="ok"
        else ipst="warn"; ipdt="$lastip → $ip"; fi
        [ -n "$ip" ] && echo "$ip" > "$BASE/lastip"
        # IP 审查:48h 内出口 IP 变化 ≥3 次提示账号画像风险(只 warn 不 fail)
        ipn=$(iphist_count "$ip")
        if [ "$ipn" -ge 3 ] 2>/dev/null; then
            if [ "$ipst" = "ok" ]; then
                ipst="warn"; ipdt="48h内出口IP变化${ipn}次,账号画像有风险,建议固定节点"
            else
                ipdt="$ipdt;48h内出口IP变化${ipn}次,账号画像有风险,建议固定节点"
            fi
        fi
        optional_result "IP一致" "$ipst" "$ipdt"
    fi
    tz=$(readlink /etc/localtime 2>/dev/null)
    if [ "$tz" = "/var/db/timezone/zoneinfo/$TZ_TARGET" ]; then echo '时区=ok'; else echo "时区=fail:$tz"; fi
    locale=$(defaults read -g AppleLocale 2>/dev/null)
    if [ "$locale" = "$LOCALE_TARGET" ]; then echo '地区=ok'; else echo "地区=fail:$locale"; fi
    echo "IPv6=$(ipv6_status)"
    if check_disabled "DNS"; then
        optional_result "DNS" ok
    else
        dnsip=$(dig +short +time=3 +tries=1 claude.ai 2>/dev/null | head -1)
        case "$dnsip" in
            198.18.*|198.19.*) optional_result "DNS" warn "检测到合成地址；不能证明上游DNS加密或最终代理出口" ;;
            '') optional_result "DNS" fail "解析未返回地址" ;;
            *) optional_result "DNS" warn "解析返回普通地址；上游DNS链路未验证" ;;
        esac
    fi
    if check_disabled "WebRTC"; then
        optional_result "WebRTC" ok
    else
        wr=$(webrtc_status "$ip")
        wrst=${wr%%:*}; wrdt=""
        case "$wr" in *:*) wrdt=${wr#*:} ;; esac
        optional_result "WebRTC" "$wrst" "$wrdt"
    fi
    echo "保镖=$(bodyguard_status)"
    echo "规则置顶=$(proxy_rules_status)"
    # 12. Claude 客户端语言(单独设为英文,或系统首选语言为英文均可;防止 JS 层读出中文)
    if check_disabled "客户端语言"; then
        optional_result "客户端语言" ok
    else
        clang=$(defaults read com.anthropic.claudefordesktop AppleLanguages 2>/dev/null | head -2)
        syslang=$(defaults read -g AppleLanguages 2>/dev/null | sed -n '2p' | tr -d ' ,"')
        if echo "$clang" | grep -q 'en-US'; then
            optional_result "客户端语言" ok "en-US"
        elif [[ "$syslang" == en* ]]; then
            optional_result "客户端语言" ok "跟随系统(英文)"
        else
            optional_result "客户端语言" warn "跟随系统(${syslang:-未知})，语言本身不证明账号风险"
        fi
    fi
    # 13. ASN 网络属性(住宅宽带 vs 机房,按目标地区收录主流运营商)
    if check_disabled "ASN属性"; then
        optional_result "ASN属性" ok
    else
        org=$(curl -s --max-time 6 https://ipinfo.io/json 2>/dev/null | grep '"org"' | cut -d'"' -f4)
        case "$org" in
            '') optional_result "ASN属性" warn "未获取" ;;
            *Comcast*|*AT\&T*|*Verizon*|*Spectrum*|*Charter*|*Cox*|*T-Mobile*|*Cable*|*esidential*|\
            *NTT*|*KDDI*|*SoftBank*|*IIJ*|*OCN*|*So-net*|\
            *HiNet*|*Chunghwa*|*CHT*|*kbro*|*Taiwan*|*FarEasTone*|*Data\ Communication*|\
            *Singtel*|*StarHub*|*M1*|\
            *HKT*|*PCCW*|*HKBN*|*HGC*|\
            *BT*|*Sky*|*Virgin*|*TalkTalk*|*Vodafone*|\
            *Deutsche\ Telekom*|*1\&1*|*O2*) optional_result "ASN属性" ok "$org" ;;
            *) optional_result "ASN属性" warn "疑似机房($org)" ;;
        esac
    fi
    # 14. Anthropic 生态可达性
    if check_disabled "生态可达性"; then
        optional_result "生态可达性" ok
    else
        r1=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' https://claude.ai/ 2>/dev/null)
        r2=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/models 2>/dev/null)
        if [ "$r1" != "000" ] && [ "$r2" != "000" ]; then
            optional_result "生态可达性" ok "claude.ai:$r1 api:$r2"
        else
            optional_result "生态可达性" fail "claude.ai:$r1 api:$r2"
        fi
    fi
}

# 保留隐藏入口供安装器调用;现在生成全局 Script.js 并迁移旧管理块。
write_merge_rules() {
    configuration_write_begin write-merge-rules || return 1
    clash_rules write
}

flclash_repair() {
    "${BASH:-/bin/bash}" "$0" flclash-repair "$@"
}

fix_flclash() {
    local dir="$HOME/Library/Application Support/com.follow.clash"
    local result backup trace ip wr i
    [ -d "$dir" ] || { echo "FIX:FlClash 配置目录不存在，无法自动修复"; return 1; }
    if pgrep -f FlClashCore >/dev/null 2>&1 || pgrep -x FlClash >/dev/null 2>&1; then
        osascript -e 'tell application "FlClash" to quit' >/dev/null 2>&1
        for i in $(seq 1 15); do
            pgrep -f FlClashCore >/dev/null 2>&1 || pgrep -x FlClash >/dev/null 2>&1 || break
            sleep 1
        done
    fi
    if pgrep -f FlClashCore >/dev/null 2>&1 || pgrep -x FlClash >/dev/null 2>&1; then
        echo "FIX:FlClash 未能退出，请手动退出后重试"
        return 1
    fi
    if ! result=$(flclash_repair repair "$dir" "$PROXY_GROUP" 2>&1); then
        echo "$result"
        echo "FIX:FlClash 未修改配置"
        return 1
    fi
    backup=$(printf '%s\n' "$result" | sed -n 's/^BACKUP=//p' | head -1)
    [ -n "$backup" ] || { echo "FIX:FlClash 未生成有效备份，已中止"; return 1; }
    open -a "FlClash"
    for i in $(seq 1 30); do
        if proxy_alive && tun_up; then break; fi
        sleep 1
    done
    sleep 2
    if route_ok; then
        trace=$(curl -4 -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)
        ip=$(echo "$trace" | grep '^ip=' | cut -d= -f2)
        wr=$(webrtc_status "$ip")
        case "$wr" in
            ok:*) echo "FIX:FlClash 域名与 STUN 规则已生效 ✓"; return 0 ;;
            warn:*) echo "FIX:FlClash 规则已写入，但 STUN 状态未确认($wr)"; return 0 ;;
        esac
    fi
    osascript -e 'tell application "FlClash" to quit' >/dev/null 2>&1
    for i in $(seq 1 15); do
        pgrep -f FlClashCore >/dev/null 2>&1 || pgrep -x FlClash >/dev/null 2>&1 || break
        sleep 1
    done
    local rollback_result
    if ! rollback_result=$(flclash_repair rollback "$dir" "$backup" 2>&1); then
        echo "$rollback_result"
        echo "FIX:FlClash 回滚失败，请手动恢复备份 $backup"
        return 1
    fi
    open -a "FlClash"
    echo "FIX:FlClash 验证失败，已回滚；请检查 fake-IP、代理组及节点 UDP 支持"
    return 1
}

# 一键修复:按项自动修复,每步输出结果
fix() {
    configuration_write_begin fix || return 1
    if [ "$PROXY_MODE" = "flclash" ]; then
        fix_flclash || return 1
    fi
    if [ "$PROXY_MODE" = "clash-verge" ]; then
        local policy_result
        if ! policy_result=$(clash_rules policy 2>&1); then
            echo "FIX:WebRTC 策略无效 ✗ $policy_result"
            return 1
        fi
        WEBRTC_POLICY="$policy_result"
    fi
    echo "FIX:开始一键修复"

    # A. 代理内核 + TUN
    if [ "$PROXY_MODE" = "clash-verge" ]; then
        if ! pgrep -f verge-mihomo >/dev/null 2>&1; then
            echo "FIX:正在启动 Clash Verge..."
            open -a "Clash Verge"
        fi
        for i in $(seq 1 40); do
            pgrep -f verge-mihomo >/dev/null 2>&1 && tun_up && break
            sleep 1
        done
        if pgrep -f verge-mihomo >/dev/null 2>&1 && tun_up; then
            echo "FIX:代理内核+TUN ✓"
        else
            V="$CLASH_DIR/verge.yaml"
            if [ -f "$V" ] && grep -q '^enable_tun_mode: false' "$V"; then
                echo "FIX:检测到 TUN 开关被关闭,正在恢复..."
                osascript -e 'tell application "Clash Verge" to quit' >/dev/null 2>&1
                sleep 3
                sed -i '' 's/^enable_tun_mode: false/enable_tun_mode: true/' "$V"
                open -a "Clash Verge"
                for i in $(seq 1 40); do
                    tun_up && break
                    sleep 1
                done
            fi
            if pgrep -f verge-mihomo >/dev/null 2>&1 && tun_up; then
                echo "FIX:代理内核+TUN ✓(已恢复TUN开关)"
            else
                echo "FIX:代理内核/TUN ✗(请打开 Clash Verge 检查节点/线路)"
            fi
        fi
    else
        app=$(proxy_app_name)
        if ! proxy_alive && [ -n "$app" ]; then
            echo "FIX:正在启动 $app..."
            open -a "$app" >/dev/null 2>&1
        fi
        for i in $(seq 1 40); do
            proxy_alive && tun_up && break
            sleep 1
        done
        if proxy_alive && tun_up; then
            echo "FIX:代理内核+TUN ✓"
        else
            echo "FIX:代理内核/TUN ✗(请打开 $(proxy_name) 手动检查节点/线路及TUN模式)"
        fi
    fi

    # B. 时区/IPv6/DNS缓存/保镖(需管理员权限,会弹一次密码框)
    tz=$(readlink /etc/localtime 2>/dev/null)
    v6=$(networksetup -getinfo Wi-Fi 2>/dev/null | grep -i '^IPv6:')
    adminCmds=""
    [ "$tz" != "/var/db/timezone/zoneinfo/$TZ_TARGET" ] && adminCmds="${adminCmds}/usr/sbin/systemsetup -settimezone $TZ_TARGET; "
    echo "$v6" | grep -qi 'off' || adminCmds="${adminCmds}networksetup -setv6off Wi-Fi; "
    if ! launchctl print system/local.claude-bodyguard >/dev/null 2>&1; then
        adminCmds="${adminCmds}cp $HOME/.local/bin/local.claude-bodyguard.plist /Library/LaunchDaemons/ && chown root:wheel /Library/LaunchDaemons/local.claude-bodyguard.plist && launchctl bootstrap system /Library/LaunchDaemons/local.claude-bodyguard.plist; "
    fi
    if [ -n "$adminCmds" ]; then
        adminCmds="${adminCmds}dscacheutil -flushcache; killall -HUP mDNSResponder"
        echo "FIX:修复时区/IPv6/DNS/保镖(需要密码)..."
        if osascript <<ASEOF >/dev/null 2>&1
do shell script "$adminCmds" with prompt "一键修复需要管理员权限" with administrator privileges
ASEOF
        then
            echo "FIX:时区/IPv6/DNS/保镖 ✓"
        else
            echo "FIX:时区/IPv6/保镖 ✗(取消或失败)"
        fi
    else
        echo "FIX:时区/IPv6/保镖 无需修复 ✓"
    fi

    # B3. Claude 客户端单独设英文(用户级,无需密码;系统首选语言已是英文则跳过)
    syslang=$(defaults read -g AppleLanguages 2>/dev/null | sed -n '2p' | tr -d ' ,"')
    if ! defaults read com.anthropic.claudefordesktop AppleLanguages 2>/dev/null | grep -q 'en-US' && [[ "$syslang" != en* ]]; then
        defaults write com.anthropic.claudefordesktop AppleLanguages -array en-US
        echo "FIX:Claude 客户端语言 → en-US ✓(重启 Claude 后生效)"
    fi

    # B4. 地区格式(用户级,无需密码)
    locale=$(defaults read -g AppleLocale 2>/dev/null)
    if [ "$locale" != "$LOCALE_TARGET" ]; then
        defaults write -g AppleLocale "$LOCALE_TARGET"
        echo "FIX:地区格式 → $LOCALE_TARGET ✓"
    fi

    # B2. 更新全局脚本并检查运行规则;写入失败或未加载均不能报成功。全局模式会绕过防护，不能报告成功。
    if [ "$PROXY_MODE" = "clash-verge" ] && [ "$(clash_mode)" = "global" ]; then
        echo "FIX:全局模式绕过自有规则和 STUN 防护 ✗ 请在 Clash Verge 切换到规则模式；国内直连、Claude/IP 检测优先代理"
        return 1
    elif [ "$PROXY_MODE" = "clash-verge" ]; then
        echo "FIX:写入全局扩展脚本(保留已有 main 与无关配置)..."
        local rules_result
        if ! rules_result=$(write_merge_rules 2>&1); then
            echo "FIX:置顶规则 ✗ $rules_result"
            return 1
        fi
        osascript -e 'tell application "Clash Verge" to quit' >/dev/null 2>&1
        sleep 3
        open -a "Clash Verge"
        for i in $(seq 1 40); do tun_up && break; sleep 1; done
        local loaded=0
        for i in $(seq 1 5); do
            if rules_result=$(clash_rules verify 2>&1); then loaded=1; break; fi
            sleep 1
        done
        if [ "$loaded" -ne 1 ]; then
            echo "FIX:置顶规则 ✗ 未验证内核加载:$rules_result"
            echo "FIX:请检查全局扩展脚本是否启用、代理组是否存在，以及节点 UDP 支持"
            return 1
        fi
        echo "FIX:置顶规则 ✓ 已验证内核实际加载"
    elif [ "$PROXY_MODE" != "flclash" ] && ! route_ok; then
        echo "FIX:置顶规则 ✗ 请在 $(proxy_name) 中自行添加 DOMAIN-SUFFIX 置顶规则"
    fi

    # C. 复测与日常审计共用判断，空结果、非零退出及无响应不会冒充代理成功。
    local fix_failed=0
    wrip=$(curl -4 -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null | grep '^ip=' | cut -d= -f2)
    wr=$(webrtc_status "$wrip")
    case "$wr" in
        ok:*) echo "FIX:WebRTC ✓ ${wr#ok:}" ;;
        *) echo "FIX:WebRTC ✗ ${wr#fail:}"; fix_failed=1 ;;
    esac

    # D. 出口IP最终验证
    sleep 2
    trace=$(curl -4 -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)
    ip=$(echo "$trace" | grep '^ip=' | cut -d= -f2)
    loc=$(echo "$trace" | grep '^loc=' | cut -d= -f2)
    if [ "$loc" = "$COUNTRY_TARGET" ]; then
        echo "FIX:出口IP ✓ ($ip/$loc)"
    else
        echo "FIX:出口IP ✗ (${ip:-无}/${loc:-未知},期望$COUNTRY_TARGET) — 可能是节点故障,请到 $(proxy_name) 里换条线路"
    fi
    echo "FIX:修复流程结束"
    return "$fix_failed"
}

# 更换代理客户端:改写已安装脚本头部的 PROXY_MODE(用于安装后换客户端/装错客户端)
set_client() {
    configuration_write_begin set-client "${1:-}" || return 1
    mode="${1:-}"
    case "$mode" in
        clash-verge|flclash|shadowrocket|sing-box|clashx|surge|auto) ;;
        *) echo "FAIL:用法: claude-switch.sh set-client <clash-verge|flclash|shadowrocket|sing-box|clashx|surge|auto>"; return 1 ;;
    esac
    target="${CLAUDE_SWITCHER_CORE:-}"
    if [ -z "$target" ]; then
        if [ -f "$HOME/.local/bin/claude-switch.sh" ] && [ -w "$HOME/.local/bin/claude-switch.sh" ]; then
            target="$HOME/.local/bin/claude-switch.sh"
        else
            target="$0"
        fi
    fi
    [ -f "$target" ] && [ -w "$target" ] || { echo "FAIL:找不到可写的核心脚本: $target"; return 1; }
    sed -i '' "s|^PROXY_MODE=\"[^\"]*\"|PROXY_MODE=\"$mode\"|" "$target" || { echo "FAIL:写入失败"; return 1; }
    grep -q "^PROXY_MODE=\"$mode\"" "$target" || { echo "FAIL:校验失败"; return 1; }
    PROXY_MODE="$mode"
    echo "OK:代理客户端已设为 $mode"
    guard_state=$(bodyguard_status)
    case "$guard_state" in
        fail:未安装) echo "INFO:保镖尚未安装,checkdetail 会提示未安装" ;;
        fail:*) echo "WARN:保镖校验未通过(${guard_state#fail:});请重新运行安装器同步 root 守护脚本(需管理员权限),完成前不要启动 Claude" ;;
    esac
    echo "INFO:重新运行 checkdetail 生效;非 Clash 客户端请在应用内自行添加 Claude 域名置顶规则"
    return 0
}

# 只读输出当前代理客户端(供 UI 下拉高亮),同时列出全部支持项
get_client() {
    echo "client=$PROXY_MODE"
    echo "clients=clash-verge,flclash,shadowrocket,sing-box,clashx,surge"
}

# 地区自适应:按当前出口国家一键重设 国家/时区/地区格式(换了国家节点后运行)
set_region() {
    configuration_write_begin set-region || return 1
    local geo gfile ctry ntz src nlocale rc
    gfile=$(mktemp "${TMPDIR:-/tmp}/claude-geo.XXXXXX" 2>/dev/null) || gfile=""
    GEO_DIAG_FILE="$gfile"
    geo=$(geo_lookup); rc=$?
    if [ -n "$gfile" ]; then
        GEO_DIAG=$(tr '\n' ';' < "$gfile" 2>/dev/null | sed 's/;$//')
        rm -f "$gfile"
    else
        GEO_DIAG=""
    fi
    unset GEO_DIAG_FILE
    if [ "$rc" -ne 0 ] || [ -z "$geo" ] || [ "${geo%%|*}" = "" ]; then
        echo "FAIL:所有出口探测源均失败(${GEO_DIAG:-无诊断信息})"
        echo "INFO:请确认代理已开启;若代理规则把 ipinfo/ipapi 判直连,可改为手动设置或稍后重试"
        return 1
    fi
    ctry=${geo%%|*}; geo=${geo#*|}
    if ! [[ "$ctry" =~ ^[A-Z]{2}$ ]]; then
        echo 'FAIL:出口国家格式无效，地区设置未变更'
        return 1
    fi
    ntz=${geo%%|*}; src=${geo#*|}
    if [ "$ctry" = "CN" ]; then
        echo "FAIL:出口国家为 CN(来源 $src),已中止;请先切换节点再重试"
        return 1
    fi
    [ -n "$ntz" ] || ntz=$(default_tz_for "$ctry")
    [ "$src" = "cloudflare" ] && echo "INFO:时区使用国家默认(可手动改)"
    nlocale=$(locale_for "$ctry")

    # 改写脚本自身头部三个配置项
    sed -i '' \
        -e "s|^COUNTRY_TARGET=\".*\"|COUNTRY_TARGET=\"$ctry\"|" \
        -e "s|^TZ_TARGET=\".*\"|TZ_TARGET=\"$ntz\"|" \
        -e "s|^LOCALE_TARGET=\".*\"|LOCALE_TARGET=\"$nlocale\"|" \
        "$0"
    COUNTRY_TARGET="$ctry"; TZ_TARGET="$ntz"; LOCALE_TARGET="$nlocale"

    # 地区格式(用户级,无需密码)
    defaults write -g AppleLocale "$nlocale"

    # 时区(需管理员权限,会弹一次授权框);看门狗脚本在 root 目录下,顺道用同一次授权同步目标值
    WD="/Library/Application Support/claude-switcher/tz-watchdog.sh"
    adminCmds="/usr/sbin/systemsetup -settimezone $ntz"
    wdsync=0
    if [ -f "$WD" ] && [ ! -w "$WD" ]; then
        adminCmds="$adminCmds; /usr/bin/sed -i '' 's|^TARGET=.*|TARGET=$ntz|' '$WD'"
        wdsync=1
    fi
    BG="/Library/Application Support/claude-switcher/claude-bodyguard.sh"
    if [ -r "$BG" ] && grep -q '^COUNTRY_TARGET=' "$BG"; then
        adminCmds="$adminCmds && /usr/bin/sed -i '' 's|^COUNTRY_TARGET=.*|COUNTRY_TARGET=$ctry|' '$BG' && /bin/launchctl kickstart -k system/local.claude-bodyguard"
    fi
    if ! osascript <<ASEOF >/dev/null 2>&1
do shell script "$adminCmds" with prompt "set-region 需要管理员权限以锁定时区" with administrator privileges
ASEOF
    then
        echo "FAIL:时区设置被取消或失败(国家/地区格式已更新为 $ctry/$nlocale)"
        return 1
    fi
    # 看门狗目标同步兜底(可写的老版本安装直接 sed;失败则提示运行安装器刷新)
    if [ -f "$WD" ] && [ "$wdsync" -eq 0 ]; then
        sed -i '' "s|^TARGET=.*|TARGET=$ntz|" "$WD" 2>/dev/null || echo "WARN:看门狗目标未同步,请运行安装器刷新"
    fi

    echo "OK:region=$ctry tz=$ntz locale=$nlocale source=$src"
}

# 更新锁定稳定 Release 的提交与组件清单；main 必须显式 --track main。
# 用户组件事务提交，App/root守护变更只暂存完整安装包并提示授权安装。
self_update() {
    local manager bin_dir arg readonly=0 automatic=0
    bin_dir=$(cd "$(dirname "$0")" && pwd)
    manager="$BASE/component-manager.py"
    [ -f "$manager" ] || manager="$bin_dir/component-manager.py"
    for arg in "$@"; do
        [ "$arg" = "--check" ] && readonly=1
        [ "$arg" = "--auto" ] && automatic=1
    done
    if [ ! -f "$manager" ]; then
        echo "FAIL:缺少组件更新器,请用完整 installer-kit 安装"; return 1
    fi
    if [ "$readonly" -eq 0 ] && type account_lock_reexec >/dev/null 2>&1; then
        account_lock_reexec update "$@"
    fi
    if [ "$automatic" -eq 1 ]; then
        mkdir -p "$BASE"
        if [ -f "$BASE/update.log" ] && [ "$(wc -c < "$BASE/update.log")" -gt 524288 ]; then
            mv "$BASE/update.log" "$BASE/update.log.1"
        fi
        "${CLAUDE_SWITCHER_PYTHON:-python3}" "$manager" update --base "$BASE" --bin-dir "$bin_dir" "$@" >>"$BASE/update.log" 2>&1
    else
        "${CLAUDE_SWITCHER_PYTHON:-python3}" "$manager" update --base "$BASE" --bin-dir "$bin_dir" "$@"
    fi
}

# 设备指纹体检:字体地区特征 + GPU/虚拟机特征(Canvas/WebGL 由 Web 检测页覆盖)
fingerprint() {
    echo "# 设备指纹体检 $(date '+%F %T')"
    hits=""
    for d in /Library/Fonts "$HOME/Library/Fonts"; do
        [ -d "$d" ] || continue
        while IFS= read -r f; do
            b=$(basename "$f" | tr '[:upper:]' '[:lower:]')
            case "$b" in
                *simsun*|*simhei*|*simkai*|*simfang*|*msyh*|*msjh*|*dengxian*|*wqy*|*fzshu*|*fzkai*|*fzhei*|*fangsong*) hits="$hits $b" ;;
            esac
        done < <(find "$d" -type f \( -iname '*.ttf' -o -iname '*.ttc' -o -iname '*.otf' \) 2>/dev/null)
    done
    if [ -n "$hits" ]; then
        echo "字体=warn:检测到地区特征字体:${hits# }"
        echo "建议=上述字体通常随中文办公软件安装,美区环境很少出现;不用的可在字体册停用"
    else
        echo "字体=ok:未发现中文办公字体(仅系统自带字体)"
    fi

    gpu=$(system_profiler SPDisplaysDataType 2>/dev/null)
    chip=$(echo "$gpu" | grep -m1 'Chipset Model' | sed 's/.*: //')
    res=$(echo "$gpu" | grep -m1 'Resolution' | sed 's/.*: //')
    [ -z "$chip" ] && chip="未知"
    echo "GPU=ok:${chip}"
    echo "显示=ok:${res:-未知}"
    virt=$(sysctl -n kern.hv_vmm_present 2>/dev/null)
    case "$chip" in
        *Virtual*|*VMware*|*Parallels*|*VirtualBox*|*SwiftShader*|*llvmpipe*)
            echo "虚拟机=warn:检测到虚拟 GPU($chip),这是强指纹" ;;
        *)
            [ "$virt" = "1" ] && echo "虚拟机=warn:运行在虚拟机中" || echo "虚拟机=ok:物理机" ;;
    esac
    echo "CanvasWebGL=info:原生检查读不到 Claude Electron 的渲染特征,请打开 Web 检测页 https://yoursjeyo.github.io/claude-switcher/ 查看 Canvas/WebGL 指纹"
}

# 账号快照加密导出/导入(macOS):AES-256 加密 DMG,密码只经 stdin 传递
# 一次性隔离浏览器:独立临时 profile,禁用非代理 UDP,关闭后销毁;URL 仅允许 http/https
# Claude Code CLI 体检:只读检测安装方式/版本/多份安装冲突,不执行卸载或重装
cli_info() {
    echo "# Claude Code CLI 体检 $(date '+%F %T')"
    path=$(command -v claude 2>/dev/null)
    if [ -n "$path" ]; then
        real=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$path" 2>/dev/null || echo "$path")
        ver=$(perl -e 'alarm 4; exec @ARGV' "$real" --version 2>/dev/null | head -1)
        [ -z "$ver" ] && ver=$(perl -e 'alarm 4; exec @ARGV' "$path" --version 2>/dev/null | head -1)
        echo "CLI=ok:$path"
        [ "$real" != "$path" ] && echo "CLI真实路径=info:$real"
        echo "版本=ok:${ver:-未获取}"
        case "$real" in
            *".claude/local"*|*".local/share/claude"*) method="native(官方安装脚本)" ;;
            *"/opt/homebrew/"*|*"/usr/local/Cellar/"*|*"/home/linuxbrew/"*) method="homebrew" ;;
            *"node_modules"*|*".npm-global"*|*"/.bun/"*) method="npm/bun" ;;
            *) method="未知(建议核对 PATH)" ;;
        esac
        echo "安装方式=info:$method"
    else
        echo "CLI=warn:PATH 中没有 claude 命令"
        echo "安装方式=info:未安装(桌面版可能自带内置 CLI,见下)"
    fi

    bundled=$(ls -d "$HOME/Library/Application Support/Claude/claude-code"/* 2>/dev/null | tail -1)
    if [ -n "$bundled" ]; then
        echo "桌面版内置=ok:$(basename "$bundled")"
    else
        echo "桌面版内置=info:未发现(Claude 桌面版未安装或版本较旧)"
    fi

    if command -v npm >/dev/null 2>&1 && npm ls -g --depth=0 @anthropic-ai/claude-code >/dev/null 2>&1; then
        echo "npm全局包=warn:检测到 @anthropic-ai/claude-code"
    else
        echo "npm全局包=ok:未检测到"
    fi
    if command -v brew >/dev/null 2>&1 && brew list --versions claude-code >/dev/null 2>&1; then
        echo "Homebrew包=warn:$(brew list --versions claude-code 2>/dev/null | head -1)"
    else
        echo "Homebrew包=ok:未检测到"
    fi

    cfg="$HOME/.claude"
    if [ -d "$cfg" ]; then
        echo "配置目录=ok:$cfg ($(du -sh "$cfg" 2>/dev/null | cut -f1))"
    else
        echo "配置目录=info:未创建(尚未运行过 CLI)"
    fi

    [ -n "$path" ] && echo "建议=info:升级用对应包管理器(brew upgrade claude-code / npm i -g @anthropic-ai/claude-code@latest);卸载前先确认桌面版是否依赖它"
    return 0
}

open_safe() {
    url="${1:-}"
    case "$url" in
        http://*|https://*) ;;
        '') echo "FAIL:用法: claude-switch.sh open-safe <http(s)://链接>"; return 1 ;;
        *) echo "FAIL:只允许 http/https 链接"; return 1 ;;
    esac
    require_launch_audit || { echo 'FAIL:完整环境审计未通过，拒绝打开临时浏览器'; return 1; }
    chrome="${CLAUDE_SWITCHER_CHROME:-}"
    if [ -z "$chrome" ]; then
        for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
                 "/Applications/Chromium.app/Contents/MacOS/Chromium" \
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
                 "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"; do
            [ -x "$c" ] && chrome="$c" && break
        done
    fi
    [ -n "$chrome" ] && [ -x "$chrome" ] || { echo "FAIL:未找到 Chrome/Chromium/Edge/Brave"; return 1; }

    profile=$(mktemp -d "${TMPDIR:-/tmp}/claude-switcher-safe.XXXXXX") || { echo "FAIL:无法创建临时 profile"; return 1; }
    chmod 700 "$profile"
    lang="${LOCALE_TARGET:-en_US}"; lang="${lang/_/-}"
    nohup "$chrome" \
        --user-data-dir="$profile" \
        --no-first-run --no-default-browser-check \
        --disable-sync --disable-extensions \
        --force-webrtc-ip-handling-policy=disable_non_proxied_udp \
        --lang="$lang" \
        "$url" >/dev/null 2>&1 &
    pid=$!
    nohup bash -c '
        while kill -0 "$1" 2>/dev/null; do
            sleep 2
            if ! /bin/bash "$3" guard-check >/dev/null 2>&1; then
                kill -TERM "$1" 2>/dev/null
                pkill -TERM -f -- "--user-data-dir=$2" 2>/dev/null || true
                break
            fi
        done
        for attempt in 1 2 3 4 5; do
            kill -0 "$1" 2>/dev/null || break
            sleep 1
        done
        # Do not delete a profile while its browser remains alive.
        kill -0 "$1" 2>/dev/null || rm -rf "$2"
    ' _ "$pid" "$profile" "$0" >/dev/null 2>&1 &
    disown 2>/dev/null || true
    echo "OK:已在一次性隔离浏览器打开(profile 将在窗口关闭后销毁)"
    echo "INFO:该窗口不继承你日常浏览器的 cookies/插件;在其中登录产生的会话会随窗口关闭一并丢弃"
}

snapshot_export() {
    account_lock_reexec snapshot-export "${1:-}" "${2:-}" "${3:-}" || return 1
    (
        target="${1:-}"; out="${2:-}"; mode="${3:-}"
        trap 'stty echo 2>/dev/null || true; exit 130' INT TERM
        source=$(account_state_call snapshot-source --account "$target") || { printf '%s\n' "$source"; exit 1; }
        [ -n "$out" ] || { echo "FAIL:请指定导出文件路径"; exit 1; }
        case "$out" in /*) ;; *) out="$PWD/$out" ;; esac
        [ ! -e "$out" ] && [ ! -L "$out" ] || { echo "FAIL:目标文件已存在,请换一个路径"; exit 1; }
        if [ "$mode" = "--password-stdin" ]; then
            IFS= read -r pass || true
        else
            printf '设置导出密码(至少 8 位): '; stty -echo 2>/dev/null; IFS= read -r pass; stty echo 2>/dev/null; echo
            printf '再次输入: '; stty -echo 2>/dev/null; IFS= read -r pass2; stty echo 2>/dev/null; echo
            [ "$pass" = "$pass2" ] || { echo "FAIL:两次密码不一致"; exit 1; }
        fi
        [ ${#pass} -ge 8 ] || { unset pass; echo "FAIL:密码至少 8 位"; exit 1; }
        stage=$(mktemp -d "$BASE/.export.XXXXXXXX") || { echo "FAIL:无法创建导出暂存目录"; exit 1; }
        chmod 700 "$stage"
        trap 'unset pass pass2; rm -rf "$stage"' EXIT
        trap 'exit 130' INT TERM
        if ! ditto "$source/" "$stage/profile/"; then echo "FAIL:复制快照失败"; exit 1; fi
        cat > "$stage/profile/.claude-switcher-snapshot.json" <<EOF
{"format":"claude-switcher-snapshot","version":1,"account":"$target","created":"$(date '+%F %T')"}
EOF
        if ! printf '%s\n' "$pass" | hdiutil create -srcfolder "$stage/profile" -volname "ClaudeSnapshot-$target" \
            -encryption AES-256 -stdinpass "$stage/snapshot.dmg" >/dev/null 2>&1; then
            echo "FAIL:创建加密镜像失败"; exit 1
        fi
        if ! printf '%s\n' "$pass" | hdiutil verify -stdinpass "$stage/snapshot.dmg" >/dev/null 2>&1; then
            echo "FAIL:镜像校验失败"; exit 1
        fi
        unset pass pass2
        # O_EXCL never truncates a pre-existing output, including a broken symlink.
        if ! python3 - "$stage/snapshot.dmg" "$out" <<'PY_EXPORT'
import os, shutil, sys
source, destination = sys.argv[1:]
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
created = os.fstat(fd)
try:
    with os.fdopen(fd, "wb") as output, open(source, "rb") as data:
        shutil.copyfileobj(data, output)
        output.flush()
        os.fsync(output.fileno())
except BaseException:
    try:
        current = os.lstat(destination)
        if (current.st_ino, current.st_dev) == (created.st_ino, created.st_dev):
            os.unlink(destination)
    except FileNotFoundError:
        pass
    raise
PY_EXPORT
        then echo "FAIL:导出文件写入失败，未覆盖已有文件"; exit 1; fi
        echo "OK:账号$target 已加密导出到 $out"
    )
}

snapshot_import() {
    account_lock_reexec snapshot-import "${1:-}" "${2:-}" "${3:-}" || return 1
    (
        file="${1:-}"; target="${2:-}"; mode="${3:-}"
        trap 'stty echo 2>/dev/null || true; exit 130' INT TERM
        account_state_call validate-import --account "$target" || exit 1
        [ -f "$file" ] || { echo "FAIL:找不到镜像文件"; exit 1; }
        if [ "$mode" = "--password-stdin" ]; then
            IFS= read -r pass || true
        else
            printf '输入镜像密码: '; stty -echo 2>/dev/null; IFS= read -r pass; stty echo 2>/dev/null; echo
        fi
        [ -n "$pass" ] || { echo "FAIL:密码为空"; exit 1; }
        mnt=$(mktemp -d "$BASE/.import-mount.XXXXXXXX") || { echo "FAIL:无法创建挂载目录"; exit 1; }
        chmod 700 "$mnt"
        attached=0
        cleanup_import_mount() {
            unset pass
            if [ "$attached" = "1" ]; then
                if ! hdiutil detach "$mnt" >/dev/null 2>&1; then
                    echo "WARN:镜像仍挂载，已保留挂载目录，请手动弹出后再清理" >&2
                    return
                fi
            fi
            rmdir "$mnt" 2>/dev/null || true
        }
        trap cleanup_import_mount EXIT
        trap 'exit 130' INT TERM
        if ! printf '%s\n' "$pass" | hdiutil attach -stdinpass -nobrowse -readonly -mountpoint "$mnt" "$file" >/dev/null 2>&1; then
            echo "FAIL:密码错误或镜像损坏"; exit 1
        fi
        attached=1
        unset pass
        account_state_call snapshot-import --source "$mnt" --account "$target"
    )
}

status() {
    account_state_call status
}

quit_claude() {
    local pattern='/Claude[.]app/Contents/'
    pgrep -u "$(id -u)" -f "$pattern" >/dev/null 2>&1 || return 0
    osascript -e 'tell application "Claude" to quit' >/dev/null 2>&1 || return 1
    for i in $(seq 1 15); do
        pgrep -u "$(id -u)" -f "$pattern" >/dev/null 2>&1 || return 0
        sleep 1
    done
    return 1
}

valid_account() {
    case "${1:-}" in
        A|B|C|D) return 0 ;;
        *) echo "FAIL:账号必须是 A、B、C 或 D"; return 1 ;;
    esac
}

recover_accounts() {
    account_lock_reexec recover || return 1
    quit_claude || { echo "FAIL:Claude 未能退出，恢复资料未修改"; return 1; }
    account_state_call recover
}

switch_to() {
    local target="${1:-}" output
    valid_account "$target" || return 1
    account_lock_reexec switch "$target" || return 1
    # The inherited account lock covers audit, recovery, data commit and launch.
    require_launch_audit || return 1
    quit_claude || { echo "FAIL:Claude 未能退出，请手动退出后重试"; return 1; }
    output=$(account_state_call switch --account "$target") || { printf '%s\n' "$output"; return 1; }
    printf '%s\n' "$output"
    require_launch_audit || { echo "FAIL:账号数据已安全切换，但启动前环境复核失败"; return 1; }
    if ! open -a "Claude"; then
        echo "FAIL:账号数据已切换，但 Claude 启动失败；修复应用后可重试同一账号"
        return 1
    fi
    log "已切换到账号$target 并提交事务"
    echo "OK:已切换到账号$target 并启动 Claude"
}

set_check_config() {
    configuration_write_begin set-check-config "${1:-}" || return 1
    "$0" _support config-set "${1:-}"
}

operation="${1:-}"
started_at=$(date +%s)
summary=""
case "$operation" in
    guard-check)
        proxy_alive && network_check health >/dev/null 2>&1 && route_ok ;;
    check)  check ;;
    checkdetail)
        OUT_FILE="$BASE/last-check.txt"
        report=$(checkdetail 2>&1); report_rc=$?
        printf '%s\n' "$report"
        if check_tmp=$(mktemp "$BASE/.last-check.XXXXXX"); then
            printf '%s\n' "$report" > "$check_tmp" && mv -f "$check_tmp" "$OUT_FILE"
            [ ! -f "$check_tmp" ] || rm -f "$check_tmp"
        fi
        summary=$(printf '%s\n' "$report" | awk -F= '{ split($2, s, ":"); if ($1 != "" && s[1] ~ /^(ok|warn|fail|skip)$/) print $1 "=" s[1] }' | tr '\n' ' ')
        [ "$report_rc" -eq 0 ] && printf '%s\n' "$report" | audit_validate >/dev/null
        ;;
    fix) fix ;;
    status) status ;;
    recover) recover_accounts ;;
    switch) switch_to "${2:-}" ;;
    set-region) set_region ;;
    set-client) set_client "${2:-}" ;;
    get-client) get_client ;;
    update) self_update "${@:2}" ;;
    update-check) self_update --check ;;  # 只读检查;独立入口,避免旧版核心把 --check 当成真实更新
    open-safe) open_safe "${2:-}" ;;
    cli-info) cli_info ;;
    snapshot-export) snapshot_export "${2:-}" "${3:-}" "${4:-}" ;;
    snapshot-import) snapshot_import "${2:-}" "${3:-}" "${4:-}" ;;
    export-diagnostics) "$0" _support export-diagnostics "${2:-}" ;;
    get-check-config) "$0" _support config-get ;;
    set-check-config) set_check_config "${2:-}" ;;
    write-merge-rules) write_merge_rules ;;  # 隐藏入口:供安装器调用,保证两处合并逻辑一致
    fingerprint) fingerprint ;;
    *) echo "用法: claude-switch.sh check|checkdetail|fix|status|recover|switch A|B|C|D|set-region|set-client <mode>|update [--check|--auto]|update-check|fingerprint|snapshot-export <A|B|C|D> <file>|snapshot-import <file> <A|B|C|D>|open-safe <url>|cli-info|export-diagnostics <file>|get-check-config|set-check-config <json>|diagnose-proxy [--proxy URL|--config FILE]"; exit 2 ;;
esac
rc=$?
case "$operation" in
    get-check-config|set-check-config|export-diagnostics) ;;
    *) log_result "$started_at" "$operation" "$rc" "$summary" ;;
esac
exit "$rc"
