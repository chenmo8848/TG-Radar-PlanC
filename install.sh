#!/usr/bin/env bash
set -euo pipefail

REPO_URL_DEFAULT="https://github.com/chenmo8848/TG-Radar-PlanC.git"
BRANCH_DEFAULT="main"
INSTALL_DIR_DEFAULT="/opt/TG-Radar-PlanC"

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
  printf "%b\n" "${DIM}远程执行模式已检测到，先拉取仓库到本地目录后继续安装。${C0}"
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

  exec bash "$INSTALL_DIR/install.sh" "$@"
fi

APP_DIR="$SCRIPT_DIR"
SRC_DIR="$APP_DIR/src"
VENV_DIR="$APP_DIR/venv"
PY="$VENV_DIR/bin/python3"
PIP="$VENV_DIR/bin/pip"
DEPLOY_SH="$APP_DIR/deploy.sh"
BOOTSTRAP_PY="$SRC_DIR/bootstrap_session.py"
SYNC_ONCE_PY="$SRC_DIR/sync_once.py"
TTY_DEV=""
[ -r /dev/tty ] && TTY_DEV="/dev/tty"

line
printf "%b\n" "${B}TGRC 一键部署向导${C0}"
printf "%b\n" "${DIM}一键完成依赖安装、配置写入、首次授权、服务注册与启动。${C0}"
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

step "运行配置向导，生成 config.json"
WIZARD_FILE="$(mktemp)"
trap 'rm -f "$WIZARD_FILE"' EXIT
cat >"$WIZARD_FILE" <<'PYCONF'
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from tgr.config import read_config_data, save_config_data

app_dir = Path(os.environ["TGRC_APP_DIR"])
config = read_config_data(app_dir)

def ask_text(prompt: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default
        if not required:
            return ""
        print("该项不能为空。")

def ask_int(prompt: str, default: int | None = None, allow_empty: bool = True) -> int | None:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw:
            if allow_empty:
                return default
            if default is not None:
                return default
        if raw.lower() in {"off", "none", "null", "skip"}:
            return None
        try:
            return int(raw)
        except ValueError:
            print("请输入有效数字，或输入 off 关闭。")

def ask_prefix(default: str) -> str:
    while True:
        value = input(f"命令前缀 [{default}]: ").strip() or default
        if 1 <= len(value) <= 3 and " " not in value:
            return value
        print("命令前缀建议 1-3 个字符，且不能包含空格。")

print("\nTGRC 配置向导\n" + "-" * 56)
print("这一步会直接写入 config.json，不需要你手动打开文件编辑。")
print("Telegram API 凭据来自 my.telegram.org。\n")

def_api_id = config.get("api_id") if config.get("api_id") not in (None, 0, 1234567) else None
api_id = ask_int("Telegram API_ID", def_api_id, allow_empty=False)
api_hash = ask_text("Telegram API_HASH", config.get("api_hash") if config.get("api_hash") not in (None, "", "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx") else "", required=True)
global_alert_channel_id = ask_int("默认告警频道 ID（可留空）", config.get("global_alert_channel_id"), allow_empty=True)
notify_channel_id = ask_int("系统通知频道 ID（留空则发到 Saved Messages）", config.get("notify_channel_id"), allow_empty=True)
cmd_prefix = ask_prefix(config.get("cmd_prefix") or "-")
service_name_prefix = ask_text("systemd 服务名前缀", config.get("service_name_prefix") or "tgrc-radar", required=True)
repo_url = ask_text("仓库地址（可留空）", config.get("repo_url") or "https://github.com/chenmo8848/TG-Radar-PlanC.git")

if not api_id or api_id == 1234567 or not api_hash or api_hash == "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx":
    raise SystemExit("未写入有效 Telegram API 凭据，安装已中止。")

path = save_config_data(app_dir, {
    **config,
    "api_id": int(api_id),
    "api_hash": api_hash,
    "global_alert_channel_id": global_alert_channel_id,
    "notify_channel_id": notify_channel_id,
    "cmd_prefix": cmd_prefix,
    "service_name_prefix": service_name_prefix,
    "repo_url": repo_url,
})
print(f"\n配置已写入：{path}\n")
PYCONF

if [ -n "$TTY_DEV" ]; then
  TGRC_APP_DIR="$APP_DIR" PYTHONPATH="$SRC_DIR" "$PY" "$WIZARD_FILE" <"$TTY_DEV"
else
  TGRC_APP_DIR="$APP_DIR" PYTHONPATH="$SRC_DIR" "$PY" "$WIZARD_FILE"
fi
rm -f "$WIZARD_FILE"
ok "配置已写入"

step "执行 Telegram 首次授权"
if [ -n "$TTY_DEV" ]; then
  PYTHONPATH="$SRC_DIR" "$PY" "$BOOTSTRAP_PY" <"$TTY_DEV"
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
printf "%b\n" "- 管理命令：${B}TGRC${C0}"
printf "%b\n" "- 状态查看：${B}TGRC status${C0}"
printf "%b\n" "- 环境自检：${B}TGRC doctor${C0}"
printf "%b\n" "- Telegram 管理入口：收藏夹发送 ${B}${CMD_HELP}${C0}"
printf "%b\n" "- 彻底卸载：${B}TGRC uninstall${C0}"
line
