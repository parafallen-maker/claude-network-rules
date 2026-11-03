#!/bin/bash
# Root-owned template. Logs go to the system logger, never a user-writable path.
TARGET="__TZ_TARGET__"
current=$(/usr/bin/readlink /etc/localtime 2>/dev/null)
if [ "$current" != "/var/db/timezone/zoneinfo/$TARGET" ]; then
    if /usr/sbin/systemsetup -settimezone "$TARGET" >/dev/null 2>&1; then
        /usr/bin/logger -t claude-switcher 'Configured timezone restored.'
    else
        /usr/bin/logger -t claude-switcher 'Timezone restoration failed; administrator review required.'
    fi
fi
