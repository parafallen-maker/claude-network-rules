#!/bin/bash
# Root-owned launchd job. Only system logging; never opens a user-owned log file.
# Installer substitutes __HOME__, __PROXY_MODE__, and __UID__ safely.
set -u
MODE="__PROXY_MODE__"
OWNER_UID="__UID__"
USER_DIR="__HOME__"
PROXY_GROUP="__PROXY_GROUP__"
COUNTRY_TARGET="__COUNTRY_TARGET__"
ROOT_DIR="/Library/Application Support/claude-switcher"
PYTHON="/usr/bin/python3"
ok=1

case "$OWNER_UID" in ''|*[!0-9]*) exit 1 ;; esac
[ -x "$PYTHON" ] || exit 1

case "$MODE" in
    clash-verge|flclash)
        exec "$PYTHON" "$ROOT_DIR/network-monitor.py" run --owner-uid "$OWNER_UID" \
            --user-home "$USER_DIR" --proxy-mode "$MODE" --proxy-group "$PROXY_GROUP" \
            --expected-country "$COUNTRY_TARGET" ;;
esac

# Verify active interfaces, actual IPv4/IPv6 target routes, not fake-IP ranges.
"$PYTHON" "$ROOT_DIR/network-check.py" health >/dev/null 2>&1 || ok=0
case "$MODE" in
    clash-verge) /usr/bin/pgrep -f verge-mihomo >/dev/null 2>&1 || ok=0 ;;
    flclash) /usr/bin/pgrep -f FlClashCore >/dev/null 2>&1 || ok=0 ;;
    shadowrocket) /usr/sbin/scutil --nc list 2>/dev/null | /usr/bin/grep 'com.liguangming.Shadowrocket' | /usr/bin/grep -q '(Connected)' || ok=0 ;;
    sing-box) /usr/bin/pgrep -f sing-box >/dev/null 2>&1 || ok=0 ;;
    clashx) /usr/bin/pgrep -x ClashX >/dev/null 2>&1 || ok=0 ;;
    surge) /usr/bin/pgrep -f Surge >/dev/null 2>&1 || ok=0 ;;
    auto) : ;;  # Interface/route evidence remains mandatory.
    *) ok=0 ;;
esac

# Inspect the live controller with a root-owned parser; never execute a script
# from the account owner's directory as root. guard infers/validates its group.
case "$MODE" in
    clash-verge|flclash)
        config_dir="$USER_DIR/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
        [ "$MODE" = "flclash" ] && config_dir="$USER_DIR/Library/Application Support/com.follow.clash"
        "$PYTHON" "$ROOT_DIR/clash-policy.py" guard "$config_dir" "$USER_DIR/.claude-switcher" auto '' >/dev/null 2>&1 || ok=0 ;;
esac

# A disabled PF guard stays disabled. If explicitly enabled, watch checks its
# parent anchor, states, runtime rules and routes, and locks down on drift.
"$PYTHON" "$ROOT_DIR/network-guard.py" watch >/dev/null 2>&1 || ok=0
if [ "$ok" -eq 0 ]; then
    if [ -f /var/run/claude-switcher-network-guard.json ]; then
        "$PYTHON" "$ROOT_DIR/network-guard.py" lockdown --if-enabled >/dev/null 2>&1 || true
    fi
    if /usr/bin/pgrep -u "$OWNER_UID" -f '/Applications/Claude.app/Contents/' >/dev/null 2>&1; then
        /usr/bin/pkill -TERM -u "$OWNER_UID" -f '/Applications/Claude.app/Contents/'
        /bin/sleep 2
        /usr/bin/pkill -KILL -u "$OWNER_UID" -f '/Applications/Claude.app/Contents/' 2>/dev/null || true
        /usr/bin/logger -t claude-switcher 'Network evidence failed; protected desktop process tree stopped. Review local diagnostics.'
    fi
fi
