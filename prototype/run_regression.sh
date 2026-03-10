#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

PDF_DIR="${1:-/Users/liuchang/Downloads/Test pdf}"
shift || true

if [[ ! -x ".venv/bin/python" ]]; then
  echo "[ERROR] 未找到 .venv，请先执行 ./start.sh 或手动创建虚拟环境。"
  exit 1
fi

echo "[INFO] 运行回归套件，PDF目录: $PDF_DIR"
PYTHONPATH=. .venv/bin/python scripts/regression_suite.py "$PDF_DIR" "$@"
