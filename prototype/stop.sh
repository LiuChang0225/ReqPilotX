#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-8010}"

PIDS="$(lsof -ti TCP:"$PORT" -sTCP:LISTEN || true)"
if [[ -z "$PIDS" ]]; then
  echo "[INFO] 端口 $PORT 没有运行中的服务。"
  exit 0
fi

echo "[INFO] 正在停止端口 $PORT 上的进程: $PIDS"
kill $PIDS || true
sleep 1

REMAIN="$(lsof -ti TCP:"$PORT" -sTCP:LISTEN || true)"
if [[ -n "$REMAIN" ]]; then
  echo "[WARN] 进程仍在运行，执行强制停止: $REMAIN"
  kill -9 $REMAIN || true
fi

echo "[OK] 服务已停止。"
