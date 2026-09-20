#!/usr/bin/env bash
set -euo pipefail

export REQPILOT_ENV=cloud
export REQPILOT_PROCESS_ROLE=worker

exec python -m app.worker \
  --lease-seconds "${REQPILOT_JOB_LEASE_SECONDS:-120}" \
  --poll-seconds "${REQPILOT_JOB_POLL_SECONDS:-1}"
