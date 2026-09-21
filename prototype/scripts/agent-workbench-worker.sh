#!/bin/bash
set -euo pipefail

prototype_dir="$(cd "$(dirname "$0")/.." && pwd)"
repo_dir="$(cd "$prototype_dir/.." && pwd)"
worker_pid_file="$prototype_dir/runtime-data/agent-workbench-worker.pid"
worker_cli="$prototype_dir/scripts/agent_workflow.py"
worker_python="$prototype_dir/.venv/bin/python"
worker_pid=""
if [[ -f "$worker_pid_file" ]]; then
  worker_pid="$(cat "$worker_pid_file")"
fi
worker_running=false
if [[ "$worker_pid" =~ ^[0-9]+$ ]] && kill -0 "$worker_pid" 2>/dev/null; then
  worker_command="$(ps -p "$worker_pid" -o command=)"
  if [[ "$worker_command" == *"$worker_cli serve"* ]]; then
    worker_running=true
  fi
fi
if [[ "${1:-start}" == "stop" ]]; then
  if $worker_running; then
    kill -TERM "$worker_pid"
    echo "DRAIN_REQUESTED $worker_pid"
  else
    echo "NOT_RUNNING"
  fi
  exit 0
fi
if [[ "${1:-start}" != "start" ]]; then
  echo "Usage: agent-workbench-worker.sh start|stop" >&2
  exit 2
fi
if $worker_running; then
  echo "ALREADY_RUNNING $worker_pid"
  exit 0
fi
mkdir -p "$prototype_dir/runtime-data"
cd "$prototype_dir"
export REQPILOT_CODEX_AUTH_STORE="${REQPILOT_CODEX_AUTH_STORE:-file}"
export REQPILOT_AGENT_CUSTOMER_DATA_ENABLED="${REQPILOT_AGENT_CUSTOMER_DATA_ENABLED:-1}"
export PYTHONPATH="$repo_dir/packages/engine/src:$repo_dir/apps/server/src:$prototype_dir${PYTHONPATH:+:$PYTHONPATH}"
export REQPILOT_STORAGE_DIR="${REQPILOT_STORAGE_DIR:-$prototype_dir/storage}"
export REQPILOT_FRONTEND_DIR="${REQPILOT_FRONTEND_DIR:-$prototype_dir/frontend/dist}"
echo "$$" > "$worker_pid_file"
exec "$worker_python" "$worker_cli" serve
