#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd -P)"
SRC_DIR="$APP_DIR/src"
VENV_PY="$APP_DIR/venv/bin/python3"
SYSTEM_PY="$(command -v python3)"
PY="$SYSTEM_PY"
[ -x "$VENV_PY" ] && PY="$VENV_PY"
export PYTHONPATH="$SRC_DIR"

SERVICE_PREFIX="$(PYTHONPATH="$SRC_DIR" "$PY" - <<PY
from pathlib import Path
try:
    from tgr.config import read_config_data
    print(read_config_data(Path(r"$APP_DIR")).get('service_name_prefix') or 'tgrc-radar')
except Exception:
    print('tgrc-radar')
PY
)"
ADMIN_SVC="${SERVICE_PREFIX}-admin"
CORE_SVC="${SERVICE_PREFIX}-core"
ADMIN_SVC_FILE="/etc/systemd/system/${ADMIN_SVC}.service"
CORE_SVC_FILE="/etc/systemd/system/${CORE_SVC}.service"

C0='\033[0m'; B='\033[1m'; DIM='\033[2m'; CY='\033[36m'; GR='\033[32m'; YE='\033[33m'; RD='\033[31m'
line(){ printf "%b\n" "${DIM}────────────────────────────────────────────────────────${C0}"; }
ok(){ printf "%b\n" "${GR}✔${C0} $*"; }
warn(){ printf "%b\n" "${YE}⚠${C0} $*"; }
err(){ printf "%b\n" "${RD}✖${C0} $*"; }

ensure_root() {
  if [ "$(id -u)" -ne 0 ]; then
    err "请使用 root 运行 TGRC。"
    exit 1
  fi
}

collect_known_services() {
  local -a found=()
  local f base
  for base in "$ADMIN_SVC" "$CORE_SVC" "tgrc-radar-admin" "tgrc-radar-core" "tg-radar-admin" "tg-radar-core" "tg_monitor"; do
    [[ " ${found[*]} " == *" ${base} "* ]] || found+=("$base")
  done
  for f in /etc/systemd/system/*.service /lib/systemd/system/*.service; do
    [ -e "$f" ] || continue
    base="$(basename "$f" .service)"
    case "$base" in
      tgrc-radar*|tg-radar*|tg_monitor) [[ " ${found[*]} " == *" ${base} "* ]] || found+=("$base") ;;
    esac
  done
  printf '%s\n' "${found[@]}"
}

stop_disable_service() {
  local svc="$1"
  systemctl disable --now "$svc" >/dev/null 2>&1 || true
  systemctl stop "$svc" >/dev/null 2>&1 || true
  systemctl reset-failed "$svc" >/dev/null 2>&1 || true
}

remove_service_file_if_exists() {
  local svc="$1"
  rm -f "/etc/systemd/system/${svc}.service" "/lib/systemd/system/${svc}.service" || true
}

kill_residual_processes() {
  local -a patterns=(
    "$APP_DIR/src/radar_admin.py"
    "$APP_DIR/src/radar_core.py"
    "$APP_DIR/src/bootstrap_session.py"
    "$APP_DIR/src/sync_once.py"
    "/root/TG-Radar/tg_monitor.py"
    "/root/TG-Radar/sync_engine.py"
    "/root/TG-Radar-PlanC/src/radar_admin.py"
    "/root/TG-Radar-PlanC/src/radar_core.py"
  )
  local p
  for p in "${patterns[@]}"; do
    pkill -9 -f "$p" >/dev/null 2>&1 || true
  done
}

cleanup_wrappers() {
  rm -f /usr/local/bin/TGRC /usr/bin/TGRC /usr/local/bin/TGR /usr/bin/TGR || true
}

cleanup_crontab() {
  local tmp
  tmp="$(mktemp)"
  crontab -l > "$tmp" 2>/dev/null || true
  grep -vE 'sync_engine\.py|journalctl --vacuum-time=1d|TG-Radar|TGRC|TGR' "$tmp" > "${tmp}.new" 2>/dev/null || true
  crontab "${tmp}.new" 2>/dev/null || crontab -r 2>/dev/null || true
  rm -f "$tmp" "${tmp}.new"
}

cleanup_legacy() {
  ensure_root
  local keep_current="${1:-0}"
  local purge_dirs="${2:-0}"
  local svc dir

  while IFS= read -r svc; do
    [ -n "$svc" ] || continue
    stop_disable_service "$svc"
    remove_service_file_if_exists "$svc"
  done < <(collect_known_services)

  cleanup_wrappers
  cleanup_crontab
  kill_residual_processes

  if [ "$purge_dirs" = "1" ]; then
    for dir in /root/TG-Radar /root/TG-Radar-PlanC /opt/TG-Radar-PlanC /opt/TGRC; do
      [ -d "$dir" ] || continue
      if [ "$keep_current" = "1" ] && [ "$dir" = "$APP_DIR" ]; then
        continue
      fi
      rm -rf "$dir" || true
    done
  fi

  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl reset-failed >/dev/null 2>&1 || true
  ok "旧版服务、命令和残留进程已清理。"
}

create_services() {
  ensure_root
  [ -x "$VENV_PY" ] || { err "缺少虚拟环境 Python：$VENV_PY"; exit 1; }

  cat >"$ADMIN_SVC_FILE" <<SERVICE
[Unit]
Description=TGRC Radar Admin Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$VENV_PY $APP_DIR/src/radar_admin.py
Restart=always
RestartSec=5
TimeoutStopSec=120
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$SRC_DIR

[Install]
WantedBy=multi-user.target
SERVICE

  cat >"$CORE_SVC_FILE" <<SERVICE
[Unit]
Description=TGRC Radar Core Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$VENV_PY $APP_DIR/src/radar_core.py
Restart=always
RestartSec=5
TimeoutStopSec=120
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$SRC_DIR

[Install]
WantedBy=multi-user.target
SERVICE

  systemctl daemon-reload
  systemctl enable "$ADMIN_SVC" "$CORE_SVC" >/dev/null 2>&1
  ok "systemd 服务已写入并启用：$ADMIN_SVC / $CORE_SVC"
}

status_view() {
  line
  printf "%b\n" "${B}TGRC 控制台${C0}"
  line
  printf "工作目录: %s\n" "$APP_DIR"
  printf "源码目录: %s\n" "$SRC_DIR"
  printf "命令入口: %s\n" "/usr/local/bin/TGRC"
  printf "Admin 服务: %s\n" "$ADMIN_SVC"
  printf "Core 服务 : %s\n\n" "$CORE_SVC"
  systemctl status "$ADMIN_SVC" --no-pager -l || true
  printf "\n"
  systemctl status "$CORE_SVC" --no-pager -l || true
}

start_services(){ ensure_root; systemctl start "$ADMIN_SVC" "$CORE_SVC"; ok "服务已启动。"; }
stop_services(){ ensure_root; systemctl stop "$ADMIN_SVC" "$CORE_SVC" || true; ok "服务已停止。"; }
restart_services(){ ensure_root; systemctl restart "$ADMIN_SVC" "$CORE_SVC"; ok "服务已重启。"; }
sync_once(){ cd "$APP_DIR"; PYTHONPATH="$SRC_DIR" "$PY" "$SRC_DIR/sync_once.py"; }
reauth(){ cd "$APP_DIR"; PYTHONPATH="$SRC_DIR" "$PY" "$SRC_DIR/bootstrap_session.py" </dev/tty; }
update_repo(){
  ensure_root
  if [ ! -d "$APP_DIR/.git" ]; then
    err "当前目录不是 git 仓库，无法执行 TGRC update。"
    exit 1
  fi
  git -C "$APP_DIR" pull --ff-only
  ok "代码已更新。"
  restart_services
}

runtime_db="$APP_DIR/runtime/radar.db"
admin_session_file="$APP_DIR/runtime/sessions/tg_radar_admin.session"
core_session_file="$APP_DIR/runtime/sessions/tg_radar_core.session"

doctor() {
  line
  printf "%b\n" "${B}TGRC Doctor${C0}"
  line
  [ -x "$VENV_PY" ] && ok "Python venv: $VENV_PY" || err "缺少 venv Python"
  [ -d "$SRC_DIR/tgr" ] && ok "源码目录: $SRC_DIR" || err "缺少 src/tgr"
  [ -f "$APP_DIR/config.json" ] && ok "配置文件: $APP_DIR/config.json" || err "缺少 config.json"
  [ -f "$runtime_db" ] && ok "运行数据库: $runtime_db" || warn "数据库尚未生成"
  [ -f "$admin_session_file" ] && ok "Admin Session 已存在" || warn "缺少 admin session"
  [ -f "$core_session_file" ] && ok "Core Session 已存在" || warn "缺少 core session"
  systemctl is-enabled "$ADMIN_SVC" >/dev/null 2>&1 && ok "$ADMIN_SVC 已启用" || warn "$ADMIN_SVC 未启用"
  systemctl is-enabled "$CORE_SVC" >/dev/null 2>&1 && ok "$CORE_SVC 已启用" || warn "$CORE_SVC 未启用"
  systemctl is-active "$ADMIN_SVC" >/dev/null 2>&1 && ok "$ADMIN_SVC 运行中" || warn "$ADMIN_SVC 未运行"
  systemctl is-active "$CORE_SVC" >/dev/null 2>&1 && ok "$CORE_SVC 运行中" || warn "$CORE_SVC 未运行"
  [ -x /usr/local/bin/TGRC ] && ok "TGRC 命令已存在" || warn "TGRC 命令不存在"
  [ -x /usr/local/bin/TGR ] && warn "检测到旧版 TGR 命令残留" || true
}

uninstall_all() {
  ensure_root
  local mode="${1:-ask}"
  local answer=""
  if [ "$mode" = "ask" ]; then
    printf "这将停止服务、删除 TGRC/TGR 命令，并彻底清除项目目录。确认继续？[y/N]: "
    if [ -r /dev/tty ]; then
      read -r answer </dev/tty || true
    else
      read -r answer || true
    fi
    case "${answer:-N}" in
      y|Y|yes|YES) mode="purge" ;;
      *) warn "已取消卸载。"; return 0 ;;
    esac
  fi

  cleanup_legacy 1 0

  if [ "$mode" != "keep-data" ]; then
    cd /
    rm -rf "$APP_DIR" || true
    ok "项目目录已删除：$APP_DIR"
  else
    ok "项目目录已保留：$APP_DIR"
  fi

  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl reset-failed >/dev/null 2>&1 || true
  ok "卸载完成。"
}

show_logs() {
  local target="${1:-all}"
  case "$target" in
    admin) journalctl -u "$ADMIN_SVC" -n 80 --no-pager ;;
    core) journalctl -u "$CORE_SVC" -n 80 --no-pager ;;
    all)
      journalctl -u "$ADMIN_SVC" -n 40 --no-pager
      printf "\n"
      journalctl -u "$CORE_SVC" -n 40 --no-pager ;;
    *) err "未知日志目标：$target"; exit 1 ;;
  esac
}

menu() {
  clear
  line
  printf "%b\n" "${B}TGRC · Telegram Radar Command Center${C0}"
  printf "%b\n" "${DIM}Plan C 架构 · 保留原始监控逻辑 · /root 一键部署${C0}"
  line
  cat <<'MENU'
1) 写入 / 刷新 systemd 服务
2) 启动服务
3) 停止服务
4) 重启服务
5) 查看服务状态
6) 查看日志（admin）
7) 查看日志（core）
8) 执行一次同步
9) 重新授权 Telegram
10) 运行 Doctor 自检
11) 清理旧版残留（服务 / 命令 / 进程）
12) git pull 更新并重启
13) 彻底卸载（删除服务 / 命令 / 项目目录）
0) 退出
MENU
  line
}

case "${1:-menu}" in
  install-services) create_services ;;
  start) start_services ;;
  stop) stop_services ;;
  restart) restart_services ;;
  status) status_view ;;
  sync) sync_once ;;
  reauth) reauth ;;
  doctor) doctor ;;
  update) update_repo ;;
  cleanup-legacy)
    keep_current=0
    [ "${2:-}" = "--keep-current" ] && keep_current=1
    cleanup_legacy "$keep_current" 1 ;;
  uninstall)
    mode="ask"
    case "${2:-}" in
      --purge|purge) mode="purge" ;;
      --keep-data|keep-data) mode="keep-data" ;;
    esac
    uninstall_all "$mode" ;;
  logs) show_logs "${2:-all}" ;;
  menu)
    while true; do
      menu
      if [ -r /dev/tty ]; then
        read -rp "请选择: " choice </dev/tty
      else
        read -rp "请选择: " choice
      fi
      case "$choice" in
        1) create_services ;;
        2) start_services ;;
        3) stop_services ;;
        4) restart_services ;;
        5) status_view ;;
        6) show_logs admin ;;
        7) show_logs core ;;
        8) sync_once ;;
        9) reauth ;;
        10) doctor ;;
        11) cleanup_legacy 1 1 ;;
        12) update_repo ;;
        13) uninstall_all ask ;;
        0) exit 0 ;;
        *) warn "无效选项" ;;
      esac
      echo
      if [ -r /dev/tty ]; then
        read -rp "按回车继续..." _dummy </dev/tty
      else
        read -rp "按回车继续..." _dummy
      fi
    done ;;
  *)
    cat <<USAGE
用法:
  TGRC                     打开交互菜单
  TGRC status              查看服务状态
  TGRC start|stop|restart  管理双服务
  TGRC sync                执行一次同步
  TGRC reauth              重新授权 Telegram
  TGRC logs [admin|core]   查看日志
  TGRC doctor              运行环境自检
  TGRC update              git pull 更新并重启
  TGRC cleanup-legacy      清理旧版服务 / 命令 / 后台进程
  TGRC uninstall           彻底卸载（默认删除项目目录）
  TGRC uninstall keep-data 卸载服务与命令，但保留项目目录
USAGE
    exit 1 ;;
esac
