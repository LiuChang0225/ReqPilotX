"""Test-only product module origin receipts for isolated release verification."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def product_module_origins() -> dict[str, list[str]]:
    result = {}
    for name, module in sorted(sys.modules.items()):
        if name == "reqpilotx" or name.startswith("reqpilotx.") or name == "reqpilotx_engine" or name.startswith("reqpilotx_engine."):
            source = getattr(module, "__file__", None)
            paths = [str(Path(source).resolve())] if source else [str(Path(item).resolve()) for item in getattr(module, "__path__", [])]
            result[name] = paths
    return result


def write_test_origin_receipt(component: str) -> None:
    if os.environ.get("REQPILOT_TEST_MODE") != "1":
        return
    directory_value = os.environ.get("REQPILOT_TEST_ORIGIN_DIR")
    allowed_value = os.environ.get("REQPILOT_TEST_ALLOWED_PRODUCT_ROOT")
    if not directory_value or not allowed_value:
        return
    try:
        directory = Path(directory_value).expanduser().resolve(strict=True)
        allowed = Path(allowed_value).expanduser().resolve(strict=True)
        origins = product_module_origins()
        outside = {
            name: paths for name, paths in origins.items()
            if not paths or any(not Path(source).is_relative_to(allowed) for source in paths)
        }
        receipt = {
            "schema_version": "reqpilotx-test-module-origins/v1",
            "component": component,
            "pid": os.getpid(),
            "allowed_product_root": str(allowed),
            "module_origins": origins,
            "outside_allowed_root": outside,
            "argv": sys.argv,
        }
        path = directory / f"{component}-{os.getpid()}.json"
        path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"ReqPilotX test origin receipt failed: {exc}", file=sys.stderr)
