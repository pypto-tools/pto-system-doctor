#!/usr/bin/env bash
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_ROOT="/home/pypto-tools"; BIN_DIR="/usr/local/bin"
TOOL_NAME="system-doctor"; COMMAND_NAME="pto-system-doctor"; INIT_CONFIG=0
RUN_USER="${SUDO_USER:-$(id -un)}"; RUN_GROUP="$(id -gn "$RUN_USER")"
usage() { cat <<'EOF'
Usage: ./install.sh [--tools-root DIR] [--init-config] [--bin-dir DIR]
安装只更新 app/ 和公开命令，不运行诊断、不扫描磁盘、不发送飞书，
也不会自动安装或启用 systemd timer。
EOF
}
while [[ $# -gt 0 ]]; do case "$1" in
  --tools-root) TOOLS_ROOT="$2"; shift 2 ;; --bin-dir) BIN_DIR="$2"; shift 2 ;;
  --init-config) INIT_CONFIG=1; shift ;; -h|--help) usage; exit 0 ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac; done
TOOL_ROOT="$TOOLS_ROOT/$TOOL_NAME"
mkdir -p "$TOOL_ROOT/config" "$TOOL_ROOT/state" "$TOOL_ROOT/logs" "$TOOL_ROOT/tmp" "$BIN_DIR"
TOOLS_ROOT="$(cd "$TOOLS_ROOT" && pwd)"; TOOL_ROOT="$(cd "$TOOL_ROOT" && pwd)"
BIN_DIR="$(cd "$BIN_DIR" && pwd)"; APP_DIR="$TOOL_ROOT/app"
chown "$RUN_USER:$RUN_GROUP" "$TOOL_ROOT/config" "$TOOL_ROOT/state" "$TOOL_ROOT/logs" "$TOOL_ROOT/tmp"
chmod 0755 "$TOOLS_ROOT" "$TOOL_ROOT" "$TOOL_ROOT/config" "$TOOL_ROOT/state" "$TOOL_ROOT/logs" "$TOOL_ROOT/tmp"
STAGE_DIR="$(mktemp -d "$TOOL_ROOT/.app.install.XXXXXX")"
cleanup() { rm -rf -- "$STAGE_DIR"; }; trap cleanup EXIT
for file in pto-system-doctor network.sh disk.sh runtime_paths.sh systemd-manage.sh system-doctor.conf.example README.md; do
  install -m 0644 "$SOURCE_DIR/$file" "$STAGE_DIR/$file"
done
mkdir -p "$STAGE_DIR/systemd" "$STAGE_DIR/docs"
install -m 0644 "$SOURCE_DIR"/systemd/* "$STAGE_DIR/systemd/"
install -m 0644 "$SOURCE_DIR/docs/NETWORK_RUNBOOK.md" "$STAGE_DIR/docs/"
chmod 0755 "$STAGE_DIR" "$STAGE_DIR/pto-system-doctor" "$STAGE_DIR/network.sh" "$STAGE_DIR/disk.sh" "$STAGE_DIR/systemd-manage.sh"
OLD_APP=""; if [[ -e "$APP_DIR" ]]; then OLD_APP="$TOOL_ROOT/.app.previous.$$"; mv "$APP_DIR" "$OLD_APP"; fi
mv "$STAGE_DIR" "$APP_DIR"; trap - EXIT; [[ -z "$OLD_APP" ]] || rm -rf -- "$OLD_APP"
if [[ "$INIT_CONFIG" -eq 1 && ! -e "$TOOL_ROOT/config/system-doctor.conf" ]]; then
  install -o "$RUN_USER" -g "$RUN_GROUP" -m 0600 "$APP_DIR/system-doctor.conf.example" "$TOOL_ROOT/config/system-doctor.conf"
fi
ln -sfn "$APP_DIR/pto-system-doctor" "$BIN_DIR/$COMMAND_NAME"
echo "installed $COMMAND_NAME -> $APP_DIR/pto-system-doctor"
