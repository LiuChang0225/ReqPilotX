#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROTOTYPE_DIR = Path(__file__).resolve().parents[1]
if str(PROTOTYPE_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_DIR))

from app.application import runtime
from app.application.agent_workflow.native_structure_baseline_jobs import (
    _current_reference_context,
)
from app.application.agent_workflow.native_structure_bridge import (
    _verified_projection_operations,
    materialize_native_structure_table_preview,
)
from app.application.agent_workflow.native_structure_working_revision_replay import (
    replay_accepted_native_structure_revisions,
)
from app.infrastructure.runtime_container import build_runtime_container
from app.infrastructure.runtime_settings import RuntimeSettings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replay the current Host-accepted native structure through immutable "
            "working revisions. This is a transport proof and never invokes PDF "
            "parsing or an Agent."
        )
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument(
        "--through-revision",
        type=int,
        choices=(1, 2, 3),
        required=True,
    )
    args = parser.parse_args()

    settings = RuntimeSettings.from_environment(prototype_dir=PROTOTYPE_DIR)
    container = build_runtime_container(settings, apply_migrations=True)
    try:
        runtime.configure_worker_runtime(container)
        if container.agent_structure_loop_repository is None:
            raise RuntimeError(
                "Working revision replay requires PostgreSQL persistence."
            )
        current = _current_reference_context(args.project_id, args.document_id)
        operations = _verified_projection_operations(current["handoff_attempts"])
        exact_table_previews = tuple(
            materialize_native_structure_table_preview(
                bundle=current["bundle"],
                source_sha256=current["source_sha256"],
                scope_id=str(item["scope_id"]),
                projection_operations=operations,
            )
            for item in current["public_reference"]["logical_tables"]
        )
        result = replay_accepted_native_structure_revisions(
            project_id=args.project_id,
            document_id=args.document_id,
            accepted_public_reference=current["public_reference"],
            table_previews=exact_table_previews,
            through_revision=args.through_revision,
            repository=container.agent_structure_loop_repository,
            object_store=container.object_store,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        container.close()


if __name__ == "__main__":
    raise SystemExit(main())
