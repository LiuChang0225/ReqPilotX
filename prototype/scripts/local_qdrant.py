from __future__ import annotations

# ruff: noqa: E402

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path

PROTOTYPE_DIR = Path(__file__).resolve().parents[1]
if str(PROTOTYPE_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_DIR))

from app.infrastructure.local_qdrant import (
    LocalQdrantConfigurationError,
    LocalQdrantError,
    LocalQdrantResult,
    configured_qdrant_url,
    ensure_local_qdrant_running,
    get_local_qdrant_status,
    stop_owned_local_qdrant,
)
from app.runtime_paths import resolve_storage_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely manage the optional ReqPilotX loopback Qdrant process."
    )
    parser.add_argument("--storage-dir", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    ensure = subparsers.add_parser("ensure")
    ensure.add_argument("--url")
    ensure.add_argument("--timeout-seconds", type=float, default=10.0)
    status = subparsers.add_parser("status")
    status.add_argument("--url")
    subparsers.add_parser("stop-owned")
    return parser


def _render(result: LocalQdrantResult) -> None:
    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    storage_dir = (
        args.storage_dir.expanduser().resolve(strict=False)
        if args.storage_dir is not None
        else resolve_storage_dir(PROTOTYPE_DIR).expanduser().resolve(strict=False)
    )
    if args.command == "stop-owned":
        _render(stop_owned_local_qdrant(storage_dir))
        return 0
    url = str(args.url or configured_qdrant_url(storage_dir)).strip()
    if args.command == "status":
        _render(get_local_qdrant_status(storage_dir, url))
        return 0
    _render(
        ensure_local_qdrant_running(
            storage_dir,
            url,
            timeout_seconds=args.timeout_seconds,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LocalQdrantConfigurationError, LocalQdrantError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
