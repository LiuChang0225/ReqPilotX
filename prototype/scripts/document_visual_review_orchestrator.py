"""Manifest-bound, deduplicated orchestration for document visual review."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from scripts.document_visual_ai_pre_review import (
    prepare_document_visual_ai_pre_review,
    validate_document_visual_ai_pre_review,
)
from scripts.document_visual_pipeline_export import (
    export_document_visual_pipeline,
)
from scripts.document_visual_review_bundle import (
    canonical_profile_sha256,
    export_document_visual_review_bundle,
    verify_document_visual_review_bundle,
)
from scripts.document_visual_tif_render_profile import (
    export_tif_render_profile,
)
from scripts.storage_backup import BackupError, _rename_no_replace


ORCHESTRATION_SCHEMA_VERSION = "document-visual-review-orchestration/v1"
REVIEW_RESULT_SCHEMA_VERSION = "document-visual-review-result/v1"
BUNDLE_BATCH_SCHEMA_VERSION = "document-visual-review-bundle-batch/v1"
_EXTERNAL_PREFIX = "external://training-corpus/"
_SUPPORTED_FORMATS = frozenset({"pdf", "docx", "xlsx", "xlsb", "tif"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+\-]{0,159}$")
_DIAGNOSTIC_CODE = re.compile(r"^[a-z0-9][a-z0-9._\-]{0,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRIVACY = {
    "classification": "CUSTOMER_SENSITIVE",
    "handling": "CONTROLLED_LOCAL_REVIEW_ONLY",
    "contains_customer_text": False,
    "contains_source_path": False,
    "contains_source_filename": False,
}
_PROVENANCE = {
    "is_human_review": False,
    "is_human_gold": False,
    "is_gold": False,
    "is_formal_approval": False,
    "is_serving_activation_gate": False,
}


class DocumentVisualReviewOrchestrationRejected(RuntimeError):
    """Stable, customer-text-free orchestration rejection."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class _Pin:
    path: Path
    sha256: str
    size: int
    device: int
    inode: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _ResolvedSource:
    document_id: str
    document_format: str
    source_sha256: str
    source: Path
    pin: _Pin
    slot_document_ids: tuple[str, ...]

    @property
    def review_id(self) -> str:
        return f"review-{self.source_sha256[:16]}"


@dataclass(frozen=True, slots=True)
class _ResolvedManifest:
    manifest_sha256: str
    slot_count: int
    sources: tuple[_ResolvedSource, ...]


def export_document_visual_review_plan(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    corpus_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Verify 25 manifest slots, deduplicate by exact source hash, publish plan."""

    resolved = _resolve_manifest(
        Path(manifest_path),
        expected_manifest_sha256=expected_manifest_sha256,
        corpus_root=Path(corpus_root),
    )
    counts: dict[str, int] = {}
    documents = []
    for source in resolved.sources:
        counts[source.document_format] = counts.get(source.document_format, 0) + 1
        documents.append(
            {
                "review_id": source.review_id,
                "document_id": source.document_id,
                "source_sha256": source.source_sha256,
                "format": source.document_format,
                "slot_count": len(source.slot_document_ids),
                "slot_document_ids": list(source.slot_document_ids),
                "lane": _lane(source.document_format),
            }
        )
    plan: dict[str, Any] = {
        "schema_version": ORCHESTRATION_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": resolved.manifest_sha256,
        "source_slot_count": resolved.slot_count,
        "unique_source_count": len(resolved.sources),
        "duplicate_slot_count": resolved.slot_count - len(resolved.sources),
        "format_counts": dict(sorted(counts.items())),
        "documents": documents,
        "privacy": dict(_PRIVACY),
        "review_provenance": dict(_PROVENANCE),
    }
    plan["plan_sha256"] = _canonical_hash(plan, omit="plan_sha256")
    destination = _publish_single_json_directory(
        Path(output_dir),
        filename="orchestration-plan.json",
        payload=plan,
        corpus_root=Path(corpus_root),
        pins=(source.pin for source in resolved.sources),
    )
    del destination
    return plan


def execute_tif_not_evaluated_review(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    corpus_root: Path,
    controlled_root: Path,
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    """Run the one unique TIF through render, BLOCKED pipeline, bundle, ledger."""

    resolved = _resolve_manifest(
        Path(manifest_path),
        expected_manifest_sha256=expected_manifest_sha256,
        corpus_root=Path(corpus_root),
    )
    tif_sources = tuple(
        source for source in resolved.sources if source.document_format == "tif"
    )
    if len(tif_sources) != 1:
        _fail("orchestrator_tif_unique_source_count_invalid")
    source = tif_sources[0]
    root = _controlled_root(Path(controlled_root), corpus_root=Path(corpus_root))
    destination = root / f"tif-{source.review_id}"
    if destination.exists() or destination.is_symlink():
        _fail("orchestrator_tif_output_exists")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=root,
        )
    )
    staging.chmod(0o700)
    try:
        render = export_tif_render_profile(
            source.source,
            expected_source_sha256=source.source_sha256,
            output_dir=staging / "render-profile",
            corpus_root=Path(corpus_root),
        )
        pipeline = export_document_visual_pipeline(
            source.source,
            document_format="tif",
            expected_source_sha256=source.source_sha256,
            output_dir=staging / "pipeline-profile",
            corpus_root=Path(corpus_root),
        )
        if pipeline["pipeline_status"] != "BLOCKED":
            _fail("orchestrator_tif_pipeline_must_be_blocked")
        bundle = export_document_visual_review_bundle(
            source.source,
            document_id=source.document_id,
            document_format="tif",
            expected_source_sha256=source.source_sha256,
            render_profile_path=staging
            / "render-profile"
            / "render-profile.json",
            expected_render_profile_sha256=render["profile_file_sha256"],
            pipeline_profile_path=staging
            / "pipeline-profile"
            / "pipeline-profile.json",
            expected_pipeline_profile_sha256=pipeline[
                "profile_file_sha256"
            ],
            output_dir=staging / "bundle",
            corpus_root=Path(corpus_root),
        )
        timestamp = reviewed_at or datetime.now(timezone.utc).isoformat()
        pages = [
            {
                "page_number": page_number,
                "verdict": "NOT_EVALUATED",
                "issue_codes": ["PIPELINE_NOT_RUN"],
            }
            for page_number in range(1, bundle["document"]["page_count"] + 1)
        ]
        ledger = prepare_document_visual_ai_pre_review(
            bundle_dir=staging / "bundle",
            source=source.source,
            document_format="tif",
            expected_source_sha256=source.source_sha256,
            render_profile_path=staging
            / "render-profile"
            / "render-profile.json",
            expected_render_profile_sha256=render["profile_file_sha256"],
            pipeline_profile_path=staging
            / "pipeline-profile"
            / "pipeline-profile.json",
            expected_pipeline_profile_sha256=pipeline[
                "profile_file_sha256"
            ],
            expected_review_bundle_sha256=bundle["bundle_sha256"],
            expected_artifact_inventory_sha256=bundle[
                "artifact_inventory_sha256"
            ],
            reviewer={
                "kind": "AI",
                "reviewer_id": "reqpilotx-ai-pre-review",
                "model_id": "not-invoked",
                "model_version": "not-run",
                "reviewed_at": timestamp,
            },
            page_reviews=pages,
        )
        _write_json(staging / "ai-pre-review.json", ledger)
        validate_document_visual_ai_pre_review(
            ledger,
            bundle_dir=staging / "bundle",
            source=source.source,
            document_format="tif",
            expected_source_sha256=source.source_sha256,
            render_profile_path=staging
            / "render-profile"
            / "render-profile.json",
            expected_render_profile_sha256=render["profile_file_sha256"],
            pipeline_profile_path=staging
            / "pipeline-profile"
            / "pipeline-profile.json",
            expected_pipeline_profile_sha256=pipeline[
                "profile_file_sha256"
            ],
            expected_review_bundle_sha256=bundle["bundle_sha256"],
            expected_artifact_inventory_sha256=bundle[
                "artifact_inventory_sha256"
            ],
            expected_artifact_sha256=ledger["artifact_sha256"],
        )
        result: dict[str, Any] = {
            "schema_version": REVIEW_RESULT_SCHEMA_VERSION,
            "generated_at": timestamp,
            "manifest_sha256": resolved.manifest_sha256,
            "review_id": source.review_id,
            "document_id": source.document_id,
            "source_sha256": source.source_sha256,
            "format": "tif",
            "page_count": bundle["document"]["page_count"],
            "render_profile_file_sha256": render["profile_file_sha256"],
            "pipeline_profile_file_sha256": pipeline[
                "profile_file_sha256"
            ],
            "pipeline_status": pipeline["pipeline_status"],
            "bundle_sha256": bundle["bundle_sha256"],
            "artifact_inventory_sha256": bundle[
                "artifact_inventory_sha256"
            ],
            "ai_pre_review_sha256": ledger["artifact_sha256"],
            "ai_pre_review_verdict": ledger["summary"]["verdict"],
            "privacy": dict(_PRIVACY),
            "review_provenance": dict(_PROVENANCE),
        }
        result["result_sha256"] = _canonical_hash(
            result,
            omit="result_sha256",
        )
        _write_json(staging / "review-result.json", result)
        _verify_pin_unchanged(source.pin)
        _reject_tree_symlinks(staging)
        _fsync_tree(staging)
        _publish_staging(staging, destination)
        return result
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def execute_office_pipeline_profile_batch(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    corpus_root: Path,
    controlled_root: Path,
    document_formats: Sequence[str] = ("docx",),
    output_name: str = "docx-pipeline-profiles",
) -> dict[str, Any]:
    """Run selected unique Office sources sequentially and atomically publish."""

    formats = tuple(str(value).lower() for value in document_formats)
    if (
        not formats
        or len(formats) != len(set(formats))
        or any(value not in {"docx", "xlsx", "xlsb"} for value in formats)
    ):
        _fail("orchestrator_office_format_selection_invalid")
    resolved = _resolve_manifest(
        Path(manifest_path),
        expected_manifest_sha256=expected_manifest_sha256,
        corpus_root=Path(corpus_root),
    )
    selected = tuple(
        source
        for source in resolved.sources
        if source.document_format in formats
    )
    if not selected:
        _fail("orchestrator_office_selection_empty")
    root = _controlled_root(Path(controlled_root), corpus_root=Path(corpus_root))
    if _SAFE_ID.fullmatch(output_name) is None:
        _fail("orchestrator_office_output_name_invalid")
    destination = root / output_name
    if destination.exists() or destination.is_symlink():
        _fail("orchestrator_office_output_exists")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_name}.",
            suffix=".tmp",
            dir=root,
        )
    )
    staging.chmod(0o700)
    try:
        documents = []
        for source in selected:
            exported = export_document_visual_pipeline(
                source.source,
                document_format=source.document_format,
                expected_source_sha256=source.source_sha256,
                output_dir=staging / source.review_id,
                corpus_root=Path(corpus_root),
            )
            documents.append(
                {
                    "review_id": source.review_id,
                    "document_id": source.document_id,
                    "source_sha256": source.source_sha256,
                    "format": source.document_format,
                    "pipeline_status": exported["pipeline_status"],
                    "profile_file_sha256": exported[
                        "profile_file_sha256"
                    ],
                }
            )
        index: dict[str, Any] = {
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "batch_kind": "OFFICE_PIPELINE_PROFILES",
            "manifest_sha256": resolved.manifest_sha256,
            "formats": list(formats),
            "document_count": len(documents),
            "documents": documents,
            "privacy": dict(_PRIVACY),
            "review_provenance": dict(_PROVENANCE),
        }
        index["batch_sha256"] = _canonical_hash(index, omit="batch_sha256")
        _write_json(staging / "batch-index.json", index)
        for source in selected:
            _verify_pin_unchanged(source.pin)
        _reject_tree_symlinks(staging)
        _fsync_tree(staging)
        _publish_staging(staging, destination)
        return index
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def execute_docx_review_bundle_batch(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    corpus_root: Path,
    controlled_root: Path,
    render_profiles_root: Path,
    pipeline_profiles_root: Path,
    expected_pipeline_batch_sha256: str,
    output_name: str = "docx-review-bundles-v1",
) -> dict[str, Any]:
    """Join pinned DOCX render and pipeline profiles into verified bundles.

    The whole batch is built below one staging directory and published with a
    single no-replace rename.  Every bundle is verified both before and after
    that batch-level publish.  This function intentionally does not create an
    AI review ledger.
    """

    resolved = _resolve_manifest(
        Path(manifest_path),
        expected_manifest_sha256=expected_manifest_sha256,
        corpus_root=Path(corpus_root),
    )
    selected = tuple(
        source
        for source in resolved.sources
        if source.document_format == "docx"
    )
    if not selected:
        _fail("orchestrator_docx_bundle_selection_empty")
    root = _controlled_root(Path(controlled_root), corpus_root=Path(corpus_root))
    if _SAFE_ID.fullmatch(output_name) is None:
        _fail("orchestrator_docx_bundle_output_name_invalid")
    destination = root / output_name
    if destination.exists() or destination.is_symlink():
        _fail("orchestrator_docx_bundle_output_exists")

    render_root = _controlled_input_directory(
        Path(render_profiles_root),
        controlled_root=root,
        code="orchestrator_docx_render_profiles_root",
    )
    pipeline_root = _controlled_input_directory(
        Path(pipeline_profiles_root),
        controlled_root=root,
        code="orchestrator_docx_pipeline_profiles_root",
    )
    if (
        render_root == pipeline_root
        or render_root in pipeline_root.parents
        or pipeline_root in render_root.parents
    ):
        _fail("orchestrator_docx_profile_roots_overlap")
    _reject_tree_symlinks(render_root)
    _reject_tree_symlinks(pipeline_root)

    expected_render_entries = {source.document_id for source in selected}
    if {entry.name for entry in render_root.iterdir()} != (
        expected_render_entries
    ):
        _fail("orchestrator_docx_render_profile_inventory_mismatch")
    for source in selected:
        if not (render_root / source.document_id).is_dir():
            _fail("orchestrator_docx_render_profile_inventory_mismatch")

    expected_batch_hash = _sha256(
        expected_pipeline_batch_sha256,
        "orchestrator_docx_pipeline_batch_sha256_invalid",
    )
    batch_path = pipeline_root / "batch-index.json"
    batch_pin = _pin_current_file(
        batch_path,
        code="orchestrator_docx_pipeline_batch",
        containment_root=pipeline_root,
    )
    pipeline_batch = _parse_json(
        batch_pin.path.read_bytes(),
        "orchestrator_docx_pipeline_batch_json_invalid",
    )
    _validate_docx_pipeline_batch(
        pipeline_batch,
        expected_batch_sha256=expected_batch_hash,
        manifest_sha256=resolved.manifest_sha256,
        selected=selected,
    )
    pipeline_rows = pipeline_batch["documents"]
    expected_pipeline_entries = {
        "batch-index.json",
        *(source.review_id for source in selected),
    }
    if {entry.name for entry in pipeline_root.iterdir()} != (
        expected_pipeline_entries
    ):
        _fail("orchestrator_docx_pipeline_profile_inventory_mismatch")
    for source in selected:
        if not (pipeline_root / source.review_id).is_dir():
            _fail("orchestrator_docx_pipeline_profile_inventory_mismatch")

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_name}.",
            suffix=".tmp",
            dir=root,
        )
    )
    staging.chmod(0o700)
    verification_records: list[dict[str, Any]] = []
    input_pins: list[_Pin] = [batch_pin]
    try:
        documents: list[dict[str, Any]] = []
        for source, pipeline_row in zip(
            selected,
            pipeline_rows,
            strict=True,
        ):
            render_profile_path = (
                render_root / source.document_id / "render-profile.json"
            )
            render_pin = _pin_current_file(
                render_profile_path,
                code="orchestrator_docx_render_profile",
                containment_root=render_root,
            )
            render_profile = _parse_json(
                render_pin.path.read_bytes(),
                "orchestrator_docx_render_profile_json_invalid",
            )
            _validate_profile_identity(
                render_profile,
                expected_schema="document-visual-render-profile/v1",
                source=source,
                code="orchestrator_docx_render_profile",
            )

            pipeline_profile_path = (
                pipeline_root / source.review_id / "pipeline-profile.json"
            )
            pipeline_pin = _pin_file(
                pipeline_profile_path,
                expected_sha256=pipeline_row["profile_file_sha256"],
                code="orchestrator_docx_pipeline_profile",
                containment_root=pipeline_root,
            )
            pipeline_profile = _parse_json(
                pipeline_pin.path.read_bytes(),
                "orchestrator_docx_pipeline_profile_json_invalid",
            )
            _validate_profile_identity(
                pipeline_profile,
                expected_schema="document-visual-pipeline-profile/v1",
                source=source,
                code="orchestrator_docx_pipeline_profile",
            )
            pipeline = pipeline_profile.get("pipeline")
            if (
                not isinstance(pipeline, dict)
                or pipeline.get("status")
                != pipeline_row["pipeline_status"]
            ):
                _fail("orchestrator_docx_pipeline_status_mismatch")

            bundle = export_document_visual_review_bundle(
                source.source,
                document_id=source.document_id,
                document_format="docx",
                expected_source_sha256=source.source_sha256,
                render_profile_path=render_profile_path,
                expected_render_profile_sha256=render_pin.sha256,
                pipeline_profile_path=pipeline_profile_path,
                expected_pipeline_profile_sha256=pipeline_pin.sha256,
                output_dir=staging / source.review_id,
                corpus_root=Path(corpus_root),
            )
            verified = verify_document_visual_review_bundle(
                staging / source.review_id,
                source=source.source,
                document_format="docx",
                expected_source_sha256=source.source_sha256,
                render_profile_path=render_profile_path,
                expected_render_profile_sha256=render_pin.sha256,
                pipeline_profile_path=pipeline_profile_path,
                expected_pipeline_profile_sha256=pipeline_pin.sha256,
                expected_bundle_sha256=bundle["bundle_sha256"],
                expected_artifact_inventory_sha256=bundle[
                    "artifact_inventory_sha256"
                ],
            )
            if verified["document"]["document_id"] != source.document_id:
                _fail("orchestrator_docx_bundle_document_id_mismatch")
            diagnostic_counts, diagnostic_pins = (
                _source_ir_diagnostic_code_counts(
                    pipeline_profile_path.parent,
                    pipeline_profile,
                )
            )
            input_pins.extend((render_pin, pipeline_pin, *diagnostic_pins))
            row = {
                "review_id": source.review_id,
                "document_id": source.document_id,
                "source_sha256": source.source_sha256,
                "format": "docx",
                "page_count": bundle["document"]["page_count"],
                "pipeline_status": pipeline["status"],
                "source_ir_diagnostic_code_counts": diagnostic_counts,
                "render_profile_file_sha256": render_pin.sha256,
                "render_profile_sha256": bundle["profile_pins"]["render"][
                    "profile_sha256"
                ],
                "pipeline_profile_file_sha256": pipeline_pin.sha256,
                "pipeline_profile_sha256": bundle["profile_pins"]["pipeline"][
                    "profile_sha256"
                ],
                "bundle_sha256": bundle["bundle_sha256"],
                "artifact_inventory_sha256": bundle[
                    "artifact_inventory_sha256"
                ],
            }
            documents.append(row)
            verification_records.append(
                {
                    "source": source,
                    "render_profile_path": render_profile_path,
                    "render_profile_file_sha256": render_pin.sha256,
                    "pipeline_profile_path": pipeline_profile_path,
                    "pipeline_profile_file_sha256": pipeline_pin.sha256,
                    "bundle_sha256": row["bundle_sha256"],
                    "artifact_inventory_sha256": row[
                        "artifact_inventory_sha256"
                    ],
                }
            )

        status_counts: dict[str, int] = {}
        diagnostic_totals: dict[str, int] = {}
        for document in documents:
            status = document["pipeline_status"]
            status_counts[status] = status_counts.get(status, 0) + 1
            for code, count in document[
                "source_ir_diagnostic_code_counts"
            ].items():
                diagnostic_totals[code] = diagnostic_totals.get(code, 0) + count
        index: dict[str, Any] = {
            "schema_version": BUNDLE_BATCH_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "batch_kind": "DOCX_WHOLE_DOCUMENT_VISUAL_REVIEW_BUNDLES",
            "manifest_sha256": resolved.manifest_sha256,
            "upstream_pipeline_batch_sha256": expected_batch_hash,
            "document_count": len(documents),
            "page_count": sum(
                int(document["page_count"]) for document in documents
            ),
            "pipeline_status_counts": dict(sorted(status_counts.items())),
            "source_ir_diagnostic_code_counts": dict(
                sorted(diagnostic_totals.items())
            ),
            "documents": documents,
            "privacy": dict(_PRIVACY),
            "review_provenance": dict(_PROVENANCE),
        }
        index["batch_sha256"] = _canonical_hash(index, omit="batch_sha256")
        _assert_batch_index_privacy(index, selected=selected)
        _write_json(staging / "batch-index.json", index)
        for source in selected:
            _verify_pin_unchanged(source.pin)
        for pin in input_pins:
            _verify_pin_unchanged(pin)
        _reject_tree_symlinks(staging)
        _fsync_tree(staging)
        _publish_staging(staging, destination)

        # A successful staging verification is not treated as proof of the
        # published tree: re-open and recompute every bundle from final paths.
        for record in verification_records:
            source = record["source"]
            verified = verify_document_visual_review_bundle(
                destination / source.review_id,
                source=source.source,
                document_format="docx",
                expected_source_sha256=source.source_sha256,
                render_profile_path=record["render_profile_path"],
                expected_render_profile_sha256=record[
                    "render_profile_file_sha256"
                ],
                pipeline_profile_path=record["pipeline_profile_path"],
                expected_pipeline_profile_sha256=record[
                    "pipeline_profile_file_sha256"
                ],
                expected_bundle_sha256=record["bundle_sha256"],
                expected_artifact_inventory_sha256=record[
                    "artifact_inventory_sha256"
                ],
            )
            if verified["document"]["document_id"] != source.document_id:
                _fail("orchestrator_docx_bundle_document_id_mismatch")
        _reject_tree_symlinks(destination)
        return index
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _validate_docx_pipeline_batch(
    batch: Mapping[str, Any],
    *,
    expected_batch_sha256: str,
    manifest_sha256: str,
    selected: Sequence[_ResolvedSource],
) -> None:
    required = {
        "schema_version",
        "generated_at",
        "batch_kind",
        "manifest_sha256",
        "formats",
        "document_count",
        "documents",
        "privacy",
        "review_provenance",
        "batch_sha256",
    }
    if set(batch) != required:
        _fail("orchestrator_docx_pipeline_batch_fields_invalid")
    if (
        batch.get("schema_version") != ORCHESTRATION_SCHEMA_VERSION
        or batch.get("batch_kind") != "OFFICE_PIPELINE_PROFILES"
        or batch.get("manifest_sha256") != manifest_sha256
        or batch.get("formats") != ["docx"]
        or batch.get("document_count") != len(selected)
        or batch.get("privacy") != _PRIVACY
        or batch.get("review_provenance") != _PROVENANCE
        or not isinstance(batch.get("generated_at"), str)
        or not batch["generated_at"]
    ):
        _fail("orchestrator_docx_pipeline_batch_contract_invalid")
    if (
        _sha256(
            batch.get("batch_sha256"),
            "orchestrator_docx_pipeline_batch_hash_invalid",
        )
        != expected_batch_sha256
        or _canonical_hash(batch, omit="batch_sha256")
        != expected_batch_sha256
    ):
        _fail("orchestrator_docx_pipeline_batch_hash_mismatch")
    rows = batch.get("documents")
    if not isinstance(rows, list) or len(rows) != len(selected):
        _fail("orchestrator_docx_pipeline_batch_documents_invalid")
    row_fields = {
        "review_id",
        "document_id",
        "source_sha256",
        "format",
        "pipeline_status",
        "profile_file_sha256",
    }
    statuses = {"COMPLETE", "PARTIAL", "BLOCKED", "FAILED", "NOT_RUN"}
    for source, row in zip(selected, rows, strict=True):
        if not isinstance(row, dict) or set(row) != row_fields:
            _fail("orchestrator_docx_pipeline_batch_row_invalid")
        if (
            row.get("review_id") != source.review_id
            or row.get("document_id") != source.document_id
            or row.get("source_sha256") != source.source_sha256
            or row.get("format") != "docx"
            or row.get("pipeline_status") not in statuses
        ):
            _fail("orchestrator_docx_pipeline_batch_binding_mismatch")
        _sha256(
            row.get("profile_file_sha256"),
            "orchestrator_docx_pipeline_profile_sha256_invalid",
        )


def _validate_profile_identity(
    profile: Mapping[str, Any],
    *,
    expected_schema: str,
    source: _ResolvedSource,
    code: str,
) -> None:
    source_value = profile.get("source")
    if (
        profile.get("schema_version") != expected_schema
        or profile.get("profile_hash_contract")
        != "canonical-json-without-profile-sha256/v1"
        or not isinstance(source_value, dict)
        or source_value
        != {
            "source_sha256": source.source_sha256,
            "format": "docx",
        }
    ):
        _fail(f"{code}_identity_mismatch")
    profile_hash = _sha256(
        profile.get("profile_sha256"),
        f"{code}_profile_sha256_invalid",
    )
    try:
        canonical_hash = canonical_profile_sha256(profile)
    except Exception as exc:
        raise DocumentVisualReviewOrchestrationRejected(
            f"{code}_not_canonical"
        ) from exc
    if profile_hash != canonical_hash:
        _fail(f"{code}_profile_sha256_mismatch")


def _source_ir_diagnostic_code_counts(
    profile_root: Path,
    pipeline_profile: Mapping[str, Any],
) -> tuple[dict[str, int], tuple[_Pin, ...]]:
    artifacts = pipeline_profile.get("artifacts")
    if not isinstance(artifacts, list):
        _fail("orchestrator_docx_pipeline_artifacts_invalid")
    candidates = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("kind") == "SOURCE_IR"
    ]
    if len(candidates) != 1:
        _fail("orchestrator_docx_source_ir_artifact_count_invalid")
    artifact = candidates[0]
    if (
        artifact.get("media_type") != "application/json"
        or artifact.get("customer_sensitive") is not True
    ):
        _fail("orchestrator_docx_source_ir_artifact_contract_invalid")
    artifact_path = _resolve_relative_artifact(
        profile_root,
        artifact.get("path"),
        code="orchestrator_docx_source_ir_artifact",
    )
    pin = _pin_file(
        artifact_path,
        expected_sha256=_sha256(
            artifact.get("sha256"),
            "orchestrator_docx_source_ir_artifact_sha256_invalid",
        ),
        code="orchestrator_docx_source_ir_artifact",
        containment_root=profile_root,
    )
    source_ir = _parse_json(
        pin.path.read_bytes(),
        "orchestrator_docx_source_ir_json_invalid",
    )
    diagnostics = source_ir.get("diagnostics")
    if not isinstance(diagnostics, list):
        _fail("orchestrator_docx_source_ir_diagnostics_invalid")
    counts: dict[str, int] = {}
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            _fail("orchestrator_docx_source_ir_diagnostic_invalid")
        code = diagnostic.get("code")
        if not isinstance(code, str) or _DIAGNOSTIC_CODE.fullmatch(code) is None:
            _fail("orchestrator_docx_source_ir_diagnostic_code_invalid")
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items())), (pin,)


def _resolve_relative_artifact(
    root: Path,
    value: Any,
    *,
    code: str,
) -> Path:
    if not isinstance(value, str):
        _fail(f"{code}_path_invalid")
    token = PurePosixPath(value)
    if (
        not value
        or token.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in token.parts)
    ):
        _fail(f"{code}_path_invalid")
    candidate = Path(root).joinpath(*token.parts)
    _reject_symlink_between(root, candidate, f"{code}_symlink")
    return candidate


def _controlled_input_directory(
    path: Path,
    *,
    controlled_root: Path,
    code: str,
) -> Path:
    candidate = Path(path).absolute()
    try:
        relative = candidate.relative_to(controlled_root.absolute())
    except ValueError:
        _fail(f"{code}_outside_controlled_root")
    if not relative.parts:
        _fail(f"{code}_must_be_child")
    _reject_symlink_between(controlled_root, candidate, f"{code}_symlink")
    return _existing_directory(candidate, code).absolute()


def _pin_current_file(
    path: Path,
    *,
    code: str,
    containment_root: Path,
) -> _Pin:
    candidate = Path(path)
    try:
        payload = candidate.read_bytes()
    except OSError as exc:
        raise DocumentVisualReviewOrchestrationRejected(
            f"{code}_unavailable"
        ) from exc
    return _pin_file(
        candidate,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        code=code,
        containment_root=containment_root,
    )


def _assert_batch_index_privacy(
    index: Mapping[str, Any],
    *,
    selected: Sequence[_ResolvedSource],
) -> None:
    serialized = json.dumps(
        index,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    forbidden = {_EXTERNAL_PREFIX}
    for source in selected:
        forbidden.update(
            {
                str(source.source),
                source.source.as_posix(),
                source.source.name,
            }
        )
    if any(value and value in serialized for value in forbidden):
        _fail("orchestrator_docx_batch_index_privacy_violation")


def _resolve_manifest(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    corpus_root: Path,
) -> _ResolvedManifest:
    manifest_pin = _pin_file(
        Path(manifest_path),
        expected_sha256=_sha256(
            expected_manifest_sha256,
            "orchestrator_expected_manifest_sha256_invalid",
        ),
        code="orchestrator_manifest",
    )
    manifest = _parse_json(
        manifest_pin.path.read_bytes(),
        "orchestrator_manifest_json_invalid",
    )
    corpus = manifest.get("corpus")
    documents = manifest.get("documents")
    if (
        not isinstance(corpus, dict)
        or corpus.get("root_uri") != "external://training-corpus/"
        or corpus.get("contains_customer_content") is not True
        or corpus.get("content_committed_to_repository") is not False
        or not isinstance(documents, dict)
        or not documents
    ):
        _fail("orchestrator_manifest_contract_invalid")
    root = _existing_directory(Path(corpus_root), "orchestrator_corpus_root")
    slots: list[tuple[str, dict[str, Any], _Pin]] = []
    for document_id, value in documents.items():
        if _SAFE_ID.fullmatch(str(document_id)) is None or not isinstance(
            value, dict
        ):
            _fail("orchestrator_document_contract_invalid")
        document_format = str(value.get("format", "")).lower()
        source_sha256 = _sha256(
            value.get("source_sha256"),
            "orchestrator_document_source_sha256_invalid",
        )
        if (
            document_format not in _SUPPORTED_FORMATS
            or value.get("resolution_status") != "resolved"
        ):
            _fail("orchestrator_document_unresolved")
        source = _resolve_external_ref(
            value.get("document_ref"),
            corpus_root=root,
        )
        if not _format_suffix_matches(source, document_format):
            _fail("orchestrator_document_format_mismatch")
        pin = _pin_file(
            source,
            expected_sha256=source_sha256,
            code="orchestrator_document_source",
            containment_root=root,
        )
        slots.append((str(document_id), value, pin))
    by_id = {document_id: (value, pin) for document_id, value, pin in slots}
    groups: dict[str, list[tuple[str, dict[str, Any], _Pin]]] = {}
    for slot in slots:
        groups.setdefault(slot[2].sha256, []).append(slot)
    sources: list[_ResolvedSource] = []
    seen_groups: set[str] = set()
    for document_id, value, pin in slots:
        if pin.sha256 in seen_groups:
            continue
        group = groups[pin.sha256]
        formats = {str(item[1].get("format", "")).lower() for item in group}
        if len(formats) != 1:
            _fail("orchestrator_duplicate_format_mismatch")
        canonical = next(
            (item for item in group if item[1].get("duplicate_of") is None),
            group[0],
        )
        for slot_id, slot_value, _slot_pin in group:
            duplicate_of = slot_value.get("duplicate_of")
            if duplicate_of is not None:
                if duplicate_of not in by_id:
                    _fail("orchestrator_duplicate_target_missing")
                target_value, target_pin = by_id[duplicate_of]
                if (
                    target_pin.sha256 != pin.sha256
                    or target_value.get("format") != slot_value.get("format")
                ):
                    _fail("orchestrator_duplicate_binding_mismatch")
            elif slot_id != canonical[0] and len(group) > 1:
                _fail("orchestrator_unmarked_duplicate_source")
        sources.append(
            _ResolvedSource(
                document_id=canonical[0],
                document_format=next(iter(formats)),
                source_sha256=pin.sha256,
                source=canonical[2].path,
                pin=canonical[2],
                slot_document_ids=tuple(item[0] for item in group),
            )
        )
        seen_groups.add(pin.sha256)
    _verify_pin_unchanged(manifest_pin)
    return _ResolvedManifest(
        manifest_sha256=manifest_pin.sha256,
        slot_count=len(slots),
        sources=tuple(sources),
    )


def _publish_single_json_directory(
    destination: Path,
    *,
    filename: str,
    payload: Mapping[str, Any],
    corpus_root: Path,
    pins: Iterable[_Pin],
) -> Path:
    output = Path(destination).absolute()
    parent = _controlled_root(output.parent, corpus_root=corpus_root)
    if output.exists() or output.is_symlink():
        _fail("orchestrator_output_exists")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=parent,
        )
    )
    staging.chmod(0o700)
    try:
        _write_json(staging / filename, payload)
        for pin in pins:
            _verify_pin_unchanged(pin)
        _fsync_tree(staging)
        _publish_staging(staging, output)
        return output
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _publish_staging(staging: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        _fail("orchestrator_output_exists")
    try:
        _rename_no_replace(staging, destination)
    except BackupError as exc:
        if destination.exists() or destination.is_symlink():
            raise DocumentVisualReviewOrchestrationRejected(
                "orchestrator_output_exists"
            ) from exc
        raise DocumentVisualReviewOrchestrationRejected(
            "orchestrator_atomic_publish_failed"
        ) from exc
    _fsync_directory(destination.parent)


def _resolve_external_ref(value: Any, *, corpus_root: Path) -> Path:
    if not isinstance(value, str) or not value.startswith(_EXTERNAL_PREFIX):
        _fail("orchestrator_document_ref_invalid")
    relative_token = value[len(_EXTERNAL_PREFIX) :]
    path = PurePosixPath(relative_token)
    if (
        not relative_token
        or path.is_absolute()
        or "\\" in relative_token
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _fail("orchestrator_document_ref_invalid")
    candidate = corpus_root.joinpath(*path.parts)
    try:
        candidate.absolute().relative_to(corpus_root.absolute())
    except ValueError:
        _fail("orchestrator_document_ref_escape")
    _reject_symlink_between(
        corpus_root,
        candidate,
        "orchestrator_document_ref_symlink",
    )
    return candidate


def _pin_file(
    path: Path,
    *,
    expected_sha256: str,
    code: str,
    containment_root: Path | None = None,
) -> _Pin:
    candidate = Path(path)
    if containment_root is not None:
        try:
            candidate.absolute().relative_to(containment_root.absolute())
        except ValueError:
            _fail(f"{code}_path_escape")
        _reject_symlink_between(
            containment_root,
            candidate,
            f"{code}_symlink",
        )
    else:
        _reject_symlink_chain(candidate, f"{code}_symlink")
    try:
        before = candidate.stat(follow_symlinks=False)
        payload = candidate.read_bytes()
        after = candidate.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualReviewOrchestrationRejected(
            f"{code}_unavailable"
        ) from exc
    if not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(after.st_mode):
        _fail(f"{code}_not_regular")
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        _fail(f"{code}_toctou_detected")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        _fail(f"{code}_hash_mismatch")
    return _Pin(
        path=candidate,
        sha256=digest,
        size=after.st_size,
        device=after.st_dev,
        inode=after.st_ino,
        mtime_ns=after.st_mtime_ns,
    )


def _verify_pin_unchanged(pin: _Pin) -> None:
    current = _pin_file(
        pin.path,
        expected_sha256=pin.sha256,
        code="orchestrator_input_drift",
    )
    if (
        current.size,
        current.device,
        current.inode,
        current.mtime_ns,
    ) != (
        pin.size,
        pin.device,
        pin.inode,
        pin.mtime_ns,
    ):
        _fail("orchestrator_input_toctou_detected")


def _parse_json(payload: bytes, code: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail(code)
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DocumentVisualReviewOrchestrationRejected(code) from exc
    if not isinstance(value, dict):
        _fail(code)
    return value


def _canonical_hash(value: Mapping[str, Any], *, omit: str) -> str:
    payload = dict(value)
    payload.pop(omit, None)
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
    except OSError as exc:
        raise DocumentVisualReviewOrchestrationRejected(
            "orchestrator_write_failed"
        ) from exc


def _controlled_root(path: Path, *, corpus_root: Path) -> Path:
    root = _existing_directory(Path(path), "orchestrator_controlled_root")
    corpus = _existing_directory(
        Path(corpus_root),
        "orchestrator_corpus_root",
    )
    resolved_root = root.resolve(strict=True)
    resolved_corpus = corpus.resolve(strict=True)
    if (
        resolved_root == resolved_corpus
        or resolved_corpus in resolved_root.parents
    ):
        _fail("orchestrator_output_inside_corpus")
    return root.absolute()


def _existing_directory(path: Path, code: str) -> Path:
    _reject_symlink_chain(path, f"{code}_symlink")
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualReviewOrchestrationRejected(code) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(code)
    return path


def _reject_tree_symlinks(root: Path) -> None:
    for candidate in root.rglob("*"):
        metadata = candidate.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            _fail("orchestrator_output_symlink")
        if not stat.S_ISREG(metadata.st_mode) and not stat.S_ISDIR(
            metadata.st_mode
        ):
            _fail("orchestrator_output_non_regular")


def _reject_symlink_chain(path: Path, code: str) -> None:
    candidate = Path(path).absolute()
    current = Path(candidate.parts[0])
    for part in candidate.parts[1:]:
        current /= part
        if not current.exists() and not current.is_symlink():
            continue
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise DocumentVisualReviewOrchestrationRejected(code) from exc
        if stat.S_ISLNK(metadata.st_mode):
            _fail(code)


def _reject_symlink_between(root: Path, path: Path, code: str) -> None:
    try:
        relative = Path(path).absolute().relative_to(Path(root).absolute())
    except ValueError:
        _fail(f"{code}_escape")
    current = Path(root)
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            _fail(code)


def _format_suffix_matches(path: Path, document_format: str) -> bool:
    suffix = path.suffix.lower()
    if document_format == "tif":
        return suffix in {".tif", ".tiff"}
    return suffix == f".{document_format}"


def _lane(document_format: str) -> str:
    return {
        "pdf": "EXISTING_PDF_VISUAL_TOOL",
        "tif": "TIF_RENDER_AND_BLOCKED_PIPELINE_PRE_REVIEW",
        "docx": "OFFICE_RENDER_AND_PIPELINE",
        "xlsx": "OFFICE_RENDER_AND_PIPELINE",
        "xlsb": "CONTROLLED_CONVERSION_RENDER_AND_PIPELINE",
    }[document_format]


def _fsync_tree(root: Path) -> None:
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        _fsync_directory(directory)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise DocumentVisualReviewOrchestrationRejected(
            "orchestrator_directory_fsync_failed"
        ) from exc


def _sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(code)
    return value


def _fail(code: str) -> None:
    raise DocumentVisualReviewOrchestrationRejected(code)


__all__ = [
    "BUNDLE_BATCH_SCHEMA_VERSION",
    "DocumentVisualReviewOrchestrationRejected",
    "ORCHESTRATION_SCHEMA_VERSION",
    "REVIEW_RESULT_SCHEMA_VERSION",
    "execute_docx_review_bundle_batch",
    "execute_office_pipeline_profile_batch",
    "execute_tif_not_evaluated_review",
    "export_document_visual_review_plan",
]
