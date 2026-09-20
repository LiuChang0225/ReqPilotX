#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

HOST="${HOST:-127.0.0.1}"
APP_MODULE="${APP_MODULE:-app.main:app}"
HEALTH_PATH="${HEALTH_PATH:-/api/runtime-readiness}"
LOG_DIR="$ROOT_DIR/tmp"

MODE="stable"
PORT=""
DRAIN_RESTART="false"

usage() {
  cat <<'EOF'
用法：
  ./start.sh
  ./start.sh 8000
  ./start.sh --port 8000
  ./start.sh --dev
  ./start.sh --dev --port 8010
  ./start.sh --drain-restart

默认行为：
  - 不带参数：稳定后台模式，默认端口 8000
  - --dev：开发模式（前台 + --reload），默认端口 8010
  - --drain-restart：当前 worker 完成在手任务后退出，再成对重启 API 与 worker 池
  - 服务仅允许绑定 127.0.0.1；不提供远程监听模式
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
    --drain-restart)
      DRAIN_RESTART="true"
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

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "[ERROR] 端口必须是 1..65535 的整数: $PORT"
  exit 2
fi

if [[ "$HOST" != "127.0.0.1" ]]; then
  echo "[ERROR] 安全策略拒绝非 loopback 绑定: $HOST"
  echo "[TIP] ReqPilotX 本地服务只能使用 127.0.0.1。"
  exit 2
fi

WORKER_COUNT="${REQPILOT_LOCAL_WORKER_COUNT:-2}"
if [[ ! "$WORKER_COUNT" =~ ^[12]$ ]]; then
  echo "[ERROR] REQPILOT_LOCAL_WORKER_COUNT 只允许 1 或 2。"
  exit 2
fi
export REQPILOT_LOCAL_WORKER_COUNT="$WORKER_COUNT"

# Local ReqPilotX uses the Codex desktop runtime as its single Agent backend.
# Keep the paths overrideable for development, but require no provider setup UI
# or shell configuration for the standard desktop installation.
DEFAULT_CODEX_BIN="/Applications/ChatGPT.app/Contents/Resources/codex"
DEFAULT_CODEX_HOME="$HOME/.codex-reqpilotx"
if [[ -x "$DEFAULT_CODEX_BIN" && -d "$DEFAULT_CODEX_HOME" ]]; then
  export REQPILOT_AGENT_RUNTIME="${REQPILOT_AGENT_RUNTIME:-codex-app-server}"
  export REQPILOT_CODEX_BIN="${REQPILOT_CODEX_BIN:-$DEFAULT_CODEX_BIN}"
  export REQPILOT_CODEX_HOME="${REQPILOT_CODEX_HOME:-$DEFAULT_CODEX_HOME}"
  export REQPILOT_AGENT_MODEL_ID="${REQPILOT_AGENT_MODEL_ID:-gpt-5.6-sol}"
  # The local desktop product has one Codex-owned document path.  Operators can
  # still set this false explicitly, but a standard signed-in local launch must
  # not silently fall back to legacy provider/rules extraction.
  export REQPILOT_AGENT_CUSTOMER_DATA_ENABLED="${REQPILOT_AGENT_CUSTOMER_DATA_ENABLED:-true}"
fi

PID_FILE="$LOG_DIR/uvicorn_${PORT}.pid"
LOG_FILE="$LOG_DIR/uvicorn_${PORT}.log"
SESSION_FILE="$LOG_DIR/uvicorn_${PORT}.screen"
SCREEN_NAME="reqpilotx_${PORT}"
WORKER_GUARD="$ROOT_DIR/scripts/local_worker_guard.py"
RUNTIME_IDENTITY="$ROOT_DIR/scripts/local_runtime_identity.py"
RUNTIME_INSTANCE_FILE="$LOG_DIR/runtime_${PORT}.instance"
WORKER_DRAIN_FILE="$LOG_DIR/workers_${PORT}.draining"
FRONTEND_DIR="$ROOT_DIR/frontend"
FRONTEND_LOG_FILE="$LOG_DIR/frontend_${PORT}.log"
FRONTEND_PID_FILE="$LOG_DIR/frontend_${PORT}.pid"
LOCAL_QDRANT_CLI="$ROOT_DIR/scripts/local_qdrant.py"

worker_pid_file() {
  echo "$LOG_DIR/worker_${PORT}_$1.pid"
}

worker_log_file() {
  echo "$LOG_DIR/worker_${PORT}_$1.log"
}

worker_session_file() {
  echo "$LOG_DIR/worker_${PORT}_$1.screen"
}

worker_screen_name() {
  echo "reqpilotx_worker_${PORT}_$1"
}

tracked_worker_pid() {
  local pid_file
  pid_file="$(worker_pid_file "$1")"
  if [[ -f "$pid_file" ]]; then
    cat "$pid_file" 2>/dev/null || true
  fi
}

worker_pid_matches_slot() {
  local slot="$1"
  local worker_pid="$2"
  if [[ ! "$worker_pid" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  local pool_status
  if ! pool_status="$(python3 "$WORKER_GUARD" pool-status \
    --root "$ROOT_DIR" \
    --port "$PORT" \
    --count "$WORKER_COUNT" \
    --instance-id "$RUNTIME_INSTANCE_ID" \
    --source-revision "$CURRENT_SOURCE_REVISION" 2>/dev/null)"; then
    return 1
  fi
  printf '%s\n' "$pool_status" | grep -Fq "${slot}:${worker_pid}"
}

cleanup_local_workers() {
  local arguments=(cleanup --root "$ROOT_DIR" --port "$PORT")
  for keep_pid in "$@"; do
    [[ -n "$keep_pid" ]] && arguments+=(--keep-pid "$keep_pid")
  done
  python3 "$WORKER_GUARD" "${arguments[@]}"
}

start_stable_worker() {
  local slot="$1"
  local worker_pid_file worker_log_file worker_session_file worker_screen_name
  local worker_lane_args=""
  worker_pid_file="$(worker_pid_file "$slot")"
  worker_log_file="$(worker_log_file "$slot")"
  worker_session_file="$(worker_session_file "$slot")"
  worker_screen_name="$(worker_screen_name "$slot")"
  if (( slot > 1 )); then
    worker_lane_args="--accepted-kind document.extract --claim-profile local-extraction-no-llm"
  fi
  rm -f "$worker_pid_file" "$worker_session_file"
  if command -v screen >/dev/null 2>&1; then
    screen -S "$worker_screen_name" -X quit >/dev/null 2>&1 || true
    ROOT_DIR="$ROOT_DIR" WORKER_LOG_FILE="$worker_log_file" \
      WORKER_PID_FILE="$worker_pid_file" WORKER_SLOT="$slot" \
      WORKER_LANE_ARGS="$worker_lane_args" \
      REQPILOT_LOCAL_RUNTIME_PORT="$PORT" \
      REQPILOT_LOCAL_RUNTIME_INSTANCE_ID="$RUNTIME_INSTANCE_ID" \
      REQPILOT_RUNTIME_SOURCE_REVISION="$CURRENT_SOURCE_REVISION" \
      REQPILOT_LOCAL_WORKER_COUNT="$WORKER_COUNT" \
      screen -dmS "$worker_screen_name" bash -lc 'cd "$ROOT_DIR" && printf "%s\n" "$$" > "$WORKER_PID_FILE" && export REQPILOT_PROCESS_ROLE=worker REQPILOT_LOCAL_RUNTIME_PORT REQPILOT_LOCAL_RUNTIME_INSTANCE_ID REQPILOT_RUNTIME_SOURCE_REVISION REQPILOT_LOCAL_WORKER_COUNT REQPILOT_LOCAL_WORKER_SLOT="$WORKER_SLOT" && exec .venv/bin/python -m app.worker $WORKER_LANE_ARGS >> "$WORKER_LOG_FILE" 2>&1'
    echo "$worker_screen_name" > "$worker_session_file"
  else
      REQPILOT_LOCAL_RUNTIME_PORT="$PORT" \
      REQPILOT_LOCAL_RUNTIME_INSTANCE_ID="$RUNTIME_INSTANCE_ID" \
      REQPILOT_RUNTIME_SOURCE_REVISION="$CURRENT_SOURCE_REVISION" \
      REQPILOT_LOCAL_WORKER_COUNT="$WORKER_COUNT" \
      REQPILOT_LOCAL_WORKER_SLOT="$slot" REQPILOT_PROCESS_ROLE=worker \
      nohup .venv/bin/python -m app.worker $worker_lane_args \
      >"$worker_log_file" 2>&1 < /dev/null &
    echo "$!" > "$worker_pid_file"
  fi
}

wait_for_stable_worker() {
  local slot="$1"
  local worker_pid=""
  for _ in $(seq 1 10); do
    worker_pid="$(tracked_worker_pid "$slot")"
    if worker_pid_matches_slot "$slot" "$worker_pid"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 未安装，请先安装 Python 3。"
  exit 1
fi

mkdir -p "$LOG_DIR"
CURRENT_SOURCE_REVISION="$(
  python3 "$RUNTIME_IDENTITY" revision --root "$ROOT_DIR"
)"
RUNTIME_INSTANCE_ID="$(cat "$RUNTIME_INSTANCE_FILE" 2>/dev/null || true)"
EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
EXISTING_API_HEALTHY="false"

if [[ "$DRAIN_RESTART" == "true" ]]; then
  echo "[INFO] 进入无损排空：worker 完成在手任务后将停止领取新任务。"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$WORKER_DRAIN_FILE"
  python3 "$WORKER_GUARD" drain --root "$ROOT_DIR" --port "$PORT"
  python3 "$WORKER_GUARD" wait --root "$ROOT_DIR" --port "$PORT"
  bash "$ROOT_DIR/stop.sh" "$PORT"
  rm -f "$WORKER_DRAIN_FILE" "$RUNTIME_INSTANCE_FILE"
  EXISTING_PID=""
  RUNTIME_INSTANCE_ID=""
elif [[ -f "$WORKER_DRAIN_FILE" ]]; then
  echo "[ERROR] 当前 worker 池处于排空状态，普通 start 不会补建新槽位。"
  echo "[TIP] 等待任务完成后执行 ./start.sh --drain-restart --port $PORT。"
  exit 4
fi

if [[ -n "$EXISTING_PID" ]] \
  && [[ "$EXISTING_PID" =~ ^[0-9]+$ ]] \
  && kill -0 "$EXISTING_PID" >/dev/null 2>&1 \
  && curl -fsS "http://$HOST:$PORT$HEALTH_PATH" >/dev/null 2>&1; then
  if [[ -z "$RUNTIME_INSTANCE_ID" ]] \
    || ! python3 "$RUNTIME_IDENTITY" matches \
      --pid "$EXISTING_PID" \
      --role api \
      --port "$PORT" \
      --instance-id "$RUNTIME_INSTANCE_ID" \
      --source-revision "$CURRENT_SOURCE_REVISION" \
      --worker-count "$WORKER_COUNT"; then
    echo "[ERROR] 运行中的 API 与当前源码身份不同；拒绝构建新前端或混入新 worker。"
    echo "[TIP] 使用 ./start.sh --drain-restart --port $PORT 无损成对重启。"
    exit 4
  fi
  if ! EXISTING_POOL_STATUS="$(python3 "$WORKER_GUARD" pool-status \
    --root "$ROOT_DIR" \
    --port "$PORT" \
    --count "$WORKER_COUNT" \
    --instance-id "$RUNTIME_INSTANCE_ID" \
    --source-revision "$CURRENT_SOURCE_REVISION")"; then
    echo "[ERROR] 运行中的 worker 不属于当前 API 的同一源码/实例，拒绝混跑。"
    echo "[TIP] 使用 ./start.sh --drain-restart --port $PORT 无损成对重启。"
    exit 4
  fi
  if ! printf '%s\n' "$EXISTING_POOL_STATUS" \
    | grep -Fq "WORKER_POOL_MISSING_SLOTS: none"; then
    echo "[ERROR] 运行中的 API 缺少 worker 槽位；普通 start 不会用当前 shell 配置补槽。"
    echo "[TIP] 使用 ./start.sh --drain-restart --port $PORT 无损成对重启。"
    exit 4
  fi
  EXISTING_API_HEALTHY="true"
  echo "[INFO] 服务已经在运行，API 与 $WORKER_COUNT 个 worker 槽位身份一致。"
  echo "PID: $EXISTING_PID"
  echo "URL: http://$HOST:$PORT"
  echo "LOG: $LOG_FILE"
  exit 0
elif [[ -n "$EXISTING_PID" ]] \
  && [[ "$EXISTING_PID" =~ ^[0-9]+$ ]] \
  && kill -0 "$EXISTING_PID" >/dev/null 2>&1; then
  echo "[ERROR] 已跟踪 API 仍在运行但 readiness 失败；普通 start 不会直接终止它。"
  echo "[TIP] 先检查日志，或使用 ./start.sh --drain-restart --port $PORT。"
  exit 4
fi

if [[ "$EXISTING_API_HEALTHY" != "true" ]]; then
  RUNTIME_INSTANCE_ID="runtime-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "[INFO] 未找到 .venv，正在创建虚拟环境..."
  python3 -m venv .venv
fi

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "[ERROR] React/Vite 前端需要 Node.js >= 22.12 与 npm。"
  echo "[TIP] 安装 Node.js 后重新执行；本地仍不需要 Docker。"
  exit 1
fi
if ! node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 22 || (major === 22 && minor >= 12) ? 0 : 1)'; then
  echo "[ERROR] 当前 Node.js 版本过低；React/Vite 前端需要 Node.js >= 22.12。"
  exit 1
fi

FRONTEND_LOCK_FILE="$FRONTEND_DIR/package-lock.json"
FRONTEND_STAMP="$FRONTEND_DIR/node_modules/.reqpilotx-lock.sha256"
FRONTEND_LOCK_HASH="$(
  .venv/bin/python -c \
    'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$FRONTEND_LOCK_FILE"
)"
INSTALLED_FRONTEND_HASH="$(cat "$FRONTEND_STAMP" 2>/dev/null || true)"
if [[ ! -d "$FRONTEND_DIR/node_modules" || "$INSTALLED_FRONTEND_HASH" != "$FRONTEND_LOCK_HASH" ]]; then
  echo "[INFO] 正在同步 React/Vite 前端依赖..."
  npm --prefix "$FRONTEND_DIR" ci --no-audit --no-fund
  printf '%s\n' "$FRONTEND_LOCK_HASH" > "$FRONTEND_STAMP"
fi

FRONTEND_ENTRY="$FRONTEND_DIR/dist/index.html"
FRONTEND_BUILD_SOURCE="$(
  find \
    "$FRONTEND_DIR/src" \
    "$FRONTEND_DIR/index.html" \
    "$FRONTEND_DIR/package.json" \
    "$FRONTEND_DIR/package-lock.json" \
    "$FRONTEND_DIR/tsconfig.json" \
    "$FRONTEND_DIR/vite.config.ts" \
    -type f -newer "$FRONTEND_ENTRY" -print -quit 2>/dev/null || true
)"
if [[ ! -f "$FRONTEND_ENTRY" || -n "$FRONTEND_BUILD_SOURCE" ]]; then
  echo "[INFO] 正在构建 React/Vite 正式前端..."
  npm --prefix "$FRONTEND_DIR" run build
fi

REQUIREMENTS_FILE="$ROOT_DIR/requirements.txt"
REQUIREMENTS_STAMP="$ROOT_DIR/.venv/.reqpilotx-requirements.sha256"
REQUIREMENTS_HASH="$(
  .venv/bin/python -c \
    'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$REQUIREMENTS_FILE"
)"
INSTALLED_REQUIREMENTS_HASH="$(cat "$REQUIREMENTS_STAMP" 2>/dev/null || true)"
RUNTIME_IMPORTS_READY="true"
if ! .venv/bin/python -c \
  'import alibabacloud_oss_v2, fastapi, psycopg, uvicorn' \
  >/dev/null 2>&1; then
  RUNTIME_IMPORTS_READY="false"
fi

if [[ "$INSTALLED_REQUIREMENTS_HASH" != "$REQUIREMENTS_HASH" || "$RUNTIME_IMPORTS_READY" != "true" ]]; then
  echo "[INFO] requirements.txt 已变化或正式运行依赖不完整，正在同步虚拟环境..."
  .venv/bin/pip install -r requirements.txt
  REQUIREMENTS_STAMP_TMP="$REQUIREMENTS_STAMP.$$.tmp"
  printf '%s\n' "$REQUIREMENTS_HASH" > "$REQUIREMENTS_STAMP_TMP"
  mv "$REQUIREMENTS_STAMP_TMP" "$REQUIREMENTS_STAMP"
fi

# A missing slot may be supplemented only when every observed worker already
# matches the exact API instance and source revision. Legacy/orphan/mismatched
# processes require cooperative drain; ordinary start never mixes versions.
if ! python3 "$WORKER_GUARD" pool-status \
  --root "$ROOT_DIR" \
  --port "$PORT" \
  --count "$WORKER_COUNT" \
  --instance-id "$RUNTIME_INSTANCE_ID" \
  --source-revision "$CURRENT_SOURCE_REVISION" \
  >/dev/null; then
  echo "[ERROR] 发现不属于本次运行身份的 worker；拒绝直接清理或补槽。"
  echo "[TIP] 使用 ./start.sh --drain-restart --port $PORT。"
  exit 4
fi
for slot in $(seq 1 "$WORKER_COUNT"); do
  worker_pid="$(tracked_worker_pid "$slot")"
  if [[ -n "$worker_pid" ]] && ! kill -0 "$worker_pid" >/dev/null 2>&1; then
    rm -f \
      "$(worker_pid_file "$slot")" \
      "$(worker_session_file "$slot")"
  fi
done
rm -f "$LOG_DIR/worker_${PORT}.pid" "$LOG_DIR/worker_${PORT}.screen"

echo "[INFO] 校验原生 PostgreSQL 并应用版本化迁移..."
if ! REQPILOT_PROCESS_ROLE=api .venv/bin/python scripts/runtime_admin.py migrate; then
  echo "[ERROR] ReqPilotX 目标运行时需要本机 PostgreSQL，且不会回退到 SQLite。"
  echo "[TIP] 请安装并启动 Postgres.app、Homebrew PostgreSQL 或 PostgreSQL 官方版本，"
  echo "      创建数据库 reqpilotx；也可通过 REQPILOT_DATABASE_URL 指向其他本机数据库。"
  echo "[TIP] 本地不需要 Docker，也不会连接 RDS/OSS，除非显式设置 REQPILOT_ENV=cloud。"
  exit 3
fi

# A configured loopback Qdrant belongs to the same one-click local runtime.
# It remains optional for core readiness: a missing binary or failed vector
# backend is reported truthfully while API, review and export can still start.
if QDRANT_START_RESULT="$(
  .venv/bin/python "$LOCAL_QDRANT_CLI" ensure 2>&1
)"; then
  echo "[INFO] Qdrant lifecycle: $QDRANT_START_RESULT"
else
  echo "[WARN] 已配置本地 Qdrant，但自动启动未完成。"
  echo "$QDRANT_START_RESULT"
fi

rm -f "$PID_FILE"

if lsof -ti TCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[ERROR] 端口 $PORT 已被占用。"
  echo "[TIP] 请先执行: ./stop.sh $PORT"
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN || true
  exit 1
fi

if [[ "$MODE" == "dev" ]]; then
  printf '%s\n' "$RUNTIME_INSTANCE_ID" > "$RUNTIME_INSTANCE_FILE"
  rm -f "$WORKER_DRAIN_FILE"
  echo "[INFO] 启动开发模式: http://$HOST:$PORT"
  echo "[INFO] 模式：Vite HMR + API 前台自动重载 + $WORKER_COUNT 个隔离 worker 槽位"
  DEV_WORKER_PIDS=()
  for slot in $(seq 1 "$WORKER_COUNT"); do
    worker_lane_args=()
    if (( slot > 1 )); then
      worker_lane_args=(
        --accepted-kind document.extract
        --claim-profile local-extraction-no-llm
      )
    fi
    REQPILOT_LOCAL_RUNTIME_PORT="$PORT" \
    REQPILOT_LOCAL_RUNTIME_INSTANCE_ID="$RUNTIME_INSTANCE_ID" \
    REQPILOT_RUNTIME_SOURCE_REVISION="$CURRENT_SOURCE_REVISION" \
    REQPILOT_LOCAL_WORKER_COUNT="$WORKER_COUNT" \
    REQPILOT_LOCAL_WORKER_SLOT="$slot" REQPILOT_PROCESS_ROLE=worker \
      .venv/bin/python -m app.worker "${worker_lane_args[@]}" \
      >"$(worker_log_file "$slot")" 2>&1 &
    worker_pid="$!"
    DEV_WORKER_PIDS+=("$worker_pid")
    echo "$worker_pid" > "$(worker_pid_file "$slot")"
  done
  (
    cd "$FRONTEND_DIR"
    REQPILOT_API_ORIGIN="http://127.0.0.1:$PORT" \
      exec "$FRONTEND_DIR/node_modules/.bin/vite"
  ) >"$FRONTEND_LOG_FILE" 2>&1 &
  FRONTEND_PID="$!"
  echo "$FRONTEND_PID" > "$FRONTEND_PID_FILE"
  cleanup_dev_worker() {
    for worker_pid in "${DEV_WORKER_PIDS[@]}"; do
      kill "$worker_pid" >/dev/null 2>&1 || true
    done
    for worker_pid in "${DEV_WORKER_PIDS[@]}"; do
      wait "$worker_pid" >/dev/null 2>&1 || true
    done
    kill "$FRONTEND_PID" >/dev/null 2>&1 || true
    rm -f "$FRONTEND_PID_FILE" "$RUNTIME_INSTANCE_FILE"
    for slot in $(seq 1 "$WORKER_COUNT"); do
      rm -f "$(worker_pid_file "$slot")" "$(worker_session_file "$slot")"
    done
  }
  trap cleanup_dev_worker EXIT INT TERM
  echo "[INFO] 前端入口: http://127.0.0.1:5173"
  REQPILOT_PROCESS_ROLE=api \
    REQPILOT_LOCAL_RUNTIME_PORT="$PORT" \
    REQPILOT_LOCAL_RUNTIME_INSTANCE_ID="$RUNTIME_INSTANCE_ID" \
    REQPILOT_RUNTIME_SOURCE_REVISION="$CURRENT_SOURCE_REVISION" \
    REQPILOT_LOCAL_WORKER_COUNT="$WORKER_COUNT" \
    .venv/bin/uvicorn "$APP_MODULE" \
    --reload --host "$HOST" --port "$PORT"
  exit $?
fi

echo "[INFO] 启动稳定后台 API + $WORKER_COUNT 个 worker 槽位: http://$HOST:$PORT"
printf '%s\n' "$RUNTIME_INSTANCE_ID" > "$RUNTIME_INSTANCE_FILE"
rm -f "$WORKER_DRAIN_FILE"
if command -v screen >/dev/null 2>&1; then
  screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
  for slot in $(seq 1 "$WORKER_COUNT"); do
    start_stable_worker "$slot"
  done
  ROOT_DIR="$ROOT_DIR" APP_MODULE="$APP_MODULE" HOST="$HOST" PORT="$PORT" LOG_FILE="$LOG_FILE" \
    REQPILOT_LOCAL_RUNTIME_PORT="$PORT" \
    REQPILOT_LOCAL_RUNTIME_INSTANCE_ID="$RUNTIME_INSTANCE_ID" \
    REQPILOT_RUNTIME_SOURCE_REVISION="$CURRENT_SOURCE_REVISION" \
    REQPILOT_LOCAL_WORKER_COUNT="$WORKER_COUNT" \
    screen -dmS "$SCREEN_NAME" bash -lc 'cd "$ROOT_DIR" && export REQPILOT_PROCESS_ROLE=api REQPILOT_LOCAL_RUNTIME_PORT REQPILOT_LOCAL_RUNTIME_INSTANCE_ID REQPILOT_RUNTIME_SOURCE_REVISION REQPILOT_LOCAL_WORKER_COUNT && exec .venv/bin/uvicorn "$APP_MODULE" --host "$HOST" --port "$PORT" >> "$LOG_FILE" 2>&1'
  echo "$SCREEN_NAME" > "$SESSION_FILE"
else
  for slot in $(seq 1 "$WORKER_COUNT"); do
    start_stable_worker "$slot"
  done
  REQPILOT_PROCESS_ROLE=api \
    REQPILOT_LOCAL_RUNTIME_PORT="$PORT" \
    REQPILOT_LOCAL_RUNTIME_INSTANCE_ID="$RUNTIME_INSTANCE_ID" \
    REQPILOT_RUNTIME_SOURCE_REVISION="$CURRENT_SOURCE_REVISION" \
    REQPILOT_LOCAL_WORKER_COUNT="$WORKER_COUNT" \
    nohup .venv/bin/uvicorn "$APP_MODULE" \
    --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 < /dev/null &
  echo "nohup" > "$SESSION_FILE"
  PID="$!"
  echo "$PID" > "$PID_FILE"
fi

for slot in $(seq 1 "$WORKER_COUNT"); do
  if ! wait_for_stable_worker "$slot"; then
    echo "[ERROR] durable worker 槽位 $slot 启动失败。"
    tail -n 80 "$(worker_log_file "$slot")" || true
    cleanup_local_workers
    exit 1
  fi
done

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
  cleanup_local_workers
  if command -v screen >/dev/null 2>&1; then
    screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
    for slot in $(seq 1 "$WORKER_COUNT"); do
      screen -S "$(worker_screen_name "$slot")" -X quit >/dev/null 2>&1 || true
    done
  fi
  tail -n 80 "$LOG_FILE" || true
  rm -f "$PID_FILE" "$SESSION_FILE" "$RUNTIME_INSTANCE_FILE"
  for slot in $(seq 1 "$WORKER_COUNT"); do
    rm -f "$(worker_pid_file "$slot")" "$(worker_session_file "$slot")"
  done
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
echo "项目管理: http://$HOST:$PORT/workspace?task=projects"
echo "AI 辅助阅读: http://$HOST:$PORT/workspace?task=reading"
echo "需求提取: http://$HOST:$PORT/workspace?task=extraction&mode=organize"
echo "需求分析: http://$HOST:$PORT/workspace?task=analysis"
echo "矩阵导出: http://$HOST:$PORT/workspace?task=export"
echo "API 日志: $LOG_FILE"
for slot in $(seq 1 "$WORKER_COUNT"); do
  echo "Worker $slot 日志: $(worker_log_file "$slot")"
done
