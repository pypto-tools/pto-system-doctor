#!/usr/bin/env bash
set -euo pipefail
ACTION="${1:-status}"; UNIT_DIR="/etc/systemd/system"
UNITS=(pto-system-doctor-disk-report pto-system-doctor-disk-alert)
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
need_root() { [[ "$(id -u)" -eq 0 ]] || { echo "此操作需要 root，请使用 sudo。" >&2; exit 1; }; }
status() { for unit in "${UNITS[@]}"; do printf '%-43s enabled=%s active=%s\n' "$unit.timer" "$(systemctl is-enabled "$unit.timer" 2>/dev/null || echo no)" "$(systemctl is-active "$unit.timer" 2>/dev/null || echo no)"; done; }
case "$ACTION" in
  status) status ;;
  install) need_root; for unit in "${UNITS[@]}"; do install -m 0644 "$APP_DIR/systemd/$unit.service" "$UNIT_DIR/"; install -m 0644 "$APP_DIR/systemd/$unit.timer" "$UNIT_DIR/"; done; systemctl daemon-reload; echo "timer 已安装但未启用；确认配置后运行：sudo pto-system-doctor systemd enable" ;;
  enable) need_root; systemctl enable --now "${UNITS[@]/%/.timer}" ;;
  disable) need_root; systemctl disable --now "${UNITS[@]/%/.timer}" ;;
  uninstall) need_root; systemctl disable --now "${UNITS[@]/%/.timer}" 2>/dev/null || true; for unit in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$unit.service" "$UNIT_DIR/$unit.timer"; done; systemctl daemon-reload ;;
  *) echo "用法：pto-system-doctor systemd {status|install|enable|disable|uninstall}" >&2; exit 2 ;;
esac
