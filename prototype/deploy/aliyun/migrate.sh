#!/usr/bin/env bash
set -euo pipefail

export REQPILOT_ENV=cloud
export REQPILOT_PROCESS_ROLE=api

exec python scripts/runtime_admin.py migrate
