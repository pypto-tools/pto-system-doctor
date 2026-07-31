#!/usr/bin/env bash
#
# disk_monitor.sh - monitor a mount point, report top consumers, alert via Feishu.
#
# Usage:
#   disk_monitor.sh report   # always send a usage report to the group
#   disk_monitor.sh alert    # send only when any monitored mount is below its
#                            #   free threshold (MOUNT_POINT + EXTRA_ALERT_MOUNTS),
#                            #   with cooldown
#
# Every message embeds a full `df -h` overview of all real + tmpfs filesystems.
#
# Designed to run as root (needs to du into other users' 0700 home dirs).
# Triggered by systemd timers; it runs once and exits (not a daemon).

set -euo pipefail

MODE="${1:-report}"
case "$MODE" in
    report|alert) ;;
    -h|--help) echo "用法：pto-system-doctor disk {report|alert}"; exit 0 ;;
    *) echo "未知磁盘操作：$MODE" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${PTO_CONFIG_FILE:-${CONFIG_FILE:-$SCRIPT_DIR/runtime/config/system-doctor.conf}}"
STATE_DIR="${STATE_DIR:-$SCRIPT_DIR/runtime/state}"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/runtime/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/disk-monitor.log}"

# shellcheck source=/dev/null
[ -f "$CONF" ] || { echo "config not found: $CONF" >&2; exit 1; }
source "$CONF"

mkdir -p "$STATE_DIR" "$LOG_DIR"

log() {
    # Append a timestamped line to the log file and stderr.
    local line
    line="$(date '+%Y-%m-%d %H:%M:%S') [$MODE] $*"
    echo "$line" >>"$LOG_FILE" 2>/dev/null || true
    echo "$line" >&2
}

# --- 1. Read filesystem usage ------------------------------------------------
# df -P gives portable single-line output: Filesystem Size Used Avail Use% Mounted
read -r FS_SIZE FS_USED FS_AVAIL FS_USEPCT < <(
    df -P "$MOUNT_POINT" | awk 'NR==2 {gsub("%","",$5); print $2, $3, $4, $5}'
)
FREE_PCT=$(( 100 - FS_USEPCT ))

# Human-readable strings for the message.
read -r H_SIZE H_USED H_AVAIL < <(
    df -h "$MOUNT_POINT" | awk 'NR==2 {print $2, $3, $4}'
)

log "usage: used=${FS_USEPCT}% free=${FREE_PCT}% (avail ${H_AVAIL} of ${H_SIZE})"

# Full-system overview: every real (/dev/*) and tmpfs filesystem, as `df -h`
# prints it. Included verbatim in every message so readers see the whole box,
# not just the monitored mount.
DF_OVERVIEW="$(df -h | awk 'NR==1 || $1 ~ /^\/dev\// || $1 == "tmpfs"')"

# --- 2. Decide whether to send -----------------------------------------------
# Each entry below is checked for low free space; MOUNT_POINT plus every
# "mount:threshold" in EXTRA_ALERT_MOUNTS. Any mount under its threshold trips
# the alert. ALERT_DETAIL collects one line per tripped mount for the message.
IS_ALERT=0
ALERT_DETAIL=""

check_mount() {
    # $1 = mount point, $2 = free-percent threshold.
    local mp="$1" thr="$2"
    local usepct freepct ha
    usepct=$(df -P "$mp" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
    [ -n "$usepct" ] || { log "skip ${mp}: df returned nothing"; return; }
    freepct=$(( 100 - usepct ))
    ha=$(df -h "$mp" | awk 'NR==2 {print $4}')
    log "usage ${mp}: used=${usepct}% free=${freepct}% (avail ${ha}) thr=${thr}%"
    if [ "$freepct" -lt "$thr" ]; then
        IS_ALERT=1
        ALERT_DETAIL="${ALERT_DETAIL}  • ${mp}  剩余 ${ha} (${freepct}%) < ${thr}%
"
    fi
}

check_mount "$MOUNT_POINT" "$THRESHOLD_PCT"
for spec in ${EXTRA_ALERT_MOUNTS:-}; do
    check_mount "${spec%%:*}" "${spec##*:}"
done

if [ "$MODE" = "alert" ]; then
    if [ "$IS_ALERT" -eq 0 ]; then
        log "all monitored mounts above threshold, no alert."
        exit 0
    fi
    # Cooldown: skip if we alerted recently.
    STAMP="$STATE_DIR/last_alert"
    if [ "${ALERT_COOLDOWN_HOURS:-0}" -gt 0 ] && [ -f "$STAMP" ]; then
        last=$(cat "$STAMP" 2>/dev/null || echo 0)
        now=$(date +%s)
        if [ $(( now - last )) -lt $(( ALERT_COOLDOWN_HOURS * 3600 )) ]; then
            log "within cooldown (${ALERT_COOLDOWN_HOURS}h), skip alert."
            exit 0
        fi
    fi
fi

# --- 3. Top N consumers ------------------------------------------------------
log "scanning ${SCAN_DIR} for top ${TOP_N} consumers (timeout ${DU_TIMEOUT}s)..."
TOP_RAW="$(timeout "${DU_TIMEOUT}" du -sh "${SCAN_DIR}"/*/ 2>/dev/null | sort -rh | head -n "${TOP_N}" || true)"

if [ -z "$TOP_RAW" ]; then
    TOP_LIST="(scan returned nothing or timed out)"
else
    # Format: "  1. name   123G"
    TOP_LIST="$(echo "$TOP_RAW" | awk '{
        n=split($2, parts, "/"); name=parts[n-1];
        printf "  %2d. %-18s %s\n", NR, name, $1
    }')"
fi

# --- 4. Compose message ------------------------------------------------------
NOW="$(date '+%Y-%m-%d %H:%M')"
KW="${FEISHU_KEYWORD:+${FEISHU_KEYWORD} }"   # prefix keyword if configured

if [ "$IS_ALERT" -eq 1 ]; then
    HEADER="⚠️ ${KW}磁盘紧急告警"
    ALERT_BLOCK="触发告警的挂载点：
${ALERT_DETAIL}"
    ADVICE="🚨 上述挂载点空间不足，请相关同学立即清理 build/缓存/旧 checkpoint，或迁移数据到其他分区！"
else
    HEADER="📊 ${KW}磁盘周报  ${MOUNT_POINT}"
    ALERT_BLOCK=""
    ADVICE="👉 建议占用前列的同学清理 build_output/缓存/旧 checkpoint 等无用数据；剩余低于 ${THRESHOLD_PCT}% 将触发紧急告警。"
fi

MESSAGE="${HEADER}    ${NOW}
${ALERT_BLOCK}全盘概览（df -h）：
${DF_OVERVIEW}

${MOUNT_POINT} 容量 ${H_SIZE} / 已用 ${H_USED} (${FS_USEPCT}%) / 剩余 ${H_AVAIL} (${FREE_PCT}%)

${SCAN_DIR} 占用 Top ${TOP_N}：
${TOP_LIST}

${ADVICE}"

# --- 5. Send to Feishu -------------------------------------------------------
send_feishu() {
    local text="$1"
    # Build JSON safely with a here-doc + python for escaping.
    local payload
    payload=$(TEXT="$text" python3 -c '
import json, os
print(json.dumps({"msg_type": "text", "content": {"text": os.environ["TEXT"]}}))
')
    local resp
    resp=$(curl -sS -m 15 -X POST "$FEISHU_WEBHOOK" \
        -H 'Content-Type: application/json' \
        -d "$payload" 2>&1) || { log "curl failed: $resp"; return 1; }
    log "feishu response: $resp"
    # Feishu returns {"code":0,...} or {"StatusCode":0,...} on success.
    echo "$resp" | grep -Eq '"(code|StatusCode)" *: *0' || { log "feishu send NOT ok"; return 1; }
    return 0
}

log "sending ${MODE} message to Feishu..."
if send_feishu "$MESSAGE"; then
    log "sent ok."
    if [ "$IS_ALERT" -eq 1 ]; then
        date +%s >"$STATE_DIR/last_alert"
    fi
else
    log "send failed."
    exit 1
fi
