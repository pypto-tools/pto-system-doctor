#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"; trap 'rm -rf -- "$TEST_ROOT"' EXIT
TOOLS_ROOT="$TEST_ROOT/tools"; BIN_DIR="$TEST_ROOT/bin"

"$REPO_DIR/install.sh" --tools-root "$TOOLS_ROOT" --bin-dir "$BIN_DIR" --init-config >/dev/null
TOOL_ROOT="$TOOLS_ROOT/pto-system-doctor"; CONFIG="$TOOL_ROOT/config/system-doctor.conf"
RESOURCE_CONFIG="$TOOL_ROOT/config/resource-guard.conf"
for dir in app config state logs tmp; do [[ -d "$TOOL_ROOT/$dir" ]]; done
[[ "$(stat -c '%a' "$TOOL_ROOT/app")" == 755 ]]
[[ "$(stat -c '%a' "$CONFIG")" == 600 ]]
[[ "$(stat -c '%a' "$RESOURCE_CONFIG")" == 600 ]]
[[ "$(readlink "$BIN_DIR/pto-system-doctor")" == "$TOOL_ROOT/app/pto-system-doctor" ]]
[[ "$(find "$BIN_DIR" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1 ]]
[[ -f "$TOOL_ROOT/app/skills/pto-system-doctor/SKILL.md" ]]
[[ -f "$TOOL_ROOT/app/modules/resource/cli.py" ]]
[[ -f "$TOOL_ROOT/app/modules/alert/worker.py" ]]
[[ -f "$TOOL_ROOT/app/systemd/pto-system-doctor-resource.service" ]]
[[ -f "$TOOL_ROOT/app/systemd/pto-system-doctor-alert.service" ]]
grep -q '^TOOLS_ROOT="/home/pypto-tools"' "$REPO_DIR/install.sh"
grep -q '/home/pypto-tools/pto-system-doctor/state' \
  "$TOOL_ROOT/app/systemd/pto-system-doctor-resource.service"
grep -q '/home/pypto-tools/pto-system-doctor/state' \
  "$TOOL_ROOT/app/systemd/pto-system-doctor-alert.service"
"$BIN_DIR/pto-system-doctor" --help | grep -q 'network'
"$BIN_DIR/pto-system-doctor" --help | grep -q 'resource'
"$BIN_DIR/pto-system-doctor" network --help | grep -q '网络诊断'
"$BIN_DIR/pto-system-doctor" disk --help | grep -q 'disk'
"$BIN_DIR/pto-system-doctor" resource config-validate | grep -q 'configuration valid'
"$BIN_DIR/pto-system-doctor" resource status | grep -q '^mode:'
"$BIN_DIR/pto-system-doctor" alert status | grep -q '"pending"'

printf 'FEISHU_WEBHOOK="preserved"\n' > "$CONFIG"
printf 'ACTION_MODE="observe"\nWARN_AVAILABLE=600G\nKILL_AVAILABLE=300G\nEMERGENCY_AVAILABLE=150G\nRECOVERY_AVAILABLE=600G\n' > "$RESOURCE_CONFIG"
printf 'state\n' > "$TOOL_ROOT/state/last_alert"
printf 'stale\n' > "$TOOL_ROOT/app/stale"
"$REPO_DIR/install.sh" --tools-root "$TOOLS_ROOT" --bin-dir "$BIN_DIR" --init-config >/dev/null
grep -qx 'FEISHU_WEBHOOK="preserved"' "$CONFIG"
grep -qx 'ACTION_MODE="observe"' "$RESOURCE_CONFIG"
grep -qx state "$TOOL_ROOT/state/last_alert"
[[ ! -e "$TOOL_ROOT/app/stale" ]]

source_paths="$({ APP_DIR="$REPO_DIR"; source "$REPO_DIR/runtime_paths.sh"; printf '%s\n%s\n' "$CONFIG_DIR" "$STATE_DIR"; })"
[[ "$source_paths" == "$REPO_DIR/runtime/config
$REPO_DIR/runtime/state" ]]
grep -q 'send_feishu' "$REPO_DIR/disk.sh"
grep -q 'FEISHU_WEBHOOK' "$REPO_DIR/disk.sh"
grep -q '^FEISHU_MAX_ATTEMPTS=4$' "$TOOL_ROOT/app/system-doctor.conf.example"
grep -q '^FEISHU_RETRY_DELAYS="5 30 120"$' "$TOOL_ROOT/app/system-doctor.conf.example"
grep -q '^FEISHU_MIN_SEVERITY="critical"$' "$TOOL_ROOT/app/system-doctor.conf.example"
grep -q '^FEISHU_LOG_UPLOAD=true$' "$TOOL_ROOT/app/system-doctor.conf.example"
grep -q '^ALERT_DELIVERY="queue"$' "$TOOL_ROOT/app/system-doctor.conf.example"

# Queue delivery writes a local event and never invokes curl.
QUEUE_CONFIG="$TEST_ROOT/queue.conf"
QUEUE_ROOT="$TEST_ROOT/queue-tool"
QUEUE_SCAN="$TEST_ROOT/queue-scan"
mkdir -p "$QUEUE_ROOT/config" "$QUEUE_ROOT/state" "$QUEUE_ROOT/logs" "$QUEUE_ROOT/tmp" "$QUEUE_SCAN"
printf '%s\n' \
  'MOUNT_POINT="/"' \
  "SCAN_DIR=\"$QUEUE_SCAN\"" \
  'THRESHOLD_PCT=1' \
  'EXTRA_ALERT_MOUNTS=""' \
  'TOP_N=1' \
  'FEISHU_WEBHOOK="https://example.invalid/webhook"' \
  'FEISHU_KEYWORD=""' \
  'ALERT_DELIVERY="queue"' \
  'DU_TIMEOUT=1' \
  'ALERT_COOLDOWN_HOURS=0' >"$QUEUE_CONFIG"
curl() { echo 'curl must not run in queue mode' >&2; return 99; }
export -f curl
PTO_TOOL_ROOT="$QUEUE_ROOT" PTO_CONFIG_FILE="$QUEUE_CONFIG" \
  "$REPO_DIR/disk.sh" report >/dev/null 2>&1
unset -f curl
[[ "$(find "$QUEUE_ROOT/state/alerts/pending" -type f -name '*.json' | wc -l)" -eq 1 ]]

# A failed Feishu request retries only the send step and eventually succeeds.
RETRY_CONFIG="$TEST_ROOT/retry.conf"
RETRY_STATE="$TEST_ROOT/retry-state"
RETRY_LOGS="$TEST_ROOT/retry-logs"
RETRY_SCAN="$TEST_ROOT/retry-scan"
mkdir -p "$RETRY_STATE" "$RETRY_LOGS" "$RETRY_SCAN"
printf '%s\n' \
  'MOUNT_POINT="/"' \
  "SCAN_DIR=\"$RETRY_SCAN\"" \
  'THRESHOLD_PCT=1' \
  'EXTRA_ALERT_MOUNTS=""' \
  'TOP_N=1' \
  'FEISHU_WEBHOOK="https://example.invalid/webhook"' \
  'FEISHU_KEYWORD=""' \
  'FEISHU_MAX_ATTEMPTS=4' \
  'FEISHU_RETRY_DELAYS="0 0 0"' \
  'DU_TIMEOUT=1' \
  'ALERT_COOLDOWN_HOURS=0' >"$RETRY_CONFIG"
FAKE_CURL_STATE="$TEST_ROOT/fake-curl-attempts"
curl() {
  local attempts=0
  [[ ! -f "$FAKE_CURL_STATE" ]] || read -r attempts <"$FAKE_CURL_STATE"
  attempts=$((attempts + 1))
  printf '%s\n' "$attempts" >"$FAKE_CURL_STATE"
  if [[ "$attempts" -lt 4 ]]; then
    echo 'simulated curl failure' >&2
    return 6
  fi
  echo '{"code":0,"msg":"success"}'
}
export -f curl
PTO_CONFIG_FILE="$RETRY_CONFIG" STATE_DIR="$RETRY_STATE" LOG_DIR="$RETRY_LOGS" \
  FAKE_CURL_STATE="$FAKE_CURL_STATE" "$REPO_DIR/disk.sh" report >/dev/null 2>&1
unset -f curl
[[ "$(cat "$FAKE_CURL_STATE")" == 4 ]]
grep -q 'feishu send attempt 4/4' "$RETRY_LOGS/disk-monitor.log"

# Each directory inside the shared pyptouser home appears as its own consumer.
USER_SCAN_CONFIG="$TEST_ROOT/user-scan.conf"
USER_SCAN_STATE="$TEST_ROOT/user-scan-state"
USER_SCAN_LOGS="$TEST_ROOT/user-scan-logs"
USER_SCAN_ROOT="$TEST_ROOT/user-scan"
USER_SCAN_PAYLOAD="$TEST_ROOT/user-scan-payload"
mkdir -p "$USER_SCAN_STATE" "$USER_SCAN_LOGS" \
  "$USER_SCAN_ROOT/pyptouser/alice" "$USER_SCAN_ROOT/pyptouser/bob" \
  "$USER_SCAN_ROOT/charlie"
dd if=/dev/zero of="$USER_SCAN_ROOT/pyptouser/alice/data" bs=4096 count=1 status=none
dd if=/dev/zero of="$USER_SCAN_ROOT/pyptouser/bob/data" bs=4096 count=1 status=none
dd if=/dev/zero of="$USER_SCAN_ROOT/charlie/data" bs=4096 count=1 status=none
printf '%s\n' \
  'MOUNT_POINT="/"' \
  "SCAN_DIR=\"$USER_SCAN_ROOT\"" \
  'THRESHOLD_PCT=1' \
  'EXTRA_ALERT_MOUNTS=""' \
  'TOP_N=10' \
  'FEISHU_WEBHOOK="https://example.invalid/webhook"' \
  'FEISHU_KEYWORD=""' \
  'FEISHU_MAX_ATTEMPTS=1' \
  'FEISHU_RETRY_DELAYS=""' \
  'DU_TIMEOUT=10' \
  'ALERT_COOLDOWN_HOURS=0' >"$USER_SCAN_CONFIG"
curl() {
  local payload=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "-d" ]]; then
      payload="$2"
      shift 2
    else
      shift
    fi
  done
  printf '%s\n' "$payload" >"$USER_SCAN_PAYLOAD"
  echo '{"code":0,"msg":"success"}'
}
export -f curl
PTO_CONFIG_FILE="$USER_SCAN_CONFIG" STATE_DIR="$USER_SCAN_STATE" \
  LOG_DIR="$USER_SCAN_LOGS" USER_SCAN_PAYLOAD="$USER_SCAN_PAYLOAD" \
  "$REPO_DIR/disk.sh" report >/dev/null 2>&1
unset -f curl
grep -q 'alice' "$USER_SCAN_PAYLOAD"
grep -q 'bob' "$USER_SCAN_PAYLOAD"
grep -q 'charlie' "$USER_SCAN_PAYLOAD"
! grep -q 'pyptouser' "$USER_SCAN_PAYLOAD"

MIGRATION_ROOT="$TEST_ROOT/migration-tools"
mkdir -p "$MIGRATION_ROOT/system-doctor/config" "$MIGRATION_ROOT/system-doctor/state"
printf 'legacy-config\n' > "$MIGRATION_ROOT/system-doctor/config/sentinel"
printf 'legacy-state\n' > "$MIGRATION_ROOT/system-doctor/state/sentinel"
"$REPO_DIR/install.sh" --tools-root "$MIGRATION_ROOT" \
  --bin-dir "$TEST_ROOT/migration-bin" >/dev/null
[[ ! -e "$MIGRATION_ROOT/system-doctor" ]]
grep -qx legacy-config "$MIGRATION_ROOT/pto-system-doctor/config/sentinel"
grep -qx legacy-state "$MIGRATION_ROOT/pto-system-doctor/state/sentinel"
echo 'install/layout tests passed'
