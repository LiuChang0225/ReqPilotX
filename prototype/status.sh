#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

HOST="${HOST:-127.0.0.1}"
PORT="${1:-8000}"
PID_FILE="$ROOT_DIR/tmp/uvicorn_${PORT}.pid"
LOG_FILE="$ROOT_DIR/tmp/uvicorn_${PORT}.log"
SESSION_FILE="$ROOT_DIR/tmp/uvicorn_${PORT}.screen"
SCREEN_NAME="reqpilotx_${PORT}"
URL="http://$HOST:$PORT/workspace?stage=review"

PID=""
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
fi

LISTENER="$(lsof -ti TCP:"$PORT" -sTCP:LISTEN || true)"
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
fi

if [[ -f "$LOG_FILE" ]]; then
  echo "LOG: $LOG_FILE"
  echo "--- recent log ---"
  tail -n 20 "$LOG_FILE" || true
else
  echo "LOG: (none)"
fi
