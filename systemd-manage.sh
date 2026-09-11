#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-status}"
SCOPE="${2:-}"
UNIT_DIR="/etc/systemd/system"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DISK_UNITS=(
  pto-system-doctor-disk-report.service
  pto-system-doctor-disk-report.timer
  pto-system-doctor-disk-alert.service
  pto-system-doctor-disk-alert.timer
)
RESOURCE_UNITS=(pto-system-doctor-resource.service)
ALERT_UNITS=(pto-system-doctor-alert.service)

usage() {
  cat >&2 <<'EOF'
用法：pto-system-doctor systemd {status|install|enable|disable|uninstall} [disk|resource|alert|all]

省略范围时：status/install/uninstall 查看或处理 all；enable/disable 仅处理 disk，
避免升级后意外启动资源守护或飞书 worker。
EOF
}

need_root() {
  [[ "$(id -u)" -eq 0 ]] || { echo "此操作需要 root，请使用 sudo。" >&2; exit 1; }
}

select_units() {
  local scope="$1"
  SELECTED=()
  case "$scope" in
    disk) SELECTED=("${DISK_UNITS[@]}") ;;
    resource) SELECTED=("${RESOURCE_UNITS[@]}") ;;
    alert) SELECTED=("${ALERT_UNITS[@]}") ;;
    all) SELECTED=("${DISK_UNITS[@]}" "${RESOURCE_UNITS[@]}" "${ALERT_UNITS[@]}") ;;
    *) usage; exit 2 ;;
  esac
}

default_scope() {
  if [[ -n "$SCOPE" ]]; then
    printf '%s\n' "$SCOPE"
  elif [[ "$ACTION" == "enable" || "$ACTION" == "disable" ]]; then
    printf '%s\n' disk
  else
    printf '%s\n' all
  fi
}

SCOPE="$(default_scope)"
select_units "$SCOPE"

case "$ACTION" in
  status)
    for unit in "${SELECTED[@]}"; do
      printf '%-48s enabled=%s active=%s\n' "$unit" \
        "$(systemctl is-enabled "$unit" 2>/dev/null || echo no)" \
        "$(systemctl is-active "$unit" 2>/dev/null || echo no)"
    done
    ;;
  install)
    need_root
    for unit in "${SELECTED[@]}"; do
      install -m 0644 "$APP_DIR/systemd/$unit" "$UNIT_DIR/$unit"
    done
    systemctl daemon-reload
    echo "unit 已安装但未启用；检查配置后显式指定 enable 范围。"
    ;;
  enable)
    need_root
    ENABLE_UNITS=()
    for unit in "${SELECTED[@]}"; do
      [[ "$unit" == *.timer || "$unit" == pto-system-doctor-resource.service || "$unit" == pto-system-doctor-alert.service ]] \
        && ENABLE_UNITS+=("$unit")
    done
    systemctl enable --now "${ENABLE_UNITS[@]}"
    ;;
  disable)
    need_root
    systemctl disable --now "${SELECTED[@]}" 2>/dev/null || true
    ;;
  uninstall)
    need_root
    systemctl disable --now "${SELECTED[@]}" 2>/dev/null || true
    for unit in "${SELECTED[@]}"; do
      rm -f "$UNIT_DIR/$unit"
    done
    systemctl daemon-reload
    ;;
  *) usage; exit 2 ;;
esac
