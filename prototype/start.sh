#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

HOST="${HOST:-127.0.0.1}"
APP_MODULE="${APP_MODULE:-app.main:app}"
HEALTH_PATH="${HEALTH_PATH:-/api/health}"
LOG_DIR="$ROOT_DIR/tmp"

MODE="stable"
PORT=""

usage() {
  cat <<'EOF'
用法：
  ./start.sh
  ./start.sh 8000
  ./start.sh --port 8000
  ./start.sh --dev
  ./start.sh --dev --port 8010

默认行为：
  - 不带参数：稳定后台模式，默认端口 8000
  - --dev：开发模式（前台 + --reload），默认端口 8010
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev)
      MODE="dev"
      shift
      ;;
    --stable)
      MODE="stable"
      shift
      ;;
    --host)
      HOST="${2:-}"
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [[ -z "$PORT" && "$1" =~ ^[0-9]+$ ]]; then
        PORT="$1"
        shift
      else
        echo "[ERROR] 未知参数: $1"
        usage
        exit 1
      fi
      ;;
  esac
done

if [[ -z "$PORT" ]]; then
  if [[ "$MODE" == "dev" ]]; then
    PORT="8010"
  else
    PORT="8000"
  fi
fi

PID_FILE="$LOG_DIR/uvicorn_${PORT}.pid"
LOG_FILE="$LOG_DIR/uvicorn_${PORT}.log"
SESSION_FILE="$LOG_DIR/uvicorn_${PORT}.screen"
SCREEN_NAME="reqpilotx_${PORT}"

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

mkdir -p "$LOG_DIR"

if [[ -f "$PID_FILE" ]]; then
  EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$EXISTING_PID" ]] && kill -0 "$EXISTING_PID" >/dev/null 2>&1; then
    if curl -fsS "http://$HOST:$PORT$HEALTH_PATH" >/dev/null 2>&1; then
      echo "[INFO] 服务已经在运行。"
      echo "PID: $EXISTING_PID"
      echo "URL: http://$HOST:$PORT"
      echo "LOG: $LOG_FILE"
      exit 0
    fi
    echo "[WARN] PID 文件对应进程仍在，但健康检查失败，先清理旧进程。"
    kill "$EXISTING_PID" >/dev/null 2>&1 || true
    sleep 1
  fi
  rm -f "$PID_FILE"
fi

if lsof -ti TCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[ERROR] 端口 $PORT 已被占用。"
  echo "[TIP] 请先执行: ./stop.sh $PORT"
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN || true
  exit 1
fi

if [[ "$MODE" == "dev" ]]; then
  echo "[INFO] 启动开发模式: http://$HOST:$PORT"
  echo "[INFO] 模式：前台 + 自动重载"
  exec .venv/bin/uvicorn "$APP_MODULE" --reload --host "$HOST" --port "$PORT"
fi

echo "[INFO] 启动稳定后台服务: http://$HOST:$PORT"
if command -v screen >/dev/null 2>&1; then
  screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
  ROOT_DIR="$ROOT_DIR" APP_MODULE="$APP_MODULE" HOST="$HOST" PORT="$PORT" LOG_FILE="$LOG_FILE" \
    screen -dmS "$SCREEN_NAME" bash -lc 'cd "$ROOT_DIR" && exec .venv/bin/uvicorn "$APP_MODULE" --host "$HOST" --port "$PORT" >> "$LOG_FILE" 2>&1'
  echo "$SCREEN_NAME" > "$SESSION_FILE"
else
  nohup .venv/bin/uvicorn "$APP_MODULE" --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 < /dev/null &
  echo "nohup" > "$SESSION_FILE"
  PID="$!"
  echo "$PID" > "$PID_FILE"
fi

READY="false"
for _ in $(seq 1 20); do
  if curl -fsS "http://$HOST:$PORT$HEALTH_PATH" >/dev/null 2>&1; then
    READY="true"
    break
  fi
  sleep 1
done

if [[ "$READY" != "true" ]]; then
  echo "[ERROR] 服务已启动但 readiness 未通过。"
  echo "READINESS_URL: http://$HOST:$PORT$HEALTH_PATH"
  READINESS_BODY="$(curl -sS "http://$HOST:$PORT$HEALTH_PATH" 2>/dev/null || true)"
  if [[ -n "$READINESS_BODY" ]]; then
    echo "--- readiness response ---"
    echo "$READINESS_BODY"
  fi
  echo "LOG: $LOG_FILE"
  if command -v screen >/dev/null 2>&1; then
    screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
  fi
  tail -n 80 "$LOG_FILE" || true
  rm -f "$PID_FILE" "$SESSION_FILE"
  exit 1
fi

PID="$(lsof -ti TCP:"$PORT" -sTCP:LISTEN | head -n 1 || true)"
if [[ -n "$PID" ]]; then
  echo "$PID" > "$PID_FILE"
fi

echo "[OK] 服务已启动。"
echo "PID: $PID"
echo "主页: http://$HOST:$PORT/"
echo "工作台: http://$HOST:$PORT/workspace"
echo "审阅台: http://$HOST:$PORT/workspace?stage=review"
echo "日志: $LOG_FILE"
