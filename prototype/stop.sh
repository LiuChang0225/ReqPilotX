#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

MODE="stop"
PORT="8000"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --drain)
      MODE="drain"
      shift
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    *)
      if [[ "$1" =~ ^[0-9]+$ ]]; then
        PORT="$1"
        shift
      else
        echo "[ERROR] 未知参数: $1"
        exit 1
      fi
      ;;
  esac
done
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "[ERROR] 端口必须是 1..65535 的整数: $PORT"
  exit 2
fi
PID_FILE="$ROOT_DIR/tmp/uvicorn_${PORT}.pid"
SESSION_FILE="$ROOT_DIR/tmp/uvicorn_${PORT}.screen"
SCREEN_NAME="reqpilotx_${PORT}"
WORKER_GUARD="$ROOT_DIR/scripts/local_worker_guard.py"
WORKER_DRAIN_FILE="$ROOT_DIR/tmp/workers_${PORT}.draining"
RUNTIME_INSTANCE_FILE="$ROOT_DIR/tmp/runtime_${PORT}.instance"
FRONTEND_PID_FILE="$ROOT_DIR/tmp/frontend_${PORT}.pid"
LOCAL_QDRANT_CLI="$ROOT_DIR/scripts/local_qdrant.py"

if [[ "$MODE" == "drain" ]]; then
  date -u +%Y-%m-%dT%H:%M:%SZ > "$WORKER_DRAIN_FILE"
  python3 "$WORKER_GUARD" drain --root "$ROOT_DIR" --port "$PORT"
  echo "[OK] worker 已进入无损排空：完成在手任务后退出，API 保持可读。"
  echo "[TIP] 使用 ./status.sh $PORT 查看状态；完成后执行 ./start.sh --drain-restart --port $PORT。"
  exit 0
fi

PID=""
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
fi

FRONTEND_PID=""
if [[ -f "$FRONTEND_PID_FILE" ]]; then
  FRONTEND_PID="$(cat "$FRONTEND_PID_FILE" 2>/dev/null || true)"
fi

stop_tracked_process() {
  local label="$1"
  local tracked_pid="$2"
  local expected_command="$3"
  if [[ -z "$tracked_pid" ]]; then
    return
  fi
  if [[ ! "$tracked_pid" =~ ^[0-9]+$ ]]; then
    echo "[WARN] $label PID 文件无效，拒绝发送信号: $tracked_pid"
    return
  fi
  if ! kill -0 "$tracked_pid" >/dev/null 2>&1; then
    return
  fi
  local command_line
  command_line="$(ps -p "$tracked_pid" -o command= 2>/dev/null || true)"
  if [[ "$command_line" != *"$expected_command"* ]]; then
    echo "[WARN] $label PID 已被其他进程复用，拒绝发送信号: $tracked_pid"
    return
  fi
  echo "[INFO] 正在停止 $label: $tracked_pid"
  kill "$tracked_pid" >/dev/null 2>&1 || true
  for _ in $(seq 1 10); do
    if ! kill -0 "$tracked_pid" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  if kill -0 "$tracked_pid" >/dev/null 2>&1; then
    echo "[WARN] $label 仍在运行，执行强制停止: $tracked_pid"
    kill -9 "$tracked_pid" >/dev/null 2>&1 || true
  fi
}

stop_tracked_process "Vite frontend" "$FRONTEND_PID" "vite"
# Stop every worker that is proven to belong to this repository and port,
# including legacy orphans whose PID file was overwritten. The guard protects
# workers rooted in another repository or explicitly owned by another port.
python3 "$WORKER_GUARD" cleanup --root "$ROOT_DIR" --port "$PORT"
stop_tracked_process "API" "$PID" "uvicorn"

if command -v screen >/dev/null 2>&1; then
  screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
  for slot in 1 2; do
    screen -S "reqpilotx_worker_${PORT}_${slot}" -X quit >/dev/null 2>&1 || true
  done
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

QDRANT_PYTHON="python3"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  QDRANT_PYTHON="$ROOT_DIR/.venv/bin/python"
fi
OTHER_RUNTIME_FOUND="false"
for OTHER_PID_FILE in "$ROOT_DIR"/tmp/uvicorn_*.pid "$ROOT_DIR"/tmp/worker_*.pid; do
  [[ -f "$OTHER_PID_FILE" ]] || continue
  [[ "$OTHER_PID_FILE" == "$PID_FILE" ]] && continue
  [[ "$OTHER_PID_FILE" == "$ROOT_DIR/tmp/worker_${PORT}.pid" ]] && continue
  [[ "$OTHER_PID_FILE" == "$ROOT_DIR/tmp/worker_${PORT}_"*.pid ]] && continue
  OTHER_PID="$(cat "$OTHER_PID_FILE" 2>/dev/null || true)"
  if [[ "$OTHER_PID" =~ ^[0-9]+$ ]] && kill -0 "$OTHER_PID" >/dev/null 2>&1; then
    OTHER_RUNTIME_FOUND="true"
    break
  fi
done
if [[ "$OTHER_RUNTIME_FOUND" == "true" ]]; then
  echo "[INFO] 仍有其他本仓库 API/worker 运行；保留共享 Qdrant。"
elif QDRANT_STOP_RESULT="$(
  "$QDRANT_PYTHON" "$LOCAL_QDRANT_CLI" stop-owned 2>&1
)"; then
  echo "[INFO] Qdrant lifecycle: $QDRANT_STOP_RESULT"
else
  echo "[WARN] 无法完成已归属 Qdrant 的停止检查。"
  echo "$QDRANT_STOP_RESULT"
fi

rm -f \
  "$PID_FILE" \
  "$SESSION_FILE" \
  "$FRONTEND_PID_FILE" \
  "$RUNTIME_INSTANCE_FILE" \
  "$WORKER_DRAIN_FILE" \
  "$ROOT_DIR/tmp/worker_${PORT}.pid" \
  "$ROOT_DIR/tmp/worker_${PORT}.screen" \
  "$ROOT_DIR"/tmp/worker_${PORT}_*.pid \
  "$ROOT_DIR"/tmp/worker_${PORT}_*.screen

echo "[OK] API、worker 与已跟踪前端已停止；共享 Qdrant 生命周期已协调。"
