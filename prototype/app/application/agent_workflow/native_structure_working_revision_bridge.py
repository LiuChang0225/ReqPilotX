from __future__ import annotations

import hashlib
import json
import re
from io import BytesIO
from typing import Any

from app.application.agent_workflow.native_structure_bridge import (
    _document_source_sha256,
    _no_store_headers,
    _require_project_document,
)
from app.application.agent_workflow.native_structure_working_revision import (
    NativeStructureWorkingRevisionError,
    NativeStructureWorkingRevisionObjectV1,
    NativeStructureWorkingRevisionV1,
    WORKING_REVISION_MEDIA_TYPE,
    canonical_json_sha256,
    native_structure_working_revision_bytes,
    native_structure_working_revision_object_key,
    validate_native_structure_table_preview,
    validate_native_structure_working_revision_lineage,
)
from app.application.errors import ApplicationError
from app.application.runtime import RUNTIME_STATE
from app.application.transport import ResponseSink
from app.ports.agent_structure_loop_repository import (
    AgentStructureLoopRepository,
)
from app.ports.object_store import ObjectStore


_RUN_ID = re.compile(r"^agent-run-[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_REVISION_OBJECT_BYTES = 16 * 1024 * 1024


def publish_native_structure_working_revision(
    *,
    project_id: str,
    document_id: str,
    revision: NativeStructureWorkingRevisionV1,
    repository: AgentStructureLoopRepository,
    object_store: ObjectStore,
) -> tuple[Any, bool]:
    verified = NativeStructureWorkingRevisionV1.model_validate(
        revision.model_dump(mode="python")
    )
    if verified.validator_kind not in ("ACCEPTED_REFERENCE_REPLAY", "D2_HOST_COMPILER"):
        raise NativeStructureWorkingRevisionError(
            "D2_HOST_COMPILER_REQUIRES_TYPED_SOURCE_EVIDENCE_RECEIPT"
        )
    predecessor = None
    if verified.revision > 1:
        predecessor_record = repository.get_working_revision_object(
            project_id=project_id,
            document_id=document_id,
            run_id=verified.run_id,
            revision=verified.revision - 1,
        )
        predecessor = load_native_structure_working_revision(
            record=predecessor_record,
            object_store=object_store,
        )
    validate_native_structure_working_revision_lineage(verified, predecessor)
    existing = repository.get_working_revision_object(
        project_id=project_id,
        document_id=document_id,
        run_id=verified.run_id,
        revision=verified.revision,
    )
    if existing is not None:
        persisted = load_native_structure_working_revision(
            record=existing,
            object_store=object_store,
        )
        if persisted.revision_sha256 != verified.revision_sha256:
            raise NativeStructureWorkingRevisionError(
                "working revision replay changed immutable content"
            )
        return persisted.head(), False
    encoded = native_structure_working_revision_bytes(verified)
    if len(encoded) > _MAX_REVISION_OBJECT_BYTES:
        raise NativeStructureWorkingRevisionError(
            "working revision object exceeds the publication budget"
        )
    object_sha256 = hashlib.sha256(encoded).hexdigest()
    object_key = native_structure_working_revision_object_key(verified)
    stored = object_store.put(
        key=object_key,
        source=BytesIO(encoded),
        expected_sha256=object_sha256,
        media_type=WORKING_REVISION_MEDIA_TYPE,
    )
    if (
        stored.key != object_key
        or stored.sha256 != object_sha256
        or stored.size != len(encoded)
        or stored.media_type != WORKING_REVISION_MEDIA_TYPE
    ):
        raise NativeStructureWorkingRevisionError(
            "working revision object-store receipt drifted"
        )
    return repository.append_working_revision(
        project_id=project_id,
        document_id=document_id,
        source_sha256=verified.source_sha256,
        base_artifact_id=verified.base_artifact_id,
        base_projection_sha256=verified.base_projection_sha256,
        revision=verified,
        object_key=stored.key,
        object_sha256=stored.sha256,
        object_size=stored.size,
        media_type=stored.media_type,
        expected_head_revision_sha256=verified.previous_revision_sha256,
    )


def load_native_structure_working_revision(
    *,
    record: NativeStructureWorkingRevisionObjectV1 | None,
    object_store: ObjectStore,
) -> NativeStructureWorkingRevisionV1:
    if record is None:
        raise NativeStructureWorkingRevisionError(
            "working revision object metadata is missing"
        )
    with object_store.open(record.object_key) as stream:
        encoded = stream.read(_MAX_REVISION_OBJECT_BYTES + 1)
    if len(encoded) != record.object_size:
        raise NativeStructureWorkingRevisionError(
            "working revision object size drifted"
        )
    if len(encoded) > _MAX_REVISION_OBJECT_BYTES:
        raise NativeStructureWorkingRevisionError(
            "working revision object exceeds the read budget"
        )
    if hashlib.sha256(encoded).hexdigest() != record.object_sha256:
        raise NativeStructureWorkingRevisionError(
            "working revision object SHA drifted"
        )
    try:
        revision = NativeStructureWorkingRevisionV1.model_validate_json(encoded)
    except Exception as exc:
        raise NativeStructureWorkingRevisionError(
            "working revision object payload is invalid"
        ) from exc
    if (
        revision.run_id != record.run_id
        or revision.revision_id != record.revision_id
        or revision.revision != record.revision
        or revision.revision_sha256 != record.revision_sha256
        or revision.previous_revision_sha256
        != record.previous_revision_sha256
        or revision.source_sha256 != record.source_sha256
        or revision.base_artifact_id != record.base_artifact_id
        or revision.base_projection_sha256 != record.base_projection_sha256
        or revision.evidence_artifact_id != record.evidence_artifact_id
        or revision.validator_kind != record.validator_kind
        or revision.validator_receipt_sha256
        != record.validator_receipt_sha256
        or revision.d2_compiler_receipt_sha256
        != record.d2_compiler_receipt_sha256
        or revision.state != record.state
        or revision.quality_claim != record.quality_claim
        or revision.handoff_id != record.handoff_id
        or revision.created_at_utc != record.created_at_utc
    ):
        raise NativeStructureWorkingRevisionError(
            "working revision object identity drifted"
        )
    return revision


def get_native_structure_working_revision_head(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    document = _require_project_document(project_id, document_id)
    response.headers.update(_no_store_headers())
    source_sha256 = _document_source_sha256(document)
    repository = _runtime_repository(required=False)
    if repository is not None:
        head = repository.get_working_revision_head(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
        )
        if head is not None and head.validator_kind == "D2_HOST_COMPILER":
            return {
                "schema_version": "native-structure-working-revision-head-response/v1",
                "source_sha256": source_sha256,
                "head": head.model_dump(mode="json"),
            }
    try:
        from app.application.agent_workflow.native_structure_baseline_jobs import (
            _current_reference_context,
        )

        current = _current_reference_context(project_id, document_id)
        projection = current["public_reference"]["structure_projection"]
        base_artifact_id = current["artifact_id"]
        base_projection_sha256 = projection["projection_sha256"]
    except ApplicationError:
        # ACCEPTED_REFERENCE_REPLAY is the only enabled producer in this slice,
        # so a head is current only when its accepted base can be revalidated.
        # A future initial-D2 bootstrap must use a typed source/evidence-bound
        # compiler receipt before D2_HOST_COMPILER heads are enabled.
        return {
            "schema_version": "native-structure-working-revision-head-response/v1",
            "source_sha256": source_sha256,
            "head": None,
        }
    head = (
        None
        if repository is None
        else repository.get_working_revision_head(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            base_artifact_id=base_artifact_id,
            base_projection_sha256=base_projection_sha256,
        )
    )
    return {
        "schema_version": "native-structure-working-revision-head-response/v1",
        "source_sha256": source_sha256,
        "head": None if head is None else head.model_dump(mode="json"),
    }


def get_native_structure_working_revision(
    project_id: str,
    document_id: str,
    run_id: str,
    revision: int,
    response: ResponseSink,
    expected_revision_sha256: str | None = None,
) -> dict[str, Any]:
    document = _require_project_document(project_id, document_id)
    response.headers.update(_no_store_headers())
    source_sha256 = _document_source_sha256(document)
    _validate_revision_locator(
        run_id=run_id,
        revision=revision,
        expected_revision_sha256=expected_revision_sha256,
    )
    repository = _runtime_repository(required=True)
    record = repository.get_working_revision_object(
        project_id=project_id,
        document_id=document_id,
        run_id=run_id,
        revision=revision,
    )
    if record is None or record.source_sha256 != source_sha256:
        raise _not_found()
    if (
        expected_revision_sha256 is not None
        and record.revision_sha256 != expected_revision_sha256
    ):
        raise _conflict(
            "native_structure_working_revision_changed",
            "The requested working revision SHA no longer matches this locator.",
        )
    loaded = load_native_structure_working_revision(
        record=record,
        object_store=_runtime_object_store(),
    )
    predecessor = None
    if loaded.revision > 1:
        predecessor = load_native_structure_working_revision(
            record=repository.get_working_revision_object(
                project_id=project_id,
                document_id=document_id,
                run_id=run_id,
                revision=loaded.revision - 1,
            ),
            object_store=_runtime_object_store(),
        )
    validate_native_structure_working_revision_lineage(loaded, predecessor)
    return loaded.model_dump(mode="json")


def get_native_structure_working_revision_table_preview(
    project_id: str,
    document_id: str,
    run_id: str,
    revision: int,
    scope_id: str,
    response: ResponseSink,
    expected_revision_sha256: str,
) -> dict[str, Any]:
    payload = get_native_structure_working_revision(
        project_id,
        document_id,
        run_id,
        revision,
        response,
        expected_revision_sha256=expected_revision_sha256,
    )
    logical_table = next(
        (
            item
            for item in payload.get("logical_tables", [])
            if isinstance(item, dict) and item.get("scope_id") == scope_id
        ),
        None,
    )
    if logical_table is None:
        raise _not_found()
    if payload.get("validator_kind") == "D2_HOST_COMPILER":
        if not payload.get("d2_compiler_receipt_sha256"):
            raise _working_table_not_materialized()
    elif payload.get("validator_kind") != "ACCEPTED_REFERENCE_REPLAY":
        raise _working_table_not_materialized()
    binding = next(
        (
            item
            for item in payload.get("table_preview_bindings", [])
            if item.get("scope_id") == scope_id
        ),
        None,
    )
    if binding is None:
        raise _working_table_not_materialized()
    store = _runtime_object_store()
    with store.open(str(binding["object_key"])) as stream:
        encoded = stream.read(_MAX_REVISION_OBJECT_BYTES + 1)
    if (
        len(encoded) != int(binding["object_size"])
        or hashlib.sha256(encoded).hexdigest() != binding["object_sha256"]
    ):
        raise _working_table_not_materialized()
    try:
        preview = json.loads(encoded)
    except Exception as exc:
        raise _working_table_not_materialized() from exc
    if not isinstance(preview, dict) or canonical_json_sha256(preview) != binding[
        "preview_sha256"
    ]:
        raise _working_table_not_materialized()
    try:
        validate_native_structure_table_preview(
            logical_table=logical_table,
            preview=preview,
            source_sha256=str(payload.get("source_sha256") or ""),
            artifact_id=str(payload.get("base_artifact_id") or ""),
        )
    except NativeStructureWorkingRevisionError as exc:
        raise _working_table_not_materialized() from exc
    return preview


def _validate_revision_locator(
    *,
    run_id: str,
    revision: int,
    expected_revision_sha256: str | None,
) -> None:
    if not _RUN_ID.fullmatch(run_id) or type(revision) is not int or revision < 1:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_working_revision_locator_invalid",
                "message": "The working revision locator is invalid.",
            },
        )
    if expected_revision_sha256 is not None and not _SHA256.fullmatch(
        expected_revision_sha256
    ):
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_working_revision_sha_invalid",
                "message": "The expected working revision SHA is invalid.",
            },
        )


def _runtime_repository(*, required: bool) -> AgentStructureLoopRepository | None:
    container = RUNTIME_STATE.container
    repository = (
        None if container is None else container.agent_structure_loop_repository
    )
    if repository is None and required:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "native_structure_working_revision_unavailable",
                "message": "Working revision persistence is unavailable.",
            },
        )
    return repository


def _runtime_object_store() -> ObjectStore:
    container = RUNTIME_STATE.container
    if container is None or container.object_store is None:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "native_structure_working_revision_unavailable",
                "message": "Working revision object storage is unavailable.",
            },
        )
    return container.object_store


def _not_found() -> ApplicationError:
    return ApplicationError(
        status_code=404,
        detail={
            "code": "native_structure_working_revision_not_found",
            "message": "The requested working revision was not found.",
        },
    )


def _conflict(code: str, message: str) -> ApplicationError:
    return ApplicationError(
        status_code=409,
        detail={"code": code, "message": message},
    )


def _working_table_not_materialized() -> ApplicationError:
    return _conflict(
        "working_revision_table_evidence_not_materialized",
        "This working revision has no immutable table evidence projection yet.",
    )


__all__ = [
    "get_native_structure_working_revision",
    "get_native_structure_working_revision_head",
    "get_native_structure_working_revision_table_preview",
    "load_native_structure_working_revision",
    "publish_native_structure_working_revision",
]
