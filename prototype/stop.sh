#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

PORT="${1:-8000}"
PID_FILE="$ROOT_DIR/tmp/uvicorn_${PORT}.pid"
SESSION_FILE="$ROOT_DIR/tmp/uvicorn_${PORT}.screen"
SCREEN_NAME="reqpilotx_${PORT}"

PID=""
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
fi

if command -v screen >/dev/null 2>&1; then
  screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
fi

if [[ -n "$PID" ]] && kill -0 "$PID" >/dev/null 2>&1; then
  echo "[INFO] 正在停止 PID 文件记录的进程: $PID"
  kill "$PID" || true
  for _ in $(seq 1 10); do
    if ! kill -0 "$PID" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  if kill -0 "$PID" >/dev/null 2>&1; then
    echo "[WARN] 进程仍在运行，执行强制停止: $PID"
    kill -9 "$PID" || true
  fi
fi

PIDS="$(lsof -ti TCP:"$PORT" -sTCP:LISTEN || true)"
if [[ -n "$PIDS" ]]; then
  echo "[INFO] 正在停止端口 $PORT 上的监听进程: $PIDS"
  kill $PIDS || true
  for _ in $(seq 1 10); do
    sleep 1
    if ! lsof -ti TCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      break
    fi
  done
fi

REMAIN="$(lsof -ti TCP:"$PORT" -sTCP:LISTEN || true)"
if [[ -n "$REMAIN" ]]; then
  echo "[WARN] 进程仍在运行，执行强制停止: $REMAIN"
  kill -9 $REMAIN || true
fi

rm -f "$PID_FILE" "$SESSION_FILE"

echo "[OK] 服务已停止。"
