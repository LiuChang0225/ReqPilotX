#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from app.adapters.agents.portable_kit import (  # noqa: E402
    AgentKitLockV1,
    compute_agent_kit_lock,
)


def _serialized(lock: AgentKitLockV1) -> str:
    return lock.model_dump_json(indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--kit-root",
        type=Path,
        default=PROTOTYPE_ROOT.parent / "agent-kit",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    kit_root = args.kit_root.resolve()
    payload = _serialized(compute_agent_kit_lock(kit_root))
    lock_path = kit_root / "agent-kit.lock.json"
    if args.check:
        if not lock_path.is_file() or lock_path.read_text(encoding="utf-8") != payload:
            raise SystemExit("Agent Kit lock drifted")
        return 0
    lock_path.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
