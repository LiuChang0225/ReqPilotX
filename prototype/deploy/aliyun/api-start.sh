#!/usr/bin/env bash
set -euo pipefail

export REQPILOT_ENV=cloud
export REQPILOT_PROCESS_ROLE=api

PORT="${SERVER_PORT:-8000}"
WORKERS="${WEB_CONCURRENCY:-2}"

exec python -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers "$WORKERS" \
  --proxy-headers \
  --forwarded-allow-ips="${REQPILOT_TRUSTED_PROXY_IPS:-127.0.0.1}"
