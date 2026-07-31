#!/usr/bin/env bash
if [[ -n "${PTO_TOOL_ROOT:-}" ]]; then
  TOOL_ROOT="$PTO_TOOL_ROOT"
elif [[ "$(basename "$APP_DIR")" == "app" ]]; then
  TOOL_ROOT="$(cd "$APP_DIR/.." && pwd)"
else
  TOOL_ROOT="$APP_DIR/runtime"
fi
CONFIG_DIR="$TOOL_ROOT/config"; STATE_DIR="$TOOL_ROOT/state"
LOG_DIR="$TOOL_ROOT/logs"; TMP_DIR="$TOOL_ROOT/tmp"
CONFIG_FILE="${PTO_CONFIG_FILE:-$CONFIG_DIR/system-doctor.conf}"
mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR" "$TMP_DIR"
export TOOL_ROOT CONFIG_DIR STATE_DIR LOG_DIR TMP_DIR CONFIG_FILE
export TMPDIR="$TMP_DIR"
