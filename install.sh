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

if [ "$(id -u)" -ne 0 ]; then
  err "请使用 root 运行安装脚本。"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" 2>/dev/null && pwd -P || pwd -P)"

is_remote_bootstrap=0
case "$SCRIPT_PATH" in
  /dev/fd/*|/proc/self/fd/*|/tmp/*|stdin) is_remote_bootstrap=1 ;;
esac

# If the script is being piped from curl/wget or otherwise not running from a real repo checkout,
# clone/update the repo to a stable directory and re-exec the local install.sh.
if [ "$is_remote_bootstrap" -eq 1 ] || [ ! -f "$SCRIPT_DIR/requirements.txt" ] || [ ! -f "$SCRIPT_DIR/config.example.json" ]; then
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
VENV_DIR="$APP_DIR/venv"
PY="$VENV_DIR/bin/python3"
PIP="$VENV_DIR/bin/pip"

find_file() {
  local p
  for p in "$@"; do
    if [ -f "$APP_DIR/$p" ]; then
      printf '%s\n' "$APP_DIR/$p"
      return 0
    fi
  done
  return 1
}

REQ_FILE="$(find_file requirements.txt src/requirements.txt)"
CONFIG_EXAMPLE="$(find_file config.example.json config/config.example.json)"
BOOTSTRAP_PY="$(find_file bootstrap_session.py src/bootstrap_session.py)"
SYNC_ONCE_PY="$(find_file sync_once.py src/sync_once.py)"
DEPLOY_SH="$(find_file deploy.sh scripts/deploy.sh)"

if [ -z "$REQ_FILE" ] || [ -z "$CONFIG_EXAMPLE" ] || [ -z "$BOOTSTRAP_PY" ] || [ -z "$DEPLOY_SH" ]; then
  err "项目文件不完整，缺少 requirements/config/bootstrap/deploy 之一。"
  exit 1
fi

line
printf "%b\n" "${B}TGRC 一键部署向导${C0}"
printf "%b\n" "${DIM}不需要手动编辑 config.json，安装脚本会直接完成配置与授权。${C0}"
line

step "安装系统依赖"
apt-get update -y >/dev/null
apt-get install -y python3 python3-venv python3-pip git curl unzip ca-certificates systemd cron >/dev/null
ok "系统依赖已就绪"

step "初始化 Python 运行环境"
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi
"$PY" -m pip install --upgrade pip >/dev/null
"$PIP" install -r "$REQ_FILE" >/dev/null
mkdir -p "$APP_DIR/logs" "$APP_DIR/sessions" "$APP_DIR/backups"
ok "虚拟环境与依赖已完成"

step "准备默认配置模板"
if [ ! -f "$APP_DIR/config.json" ]; then
  cp "$CONFIG_EXAMPLE" "$APP_DIR/config.json"
fi
ok "配置模板已准备"

step "运行安装向导，生成 config.json"
APP_DIR="$APP_DIR" "$PY" <<'PYCONF'
import json, os
from pathlib import Path

app = Path(os.environ['APP_DIR'])
config_path = app / 'config.json'
with config_path.open('r', encoding='utf-8') as f:
    cfg = json.load(f)

def ask(prompt, default=''):
    suffix = f" [{default}]" if default else ''
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default

print("\nTGRC 配置向导\n")
cfg['api_id'] = int(ask('Telegram API_ID', str(cfg.get('api_id') or '')).strip() or '0')
cfg['api_hash'] = ask('Telegram API_HASH', cfg.get('api_hash') or '')
cfg['phone'] = ask('Telegram 登录手机号(带国家区号)', cfg.get('phone') or '')
cfg['session_name'] = ask('会话文件名', cfg.get('session_name') or 'tgrc')
cfg['service_name_prefix'] = ask('systemd 服务名前缀', cfg.get('service_name_prefix') or 'tgrc-radar')
cfg['command_alias'] = ask('全局命令别名', cfg.get('command_alias') or 'TGRC')
notify = ask('系统通知目标(留空默认发到收藏夹 me)', str(cfg.get('notify_channel_id') or ''))
alert = ask('默认告警目标(留空可稍后在 Telegram 内设置)', str(cfg.get('default_alert_channel') or ''))
prefix = ask('Telegram 命令前缀', cfg.get('command_prefix') or '-')
repo = ask('仓库地址', cfg.get('repo_url') or 'https://github.com/chenmo8848/TG-Radar-PlanC.git')
branch = ask('仓库分支', cfg.get('repo_branch') or 'main')

cfg['notify_channel_id'] = notify
cfg['default_alert_channel'] = alert
cfg['command_prefix'] = prefix
cfg['repo_url'] = repo
cfg['repo_branch'] = branch

with config_path.open('w', encoding='utf-8') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write('\n')
print(f"\n配置已写入: {config_path}\n")
PYCONF
ok "配置文件已生成"

step "执行 Telegram 首次授权"
TGRC_APP_DIR="$APP_DIR" "$PY" "$BOOTSTRAP_PY"
ok "Telegram 授权完成"

step "写入 TGRC 全局命令"
cat > /usr/local/bin/TGRC <<EOF2
#!/usr/bin/env bash
exec bash "$DEPLOY_SH" "\$@"
EOF2
chmod +x /usr/local/bin/TGRC
ok "全局命令已注册：TGRC"

step "注册 systemd 服务"
bash "$DEPLOY_SH" install >/dev/null
ok "systemd 服务已注册"

if [ -n "$SYNC_ONCE_PY" ]; then
  step "执行首次同步"
  "$PY" "$SYNC_ONCE_PY" || warn "首次同步未成功，可稍后执行 TGRC sync"
fi

step "启动双服务"
bash "$DEPLOY_SH" start >/dev/null
ok "服务已启动"

line
printf "%b\n" "${B}${GR}部署完成${C0}"
printf "%b\n" "- 管理命令：${B}TGRC${C0}"
printf "%b\n" "- 状态查看：${B}TGRC status${C0}"
printf "%b\n" "- 环境自检：${B}TGRC doctor${C0}"
printf "%b\n" "- Telegram 管理入口：收藏夹发送 ${B}-help${C0}"
line
