#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$APP_DIR/src"
VENV_DIR="$APP_DIR/venv"
PY="$VENV_DIR/bin/python3"
export PYTHONPATH="$SRC_DIR"

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
printf "%b\n" "${DIM}目录已规整：根目录只保留入口，业务代码全部收进 src/。${C0}"
line

step "安装系统依赖"
apt-get update -y >/dev/null
apt-get install -y python3 python3-venv python3-pip git curl unzip ca-certificates systemd cron >/dev/null
ok "系统依赖已就绪"

step "初始化 Python 运行环境"
[ -d "$VENV_DIR" ] || python3 -m venv "$VENV_DIR"
"$PY" -m pip install --upgrade pip >/dev/null
"$PY" -m pip install -r "$APP_DIR/requirements.txt" >/dev/null
mkdir -p "$APP_DIR/logs" "$APP_DIR/sessions" "$APP_DIR/backups"
ok "虚拟环境与依赖已完成"

step "准备默认配置模板"
[ -f "$APP_DIR/config.json" ] || cp "$APP_DIR/config.example.json" "$APP_DIR/config.json"
ok "配置模板已准备"

step "预清理旧版残留（服务 / 命令 / 进程）"
bash "$APP_DIR/deploy.sh" cleanup-legacy --keep-current >/dev/null || true
ok "旧版残留已清理"

step "写入 TGRC 全局命令"
rm -f /usr/local/bin/TGR /usr/bin/TGR || true
cat >/usr/local/bin/TGRC <<WRAP
#!/usr/bin/env bash
cd "$APP_DIR"
exec bash "$APP_DIR/deploy.sh" "\$@"
WRAP
chmod +x /usr/local/bin/TGRC
ok "全局命令已注册：TGRC"

step "运行配置向导，生成 config.json"
cd "$APP_DIR"
TGRC_APP_DIR="$APP_DIR" PYTHONPATH="$SRC_DIR" "$PY" - <<'PY'
from __future__ import annotations
from pathlib import Path
import os
from tgr.config import read_config_data, save_config_data

work_dir = Path(os.environ["TGRC_APP_DIR"])
current = read_config_data(work_dir)

def prompt_text(label: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default
        if not required:
            return ""
        print("该项不能为空。")

def prompt_int(label: str, default: int | None = None, allow_empty: bool = False) -> int | None:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if not raw:
            if allow_empty:
                return default if default is not None else None
            if default is not None:
                return default
        if raw.lower() in {"off", "none", "null", "skip"}:
            return None
        try:
            return int(raw)
        except ValueError:
            print("请输入有效数字，或输入 off 关闭。")

def prompt_prefix(default: str) -> str:
    while True:
        value = input(f"命令前缀 [{default}]: ").strip() or default
        if 1 <= len(value) <= 3 and " " not in value:
            return value
        print("命令前缀建议 1-3 个字符，且不能包含空格。")

print("\nTGRC 安装向导\n" + "-" * 56)
print("这一步会直接写入 config.json，不需要你手动打开文件编辑。")
print("Telegram API 凭据来自 my.telegram.org。\n")

def_api_id = current.get("api_id") if current.get("api_id") not in (None, 0, 1234567) else None
api_id = prompt_int("API_ID", default=def_api_id)
api_hash = prompt_text(
    "API_HASH",
    default=current.get("api_hash") if current.get("api_hash") not in (None, "", "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx") else "",
    required=True,
)
global_alert_channel_id = prompt_int("默认告警频道 ID（可留空）", default=current.get("global_alert_channel_id"), allow_empty=True)
notify_channel_id = prompt_int("系统通知频道 ID（留空则发到 Saved Messages）", default=current.get("notify_channel_id"), allow_empty=True)
cmd_prefix = prompt_prefix(current.get("cmd_prefix") or "-")
service_name_prefix = prompt_text("systemd 服务名前缀", default=current.get("service_name_prefix") or "tgrc-radar", required=True)
repo_url = prompt_text("仓库地址（可留空）", default=current.get("repo_url") or "")

if not api_id or api_id == 1234567 or not api_hash or api_hash == "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx":
    raise SystemExit("未写入有效 Telegram API 凭据，安装已中止。")

path = save_config_data(work_dir, {
    **current,
    "api_id": int(api_id),
    "api_hash": api_hash,
    "global_alert_channel_id": global_alert_channel_id,
    "notify_channel_id": notify_channel_id,
    "cmd_prefix": cmd_prefix,
    "service_name_prefix": service_name_prefix,
    "repo_url": repo_url,
})
print(f"\n配置已写入：{path}\n")
PY
ok "配置已写入"

step "执行 Telegram 首次授权"
PYTHONPATH="$SRC_DIR" "$PY" "$SRC_DIR/bootstrap_session.py"
ok "Telegram 授权完成"

step "写入并启用 systemd 双服务"
bash "$APP_DIR/deploy.sh" install-services >/dev/null
ok "systemd 服务已注册"

step "执行首次同步"
PYTHONPATH="$SRC_DIR" "$PY" "$SRC_DIR/sync_once.py" || warn "首次同步未成功，可稍后执行 TGRC sync"

step "启动双服务"
bash "$APP_DIR/deploy.sh" start >/dev/null
ok "服务已启动"

line
printf "%b\n" "${B}${GR}部署完成${C0}"
printf "%b\n" "- 管理命令：${B}TGRC${C0}"
printf "%b\n" "- 状态查看：${B}TGRC status${C0}"
printf "%b\n" "- 环境自检：${B}TGRC doctor${C0}"
printf "%b\n" "- 卸载清理：${B}TGRC uninstall${C0}"
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
printf "%b\n" "- Telegram 管理入口：收藏夹发送 ${B}${CMD_HELP}${C0}"
line
