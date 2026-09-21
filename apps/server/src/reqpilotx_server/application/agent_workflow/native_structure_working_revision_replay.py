from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from io import BytesIO
import hashlib
import json
from typing import Any

from reqpilotx_server.application.agent_workflow.native_structure_working_revision import (
    NativeStructureWorkingRevisionError,
    build_accepted_reference_replay_revisions,
    canonical_json_sha256,
    NativeStructureTablePreviewBindingV1,
    TABLE_PREVIEW_MEDIA_TYPE,
    validate_native_structure_table_preview,
)
from reqpilotx_server.application.agent_workflow.native_structure_working_revision_bridge import (
    load_native_structure_working_revision,
    publish_native_structure_working_revision,
)
from reqpilotx_server.ports.agent_structure_loop_repository import AgentStructureLoopRepository
from reqpilotx_server.ports.object_store import ObjectStore

_MAX_TABLE_PREVIEW_BYTES = 16 * 1024 * 1024


def replay_accepted_native_structure_revisions(
    *,
    project_id: str,
    document_id: str,
    accepted_public_reference: Mapping[str, Any],
    table_previews: Sequence[Mapping[str, Any]],
    through_revision: int,
    repository: AgentStructureLoopRepository,
    object_store: ObjectStore,
) -> dict[str, Any]:
    if through_revision not in {1, 2, 3}:
        raise NativeStructureWorkingRevisionError(
            "accepted-reference replay revision must be 1, 2, or 3"
        )
    projection = accepted_public_reference.get("structure_projection")
    if not isinstance(projection, Mapping):
        raise NativeStructureWorkingRevisionError(
            "accepted-reference replay projection is missing"
        )
    source_sha256 = str(accepted_public_reference.get("source_sha256") or "")
    base_artifact_id = str(accepted_public_reference.get("artifact_id") or "")
    base_projection_sha256 = str(projection.get("projection_sha256") or "")
    evidence_artifact_id = _evidence_artifact_id(accepted_public_reference)
    table_bindings = _snapshot_table_previews(
        reference=accepted_public_reference,
        table_previews=table_previews,
        object_store=object_store,
    )
    run_id = _replay_run_id(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        base_artifact_id=base_artifact_id,
        base_projection_sha256=base_projection_sha256,
        evidence_artifact_id=evidence_artifact_id,
        table_preview_bindings=table_bindings,
    )
    planned = build_accepted_reference_replay_revisions(
        accepted_public_reference,
        run_id=run_id,
        evidence_artifact_id=evidence_artifact_id,
        table_preview_bindings=table_bindings,
    )
    created_count = 0
    head = None
    for candidate in planned[:through_revision]:
        existing = repository.get_working_revision_object(
            project_id=project_id,
            document_id=document_id,
            run_id=run_id,
            revision=candidate.revision,
        )
        if existing is not None:
            persisted = load_native_structure_working_revision(
                record=existing,
                object_store=object_store,
            )
            if persisted.revision_sha256 != candidate.revision_sha256:
                raise NativeStructureWorkingRevisionError(
                    "accepted-reference replay lineage drifted"
                )
            head = persisted.head()
            continue
        head, created = publish_native_structure_working_revision(
            project_id=project_id,
            document_id=document_id,
            revision=candidate,
            repository=repository,
            object_store=object_store,
        )
        created_count += int(created)
    if head is None:
        raise NativeStructureWorkingRevisionError(
            "accepted-reference replay did not produce a revision head"
        )
    return {
        "schema_version": "native-structure-working-replay-result/v1",
        "execution_mode": "ACCEPTED_REFERENCE_REPLAY",
        "quality_claim": "TRANSPORT_REPLAY_ONLY",
        "requested_through_revision": through_revision,
        "published_revision_count": created_count,
        "head": head.model_dump(mode="json"),
    }


def _snapshot_table_previews(
    *,
    reference: Mapping[str, Any],
    table_previews: Sequence[Mapping[str, Any]],
    object_store: ObjectStore,
) -> tuple[NativeStructureTablePreviewBindingV1, ...]:
    by_scope = {
        str(item.get("logical_table_scope_id") or ""): item
        for item in table_previews
    }
    if len(by_scope) != len(table_previews) or "" in by_scope:
        raise NativeStructureWorkingRevisionError(
            "accepted replay table preview scopes are duplicated"
        )
    source_sha256 = str(reference.get("source_sha256") or "")
    artifact_id = str(reference.get("artifact_id") or "")
    projection_sha256 = str(
        (reference.get("structure_projection") or {}).get("projection_sha256") or ""
    )
    bindings = []
    for table in reference.get("logical_tables", []):
        scope_id = str(table.get("scope_id") or "")
        preview = by_scope.get(scope_id)
        if preview is None:
            raise NativeStructureWorkingRevisionError(
                "accepted replay table preview is missing"
            )
        if (
            preview.get("source_sha256") != source_sha256
            or preview.get("artifact_id") != artifact_id
        ):
            raise NativeStructureWorkingRevisionError(
                "accepted replay table preview identity drifted"
            )
        validate_native_structure_table_preview(
            logical_table=table,
            preview=preview,
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        )
        encoded = json.dumps(
            preview, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_TABLE_PREVIEW_BYTES:
            raise NativeStructureWorkingRevisionError(
                "accepted replay table preview exceeds the snapshot budget"
            )
        object_sha256 = hashlib.sha256(encoded).hexdigest()
        key = f"agent-structure/table-previews/{source_sha256}/{object_sha256}.json"
        stored = object_store.put(
            key=key,
            source=BytesIO(encoded),
            expected_sha256=object_sha256,
            media_type=TABLE_PREVIEW_MEDIA_TYPE,
        )
        if (
            stored.key != key
            or stored.sha256 != object_sha256
            or stored.size != len(encoded)
            or stored.media_type != TABLE_PREVIEW_MEDIA_TYPE
        ):
            raise NativeStructureWorkingRevisionError(
                "accepted replay table preview object-store receipt drifted"
            )
        bindings.append(
            NativeStructureTablePreviewBindingV1(
                scope_id=scope_id,
                preview_sha256=canonical_json_sha256(preview),
                object_key=stored.key,
                object_sha256=stored.sha256,
                object_size=stored.size,
                media_type=TABLE_PREVIEW_MEDIA_TYPE,
                source_sha256=source_sha256,
                base_artifact_id=artifact_id,
                base_projection_sha256=projection_sha256,
            )
        )
    return tuple(bindings)


def _evidence_artifact_id(reference: Mapping[str, Any]) -> str:
    digest = canonical_json_sha256(
        {
            "schema_version": "native-structure-replay-evidence-binding/v1",
            "source_sha256": reference.get("source_sha256"),
            "artifact_id": reference.get("artifact_id"),
            "page_ownership": reference.get("page_ownership", []),
            "logical_tables": reference.get("logical_tables", []),
            "visual_summary": reference.get("visual_summary", {}),
        }
    )
    return f"native-structure-evidence-{digest[:32]}"


def _replay_run_id(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    base_artifact_id: str,
    base_projection_sha256: str,
    evidence_artifact_id: str,
    table_preview_bindings: Sequence[NativeStructureTablePreviewBindingV1],
) -> str:
    digest = canonical_json_sha256(
        {
            "schema_version": "native-structure-working-replay-run/v1",
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": source_sha256,
            "base_artifact_id": base_artifact_id,
            "base_projection_sha256": base_projection_sha256,
            "evidence_artifact_id": evidence_artifact_id,
            "table_preview_sha256s": [
                item.preview_sha256 for item in table_preview_bindings
            ],
        }
    )
    return f"agent-run-{digest[:32]}"


__all__ = ["replay_accepted_native_structure_revisions"]
