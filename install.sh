#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$ROOT_DIR/venv"
PY="$VENV_DIR/bin/python3"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl unzip ca-certificates systemd cron

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r "$ROOT_DIR/requirements.txt"

mkdir -p "$ROOT_DIR/logs" "$ROOT_DIR/sessions"
if [ ! -f "$ROOT_DIR/config.json" ]; then
  cp "$ROOT_DIR/config.example.json" "$ROOT_DIR/config.json"
  echo "Created config.json from config.example.json"
fi

cat >/usr/local/bin/TGR <<EOS
#!/usr/bin/env bash
cd "$ROOT_DIR"
exec bash "$ROOT_DIR/deploy.sh"
EOS
chmod +x /usr/local/bin/TGR

chmod +x "$ROOT_DIR/deploy.sh" "$ROOT_DIR/install.sh"
echo
echo "Install completed."
echo "1) Edit: $ROOT_DIR/config.json"
echo "2) Run: cd $ROOT_DIR && $PY bootstrap_session.py"
echo "3) Run: TGR"
