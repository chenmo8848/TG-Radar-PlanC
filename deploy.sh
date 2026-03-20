#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$APP_DIR/venv/bin/python3"
SERVICE_PREFIX=$(python3 -c 'import json, pathlib; p=pathlib.Path("'$APP_DIR'/config.json"); print((json.loads(p.read_text(encoding="utf-8")).get("service_name_prefix") if p.exists() else None) or "tg-radar")')
ADMIN_SVC="${SERVICE_PREFIX}-admin"
CORE_SVC="${SERVICE_PREFIX}-core"
ADMIN_SVC_FILE="/etc/systemd/system/${ADMIN_SVC}.service"
CORE_SVC_FILE="/etc/systemd/system/${CORE_SVC}.service"

create_services() {
  cat >"$ADMIN_SVC_FILE" <<EOS
[Unit]
Description=TG-Radar Plan C Admin Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$VENV_PY $APP_DIR/radar_admin.py
Restart=always
RestartSec=5
TimeoutStopSec=120
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOS

  cat >"$CORE_SVC_FILE" <<EOS
[Unit]
Description=TG-Radar Plan C Core Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$VENV_PY $APP_DIR/radar_core.py
Restart=always
RestartSec=5
TimeoutStopSec=120
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOS

  systemctl daemon-reload
  systemctl enable "$ADMIN_SVC" "$CORE_SVC"
}

menu() {
  clear
  echo
  echo "TG-Radar Plan C 管理终端"
  echo "----------------------------------------"
  echo "1) 写入/刷新 systemd 服务"
  echo "2) 启动服务"
  echo "3) 停止服务"
  echo "4) 重启服务"
  echo "5) 查看 admin 日志"
  echo "6) 查看 core 日志"
  echo "7) 执行一次同步"
  echo "8) 运行授权引导"
  echo "9) 卸载服务"
  echo "0) 退出"
  echo "----------------------------------------"
}

while true; do
  menu
  read -rp "请选择: " choice
  case "$choice" in
    1)
      create_services
      echo "systemd 服务已写入"
      ;;
    2)
      systemctl start "$ADMIN_SVC" "$CORE_SVC"
      systemctl status "$ADMIN_SVC" --no-pager -l || true
      systemctl status "$CORE_SVC" --no-pager -l || true
      ;;
    3)
      systemctl stop "$ADMIN_SVC" "$CORE_SVC" || true
      ;;
    4)
      systemctl restart "$ADMIN_SVC" "$CORE_SVC"
      ;;
    5)
      journalctl -u "$ADMIN_SVC" -n 50 --no-pager
      ;;
    6)
      journalctl -u "$CORE_SVC" -n 50 --no-pager
      ;;
    7)
      cd "$APP_DIR"
      "$VENV_PY" "$APP_DIR/sync_once.py"
      ;;
    8)
      cd "$APP_DIR"
      "$VENV_PY" "$APP_DIR/bootstrap_session.py"
      ;;
    9)
      systemctl disable --now "$ADMIN_SVC" "$CORE_SVC" 2>/dev/null || true
      rm -f "$ADMIN_SVC_FILE" "$CORE_SVC_FILE"
      systemctl daemon-reload
      echo "服务已卸载"
      ;;
    0)
      exit 0
      ;;
    *)
      echo "无效选项"
      ;;
  esac
  echo
  read -rp "按回车继续..." _dummy
 done
