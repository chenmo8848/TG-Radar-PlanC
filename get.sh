#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${TGRC_REPO_URL:-https://github.com/chenmo8848/TG-Radar-PlanC.git}"
BRANCH="${TGRC_BRANCH:-main}"
INSTALL_DIR="${TGRC_INSTALL_DIR:-/opt/TG-Radar-PlanC}"

C0='\033[0m'; B='\033[1m'; DIM='\033[2m'; CY='\033[36m'; GR='\033[32m'; YE='\033[33m'; RD='\033[31m'
step(){ printf "%b\n" "${CY}▶${C0} $*"; }
ok(){ printf "%b\n" "${GR}✔${C0} $*"; }
warn(){ printf "%b\n" "${YE}⚠${C0} $*"; }
err(){ printf "%b\n" "${RD}✖${C0} $*"; }
line(){ printf "%b\n" "${DIM}────────────────────────────────────────────────────────${C0}"; }

if [ "$(id -u)" -ne 0 ]; then
  err "请使用 root 运行一键安装命令。"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
line
printf "%b\n" "${B}TG-Radar-PlanC 一键安装入口${C0}"
printf "%b\n" "${DIM}仓库将被拉取到 ${INSTALL_DIR}，随后自动运行 install.sh。${C0}"
line

step "安装拉取仓库所需基础依赖"
apt-get update -y >/dev/null
apt-get install -y git curl ca-certificates >/dev/null
ok "基础依赖已就绪"

if [ -d "$INSTALL_DIR/.git" ]; then
  step "检测到已有安装目录，更新到最新 ${BRANCH}"
  git -C "$INSTALL_DIR" fetch origin "$BRANCH" --depth=1
  git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
  git -C "$INSTALL_DIR" clean -fd
  ok "仓库已更新"
else
  step "克隆仓库"
  rm -rf "$INSTALL_DIR"
  git clone --depth=1 -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR" >/dev/null
  ok "仓库已拉取到 $INSTALL_DIR"
fi

cd "$INSTALL_DIR"
chmod +x ./install.sh ./deploy.sh || true

step "启动安装向导"
exec bash ./install.sh "$@"
