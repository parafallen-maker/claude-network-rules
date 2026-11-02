#!/bin/bash
# Claude 账号切换器 - Linux 核心脚本
# 用法: claude-switch.sh checkdetail|status|fix|recover|switch A|B|C|D|menu  (隐藏: write-merge-rules 供安装器调用)
set -u
export LC_ALL=en_US.UTF-8

# ============ 安装时可配置项(安装器自动改写) ============
PROXY_MODE="clash-verge"       # 代理客户端: clash-verge|mihomo|sing-box|v2raya|nekobox|hiddify|wireguard|other|auto
PROXY_GROUP="GLOBAL"           # Clash 代理组名(安装时自动探测)
COUNTRY_TARGET="US"            # 目标出口国家(安装时按出口自动识别)
TZ_TARGET="America/New_York"   # 锁定时区,应与出口IP所在国家一致
LOCALE_TARGET="en_US"          # 锁定地区格式
# =======================================================

BASE="$HOME/.claude-switcher"
DATA="$HOME/.config/google-chrome"   # Web 版登录态目录(Chrome)
[ -d "$HOME/.config/chromium" ] && [ ! -d "$DATA" ] && DATA="$HOME/.config/chromium"
LOG="$BASE/claude-switch.log"
CLASH_DIR="$HOME/.config/io.github.clash-verge-rev.clash-verge-rev"
[ -d "$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev" ] && CLASH_DIR="$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
ROUTE_MISSED=""
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

network_check() {
    local helper="$BASE/network-check.py"
    [ -f "$helper" ] || helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/network-check.py"
    [ -f "$helper" ] || { echo 'fail:缺少network-check.py，请重新安装完整组件'; return 1; }
    "${CLAUDE_SWITCHER_PYTHON:-python3}" "$helper" "$@"
}
tun_up() { network_check tunnel >/dev/null 2>&1; }
ipv6_status() { network_check ipv6; }

# Configuration writes serialize with account switches and component updates.
configuration_write_begin() {
    account_lock_reexec "$@" || return 1
    account_state_call config-ready
}


# No interface-name-only success path: a live public route must use the tunnel.
net_up() { tun_up; }
tun_any() { tun_up; }

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
        clash-verge) echo "Clash Verge" ;;
        mihomo)      echo "mihomo" ;;
        sing-box)    echo "sing-box" ;;
        v2raya)      echo "V2RayA" ;;
        nekobox)     echo "NekoBox" ;;
        hiddify)     echo "Hiddify" ;;
        wireguard)   echo "WireGuard" ;;
        other)       echo "其他代理" ;;
        auto)        echo "代理客户端" ;;
        *)           echo "$PROXY_MODE" ;;
    esac
}

# WireGuard 在线判定:存在活动的 wg-quick@ 服务(通配符加引号,交给 systemctl 自己展开)
wg_up() { systemctl list-units --type=service --state=active 'wg-quick@*' 2>/dev/null | grep -q 'wg-quick@'; }

# 代理内核在线判定:按安装时识别的客户端分派;other 看虚拟网卡,auto 任一命中即活
proxy_alive() {
    case "$PROXY_MODE" in
        clash-verge) pgrep -f 'verge-mihomo|mihomo' >/dev/null 2>&1 ;;
        mihomo)      pgrep -f mihomo >/dev/null 2>&1 ;;
        sing-box)    pgrep -f sing-box >/dev/null 2>&1 ;;
        v2raya)      pgrep -f v2raya >/dev/null 2>&1 ;;
        nekobox)     pgrep -f 'nekobox|nekoray' >/dev/null 2>&1 ;;
        hiddify)     pgrep -f hiddify >/dev/null 2>&1 ;;
        wireguard)   wg_up ;;
        other)       net_up ;;
        auto)        pgrep -f 'verge-mihomo|mihomo' >/dev/null 2>&1 \
                     || pgrep -f sing-box >/dev/null 2>&1 \
                     || pgrep -f v2raya >/dev/null 2>&1 \
                     || pgrep -f 'nekobox|nekoray' >/dev/null 2>&1 \
                     || pgrep -f hiddify >/dev/null 2>&1 \
                     || wg_up ;;
        *)           pgrep -f 'verge-mihomo|mihomo' >/dev/null 2>&1 ;;
    esac
}

dns_query() {
    # dig → nslookup → getent 逐级回退
    if command -v dig >/dev/null 2>&1; then dig +short +time=3 +tries=1 "$1" 2>/dev/null | head -1
    elif command -v nslookup >/dev/null 2>&1; then nslookup -timeout=3 "$1" 2>/dev/null | awk '/^Address: /{print $2}' | tail -1
    else getent ahostsv4 "$1" 2>/dev/null | awk 'NR==1{print $1}'; fi
}

egress() {
    local t
    t=$(curl -4 -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)
    echo "$(echo "$t" | grep '^ip=' | cut -d= -f2) $(echo "$t" | grep '^loc=' | cut -d= -f2)"
}

clash_rules() {
    local action="$1" helper="$BASE/clash-policy.py" rules_dir="$CLASH_DIR"
    shift
    [ -f "$helper" ] || helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/clash-policy.py"
    [ -f "$helper" ] || { echo 'FAIL:缺少clash-policy.py'; return 1; }
    if [ "$PROXY_MODE" = "mihomo" ]; then
        rules_dir="$HOME/.config/mihomo"
    fi
    "${CLAUDE_SWITCHER_PYTHON:-python3}" "$helper" "$action" "$rules_dir" "$BASE" "$PROXY_GROUP" "${WEBRTC_POLICY:-}" "$@"
}
clash_mode() { clash_rules mode 2>/dev/null; }
merge_ok() { clash_rules verify >/dev/null 2>&1; }
stun_ok() { merge_ok; }
proxy_rules_status() {
    local result
    case "$PROXY_MODE" in
        clash-verge|mihomo)
            if result=$(clash_rules verify-egress 2>&1); then
                echo "ok:${result#OK:}"; return 0
            fi
            echo "fail:${result#FAIL:}"; return 1 ;;
        *) echo 'fail:该客户端运行分流规则未适配；fake-IP不能证明规则置顶'; return 1 ;;
    esac
}
route_ok() {
    local result
    result=$(proxy_rules_status)
    case "$result" in ok:*) ROUTE_MISSED=""; return 0 ;; *) ROUTE_MISSED="${result#fail:}"; return 1 ;; esac
}

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
        clash-verge|mihomo)
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

# 逐项输出检查结果: 名称=ok|warn|fail:细节
checkdetail() {
    # 1. 代理内核(按安装时识别的客户端判定)
    if proxy_alive; then
        echo "代理内核=ok:$(proxy_name) 运行中"
    else
        echo "代理内核=fail:$(proxy_name) 未运行"
    fi
    # 2. TUN 网卡(fake-ip 段优先,通用虚拟网卡兜底)
    if tun_up; then
        echo 'TUN网卡=ok'
    elif tun_any; then
        echo 'TUN网卡=ok:通用虚拟网卡'
    else
        echo 'TUN网卡=fail:不在线'
    fi

    read -r ip loc <<< "$(egress)"
    # 3. 出口IP(按安装时锁定的目标国家)
    if [ "$loc" = "$COUNTRY_TARGET" ]; then echo "出口IP=ok:$ip/$loc"; else echo "出口IP=fail:${ip:-无}/${loc:-未知}(期望$COUNTRY_TARGET)"; fi
    # 4. IP 一致(含 48h 审查)
    lastip=""; [ -f "$BASE/lastip" ] && lastip=$(cat "$BASE/lastip")
    ipst=""; ipdt=""
    if [ -z "$ip" ]; then echo 'IP一致=fail:未获取到出口IP'
    elif [ -z "$lastip" ]; then ipst="ok"; ipdt="首次记录"
    elif [ "$ip" = "$lastip" ]; then ipst="ok"
    else ipst="warn"; ipdt="$lastip → $ip"; fi
    [ -n "$ip" ] && echo "$ip" > "$BASE/lastip"
    # IP 审查:48h 内出口 IP 变化 ≥3 次提示账号画像风险(只 warn 不 fail)
    if [ -n "$ipst" ]; then
        ipn=$(iphist_count "$ip")
        if [ "$ipn" -ge 3 ] 2>/dev/null; then
            if [ "$ipst" = "ok" ]; then
                ipst="warn"; ipdt="48h内出口IP变化${ipn}次,账号画像有风险,建议固定节点"
            else
                ipdt="$ipdt;48h内出口IP变化${ipn}次,账号画像有风险,建议固定节点"
            fi
        fi
        if [ -z "$ipdt" ]; then echo "IP一致=$ipst"; else echo "IP一致=$ipst:$ipdt"; fi
    fi

    # 5. 时区
    tz=$(timedatectl show -p Timezone --value 2>/dev/null)
    [ "$tz" = "$TZ_TARGET" ] && echo '时区=ok' || echo "时区=fail:$tz"

    # 6. 地区(LANG 未设时回退 localectl)
    lang="${LANG:-$(localectl status 2>/dev/null | grep 'LANG=' | head -1 | cut -d= -f2)}"
    case "$lang" in ${LOCALE_TARGET}*) echo '地区=ok' ;; *) echo "地区=fail:$lang(期望$LOCALE_TARGET)" ;; esac

    # 7. All active interfaces and real IPv6 routes, not a global sysctl guess.
    echo "IPv6=$(ipv6_status)"

    # 8. DNS answers are a signal, not evidence of encrypted upstream routing.
    dnsip=$(dns_query claude.ai)
    case "$dnsip" in
        '') echo 'DNS=fail:解析未返回地址' ;;
        198.18.*|198.19.*) echo 'DNS=warn:检测到合成地址，上游DNS链路和最终分流未由此证明' ;;
        *) echo 'DNS=warn:解析返回普通地址，上游DNS链路未验证' ;;
    esac

    # 9. No response is unknown; only verified reject policy can pass it.
    echo "WebRTC=$(webrtc_status "$ip")"

    # 10. 保镖
    if systemctl --user is-active claude-bodyguard.service >/dev/null 2>&1; then
        lastkill=$(tail -1 "$BASE/bodyguard.log" 2>/dev/null | cut -c1-40)
        echo "保镖=ok:${lastkill:-待命}"
    else
        echo '保镖=fail:未运行'
    fi

    # 11. Verify running policy, never configuration-text presence.
    echo "规则置顶=$(proxy_rules_status)"

    # 12. 客户端语言
    echo '客户端语言=ok:跟随系统'

    # 13. ASN 网络属性(住宅宽带 vs 机房)
    org=$(curl -s --max-time 6 https://ipinfo.io/json 2>/dev/null | grep '"org"' | cut -d'"' -f4)
    case "$org" in
        '') echo 'ASN属性=warn:未获取' ;;
        *Comcast*|*AT\&T*|*Verizon*|*Spectrum*|*Charter*|*Cox*|*T-Mobile*|*Cable*|*esidential*|\
        *NTT*|*KDDI*|*SoftBank*|*IIJ*|*OCN*|*So-net*|\
        *HiNet*|*Chunghwa*|*CHT*|*kbro*|*Taiwan*|*FarEasTone*|*Data\ Communication*|\
        *Singtel*|*StarHub*|*M1*|\
        *HKT*|*PCCW*|*HKBN*|*HGC*|\
        *BT*|*Sky*|*Virgin*|*TalkTalk*|*Vodafone*|\
        *Deutsche\ Telekom*|*1\&1*|*O2*) echo "ASN属性=ok:$org" ;;
        *) echo "ASN属性=warn:疑似机房($org)" ;;
    esac

    # 14. Anthropic 生态可达性
    r1=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' https://claude.ai/ 2>/dev/null)
    r2=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/models 2>/dev/null)
    if [ "$r1" != "000" ] && [ "$r2" != "000" ]; then
        echo "生态可达性=ok:claude.ai:$r1 api:$r2"
    else
        echo "生态可达性=fail:claude.ai:$r1 api:$r2"
    fi
}

# 置顶规则写入(智能合并):不整文件覆盖,用户已有的 Merge 配置原样保留
# 标记块自包含 prepend-rules 键+全部规则,重复执行幂等
write_merge_rules() {
    configuration_write_begin write-merge-rules || return 1
    M="$CLASH_DIR/profiles/Merge.yaml"
    B0="# >>> claude-switcher pinned rules >>>"
    B1="# <<< claude-switcher pinned rules <<<"
    mkdir -p "$CLASH_DIR/profiles"
    # 置顶规则清单(缩进两格)
    rules=""
    for d in claude.ai claude.com anthropic.com claudeusercontent.com challenges.cloudflare.com datadoghq.com segment.io stripe.com stripe.network stripecdn.com intercom.io intercomcdn.com intercomassets.com; do
        rules="$rules  - DOMAIN-SUFFIX,$d,$PROXY_GROUP
"
    done
    # STUN/TURN 标准端口置顶:国内直连规则会把 stun.miwifi.com 等判直连,UDP 需按端口强制进隧道
    for p in 3478 5349 19302; do
        rules="$rules  - DST-PORT,$p,$PROXY_GROUP
"
    done

    # 首次:文件不存在,整文件写入
    if [ ! -f "$M" ]; then
        cat > "$M" <<MEOF
# Profile Enhancement Merge Template for Clash Verge

profile:
  store-selected: true

# Claude 全家族及生态域名置顶强制走代理(优先级高于任何国内规则集)
$B0
prepend-rules:
$rules$B1
MEOF
        return 0
    fi

    # 已存在:备份后幂等替换(只在将要修改时备份)
    cp "$M" "$M.bak.$(date +%Y%m%d%H%M%S)"
    tmp="$M.cs.tmp"
    cp "$M" "$tmp"
    # 删除旧标记块(含标记行,兼容块在 prepend-rules 下缩进的情形)
    if grep -qF "$B0" "$tmp"; then
        awk -v b0="$B0" -v b1="$B1" '
            index($0, b0) {inblock=1; next}
            inblock && index($0, b1) {inblock=0; next}
            !inblock
        ' "$tmp" > "$tmp.out" && mv "$tmp.out" "$tmp"
        # 清理孤立的 prepend-rules: 键(其后没有规则行,只剩空行/注释/EOF)
        awk '
            { L[NR]=$0 }
            END {
                for (i=1; i<=NR; i++) {
                    if (L[i] ~ /^prepend-rules:/) {
                        has=0
                        for (j=i+1; j<=NR; j++) {
                            if (L[j] ~ /^[ \t]*- /) { has=1; break }
                            if (L[j] ~ /^[ \t]*$/ || L[j] ~ /^[ \t]*#/) continue
                            break
                        }
                        if (!has) continue
                    }
                    print L[i]
                }
            }
        ' "$tmp" > "$tmp.out" && mv "$tmp.out" "$tmp"
    fi
    # 注入:已有 prepend-rules: 则标记规则块(不带键)插到该行之后,否则文件顶部插入完整块
    # 注意:多行文本走环境变量传递(macOS awk 的 -v 不支持含换行的字符串);先写中间文件再替换,失败不碰原文件
    if grep -q '^prepend-rules:' "$tmp"; then
        if MARK0="$B0" MARK1="$B1" RULES="$rules" awk '
            /^prepend-rules:/ && !done {
                print
                print "  " ENVIRON["MARK0"]
                printf "%s", ENVIRON["RULES"]
                print "  " ENVIRON["MARK1"]
                done=1
                next
            }
            { print }
        ' "$tmp" > "$tmp.out"; then
            mv "$tmp.out" "$M" && rm -f "$tmp"
        else
            rm -f "$tmp" "$tmp.out"
            return 1
        fi
    else
        {
            echo "# Claude 全家族及生态域名置顶强制走代理(优先级高于任何国内规则集)"
            echo "$B0"
            echo "prepend-rules:"
            printf "%s" "$rules"
            echo "$B1"
            echo ""
            cat "$tmp"
        } > "$tmp.out" && mv "$tmp.out" "$M" && rm -f "$tmp"
    fi
    rm -f "$tmp"
}

fix() {
    configuration_write_begin fix || return 1
    echo "FIX:开始一键修复"

    # A. 代理内核 + TUN(仅 Clash Verge 代为启动,其他客户端只提示手动)
    if ! proxy_alive; then
        if [ "$PROXY_MODE" = "clash-verge" ]; then
            echo "FIX:正在尝试启动 Clash Verge..."
            (clash-verge >/dev/null 2>&1 &) || (clash-verge-rev >/dev/null 2>&1 &) || echo "FIX:未找到启动命令,请手动启动 Clash Verge"
        else
            echo "FIX:请手动启动 $(proxy_name)(各客户端启动方式不同,不代为执行)"
        fi
    fi
    for i in $(seq 1 40); do
        proxy_alive && net_up && break
        sleep 1
    done
    if proxy_alive && net_up; then
        echo "FIX:代理内核+TUN ✓"
    else
        echo "FIX:代理内核/TUN ✗(请检查 $(proxy_name) 的 TUN 开关与节点)"
    fi

    # B. 时区/IPv6/DNS(sudo)
    adminCmds=""
    tz=$(timedatectl show -p Timezone --value 2>/dev/null)
    [ "$tz" != "$TZ_TARGET" ] && adminCmds="${adminCmds}timedatectl set-timezone $TZ_TARGET; "
    [ "$(sysctl -n net.ipv6.conf.all.disable_ipv6 2>/dev/null)" != "1" ] && adminCmds="${adminCmds}sysctl -w net.ipv6.conf.all.disable_ipv6=1; "
    if [ -n "$adminCmds" ]; then
        echo "FIX:修复时区/IPv6(需要 sudo 密码)..."
        sudo bash -c "$adminCmds" && echo "FIX:时区/IPv6 ✓" || echo "FIX:时区/IPv6 ✗"
    else
        echo "FIX:时区/IPv6 无需修复 ✓"
    fi
    command -v resolvectl >/dev/null 2>&1 && resolvectl flush-caches 2>/dev/null
    command -v systemd-resolve >/dev/null 2>&1 && systemd-resolve --flush-caches 2>/dev/null

    # C. 保镖(systemd user 服务,免 sudo)
    if ! systemctl --user is-active claude-bodyguard.service >/dev/null 2>&1; then
        echo "FIX:注册并启动保镖服务..."
        mkdir -p "$HOME/.config/systemd/user"
        cat > "$HOME/.config/systemd/user/claude-bodyguard.service" <<EOF
[Unit]
Description=Claude Bodyguard (kill claude CLI when proxy down)

[Service]
ExecStart=/bin/bash $BASE/claude-bodyguard.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now claude-bodyguard.service >/dev/null 2>&1 \
            && echo "FIX:保镖 ✓" || echo "FIX:保镖 ✗"
    fi

    # D. 置顶规则(仅 Clash Verge 智能合并写入;其他客户端行为检测不通过时提示手动)
    if [ "$PROXY_MODE" = "clash-verge" ]; then
        if ! merge_ok || ! stun_ok || ! route_ok; then
            echo "FIX:写入置顶规则(智能合并,不动你的已有配置)..."
            write_merge_rules && echo "FIX:置顶规则已写入(请重启 Clash Verge 生效)" || echo "FIX:置顶规则 ✗(写入失败)"
        fi
    else
        if ! route_ok; then
            echo "FIX:置顶规则 ✗(未命中:${ROUTE_MISSED# }) — 请在 $(proxy_name) 中自行添加 DOMAIN-SUFFIX 置顶规则"
        fi
    fi

    # E. 出口IP最终验证
    sleep 2
    read -r ip loc <<< "$(egress)"
    if [ "$loc" = "$COUNTRY_TARGET" ]; then
        echo "FIX:出口IP ✓ ($ip/$loc)"
    else
        echo "FIX:出口IP ✗ (${ip:-无}/${loc:-未知},期望$COUNTRY_TARGET) — 请换节点/线路"
    fi
    echo "FIX:修复流程结束"
}

status() {
    account_state_call status
}

quit_browser() {
    account_state_call quit-browser
}

valid_account() {
    case "${1:-}" in
        A|B|C|D) return 0 ;;
        *) echo "FAIL:账号必须是 A、B、C 或 D"; return 1 ;;
    esac
}

recover_accounts() {
    account_lock_reexec recover || return 1
    quit_browser || return 1
    account_state_call recover
}

switch_to() {
    local target="${1:-}" output
    valid_account "$target" || return 1
    account_lock_reexec switch "$target" || return 1
    # The inherited account lock covers audit, recovery, data commit and launch.
    require_launch_audit || return 1
    quit_browser || return 1
    output=$(account_state_call switch --account "$target") || { printf '%s\n' "$output"; return 1; }
    printf '%s\n' "$output"
    log "已切换到账号$target 并提交事务"
    echo "OK:已切换到账号$target（请重新打开对应浏览器配置）"
}

menu() {
    while true; do
        clear
        echo "=============================="
        echo "  Claude 账号切换器 (Linux)"
        echo "=============================="
        echo ""
        echo "  1) 安全检查 (checkdetail)"
        echo "  2) 一键修复 (fix)"
        echo "  3) 切换到账号 A"
        echo "  4) 切换到账号 B"
        echo "  5) 查看状态 (status)"
        echo "  6) 切换到账号 C"
        echo "  7) 切换到账号 D"
        echo "  0) 退出"
        echo ""
        read -r -p "选择: " c
        case "$c" in
            1) checkdetail; read -r -p "回车继续..." _ ;;
            2) (fix); read -r -p "回车继续..." _ ;;
            3) (switch_to A); read -r -p "回车继续..." _ ;;
            4) (switch_to B); read -r -p "回车继续..." _ ;;
            6) (switch_to C); read -r -p "回车继续..." _ ;;
            7) (switch_to D); read -r -p "回车继续..." _ ;;
            5) status; read -r -p "回车继续..." _ ;;
            0) exit 0 ;;
        esac
    done
}

case "${1:-menu}" in
    guard-check) proxy_alive && network_check health >/dev/null 2>&1 && route_ok ;;
    checkdetail)
        report=$(checkdetail 2>&1); report_rc=$?
        printf '%s\n' "$report"
        if check_tmp=$(mktemp "$BASE/.last-check.XXXXXX"); then
            printf '%s\n' "$report" > "$check_tmp" && mv -f "$check_tmp" "$BASE/last-check.txt"
            [ ! -f "$check_tmp" ] || rm -f "$check_tmp"
        fi
        [ "$report_rc" -eq 0 ] && printf '%s\n' "$report" | audit_validate >/dev/null ;;
    status) status ;;
    recover) recover_accounts ;;
    fix) fix ;;
    switch) switch_to "${2:-}" ;;
    write-merge-rules) write_merge_rules ;;  # 隐藏入口:供安装器调用,保证两处合并逻辑一致
    menu) menu ;;
    *) echo "用法: claude-switch.sh checkdetail|status|fix|recover|switch A|B|C|D|menu"; exit 2 ;;
esac
