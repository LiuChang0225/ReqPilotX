#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

PORT="${1:-8010}"
HOST="${HOST:-127.0.0.1}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 未安装，请先安装 Python 3。"
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "[INFO] 未找到 .venv，正在创建虚拟环境..."
  python3 -m venv .venv
fi

if [[ ! -x ".venv/bin/uvicorn" ]]; then
  echo "[INFO] 未找到 uvicorn，正在安装依赖..."
  .venv/bin/pip install -r requirements.txt
fi

if lsof -ti TCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[ERROR] 端口 $PORT 已被占用。"
  echo "[TIP] 请先执行: ./stop.sh $PORT"
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN || true
  exit 1
fi

echo "[INFO] 启动服务: http://$HOST:$PORT"
exec .venv/bin/uvicorn app.main:app --reload --host "$HOST" --port "$PORT"
