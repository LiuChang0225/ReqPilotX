#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

if [[ $# -lt 1 ]]; then
  echo "用法: $0 <legacy-pdf-corpus-dir> [regression-suite-options...]" >&2
  echo "[ERROR] legacy PDF 语料目录必须显式提供；此脚本不再使用本机历史默认路径。" >&2
  exit 2
fi

PDF_DIR="$1"
shift

if [[ ! -d "$PDF_DIR" ]]; then
  echo "[ERROR] legacy PDF 语料目录不存在: $PDF_DIR" >&2
  exit 2
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "[ERROR] 未找到 .venv，请先执行 ./start.sh 或手动创建虚拟环境。"
  exit 1
fi

echo "[INFO] 运行回归套件，PDF目录: $PDF_DIR"
PYTHONPATH=. .venv/bin/python scripts/regression_suite.py "$PDF_DIR" "$@"
