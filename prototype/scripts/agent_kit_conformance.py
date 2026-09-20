#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from app.adapters.agents.agent_kit_materializer import (  # noqa: E402
    materialize_agent_kit,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--kit-root",
        type=Path,
        default=PROTOTYPE_ROOT.parent / "agent-kit",
    )
    args = parser.parse_args()
    kit = load_portable_agent_kit(args.kit_root)
    with tempfile.TemporaryDirectory(prefix="reqpilotx-agent-kit-") as raw:
        base = Path(raw)
        desktop = materialize_agent_kit(
            kit,
            base / "desktop-reference",
            runner_profile_id="desktop-reference",
        )
        product = materialize_agent_kit(
            kit,
            base / "app-server-product",
            runner_profile_id="app-server-product",
        )
        payload = {
            "schema_version": "reqpilotx-agent-kit-conformance/v1",
            "kit_id": kit.identity.kit_id,
            "kit_version": kit.identity.kit_version,
            "bundle_sha256": kit.identity.bundle_sha256,
            "desktop_profile_sha256": desktop.runner_profile_sha256,
            "app_server_profile_sha256": product.runner_profile_sha256,
            "same_bundle": desktop.identity == product.identity == kit.identity,
            "desktop_skill_count": len(desktop.runner_profile.skill_ids),
            "app_server_skill_count": len(product.runner_profile.skill_ids),
            "quality_gate_eligible": (
                desktop.runner_profile.quality_gate_eligible
                and product.runner_profile.quality_gate_eligible
            ),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
