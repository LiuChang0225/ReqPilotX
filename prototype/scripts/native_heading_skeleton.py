#!/usr/bin/env python3
"""Build a private, source-bound numbered-heading skeleton from native facts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROTOTYPE_ROOT.parent
if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from app.adapters.agents.document_evidence_host import (  # noqa: E402
    prepare_document_evidence,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit  # noqa: E402
from app.adapters.agents.stage_context import compile_agent_stage_context  # noqa: E402
from app.application.agent_workflow.extraction_runtime import (  # noqa: E402
    AgentExtractionStage,
)
from app.application.agent_workflow.native_heading_skeleton import (  # noqa: E402
    NativeHeadingCandidateState,
    NativeHeadingScopeKind,
    build_native_heading_skeleton,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--authorized-corpus-root", type=Path, required=True)
    parser.add_argument("--document-role", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    source = args.source.expanduser().resolve(strict=True)
    corpus = args.authorized_corpus_root.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve(strict=False)
    if args.source.is_symlink() or not source.is_file():
        raise SystemExit("source must be a regular non-symlink file")
    try:
        source.relative_to(corpus)
    except ValueError as exc:
        raise SystemExit("source is outside the authorized corpus") from exc
    try:
        output.relative_to(corpus)
    except ValueError:
        pass
    else:
        raise SystemExit("output must remain outside the authorized source corpus")
    if output.exists() or output.is_symlink():
        raise SystemExit("output already exists; heading skeletons are immutable")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.parent.is_symlink():
        raise SystemExit("output parent must not be a symlink")

    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    kit = load_portable_agent_kit(REPOSITORY_ROOT / "agent-kit")
    stage_context = compile_agent_stage_context(
        kit,
        AgentExtractionStage.DOCUMENT_UNDERSTANDING,
    )
    prepared = prepare_document_evidence(
        source,
        expected_source_sha256=source_sha256,
        document_role=args.document_role,
        kit_identity=kit.identity,
        stage_context_sha256=stage_context.context_sha256,
        run_id="agent-run-" + uuid4().hex,
        opaque_source_handle="source-handle-" + uuid4().hex,
    )
    artifact = build_native_heading_skeleton(prepared.source_ir)
    output.write_text(
        artifact.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        "schema_version": "native-heading-skeleton-summary/v1",
        "source_sha256": artifact.source_sha256,
        "source_ir_status": prepared.source_ir.status.value,
        "source_node_count": len(prepared.source_ir.nodes),
        "physical_table_count": len(prepared.tables_by_id),
        "candidate_count": len(artifact.candidates),
        "proposed_candidate_count": sum(
            item.state is NativeHeadingCandidateState.PROPOSED
            for item in artifact.candidates
        ),
        "review_required_candidate_count": len(
            artifact.review_required_candidate_ids
        ),
        "chapter_scope_count": sum(
            item.kind is NativeHeadingScopeKind.CHAPTER for item in artifact.scopes
        ),
        "section_scope_count": sum(
            item.kind is NativeHeadingScopeKind.SECTION for item in artifact.scopes
        ),
        "preface_scope_count": sum(
            item.kind is NativeHeadingScopeKind.PREFACE for item in artifact.scopes
        ),
        "max_scope_candidate_count": max(
            len(item.candidate_ids) for item in artifact.scopes
        ),
        "artifact_sha256": artifact.artifact_sha256,
        "output": str(output),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
