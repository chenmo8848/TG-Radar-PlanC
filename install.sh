#!/usr/bin/env bash
set -euo pipefail

REPO_URL_DEFAULT="https://github.com/chenmo8848/TG-Radar-PlanC.git"
BRANCH_DEFAULT="main"
INSTALL_DIR_DEFAULT="/root/TG-Radar-PlanC"

REPO_URL="${TGRC_REPO_URL:-$REPO_URL_DEFAULT}"
BRANCH="${TGRC_BRANCH:-$BRANCH_DEFAULT}"
INSTALL_DIR="${TGRC_INSTALL_DIR:-$INSTALL_DIR_DEFAULT}"

C0='\033[0m'; B='\033[1m'; DIM='\033[2m'; CY='\033[36m'; GR='\033[32m'; YE='\033[33m'; RD='\033[31m'
step(){ printf "%b\n" "${CY}▶${C0} $*"; }
ok(){ printf "%b\n" "${GR}✔${C0} $*"; }
warn(){ printf "%b\n" "${YE}⚠${C0} $*"; }
err(){ printf "%b\n" "${RD}✖${C0} $*"; }
line(){ printf "%b\n" "${DIM}────────────────────────────────────────────────────────${C0}"; }

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    err "请使用 root 运行安装脚本。"
    exit 1
  fi
}

read_tty() {
  local __var="$1"; shift
  local __prompt="$1"; shift || true
  local __value=""
  if [ -r /dev/tty ]; then
    read -r -p "$__prompt" __value </dev/tty || true
  else
    read -r -p "$__prompt" __value || true
  fi
  printf -v "$__var" '%s' "$__value"
}

read_tty_silent() {
  local __var="$1"; shift
  local __prompt="$1"; shift || true
  local __value=""
  if [ -r /dev/tty ]; then
    read -r -s -p "$__prompt" __value </dev/tty || true
    printf "\n" >/dev/tty
  else
    read -r -s -p "$__prompt" __value || true
    printf "\n"
  fi
  printf -v "$__var" '%s' "$__value"
}

need_root
export DEBIAN_FRONTEND=noninteractive

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" 2>/dev/null && pwd -P || pwd -P)"

is_remote_bootstrap=0
case "$SCRIPT_PATH" in
  /dev/fd/*|/proc/self/fd/*|/tmp/*|stdin) is_remote_bootstrap=1 ;;
esac

if [ "$is_remote_bootstrap" -eq 1 ] || [ ! -f "$SCRIPT_DIR/requirements.txt" ] || [ ! -f "$SCRIPT_DIR/config.example.json" ] || [ ! -f "$SCRIPT_DIR/deploy.sh" ] || [ ! -d "$SCRIPT_DIR/src/tgr" ]; then
  line
  printf "%b\n" "${B}TGRC 一键部署向导${C0}"
  printf "%b\n" "${DIM}远程执行模式已检测到，先拉取仓库到 /root 后继续安装。${C0}"
  line

  step "安装基础依赖"
  apt-get update -y >/dev/null
  apt-get install -y git curl ca-certificates python3 python3-venv python3-pip systemd cron >/dev/null
  ok "基础依赖已就绪"

  step "拉取仓库到 $INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" fetch --depth=1 origin "$BRANCH" >/dev/null 2>&1 || true
    git -C "$INSTALL_DIR" checkout -f "$BRANCH" >/dev/null 2>&1 || true
    git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH" >/dev/null 2>&1 || true
  else
    rm -rf "$INSTALL_DIR"
    git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" >/dev/null
  fi
  ok "仓库已就绪：$INSTALL_DIR"

  exec bash "$INSTALL_DIR/install.sh"
fi

APP_DIR="$SCRIPT_DIR"
SRC_DIR="$APP_DIR/src"
VENV_DIR="$APP_DIR/venv"
PY="$VENV_DIR/bin/python3"
PIP="$VENV_DIR/bin/pip"
DEPLOY_SH="$APP_DIR/deploy.sh"
BOOTSTRAP_PY="$SRC_DIR/bootstrap_session.py"
SYNC_ONCE_PY="$SRC_DIR/sync_once.py"

line
printf "%b\n" "${B}TGRC 一键部署向导${C0}"
printf "%b\n" "${DIM}Plan C 架构 + 原始逻辑完整保留：自动同步、热更新、收藏夹交互、自动收纳、双服务。${C0}"
line

step "安装系统依赖"
apt-get update -y >/dev/null
apt-get install -y python3 python3-venv python3-pip git curl unzip ca-certificates systemd cron >/dev/null
ok "系统依赖已就绪"

step "初始化 Python 运行环境"
[ -d "$VENV_DIR" ] || python3 -m venv "$VENV_DIR"
"$PY" -m pip install --upgrade pip >/dev/null
"$PIP" install -r "$APP_DIR/requirements.txt" >/dev/null
mkdir -p "$APP_DIR/runtime/logs" "$APP_DIR/runtime/sessions" "$APP_DIR/runtime/backups"
ok "虚拟环境与依赖已完成"

step "准备默认配置模板"
[ -f "$APP_DIR/config.json" ] || cp "$APP_DIR/config.example.json" "$APP_DIR/config.json"
ok "配置模板已准备"

step "预清理旧版残留（服务 / 命令 / 进程）"
bash "$DEPLOY_SH" cleanup-legacy --keep-current >/dev/null || true
ok "旧版残留已清理"

step "写入 TGRC 全局命令"
rm -f /usr/local/bin/TGR /usr/bin/TGR || true
cat >/usr/local/bin/TGRC <<WRAP
#!/usr/bin/env bash
cd "$APP_DIR"
exec bash "$DEPLOY_SH" "\$@"
WRAP
chmod +x /usr/local/bin/TGRC
ok "全局命令已注册：TGRC"

step "收集并写入配置"
current_api_id="$("$PY" - <<PY
import json, pathlib
p=pathlib.Path(r"$APP_DIR")/"config.json"
try:
    d=json.loads(p.read_text(encoding="utf-8"))
    v=d.get("api_id")
    print("" if v in (None, 0, 1234567) else v)
except Exception:
    print("")
PY
)"
current_api_hash="$("$PY" - <<PY
import json, pathlib
p=pathlib.Path(r"$APP_DIR")/"config.json"
try:
    d=json.loads(p.read_text(encoding="utf-8"))
    v=d.get("api_hash") or ""
    print("" if v=="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" else v)
except Exception:
    print("")
PY
)"
current_alert="$("$PY" - <<PY
import json, pathlib
p=pathlib.Path(r"$APP_DIR")/"config.json"
try:
    d=json.loads(p.read_text(encoding="utf-8"))
    v=d.get("global_alert_channel_id")
    print("" if v in (None,"") else v)
except Exception:
    print("")
PY
)"
current_notify="$("$PY" - <<PY
import json, pathlib
p=pathlib.Path(r"$APP_DIR")/"config.json"
try:
    d=json.loads(p.read_text(encoding="utf-8"))
    v=d.get("notify_channel_id")
    print("" if v in (None,"") else v)
except Exception:
    print("")
PY
)"
current_prefix="$("$PY" - <<PY
import json, pathlib
p=pathlib.Path(r"$APP_DIR")/"config.json"
try:
    d=json.loads(p.read_text(encoding="utf-8"))
    print(d.get("cmd_prefix") or "-")
except Exception:
    print("-")
PY
)"
current_service_prefix="$("$PY" - <<PY
import json, pathlib
p=pathlib.Path(r"$APP_DIR")/"config.json"
try:
    d=json.loads(p.read_text(encoding="utf-8"))
    print(d.get("service_name_prefix") or "tgrc-radar")
except Exception:
    print("tgrc-radar")
PY
)"
current_sync_interval="$("$PY" - <<PY
import json, pathlib
p=pathlib.Path(r"$APP_DIR")/"config.json"
try:
    d=json.loads(p.read_text(encoding="utf-8"))
    print(d.get("sync_interval_seconds") or 60)
except Exception:
    print(60)
PY
)"
current_repo_url="$("$PY" - <<PY
import json, pathlib
p=pathlib.Path(r"$APP_DIR")/"config.json"
try:
    d=json.loads(p.read_text(encoding="utf-8"))
    print(d.get("repo_url") or r"$REPO_URL_DEFAULT")
except Exception:
    print(r"$REPO_URL_DEFAULT")
PY
)"

echo
echo "TGRC 配置向导"
line
echo "这一步会直接写入 config.json，不需要你手动打开文件编辑。"
echo "Telegram API 凭据来自 my.telegram.org。"
echo

while true; do
  read_tty api_id "Telegram API_ID [${current_api_id:-1234567}]: "
  api_id="${api_id:-${current_api_id:-1234567}}"
  [[ "$api_id" =~ ^[0-9]+$ ]] && [ "$api_id" != "1234567" ] && break
  warn "API_ID 无效，请重新输入。"
done

while true; do
  read_tty api_hash "Telegram API_HASH [已保存则直接回车复用]: "
  api_hash="${api_hash:-$current_api_hash}"
  [ -n "$api_hash" ] && [ "$api_hash" != "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" ] && break
  warn "API_HASH 不能为空。"
done

read_tty global_alert_channel_id "默认告警频道 ID（可留空 / off） [${current_alert:-空}]: "
global_alert_channel_id="${global_alert_channel_id:-$current_alert}"
case "${global_alert_channel_id,,}" in off|none|null) global_alert_channel_id="";; esac

read_tty notify_channel_id "系统通知频道 ID（留空则发到 Saved Messages） [${current_notify:-空}]: "
notify_channel_id="${notify_channel_id:-$current_notify}"
case "${notify_channel_id,,}" in off|none|null) notify_channel_id="";; esac

while true; do
  read_tty cmd_prefix "命令前缀 [${current_prefix:-"-"}]: "
  cmd_prefix="${cmd_prefix:-${current_prefix:-"-"}}"
  [ "${#cmd_prefix}" -ge 1 ] && [ "${#cmd_prefix}" -le 3 ] && [[ "$cmd_prefix" != *" "* ]] && break
  warn "命令前缀建议 1-3 个字符，且不能包含空格。"
done

read_tty service_name_prefix "systemd 服务名前缀 [${current_service_prefix:-tgrc-radar}]: "
service_name_prefix="${service_name_prefix:-${current_service_prefix:-tgrc-radar}}"

while true; do
  read_tty sync_interval_seconds "自动同步轮询秒数 [${current_sync_interval:-60}]: "
  sync_interval_seconds="${sync_interval_seconds:-${current_sync_interval:-60}}"
  [[ "$sync_interval_seconds" =~ ^[0-9]+$ ]] && [ "$sync_interval_seconds" -ge 10 ] && break
  warn "请输入不小于 10 的数字。"
done

read_tty repo_url "仓库地址 [${current_repo_url:-$REPO_URL_DEFAULT}]: "
repo_url="${repo_url:-${current_repo_url:-$REPO_URL_DEFAULT}}"

"$PY" - <<PY
from pathlib import Path
from tgr.config import read_config_data, save_config_data
work_dir = Path(r"$APP_DIR")
data = read_config_data(work_dir)
def cv(v):
    return None if v in ("", None) else int(v)
save_config_data(work_dir, {
    **data,
    "api_id": int(r"$api_id"),
    "api_hash": r"$api_hash",
    "global_alert_channel_id": cv(r"$global_alert_channel_id"),
    "notify_channel_id": cv(r"$notify_channel_id"),
    "cmd_prefix": r"$cmd_prefix",
    "service_name_prefix": r"$service_name_prefix",
    "sync_interval_seconds": int(r"$sync_interval_seconds"),
    "repo_url": r"$repo_url",
})
PY
ok "配置已写入：$APP_DIR/config.json"

step "执行 Telegram 首次授权"
if [ -r /dev/tty ]; then
  PYTHONPATH="$SRC_DIR" "$PY" "$BOOTSTRAP_PY" </dev/tty
else
  PYTHONPATH="$SRC_DIR" "$PY" "$BOOTSTRAP_PY"
fi
ok "Telegram 授权完成"

step "写入并启用 systemd 双服务"
bash "$DEPLOY_SH" install-services >/dev/null
ok "systemd 服务已注册"

step "执行首次同步"
PYTHONPATH="$SRC_DIR" "$PY" "$SYNC_ONCE_PY" || warn "首次同步未成功，可稍后执行 TGRC sync"

step "启动双服务"
bash "$DEPLOY_SH" start >/dev/null
ok "服务已启动"

CMD_HELP="$(PYTHONPATH="$SRC_DIR" "$PY" - <<PY
import json
from pathlib import Path
p = Path(r"$APP_DIR") / 'config.json'
try:
    print((json.loads(p.read_text(encoding='utf-8')).get('cmd_prefix') or '-').strip() + 'help')
except Exception:
    print('-help')
PY
)"

line
printf "%b\n" "${B}${GR}部署完成${C0}"
printf "%b\n" "- 安装目录：${B}$APP_DIR${C0}"
printf "%b\n" "- 管理命令：${B}TGRC${C0}"
printf "%b\n" "- 状态查看：${B}TGRC status${C0}"
printf "%b\n" "- 环境自检：${B}TGRC doctor${C0}"
printf "%b\n" "- Telegram 管理入口：收藏夹发送 ${B}${CMD_HELP}${C0}"
printf "%b\n" "- 彻底卸载：${B}TGRC uninstall${C0}"
line
