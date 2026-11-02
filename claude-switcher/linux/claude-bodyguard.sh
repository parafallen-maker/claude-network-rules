#!/bin/bash
# User service; never controls unrelated browser profiles.
set -u
BASE="$HOME/.claude-switcher"
MODE="__PROXY_MODE__"
PYTHON="${CLAUDE_SWITCHER_PYTHON:-python3}"
while true; do
    ok=1
    "$PYTHON" "$BASE/network-check.py" health >/dev/null 2>&1 || ok=0
    case "$MODE" in
        clash-verge|mihomo)
            pgrep -f 'verge-mihomo|mihomo' >/dev/null 2>&1 || ok=0
            config_dir="$HOME/.config/io.github.clash-verge-rev.clash-verge-rev"
            [ "$MODE" = "mihomo" ] && config_dir="$HOME/.config/mihomo"
            "$PYTHON" "$BASE/clash-policy.py" guard "$config_dir" "$BASE" auto '' >/dev/null 2>&1 || ok=0 ;;
        *) ok=0 ;; # Unadapted policies are unknown, never implicitly safe.
    esac
    if [ "$ok" -eq 0 ] && pgrep -u "$(id -u)" -x claude >/dev/null 2>&1; then
        pkill -TERM -u "$(id -u)" -x claude
        logger -t claude-switcher 'Network evidence failed; protected CLI stopped.'
    fi
    sleep 10
done
