#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$APP_DIR/venv"
PY="$VENV_DIR/bin/python3"

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
"$PY" -m pip install -r "$APP_DIR/requirements.txt" >/dev/null
mkdir -p "$APP_DIR/logs" "$APP_DIR/sessions" "$APP_DIR/backups"
ok "虚拟环境与依赖已完成"

step "准备默认配置模板"
if [ ! -f "$APP_DIR/config.json" ]; then
  cp "$APP_DIR/config.example.json" "$APP_DIR/config.json"
fi
ok "配置模板已准备"

step "写入 TGRC 全局命令"
cat >/usr/local/bin/TGRC <<EOF
#!/usr/bin/env bash
cd "$APP_DIR"
exec bash "$APP_DIR/deploy.sh" "\$@"
EOF
chmod +x /usr/local/bin/TGRC
ok "全局命令已注册：TGRC"

step "运行安装向导，生成 config.json"
cd "$APP_DIR"
"$PY" "$APP_DIR/setup_wizard.py" "$@"
ok "配置已写入"

step "执行 Telegram 首次授权"
"$PY" "$APP_DIR/bootstrap_session.py"
ok "Telegram 授权完成"

step "写入并启用 systemd 双服务"
bash "$APP_DIR/deploy.sh" install-services >/dev/null
ok "systemd 服务已注册"

step "执行首次同步"
"$PY" "$APP_DIR/sync_once.py" || warn "首次同步未成功，可稍后执行 TGRC sync"

step "启动双服务"
bash "$APP_DIR/deploy.sh" start >/dev/null
ok "服务已启动"

line
printf "%b\n" "${B}${GR}部署完成${C0}"
printf "%b\n" "- 管理命令：${B}TGRC${C0}"
printf "%b\n" "- 状态查看：${B}TGRC status${C0}"
printf "%b\n" "- 环境自检：${B}TGRC doctor${C0}"
printf "%b\n" "- Telegram 管理入口：收藏夹发送 ${B}-help${C0}"
line
