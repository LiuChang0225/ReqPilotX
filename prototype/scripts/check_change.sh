#!/usr/bin/env bash
set -euo pipefail

PROTOTYPE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROTOTYPE_DIR"

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[ERROR] 缺少 $PYTHON_BIN；请先创建 venv 并安装 requirements.txt。"
  exit 2
fi

echo "[check] Python / React/Vite / shell"
"$PYTHON_BIN" -m compileall -q app
"$PYTHON_BIN" -m py_compile scripts/local_worker_guard.py
if [[ ! -d frontend/node_modules ]]; then
  npm --prefix frontend ci --no-audit --no-fund
fi
npm --prefix frontend run test
npm --prefix frontend run build
bash -n \
  start.sh \
  stop.sh \
  status.sh \
  deploy/aliyun/api-start.sh \
  deploy/aliyun/worker-start.sh \
  deploy/aliyun/migrate.sh

echo "[check] target runtime and public-route contracts"
"$PYTHON_BIN" -m unittest -v \
  tests.test_local_worker_guard \
  tests.test_target_runtime_foundation \
  tests.test_local_runtime_security.LocalRuntimeSecurityTests.test_cors_preflight_allows_only_explicit_loopback_origin \
  tests.test_local_runtime_security.LocalRuntimeSecurityTests.test_http_boundary_rejects_untrusted_host_before_routing \
  tests.test_document_ingestion_boundary.DocumentIngestionBoundaryTests.test_preflight_only_accepts_xlsx_without_invoking_compatibility_parser \
  tests.test_unified_workspace.UnifiedWorkspaceTests.test_removed_preview_routes_are_not_registered \
  tests.test_content_preparation_frontend \
  tests.test_workspace_management_frontend

echo "[OK] 提交级快检通过。完整 corpus、模型评测、容量与恢复演练不属于此命令。"
