#!/usr/bin/env python3
"""Build, register, and inspect immutable native-loop plugin capabilities."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "prototype"))
sys.path.insert(0, str(REPO / "plugins/reqpilot/scripts"))

from app.application.agent_workflow.native_loop_capabilities import (  # noqa: E402
    _registered_bundle_path,
    _verify_bundle,
    register_native_loop_capability_bundle,
)
def _configuration(registry_root: Path, bundle_sha256: str) -> dict[str, str]:
    return {
        "REQPILOT_NATIVE_CAPABILITY_MODE": "registered-release",
        "REQPILOT_NATIVE_CAPABILITY_REGISTRY": str(registry_root.resolve()),
        "REQPILOT_NATIVE_CAPABILITY_BUNDLE_SHA256": bundle_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser(
        "build-register", help="Build a full R1-wheel bundle and register it"
    )
    prepare.add_argument("--repo", type=Path, default=REPO)
    prepare.add_argument("--engine-wheel", type=Path, required=True)
    prepare.add_argument("--bundle-output", type=Path, required=True)
    prepare.add_argument("--registry-root", type=Path, required=True)

    register = commands.add_parser(
        "register", help="Register an already-built complete plugin bundle"
    )
    register.add_argument("--bundle", type=Path, required=True)
    register.add_argument("--registry-root", type=Path, required=True)
    register.add_argument("--expected-bundle-sha256")

    inspect = commands.add_parser(
        "inspect", help="Verify one exact registered release identity"
    )
    inspect.add_argument("--registry-root", type=Path, required=True)
    inspect.add_argument("--bundle-sha256", required=True)

    args = parser.parse_args()
    try:
        if args.command == "build-register":
            from build_bundle import build as build_bundle

            build_bundle(
                args.repo,
                args.bundle_output,
                engine_wheel=args.engine_wheel,
            )
            registered = register_native_loop_capability_bundle(
                args.bundle_output,
                registry_root=args.registry_root,
                require_engine_release=True,
            )
        elif args.command == "register":
            registered = register_native_loop_capability_bundle(
                args.bundle,
                registry_root=args.registry_root,
                expected=args.expected_bundle_sha256,
                require_engine_release=True,
            )
        else:
            bundle = _registered_bundle_path(
                args.registry_root, args.bundle_sha256
            )
            receipt = _verify_bundle(
                bundle,
                args.bundle_sha256,
                require_engine_release=True,
            )
            registered = {
                "status": "VERIFIED",
                "bundle_sha256": receipt["bundle_sha256"],
                "path": str(bundle.resolve()),
            }
        registered["configuration"] = _configuration(
            args.registry_root, registered["bundle_sha256"]
        )
        print(json.dumps(registered, ensure_ascii=False, indent=2))
        return 0
    except (KeyError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "CAPABILITY_PREPARATION_FAILED", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
