#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
exec bash "$APP_DIR/deploy.sh" cleanup-legacy --keep-current
