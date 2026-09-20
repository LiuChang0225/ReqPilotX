from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROTOTYPE_DIR = Path(__file__).resolve().parents[1]
if str(PROTOTYPE_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_DIR))

from app.application.cutover.sqlite_to_postgres import (
    CutoverRejected,
    build_cutover_plan,
    execute_cutover,
)
from app.infrastructure.runtime_container import build_runtime_container
from app.infrastructure.runtime_settings import (
    DatabaseBackend,
    RuntimeConfigurationError,
    RuntimeSettings,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or execute the one-way SQLite to PostgreSQL cutover."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write verified objects and rows. Without this flag the command is read-only.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        plan = build_cutover_plan(args.source)
        if not args.apply:
            print(
                json.dumps(
                    {"ok": True, "mode": "plan", **plan.public_summary()},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        settings = RuntimeSettings.from_environment(prototype_dir=PROTOTYPE_DIR)
        if settings.database_backend is not DatabaseBackend.POSTGRESQL:
            raise CutoverRejected("Cutover apply requires the PostgreSQL runtime.")
        container = build_runtime_container(settings, apply_migrations=True)
        try:
            if container.database is None:
                raise CutoverRejected("PostgreSQL is not available.")
            result = execute_cutover(
                plan=plan,
                database=container.database,
                object_store=container.object_store,
                target_environment=settings.deployment_mode.value,
            )
        finally:
            container.close()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (CutoverRejected, RuntimeConfigurationError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
