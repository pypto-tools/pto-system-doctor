#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"; trap 'rm -rf -- "$TEST_ROOT"' EXIT
TOOLS_ROOT="$TEST_ROOT/tools"; BIN_DIR="$TEST_ROOT/bin"

"$REPO_DIR/install.sh" --tools-root "$TOOLS_ROOT" --bin-dir "$BIN_DIR" --init-config >/dev/null
TOOL_ROOT="$TOOLS_ROOT/system-doctor"; CONFIG="$TOOL_ROOT/config/system-doctor.conf"
for dir in app config state logs tmp; do [[ -d "$TOOL_ROOT/$dir" ]]; done
[[ "$(stat -c '%a' "$TOOL_ROOT/app")" == 755 ]]
[[ "$(stat -c '%a' "$CONFIG")" == 600 ]]
[[ "$(readlink "$BIN_DIR/pto-system-doctor")" == "$TOOL_ROOT/app/pto-system-doctor" ]]
[[ "$(find "$BIN_DIR" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1 ]]
"$BIN_DIR/pto-system-doctor" --help | grep -q 'network'
"$BIN_DIR/pto-system-doctor" network --help | grep -q '网络诊断'
"$BIN_DIR/pto-system-doctor" disk --help | grep -q 'disk'

printf 'FEISHU_WEBHOOK="preserved"\n' > "$CONFIG"
printf 'state\n' > "$TOOL_ROOT/state/last_alert"
printf 'stale\n' > "$TOOL_ROOT/app/stale"
"$REPO_DIR/install.sh" --tools-root "$TOOLS_ROOT" --bin-dir "$BIN_DIR" --init-config >/dev/null
grep -qx 'FEISHU_WEBHOOK="preserved"' "$CONFIG"
grep -qx state "$TOOL_ROOT/state/last_alert"
[[ ! -e "$TOOL_ROOT/app/stale" ]]

source_paths="$({ APP_DIR="$REPO_DIR"; source "$REPO_DIR/runtime_paths.sh"; printf '%s\n%s\n' "$CONFIG_DIR" "$STATE_DIR"; })"
[[ "$source_paths" == "$REPO_DIR/runtime/config
$REPO_DIR/runtime/state" ]]
grep -q 'send_feishu' "$REPO_DIR/disk.sh"
grep -q 'FEISHU_WEBHOOK' "$REPO_DIR/disk.sh"
echo 'install/layout tests passed'
