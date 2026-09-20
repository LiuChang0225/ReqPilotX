#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROTOTYPE_ROOT.parent
if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from app.adapters.agents.codex_app_server.canonical_document_session import (  # noqa: E402
    CanonicalProductEvidenceRequest,
    prepare_canonical_app_server_document_session,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (  # noqa: E402
    canonical_sha256,
)
from app.adapters.agents.codex_app_server.canonical_stage_transport import (  # noqa: E402
    LocalCodexAppServerStructuredStageTransport,
)
from app.adapters.agents.codex_app_server.document_map_survey_driver import (  # noqa: E402
    CanonicalDocumentMapSurveyStageDriver,
    DocumentMapSurveyCandidateValidationError,
)
from app.adapters.agents.codex_app_server.lifecycle import (  # noqa: E402
    LifecycleProbeError,
    _canary_runtime_state,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit  # noqa: E402
from app.application.agent_workflow.document_map_exploration import (  # noqa: E402
    DocumentMapVisualCoverageMode,
)
from app.application.agent_workflow.extraction_runtime import (  # noqa: E402
    AgentExtractionStage,
)


CANARY_SCHEMA_VERSION = "reqpilotx-document-map-survey-canary/v1"


def run_document_map_survey_canary(
    *,
    source_path: Path,
    corpus_root: Path,
    document_role: str,
    codex_bin: Path,
    codex_home: Path,
    repository_root: Path,
    timeout_seconds: float,
    visual_mode: DocumentMapVisualCoverageMode,
    private_output: Path | None = None,
) -> dict[str, object]:
    source = source_path.expanduser().resolve(strict=True)
    corpus = corpus_root.expanduser().resolve(strict=True)
    repository = repository_root.expanduser().resolve(strict=True)
    if source_path.is_symlink() or not source.is_file():
        raise LifecycleProbeError("SURVEY_SOURCE_INVALID")
    try:
        source.relative_to(corpus)
    except ValueError as exc:
        raise LifecycleProbeError("SURVEY_SOURCE_UNAUTHORIZED") from exc
    if timeout_seconds <= 0:
        raise LifecycleProbeError("SURVEY_TIMEOUT_INVALID")
    output = private_output.expanduser().resolve() if private_output else None
    if output is not None:
        try:
            output.relative_to(corpus)
        except ValueError:
            pass
        else:
            raise LifecycleProbeError("SURVEY_OUTPUT_INSIDE_SOURCE_CORPUS")

    source_sha256 = _sha256(source)
    started = time.monotonic()
    with _canary_runtime_state(
        codex_bin=codex_bin,
        codex_home=codex_home,
        repository_root=repository,
    ) as base_runtime:
        kit = load_portable_agent_kit(repository / "agent-kit")
        run_id = "agent-run-" + uuid4().hex
        source_handle = "source-handle-" + uuid4().hex
        session = prepare_canonical_app_server_document_session(
            base_runtime=base_runtime,
            canonical_kit_root=repository / "agent-kit",
            stages=(AgentExtractionStage.DOCUMENT_UNDERSTANDING,),
            evidence_request=CanonicalProductEvidenceRequest(
                source_path=source,
                expected_source_sha256=source_sha256,
                document_role=document_role,
                authorized_corpus_root=corpus,
                run_id=run_id,
                opaque_source_handle=source_handle,
            ),
            repository_root=repository,
        )
        try:
            result = CanonicalDocumentMapSurveyStageDriver(
                transport=LocalCodexAppServerStructuredStageTransport(
                    timeout_seconds=timeout_seconds,
                ),
                visual_coverage_mode=visual_mode,
            ).run(
                session,
                task_binding_sha256=canonical_sha256(
                    {
                        "run_id": run_id,
                        "source_sha256": source_sha256,
                        "kit_bundle_sha256": kit.identity.bundle_sha256,
                        "execution_mode": "DOCUMENT_MAP_SURVEY",
                        "visual_mode": visual_mode.value,
                    }
                ),
            )
        finally:
            session.close()

    if output is not None:
        _write_private_result(
            output,
            {
                "candidate": result.candidate.model_dump(mode="json"),
                "visual_coverage": result.visual_coverage.model_dump(mode="json"),
                "compilation": result.compilation.model_dump(mode="json"),
            },
        )
    elapsed_milliseconds = round((time.monotonic() - started) * 1000)
    visual = result.visual_coverage
    activity = result.tool_activity_summary
    unique_item_counts = activity.get("unique_item_counts", {})
    return {
        "schema_version": CANARY_SCHEMA_VERSION,
        "status": "PASS",
        "source_sha256": source_sha256,
        "kit_version": kit.identity.kit_version,
        "kit_bundle_sha256": kit.identity.bundle_sha256,
        "visual_mode": visual.mode.value,
        "contact_sheet_opened": visual.contact_sheet_opened,
        "high_resolution_page_count": len(
            visual.high_resolution_page_numbers
        ),
        "page_count": result.compilation.page_count,
        "survey_status": result.compilation.status.value,
        "scope_profiles": [
            {
                "kind": item.kind.value,
                "page_numbers": list(item.page_numbers),
            }
            for item in result.compilation.scopes
        ],
        "finding_codes": [
            item.finding_code for item in result.compilation.findings
        ],
        "candidate_sha256": result.compilation.candidate_sha256,
        "visual_coverage_receipt_sha256": (
            result.compilation.visual_coverage_receipt_sha256
        ),
        "compilation_sha256": result.compilation.compilation_sha256,
        "elapsed_milliseconds": elapsed_milliseconds,
        "private_output_written": output is not None,
        "skill_inputs": activity.get("skill_inputs"),
        "pdf_primary_runtime": activity.get("pdf_primary_runtime"),
        "command_execution_count": (
            int(unique_item_counts.get("commandExecution") or 0)
            if isinstance(unique_item_counts, dict)
            else 0
        ),
    }


def _write_private_result(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--document-role", default="CUSTOMER_SOR")
    parser.add_argument(
        "--codex-bin",
        type=Path,
        default=Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path.home() / ".codex-reqpilotx",
    )
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument(
        "--visual-mode",
        choices=tuple(item.value for item in DocumentMapVisualCoverageMode),
        default=DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE.value,
    )
    parser.add_argument("--private-output", type=Path)
    args = parser.parse_args()
    try:
        result = run_document_map_survey_canary(
            source_path=args.source,
            corpus_root=args.corpus_root,
            document_role=args.document_role,
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            repository_root=args.repository_root,
            timeout_seconds=args.timeout_seconds,
            visual_mode=DocumentMapVisualCoverageMode(args.visual_mode),
            private_output=args.private_output,
        )
    except DocumentMapSurveyCandidateValidationError as exc:
        if args.private_output is not None:
            _write_private_result(
                args.private_output.expanduser().resolve(),
                {
                    "status": "FAILED_CANDIDATE_VALIDATION",
                    "candidate": exc.private_candidate_payload,
                    "safe_issue_codes": list(exc.safe_issue_codes),
                    "tool_activity_summary": exc.tool_activity_summary,
                },
            )
        visual = exc.tool_activity_summary.get("visual_inspection", {})
        print(
            json.dumps(
                {
                    "schema_version": CANARY_SCHEMA_VERSION,
                    "status": "FAIL",
                    "reason_code": exc.reason_code,
                    "safe_issue_codes": list(exc.safe_issue_codes),
                    "private_failure_output_written": (
                        args.private_output is not None
                    ),
                    "contact_sheet_opened": (
                        visual.get("contact_sheet_opened") is True
                        if isinstance(visual, dict)
                        else False
                    ),
                    "high_resolution_page_count": (
                        int(visual.get("high_resolution_page_count") or 0)
                        if isinstance(visual, dict)
                        else 0
                    ),
                },
                sort_keys=True,
            )
        )
        return 2
    except LifecycleProbeError as exc:
        print(
            json.dumps(
                {
                    "schema_version": CANARY_SCHEMA_VERSION,
                    "status": "FAIL",
                    "reason_code": exc.reason_code,
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
