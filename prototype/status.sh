#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

HOST="${HOST:-127.0.0.1}"
PORT="${1:-8000}"
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "[ERROR] 端口必须是 1..65535 的整数: $PORT"
  exit 2
fi
WORKER_COUNT="${REQPILOT_LOCAL_WORKER_COUNT:-2}"
if [[ ! "$WORKER_COUNT" =~ ^[12]$ ]]; then
  echo "[ERROR] REQPILOT_LOCAL_WORKER_COUNT 只允许 1 或 2。"
  exit 2
fi
if [[ "$HOST" != "127.0.0.1" ]]; then
  echo "[ERROR] 安全策略拒绝查询非 loopback 地址: $HOST"
  exit 2
fi
PID_FILE="$ROOT_DIR/tmp/uvicorn_${PORT}.pid"
LOG_FILE="$ROOT_DIR/tmp/uvicorn_${PORT}.log"
SESSION_FILE="$ROOT_DIR/tmp/uvicorn_${PORT}.screen"
SCREEN_NAME="reqpilotx_${PORT}"
WORKER_GUARD="$ROOT_DIR/scripts/local_worker_guard.py"
RUNTIME_IDENTITY="$ROOT_DIR/scripts/local_runtime_identity.py"
RUNTIME_INSTANCE_FILE="$ROOT_DIR/tmp/runtime_${PORT}.instance"
WORKER_DRAIN_FILE="$ROOT_DIR/tmp/workers_${PORT}.draining"
FRONTEND_PID_FILE="$ROOT_DIR/tmp/frontend_${PORT}.pid"
FRONTEND_LOG_FILE="$ROOT_DIR/tmp/frontend_${PORT}.log"
LOCAL_QDRANT_CLI="$ROOT_DIR/scripts/local_qdrant.py"
URL="http://$HOST:$PORT/api/runtime-readiness"

PID=""
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
fi

LISTENER="$(lsof -ti TCP:"$PORT" -sTCP:LISTEN || true)"
READINESS_BODY="$(curl -sS "$URL" 2>/dev/null || true)"
HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null || true)"
SCREEN_STATUS="n/a"
if command -v screen >/dev/null 2>&1; then
  SCREEN_LIST="$(screen -ls 2>/dev/null || true)"
  if printf '%s\n' "$SCREEN_LIST" | grep -Fq ".$SCREEN_NAME"; then
    SCREEN_STATUS="running ($SCREEN_NAME)"
  else
    SCREEN_STATUS="not found"
  fi
fi
WORKER_GUARD_STATUS="$(
  python3 "$WORKER_GUARD" status --root "$ROOT_DIR" --port "$PORT" 2>&1 \
    || true
)"

echo "PORT: $PORT"
if [[ -n "$PID" ]]; then
  echo "PID_FILE: $PID_FILE"
  echo "PID: $PID"
  if kill -0 "$PID" >/dev/null 2>&1; then
    echo "PID_STATUS: alive"
  else
    echo "PID_STATUS: stale"
  fi
else
  echo "PID_FILE: (none)"
fi

if [[ -f "$SESSION_FILE" ]]; then
  echo "SESSION_FILE: $SESSION_FILE"
fi
echo "SCREEN: $SCREEN_STATUS"
echo "WORKER_CAPACITY: $WORKER_COUNT"
if [[ -f "$WORKER_DRAIN_FILE" ]]; then
  echo "WORKER_DRAIN: requested ($(cat "$WORKER_DRAIN_FILE" 2>/dev/null || true))"
else
  echo "WORKER_DRAIN: no"
fi
RUNTIME_INSTANCE_ID="$(cat "$RUNTIME_INSTANCE_FILE" 2>/dev/null || true)"
CURRENT_SOURCE_REVISION="$(python3 "$RUNTIME_IDENTITY" revision --root "$ROOT_DIR")"
if [[ -n "$PID" && -n "$RUNTIME_INSTANCE_ID" ]] \
  && python3 "$RUNTIME_IDENTITY" matches \
    --pid "$PID" --role api --port "$PORT" \
    --instance-id "$RUNTIME_INSTANCE_ID" \
    --source-revision "$CURRENT_SOURCE_REVISION" \
    --worker-count "$WORKER_COUNT"; then
  echo "RUNTIME_SOURCE: current ($CURRENT_SOURCE_REVISION)"
  python3 "$WORKER_GUARD" pool-status \
    --root "$ROOT_DIR" --port "$PORT" --count "$WORKER_COUNT" \
    --instance-id "$RUNTIME_INSTANCE_ID" \
    --source-revision "$CURRENT_SOURCE_REVISION" \
    || true
else
  echo "RUNTIME_SOURCE: stale-or-untracked (current=$CURRENT_SOURCE_REVISION)"
fi
for slot in $(seq 1 "$WORKER_COUNT"); do
  WORKER_PID_FILE="$ROOT_DIR/tmp/worker_${PORT}_${slot}.pid"
  WORKER_PID="$(cat "$WORKER_PID_FILE" 2>/dev/null || true)"
  if [[ "$WORKER_PID" =~ ^[0-9]+$ ]] && kill -0 "$WORKER_PID" >/dev/null 2>&1; then
    echo "WORKER_${slot}: running ($WORKER_PID)"
  elif [[ -f "$WORKER_PID_FILE" ]]; then
    echo "WORKER_${slot}: stale PID file"
  else
    echo "WORKER_${slot}: not found"
  fi
  if command -v screen >/dev/null 2>&1 \
    && printf '%s\n' "${SCREEN_LIST:-}" | grep -Fq ".reqpilotx_worker_${PORT}_${slot}"; then
    echo "WORKER_${slot}_SCREEN: running (reqpilotx_worker_${PORT}_${slot})"
  fi
done
if [[ -n "$WORKER_GUARD_STATUS" ]]; then
  echo "$WORKER_GUARD_STATUS"
fi
QDRANT_PYTHON="python3"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  QDRANT_PYTHON="$ROOT_DIR/.venv/bin/python"
fi
QDRANT_STATUS="$("$QDRANT_PYTHON" "$LOCAL_QDRANT_CLI" status 2>&1 || true)"
if [[ -n "$QDRANT_STATUS" ]]; then
  echo "QDRANT: $QDRANT_STATUS"
else
  echo "QDRANT: status unavailable"
fi
if [[ -f "$FRONTEND_PID_FILE" ]]; then
  FRONTEND_PID="$(cat "$FRONTEND_PID_FILE" 2>/dev/null || true)"
  if [[ -n "$FRONTEND_PID" ]] && kill -0 "$FRONTEND_PID" >/dev/null 2>&1; then
    echo "FRONTEND: Vite running ($FRONTEND_PID, http://127.0.0.1:5173)"
  else
    echo "FRONTEND: stale PID file"
  fi
else
  echo "FRONTEND: production build served by API"
fi

if [[ -n "$LISTENER" ]]; then
  echo "LISTENING: yes ($LISTENER)"
else
  echo "LISTENING: no"
fi

if [[ "$HTTP_CODE" == "200" ]]; then
  echo "HTTP: 200 OK"
  echo "URL: $URL"
else
  echo "HTTP: $HTTP_CODE"
  echo "URL: $URL"
fi

if [[ -n "$READINESS_BODY" ]]; then
  echo "--- readiness body ---"
  echo "$READINESS_BODY"
fi

if [[ -f "$LOG_FILE" ]]; then
  echo "LOG: $LOG_FILE"
  echo "--- recent log ---"
  tail -n 20 "$LOG_FILE" || true
else
  echo "LOG: (none)"
fi
for slot in $(seq 1 "$WORKER_COUNT"); do
  WORKER_LOG_FILE="$ROOT_DIR/tmp/worker_${PORT}_${slot}.log"
  if [[ -f "$WORKER_LOG_FILE" ]]; then
    echo "WORKER_${slot}_LOG: $WORKER_LOG_FILE"
    echo "--- recent worker $slot log ---"
    tail -n 20 "$WORKER_LOG_FILE" || true
  fi
done
if [[ -f "$FRONTEND_LOG_FILE" ]]; then
  echo "FRONTEND_LOG: $FRONTEND_LOG_FILE"
  echo "--- recent frontend log ---"
  tail -n 10 "$FRONTEND_LOG_FILE" || true
fi
