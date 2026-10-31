from __future__ import annotations

# ruff: noqa: E402

import argparse
import json
import shutil
import sys
from pathlib import Path


PROTOTYPE_DIR = Path(__file__).resolve().parents[1]
if str(PROTOTYPE_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_DIR))

from app.infrastructure.postgres import PostgresUnavailableError
from app.infrastructure.runtime_container import build_runtime_container
from app.infrastructure.runtime_settings import RuntimeConfigurationError, RuntimeSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or initialize the ReqPilotX target runtime."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor", help="Check local/cloud configuration without writing.")
    subcommands.add_parser("migrate", help="Apply versioned SQLite or PostgreSQL migrations.")
    recover = subcommands.add_parser(
        "recover-historical-publication",
        help=(
            "Claim one exact pre-lease PUBLISHING draft after all publishers "
            "have been quiesced."
        ),
    )
    recover.add_argument("--project-id", required=True)
    recover.add_argument("--document-id", required=True)
    recover.add_argument("--requirement-set-id", required=True)
    recover.add_argument("--publication-key", required=True)
    recover.add_argument(
        "--confirm",
        required=True,
        help=(
            "Required after stopping both API and worker: "
            "RECOVER_HISTORICAL_EMPTY_PUBLICATION_LEASE"
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        settings = RuntimeSettings.from_environment(prototype_dir=PROTOTYPE_DIR)
    except RuntimeConfigurationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    if args.command == "doctor":
        payload = {
            "ok": True,
            "deployment_mode": settings.deployment_mode.value,
            "process_role": settings.process_role.value,
            "database_backend": settings.database_backend.value,
            "database_configured": bool(settings.database_url),
            "psql_available": shutil.which("psql") is not None,
            "object_store_backend": settings.object_store_backend.value,
            "object_root": (
                str(settings.object_root)
                if settings.object_store_backend.value == "local"
                else ""
            ),
            "oss_region_configured": bool(settings.oss_region),
            "oss_bucket_configured": bool(settings.oss_bucket),
            "allowed_host_count": len(settings.allowed_hosts),
            "allowed_origin_count": len(settings.allowed_origins),
            "docker_required": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "recover-historical-publication":
        # Runtime construction applies migrations before the recovery CAS. The
        # service and worker must be stopped first; the exact confirmation is
        # intentionally not offered as a boolean convenience flag.
        from app import db

        container = None
        try:
            container = build_runtime_container(settings, apply_migrations=True)
            db.configure_runtime(settings, postgres_database=container.database)
            recovered = db.recover_historical_requirement_set_publication(
                args.project_id,
                args.document_id,
                args.requirement_set_id,
                publication_key=args.publication_key,
                operator_confirmation=args.confirm,
            )
            if not recovered:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": (
                                "The exact historical empty-lease publication "
                                "was not eligible for recovery."
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
                return 4
            print(
                json.dumps(
                    {
                        "ok": True,
                        "project_id": args.project_id,
                        "document_id": args.document_id,
                        "requirement_set_id": args.requirement_set_id,
                        "publication_key": args.publication_key,
                        "state": "EXPIRED_FENCE_READY_FOR_NORMAL_TAKEOVER",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        except (PostgresUnavailableError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 3
        finally:
            db.close_runtime_database()
            if container is not None:
                container.close()

    if settings.is_sqlite:
        from app import db

        try:
            db.init_db()
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 3
        print(
            json.dumps(
                {
                    "ok": True,
                    "database_backend": settings.database_backend.value,
                    "migrations": "applied",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    try:
        container = build_runtime_container(settings, apply_migrations=True)
    except PostgresUnavailableError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 3
    try:
        container.database.healthcheck()
        print(
            json.dumps(
                {
                    "ok": True,
                    "database_backend": "postgresql",
                    "migrations": "applied",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    finally:
        container.close()


if __name__ == "__main__":
    raise SystemExit(main())
