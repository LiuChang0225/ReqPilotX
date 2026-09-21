from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import OrderedDict
from collections.abc import Callable
from io import BytesIO
from threading import RLock
from typing import Any, Iterable
from urllib.parse import quote

from reqpilotx_server import db
from reqpilotx_server.application.extraction.requirement_tree import (
    build_requirement_tree_snapshot,
)
from reqpilotx_server.application.runtime import RUNTIME_STATE
from reqpilotx_server.domain.extraction.requirement_tree import (
    REQUIREMENT_TREE_SCHEMA_VERSION,
    RequirementTreeMode,
    RequirementTreeSnapshotV1,
    RequirementTreeSourceIdentity,
    RequirementTreeState,
    RequirementTreeStructureIdentity,
    canonical_requirement_tree_bytes,
    canonical_requirement_tree_sha256,
)
from reqpilotx_server.domain.extraction.table_evidence import (
    RequirementEvidenceBindingV1,
    RequirementEvidenceBundleV1,
)


class RequirementTreeSnapshotError(RuntimeError):
    pass


_MAX_VERIFIED_REQUIREMENT_TREE_SNAPSHOTS = 8
_VERIFIED_REQUIREMENT_TREE_SNAPSHOT_LOCK = RLock()
_VERIFIED_REQUIREMENT_TREE_SNAPSHOTS: OrderedDict[
    tuple[int, str, str, str],
    tuple[object, RequirementTreeSnapshotV1],
] = OrderedDict()


def persist_published_requirement_tree_snapshot(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    publication_key: str,
    publication_entries: Iterable[dict[str, Any]],
    expected_source_sha256: str,
    expected_structure_snapshot_id: str,
) -> RequirementTreeSnapshotV1:
    """Freeze the current effective tree for one newly published DRAFT."""

    from reqpilotx_server.application.extraction.document_jobs import (
        _llm_extraction_result_matches_runtime,
    )
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.extraction.structure_workspace import (
        _rebuild_structure_revision_context,
        _serialize_structure_workbench_for_api,
        _structure_agent_job_payload,
    )

    document = _require_project_document(project_id, document_id)
    if str(document.get("source_sha256") or "") != expected_source_sha256:
        raise RequirementTreeSnapshotError(
            "document source changed before the Requirement Set tree was frozen"
        )
    job, payload, agent = _structure_agent_job_payload(
        project_id=project_id,
        document_id=document_id,
        document=document,
        require_final=True,
    )
    if not _llm_extraction_result_matches_runtime(payload):
        raise RequirementTreeSnapshotError(
            "the effective Structure must be regenerated for this runtime"
        )
    source_ir, base_tree, revisions = _rebuild_structure_revision_context(
        document=document,
        agent=agent,
    )
    workbench = _serialize_structure_workbench_for_api(
        project_id=project_id,
        document_id=document_id,
        document=document,
        job=job,
        payload=payload,
        agent=agent,
        source_ir=source_ir,
        base_tree=base_tree,
        revisions=revisions,
    )
    structure = _dict(workbench.get("structure"))
    effective_snapshot_id = str(structure.get("effective_snapshot_id") or "")
    base_snapshot_id = str(structure.get("base_snapshot_id") or "")
    if expected_structure_snapshot_id not in {
        effective_snapshot_id,
        base_snapshot_id,
    }:
        raise RequirementTreeSnapshotError(
            "review lineage no longer matches the effective Structure snapshot"
        )

    entries: list[dict[str, Any]] = []
    for entry in publication_entries:
        item = dict(entry)
        publication_entry_id = str(item.get("publication_entry_id") or "")
        item["requirement_entry_id"] = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"reqpilotx:{publication_key}:{publication_entry_id}",
            )
        )
        entries.append(item)
    snapshot = build_requirement_tree_snapshot(
        workbench=workbench,
        payload=payload,
        mode=RequirementTreeMode.REQUIREMENT_SET,
        requirement_set_id=requirement_set_id,
        requirement_entries=entries,
        forced_state=RequirementTreeState.READY,
    )
    persist_requirement_tree_snapshot(
        requirement_set_id=requirement_set_id,
        snapshot=snapshot,
    )
    return snapshot


def persist_requirement_tree_snapshot(
    *,
    requirement_set_id: str,
    snapshot: RequirementTreeSnapshotV1,
    publication_owner_token: str = "",
    expected_publication_currentness_generation: int | None = None,
    before_attach: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if snapshot.mode is not RequirementTreeMode.REQUIREMENT_SET:
        raise ValueError("only Requirement Set trees can be persisted")
    if snapshot.source_identity.requirement_set_id != requirement_set_id:
        raise ValueError("tree snapshot Requirement Set binding is inconsistent")
    if (
        snapshot.state is RequirementTreeState.READY
        and snapshot.requirement_evidence_bundle is None
    ):
        raise ValueError(
            "new READY Requirement Set trees require immutable requirement evidence"
        )
    container = RUNTIME_STATE.container
    if container is None:
        raise RequirementTreeSnapshotError("object storage is not initialized")

    payload = canonical_requirement_tree_bytes(snapshot)
    sha256 = canonical_requirement_tree_sha256(snapshot)
    object_key = _tree_object_key(
        project_id=snapshot.source_identity.project_id,
        document_id=snapshot.source_identity.document_id,
        requirement_set_id=requirement_set_id,
    )
    existing = container.object_store.stat(object_key)
    created_object = existing is None
    if existing is not None and existing.sha256 != sha256:
        raise RequirementTreeSnapshotError(
            "Requirement Set tree object already exists with different bytes"
        )
    try:
        if created_object:
            stored = container.object_store.put(
                key=object_key,
                source=BytesIO(payload),
                expected_sha256=sha256,
                media_type="application/json",
            )
            if stored.sha256 != sha256:
                raise RequirementTreeSnapshotError(
                    "object storage returned a mismatched tree snapshot hash"
                )
        if before_attach is not None:
            before_attach()
        attached = db.attach_requirement_tree_snapshot(
            requirement_set_id,
            object_key=object_key,
            tree_sha256=sha256,
            schema_version=REQUIREMENT_TREE_SCHEMA_VERSION,
            structure_snapshot_id=(
                snapshot.structure_identity.effective_snapshot_id
            ),
            structure_tree_sha256=(
                snapshot.structure_identity.effective_tree_sha256
            ),
            structure_revision=snapshot.structure_identity.revision,
            publication_owner_token=publication_owner_token,
            expected_publication_currentness_generation=(
                expected_publication_currentness_generation
            ),
        )
        if not attached:
            raise RequirementTreeSnapshotError(
                "Requirement Set disappeared before its tree object was attached"
            )
        return attached
    except BaseException:
        if created_object:
            with db.guard_unowned_requirement_tree_object_cleanup(
                project_id=snapshot.source_identity.project_id,
                sor_document_id=snapshot.source_identity.document_id,
                requirement_set_id=requirement_set_id,
            ) as cleanup_allowed:
                if cleanup_allowed:
                    candidate = container.object_store.stat(object_key)
                    if candidate is not None and candidate.sha256 == sha256:
                        container.object_store.delete(object_key)
        raise


def load_requirement_set_tree_snapshot(
    requirement_set: dict[str, Any],
) -> RequirementTreeSnapshotV1:
    """Load and fully verify the immutable tree owned by one version."""

    project_id = str(requirement_set.get("project_id") or "")
    document_id = str(requirement_set.get("sor_document_id") or "")
    requirement_set_id = str(requirement_set.get("id") or "")
    object_key = str(
        requirement_set.get("requirement_tree_object_key") or ""
    ).strip()
    expected_sha256 = str(
        requirement_set.get("requirement_tree_sha256") or ""
    ).strip()
    if not object_key or not expected_sha256:
        return missing_requirement_set_tree_snapshot(requirement_set)
    container = RUNTIME_STATE.container
    if container is None:
        raise RequirementTreeSnapshotError("object storage is not initialized")
    object_store = container.object_store
    cache_key = (
        id(object_store),
        requirement_set_id,
        object_key,
        expected_sha256,
    )
    with _VERIFIED_REQUIREMENT_TREE_SNAPSHOT_LOCK:
        cached = _VERIFIED_REQUIREMENT_TREE_SNAPSHOTS.get(cache_key)
        if cached is not None and cached[0] is object_store:
            snapshot = cached[1]
            _VERIFIED_REQUIREMENT_TREE_SNAPSHOTS.move_to_end(cache_key)
        else:
            stored = object_store.stat(object_key)
            if stored is None or stored.sha256 != expected_sha256:
                raise RequirementTreeSnapshotError(
                    "Requirement Set tree object is missing or has a different hash"
                )
            with object_store.open(object_key) as source:
                payload = source.read()
            if hashlib.sha256(payload).hexdigest() != expected_sha256:
                raise RequirementTreeSnapshotError(
                    "Requirement Set tree bytes failed SHA-256 verification"
                )
            try:
                raw_snapshot = json.loads(payload.decode("utf-8"))
                _drop_deprecated_embedded_focus(raw_snapshot)
                snapshot = RequirementTreeSnapshotV1.model_validate(
                    raw_snapshot
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise RequirementTreeSnapshotError(
                    "Requirement Set tree object is invalid"
                ) from exc
            _VERIFIED_REQUIREMENT_TREE_SNAPSHOTS[cache_key] = (
                object_store,
                snapshot,
            )
            _VERIFIED_REQUIREMENT_TREE_SNAPSHOTS.move_to_end(cache_key)
            while (
                len(_VERIFIED_REQUIREMENT_TREE_SNAPSHOTS)
                > _MAX_VERIFIED_REQUIREMENT_TREE_SNAPSHOTS
            ):
                _VERIFIED_REQUIREMENT_TREE_SNAPSHOTS.popitem(last=False)
    if (
        snapshot.source_identity.project_id != project_id
        or snapshot.source_identity.document_id != document_id
        or snapshot.source_identity.requirement_set_id != requirement_set_id
        or snapshot.source_identity.source_sha256
        != str(requirement_set.get("source_hash") or "")
        or snapshot.source_identity.preview_generation_id
        != str(requirement_set.get("preview_generation_id") or "")
        or snapshot.schema_version
        != str(requirement_set.get("requirement_tree_schema_version") or "")
        or snapshot.structure_identity.effective_snapshot_id
        != str(requirement_set.get("structure_snapshot_id") or "")
        or snapshot.structure_identity.effective_tree_sha256
        != str(requirement_set.get("structure_tree_sha256") or "")
        or snapshot.structure_identity.revision
        != int(requirement_set.get("structure_revision") or 0)
    ):
        raise RequirementTreeSnapshotError(
            "Requirement Set tree lineage does not match its database record"
        )
    enrichment_binding_id = str(
        requirement_set.get("enrichment_publication_binding_id") or ""
    ).strip()
    enrichment_binding_sha256 = str(
        requirement_set.get("enrichment_publication_binding_sha256") or ""
    ).strip()
    if enrichment_binding_id or enrichment_binding_sha256:
        _validate_requirement_enrichment_publication_binding_v2(
            requirement_set=requirement_set,
            snapshot=snapshot,
            binding_id=enrichment_binding_id,
            binding_sha256=enrichment_binding_sha256,
        )
        return snapshot
    publication_key = str(requirement_set.get("publication_key") or "").strip()
    supersedes_id = str(requirement_set.get("supersedes_id") or "").strip()
    if publication_key and supersedes_id:
        source_requirement_set = db.get_requirement_set(supersedes_id)
        if source_requirement_set is not None and (
            str(source_requirement_set.get("publication_key") or "").strip()
            or str(
                source_requirement_set.get(
                    "enrichment_publication_binding_id"
                )
                or ""
            ).strip()
        ):
            _validate_derived_requirement_set_snapshot(
                requirement_set=requirement_set,
                snapshot=snapshot,
                source_requirement_set=source_requirement_set,
            )
            return snapshot
    classification_snapshot_id = str(
        requirement_set.get("entry_classification_snapshot_id") or ""
    ).strip()
    classification_snapshot_sha256 = str(
        requirement_set.get("entry_classification_snapshot_sha256") or ""
    ).strip()
    has_w3_classification_projection = any(
        node.classification
        in {"TECHNICAL", "NON_TECHNICAL", "INFORMATION"}
        for node in snapshot.nodes
    )
    if (
        classification_snapshot_id
        or classification_snapshot_sha256
        or has_w3_classification_projection
    ):
        if not publication_key:
            raise RequirementTreeSnapshotError(
                "Requirement Set classification lineage has no publication key"
            )
        if not classification_snapshot_id or not classification_snapshot_sha256:
            raise RequirementTreeSnapshotError(
                "Requirement Set classification lineage is incomplete"
            )
        try:
            from reqpilotx_server.application.extraction.entry_classification_repository import (
                EntryClassificationSnapshotCorrupted,
                get_entry_classification_snapshot_by_id,
            )
            from reqpilotx_server.domain.entry_classification import (
                EntryClassificationStatus,
                entry_classification_nodes_in_tree_order,
            )

            classification_receipt = get_entry_classification_snapshot_by_id(
                classification_snapshot_id
            )
            projected_nodes = entry_classification_nodes_in_tree_order(snapshot)
            if (
                classification_receipt is None
                or classification_receipt.payload_sha256
                != classification_snapshot_sha256
                or classification_receipt.status
                is not EntryClassificationStatus.ACCEPTED
                or classification_receipt.project_id != project_id
                or classification_receipt.sor_document_id != document_id
                or classification_receipt.source_sha256
                != snapshot.source_identity.source_sha256
                or classification_receipt.effective_structure_id
                != snapshot.structure_identity.effective_snapshot_id
                or tuple(
                    (
                        entry.tree_node_id,
                        entry.workflow_node_id,
                        entry.projection_role,
                    )
                    for entry in classification_receipt.entries
                )
                != tuple(
                    (
                        node.id,
                        node.workflow_node_id or node.id,
                        node.projection_role,
                    )
                    for node in projected_nodes
                )
                or any(
                    node.classification != entry.effective_type.value
                    for node, entry in zip(
                        projected_nodes,
                        classification_receipt.entries,
                        strict=True,
                    )
                )
            ):
                raise RequirementTreeSnapshotError(
                    "Requirement Set classification receipt is not verifiable"
                )
        except RequirementTreeSnapshotError:
            raise
        except (EntryClassificationSnapshotCorrupted, ValueError) as exc:
            raise RequirementTreeSnapshotError(
                "Requirement Set classification receipt is invalid"
            ) from exc
    if publication_key:
        # The publication key freezes the exact working projection before
        # database entry identities are attached.  Recomputing it from the
        # immutable published snapshot closes the final lineage gap without
        # coupling derived (superseding) versions to a mutable current tree.
        from reqpilotx_server.application.extraction.working_tree_publication import (
            _working_tree_publication_key,
        )

        if bool(classification_snapshot_id) != bool(
            classification_snapshot_sha256
        ):
            raise RequirementTreeSnapshotError(
                "Requirement Set classification lineage is incomplete"
            )

        try:
            base_publication_key = _working_tree_publication_key(
                snapshot,
                entry_classification_snapshot_id=classification_snapshot_id,
                entry_classification_snapshot_sha256=(
                    classification_snapshot_sha256
                ),
            )
            acceptable_publication_keys = {base_publication_key}
            supersedes_id = str(
                requirement_set.get("supersedes_id") or ""
            ).strip()
            if supersedes_id:
                acceptable_publication_keys.add(
                    db.derived_requirement_set_publication_key(
                        base_publication_key=base_publication_key,
                        source_requirement_set_id=supersedes_id,
                        target_requirement_set_id=requirement_set_id,
                    )
                )
        except ValueError as exc:
            raise RequirementTreeSnapshotError(
                "Requirement Set publication identity is invalid"
            ) from exc
        if (
            str(requirement_set.get("publication_state") or "").upper()
            != "READY"
            or publication_key not in acceptable_publication_keys
        ):
            raise RequirementTreeSnapshotError(
                "Requirement Set publication lineage is not verifiable"
            )
    return snapshot


def _validate_derived_requirement_set_snapshot(
    *,
    requirement_set: dict[str, Any],
    snapshot: RequirementTreeSnapshotV1,
    source_requirement_set: dict[str, Any],
) -> None:
    """Verify a derived clone without pretending a W4 tree is a W3 publish.

    W4 publication bindings are immutable and set-local, so a derived DRAFT
    must not copy the source binding.  Instead it owns a derived publication
    key, an exact tree clone with new requirement row IDs, and the accepted
    classification identity proven by its direct source.
    """

    requirement_set_id = str(requirement_set.get("id") or "")
    source_requirement_set_id = str(source_requirement_set.get("id") or "")
    if (
        not requirement_set_id
        or not source_requirement_set_id
        or str(requirement_set.get("supersedes_id") or "")
        != source_requirement_set_id
        or str(requirement_set.get("project_id") or "")
        != str(source_requirement_set.get("project_id") or "")
        or str(requirement_set.get("sor_document_id") or "")
        != str(source_requirement_set.get("sor_document_id") or "")
        or int(source_requirement_set.get("version") or 0)
        >= int(requirement_set.get("version") or 0)
        or str(requirement_set.get("publication_state") or "").upper()
        != "READY"
        or str(requirement_set.get("enrichment_publication_binding_id") or "")
        or str(
            requirement_set.get("enrichment_publication_binding_sha256") or ""
        )
    ):
        raise RequirementTreeSnapshotError(
            "derived Requirement Set lineage is incomplete"
        )

    source_snapshot = load_requirement_set_tree_snapshot(source_requirement_set)
    classification_snapshot_id, classification_snapshot_sha256 = (
        _derived_source_classification_identity(source_requirement_set)
    )
    if (
        str(requirement_set.get("entry_classification_snapshot_id") or "")
        != classification_snapshot_id
        or str(
            requirement_set.get("entry_classification_snapshot_sha256") or ""
        )
        != classification_snapshot_sha256
    ):
        raise RequirementTreeSnapshotError(
            "derived Requirement Set classification lineage changed"
        )

    from reqpilotx_server.application.extraction.working_tree_publication import (
        _working_tree_publication_key,
    )

    try:
        base_publication_key = _working_tree_publication_key(
            snapshot,
            entry_classification_snapshot_id=classification_snapshot_id,
            entry_classification_snapshot_sha256=(
                classification_snapshot_sha256
            ),
        )
        expected_publication_key = db.derived_requirement_set_publication_key(
            base_publication_key=base_publication_key,
            source_requirement_set_id=source_requirement_set_id,
            target_requirement_set_id=requirement_set_id,
        )
    except ValueError as exc:
        raise RequirementTreeSnapshotError(
            "derived Requirement Set publication identity is invalid"
        ) from exc
    if str(requirement_set.get("publication_key") or "") != expected_publication_key:
        raise RequirementTreeSnapshotError(
            "derived Requirement Set publication identity changed"
        )

    project_id = str(requirement_set.get("project_id") or "")
    document_id = str(requirement_set.get("sor_document_id") or "")
    source_requirements = db.list_requirements(
        project_id,
        document_id,
        requirement_set_id=source_requirement_set_id,
    )
    target_requirements = db.list_requirements(
        project_id,
        document_id,
        requirement_set_id=requirement_set_id,
    )
    target_by_prior = {
        str(row.get("prior_requirement_id") or ""): str(row.get("id") or "")
        for row in target_requirements
        if str(row.get("prior_requirement_id") or "")
    }
    source_requirement_ids = {
        str(row.get("id") or "") for row in source_requirements
    }
    if (
        len(target_by_prior) != len(target_requirements)
        or set(target_by_prior) != source_requirement_ids
        or len(set(target_by_prior.values())) != len(target_by_prior)
    ):
        raise RequirementTreeSnapshotError(
            "derived Requirement Set requirement mapping is incomplete"
        )
    source_node_by_id = {node.id: node for node in source_snapshot.nodes}
    target_node_by_id = {node.id: node for node in snapshot.nodes}
    if set(source_node_by_id) != set(target_node_by_id):
        raise RequirementTreeSnapshotError(
            "derived Requirement Set node identity changed"
        )
    for node_id, source_node in source_node_by_id.items():
        target_node = target_node_by_id[node_id]
        source_requirement_id = str(source_node.requirement_entry_id or "")
        target_requirement_id = str(target_node.requirement_entry_id or "")
        if bool(source_requirement_id) != bool(target_requirement_id) or (
            source_requirement_id
            and target_by_prior.get(source_requirement_id)
            != target_requirement_id
        ):
            raise RequirementTreeSnapshotError(
                "derived Requirement Set tree requirement mapping changed"
            )
    if _derived_tree_semantic_payload(source_snapshot) != (
        _derived_tree_semantic_payload(snapshot)
    ):
        raise RequirementTreeSnapshotError(
            "derived Requirement Set tree is not an exact semantic clone"
        )


def _derived_source_classification_identity(
    source_requirement_set: dict[str, Any],
) -> tuple[str, str]:
    binding_id = str(
        source_requirement_set.get("enrichment_publication_binding_id") or ""
    ).strip()
    if not binding_id:
        return (
            str(
                source_requirement_set.get("entry_classification_snapshot_id")
                or ""
            ).strip(),
            str(
                source_requirement_set.get(
                    "entry_classification_snapshot_sha256"
                )
                or ""
            ).strip(),
        )
    container = RUNTIME_STATE.container
    if container is None:
        raise RequirementTreeSnapshotError(
            "derived enrichment lineage persistence is not initialized"
        )
    repository = container.requirement_enrichment_repository
    try:
        binding = repository.get_publication_binding_v2(
            requirement_set_id=str(source_requirement_set.get("id") or ""),
            binding_id=binding_id,
        )
        preview = (
            repository.get_publication_binding_preview_v2(
                project_id=binding.project_id,
                document_id=binding.document_id,
                session_id=binding.session_id,
                binding_preview_id=binding.binding_preview_id,
            )
            if binding is not None
            else None
        )
    except (AttributeError, RuntimeError, ValueError) as exc:
        raise RequirementTreeSnapshotError(
            "derived enrichment lineage is invalid"
        ) from exc
    if binding is None or preview is None:
        raise RequirementTreeSnapshotError(
            "derived enrichment lineage is not verifiable"
        )
    return (
        preview.projection.entry_classification_snapshot_id,
        preview.projection.entry_classification_sha256,
    )


def _derived_tree_semantic_payload(
    snapshot: RequirementTreeSnapshotV1,
) -> dict[str, Any]:
    payload = snapshot.model_dump(mode="json")
    source_identity = dict(payload.get("source_identity") or {})
    source_identity.pop("requirement_set_id", None)
    payload["source_identity"] = source_identity
    nodes = []
    for raw_node in payload.get("nodes") or []:
        node = dict(raw_node)
        node["requirement_entry_id"] = bool(node.get("requirement_entry_id"))
        node["requirement_evidence_binding_id"] = bool(
            node.get("requirement_evidence_binding_id")
        )
        nodes.append(node)
    payload["nodes"] = nodes
    bundle = payload.get("requirement_evidence_bundle")
    if isinstance(bundle, dict):
        normalized_bundle = dict(bundle)
        normalized_bundle.pop("evidence_sha256", None)
        normalized_bindings = []
        for raw_binding in normalized_bundle.get("requirement_bindings") or []:
            binding = dict(raw_binding)
            binding.pop("binding_id", None)
            binding.pop("evidence_sha256", None)
            binding["requirement_entry_id"] = "SET_LOCAL_REQUIREMENT"
            normalized_bindings.append(binding)
        normalized_bundle["requirement_bindings"] = normalized_bindings
        payload["requirement_evidence_bundle"] = normalized_bundle
    return payload


def _validate_requirement_enrichment_publication_binding_v2(
    *,
    requirement_set: dict[str, Any],
    snapshot: RequirementTreeSnapshotV1,
    binding_id: str,
    binding_sha256: str,
) -> None:
    """Verify W4 before the historical W3 publication-key branch."""

    if not binding_id or not re.fullmatch(r"[0-9a-f]{64}", binding_sha256):
        raise RequirementTreeSnapshotError(
            "Requirement Set enrichment publication binding is incomplete"
        )
    if (
        str(requirement_set.get("publication_state") or "").upper() != "READY"
        or str(requirement_set.get("publication_key") or "").strip()
    ):
        raise RequirementTreeSnapshotError(
            "Requirement Set enrichment publication state is not verifiable"
        )
    container = RUNTIME_STATE.container
    if container is None:
        raise RequirementTreeSnapshotError("enrichment persistence is not initialized")
    repository = container.requirement_enrichment_repository
    requirement_set_id = str(requirement_set.get("id") or "")
    try:
        binding = repository.get_publication_binding_v2(
            requirement_set_id=requirement_set_id,
            binding_id=binding_id,
        )
    except (ValueError, RuntimeError) as exc:
        raise RequirementTreeSnapshotError(
            "Requirement Set enrichment publication binding is invalid"
        ) from exc
    if (
        binding is None
        or binding.payload_sha256 != binding_sha256
        or binding.project_id != snapshot.source_identity.project_id
        or binding.document_id != snapshot.source_identity.document_id
        or binding.requirement_set_id != requirement_set_id
        or binding.requirement_set_version
        != int(requirement_set.get("version") or 0)
        or binding.requirement_tree_object_key
        != str(requirement_set.get("requirement_tree_object_key") or "")
        or binding.requirement_tree_sha256
        != str(requirement_set.get("requirement_tree_sha256") or "")
        or binding.requirement_tree_schema_version != snapshot.schema_version
    ):
        raise RequirementTreeSnapshotError(
            "Requirement Set enrichment publication binding does not match its tree"
        )
    preview = repository.get_publication_binding_preview_v2(
        project_id=binding.project_id,
        document_id=binding.document_id,
        session_id=binding.session_id,
        binding_preview_id=binding.binding_preview_id,
    )
    if (
        preview is None
        or preview.payload_sha256 != binding.binding_preview_sha256
        or preview.review_revision != binding.review_revision
        or preview.base_active_requirement_set_id
        != binding.base_active_requirement_set_id
        or preview.projection.projection_sha256 != binding.projection_sha256
    ):
        raise RequirementTreeSnapshotError(
            "Requirement Set enrichment binding preview is not verifiable"
        )
    projection = preview.projection
    try:
        from reqpilotx_server.application.extraction.entry_classification_repository import (
            EntryClassificationSnapshotCorrupted,
            get_entry_classification_snapshot_by_id,
        )
        from reqpilotx_server.domain.entry_classification import EntryClassificationStatus

        classification = get_entry_classification_snapshot_by_id(
            projection.entry_classification_snapshot_id
        )
        anchor_classification_by_id = {
            row.requirement_tree_node_id: row.classification
            for row in projection.rows
            if row.requirement_tree_node_id == row.source_working_tree_node_id
        }
        if (
            classification is None
            or classification.status is not EntryClassificationStatus.ACCEPTED
            or classification.payload_sha256
            != projection.entry_classification_sha256
            or classification.revision != projection.entry_classification_revision
            or classification.project_id != projection.project_id
            or classification.sor_document_id != projection.document_id
            or classification.source_sha256 != projection.source_sha256
            or classification.working_tree_id
            != projection.accepted_working_tree_id
            or classification.working_tree_sha256
            != projection.accepted_working_tree_sha256
            or classification.effective_structure_id
            != projection.effective_structure_id
            or tuple(entry.tree_node_id for entry in classification.entries)
            != projection.source_business_tree_node_ids
            or any(
                entry.effective_type is None
                or entry.effective_type.value
                != anchor_classification_by_id.get(entry.tree_node_id)
                for entry in classification.entries
            )
        ):
            raise RequirementTreeSnapshotError(
                "Requirement Set accepted classification is not verifiable"
            )
    except RequirementTreeSnapshotError:
        raise
    except (EntryClassificationSnapshotCorrupted, ValueError) as exc:
        raise RequirementTreeSnapshotError(
            "Requirement Set accepted classification is invalid"
        ) from exc

    from reqpilotx_server.domain.requirement_enrichment_publication_v2 import (
        RequirementEnrichmentAtomizationStateV2,
        canonical_publication_decision_trace_v2_json,
    )

    if projection.atomization_state is RequirementEnrichmentAtomizationStateV2.APPLIED:
        from reqpilotx_server.domain.extraction.requirement_enrichment import canonical_model_sha256

        run = repository.get_atomization(
            project_id=binding.project_id,
            document_id=binding.document_id,
            session_id=binding.session_id,
            atomization_run_id=projection.atomization_run_id,
        )
        if run is None:
            raise RequirementTreeSnapshotError(
                "Requirement Set atomization run is not verifiable"
            )
        receipt = repository.get_atomization_scope_receipt_revision(
            project_id=binding.project_id,
            document_id=binding.document_id,
            session_id=binding.session_id,
            lineage_sha256=run.lineage.lineage_sha256,
            scope_revision=projection.atomization_scope_revision,
        )
        core_preview = repository.get_publication_preview(
            project_id=binding.project_id,
            document_id=binding.document_id,
            session_id=binding.session_id,
            preview_id=projection.publication_preview_id,
        )
        if (
            receipt is None
            or receipt.receipt_id != projection.atomization_scope_receipt_id
            or receipt.payload_sha256
            != projection.atomization_scope_receipt_sha256
            or receipt.atomization_run_id != run.atomization_run_id
            or receipt.atomization_run_revision != run.revision
            or canonical_model_sha256(run) != projection.atomization_run_sha256
            or core_preview is None
            or canonical_model_sha256(core_preview)
            != projection.publication_preview_sha256
            or core_preview.atomization_run_id != run.atomization_run_id
        ):
            raise RequirementTreeSnapshotError(
                "Requirement Set atomization publication sidecars are not verifiable"
            )

    if tuple(item.projection_row for item in binding.row_bindings) != projection.rows:
        raise RequirementTreeSnapshotError(
            "Requirement Set publication row sidecars do not match the projection"
        )
    requirements = db.list_requirements(
        binding.project_id,
        binding.document_id,
        requirement_set_id=binding.requirement_set_id,
    )
    if len(requirements) != len(binding.row_bindings):
        raise RequirementTreeSnapshotError(
            "Requirement Set database rows do not exactly cover the projection"
        )
    requirement_entry_ids: dict[str, str] = {}
    for expected, actual in zip(
        binding.row_bindings,
        requirements,
        strict=True,
    ):
        row = expected.projection_row
        trace = actual.get("decision_reference")
        if not isinstance(trace, dict):
            trace = {}
        try:
            expected_trace_json = canonical_publication_decision_trace_v2_json(
                preview=preview,
                row_binding=expected,
            )
        except ValueError as exc:
            raise RequirementTreeSnapshotError(
                "Requirement Set canonical decision trace is invalid"
            ) from exc
        actual_trace_json = json.dumps(
            trace,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if (
            str(actual.get("id") or "") != expected.requirement_entry_id
            or str(actual.get("req_code") or "")
            != f"REQ{expected.publication_order + 1:04d}"
            or actual.get("source_page") != row.page_start
            or str(actual.get("source_anchor") or "")
            != f"requirement-tree:{row.source_working_tree_node_id}"
            or str(actual.get("requirement_text") or "")
            != row.published_statement
            or str(actual.get("requirement_type") or "") != row.classification
            or str(actual.get("entry_kind") or "") != row.entry_kind.value
            or str(actual.get("raw_text") or "") != row.original_statement
            or str(actual.get("display_text") or "") != row.published_statement
            or actual_trace_json != expected_trace_json
        ):
            raise RequirementTreeSnapshotError(
                "Requirement Set database row or trace does not match its binding"
            )
        requirement_entry_ids[expected.requirement_tree_node_id] = (
            expected.requirement_entry_id
        )
    try:
        from reqpilotx_server.application.extraction.requirement_enrichment_publication_v2 import (
            validate_requirement_enrichment_publication_tree_v2,
        )

        validate_requirement_enrichment_publication_tree_v2(
            tree=snapshot,
            projection=projection,
            requirement_entry_ids=requirement_entry_ids,
        )
    except ValueError as exc:
        raise RequirementTreeSnapshotError(
            "Requirement Set W4 tree/evidence projection is invalid"
        ) from exc


def clear_verified_requirement_tree_snapshot_cache() -> None:
    with _VERIFIED_REQUIREMENT_TREE_SNAPSHOT_LOCK:
        _VERIFIED_REQUIREMENT_TREE_SNAPSHOTS.clear()


def _drop_deprecated_embedded_focus(value: Any) -> None:
    """Normalize verified v1 snapshots after geometry moved to its own index."""

    if (
        not isinstance(value, dict)
        or value.get("schema_version") != REQUIREMENT_TREE_SCHEMA_VERSION
        or not isinstance(value.get("nodes"), list)
    ):
        return
    for node in value["nodes"]:
        if isinstance(node, dict):
            node.pop("focus_regions", None)


def clone_requirement_set_tree_snapshot(
    *,
    source_requirement_set: dict[str, Any],
    target_requirement_set: dict[str, Any],
    requirement_id_mapping: dict[str, str],
) -> RequirementTreeSnapshotV1:
    """Freeze a version-owned copy when a locked set is derived for editing."""

    source = load_requirement_set_tree_snapshot(source_requirement_set)
    if source.state is not RequirementTreeState.READY:
        raise RequirementTreeSnapshotError(
            "the source Requirement Set has no reviewable requirement tree"
        )
    target_id = str(target_requirement_set.get("id") or "")
    if (
        not target_id
        or str(target_requirement_set.get("project_id") or "")
        != source.source_identity.project_id
        or str(target_requirement_set.get("sor_document_id") or "")
        != source.source_identity.document_id
        or str(target_requirement_set.get("source_hash") or "")
        != source.source_identity.source_sha256
    ):
        raise RequirementTreeSnapshotError(
            "derived Requirement Set identity does not match the source tree"
        )

    source_bundle = source.requirement_evidence_bundle
    if source_bundle is None:
        raise RequirementTreeSnapshotError(
            "the source Requirement Set predates verifiable requirement evidence"
        )
    cloned_requirement_bindings: list[RequirementEvidenceBindingV1] = []
    cloned_binding_id_by_source_id: dict[str, str] = {}
    for binding in source_bundle.requirement_bindings:
        mapped_requirement_id = requirement_id_mapping.get(
            binding.requirement_entry_id
        )
        if not mapped_requirement_id:
            raise RequirementTreeSnapshotError(
                "derived Requirement Set cannot map every evidence binding"
            )
        cloned_binding = RequirementEvidenceBindingV1.create(
            requirement_entry_id=mapped_requirement_id,
            atomic_requirement_id=binding.atomic_requirement_id,
            source_spans=binding.source_spans,
            source_refs=binding.source_refs,
            table_evidence_binding_id=binding.table_evidence_binding_id,
        )
        cloned_requirement_bindings.append(cloned_binding)
        cloned_binding_id_by_source_id[binding.binding_id] = (
            cloned_binding.binding_id
        )
    cloned_bundle = RequirementEvidenceBundleV1.create(
        source_ir_id=source_bundle.source_ir_id,
        source_sha256=source_bundle.source_sha256,
        structure_snapshot_id=source_bundle.structure_snapshot_id,
        table_bindings=source_bundle.table_bindings,
        requirement_bindings=tuple(cloned_requirement_bindings),
    )

    nodes = []
    for node in source.nodes:
        requirement_entry_id = node.requirement_entry_id
        requirement_evidence_binding_id = (
            node.requirement_evidence_binding_id
        )
        if requirement_entry_id:
            mapped = requirement_id_mapping.get(requirement_entry_id)
            if not mapped:
                raise RequirementTreeSnapshotError(
                    "derived Requirement Set cannot map every requirement leaf"
                )
            mapped_evidence_id = cloned_binding_id_by_source_id.get(
                requirement_evidence_binding_id or ""
            )
            if not mapped_evidence_id:
                raise RequirementTreeSnapshotError(
                    "derived Requirement Set cannot map every node evidence pointer"
                )
            node = node.model_copy(
                update={
                    "requirement_entry_id": mapped,
                    "requirement_evidence_binding_id": mapped_evidence_id,
                }
            )
        nodes.append(node)
    cloned = RequirementTreeSnapshotV1.model_validate(
        source.model_copy(
            update={
                "source_identity": source.source_identity.model_copy(
                    update={
                        "requirement_set_id": target_id,
                        "preview_generation_id": str(
                            target_requirement_set.get("preview_generation_id")
                            or source.source_identity.preview_generation_id
                        ),
                    }
                ),
                "nodes": tuple(nodes),
                "requirement_evidence_bundle": cloned_bundle,
            }
        ).model_dump(mode="json")
    )
    persist_requirement_tree_snapshot(
        requirement_set_id=target_id,
        snapshot=cloned,
    )
    return cloned


def delete_requirement_set_tree_object(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> bool:
    """Remove only the exact object owned by a failed, rolled-back draft."""

    container = RUNTIME_STATE.container
    if container is None:
        return False
    return container.object_store.delete(
        _tree_object_key(
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=requirement_set_id,
        )
    )


def delete_unowned_requirement_set_tree_object(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> bool:
    """Delete an orphan only while replacement ownership is DB-serialized."""

    container = RUNTIME_STATE.container
    if container is None:
        return False
    with db.guard_unowned_requirement_tree_object_cleanup(
        project_id=project_id,
        sor_document_id=document_id,
        requirement_set_id=requirement_set_id,
    ) as cleanup_allowed:
        if not cleanup_allowed:
            return False
        return container.object_store.delete(
            _tree_object_key(
                project_id=project_id,
                document_id=document_id,
                requirement_set_id=requirement_set_id,
            )
        )


def missing_requirement_set_tree_snapshot(
    requirement_set: dict[str, Any],
) -> RequirementTreeSnapshotV1:
    """Fail closed for historical sets whose structure lineage is unprovable."""

    return RequirementTreeSnapshotV1(
        source_identity=RequirementTreeSourceIdentity(
            project_id=str(requirement_set.get("project_id") or ""),
            document_id=str(requirement_set.get("sor_document_id") or ""),
            source_sha256=str(requirement_set.get("source_hash") or ""),
            preview_generation_id=str(
                requirement_set.get("preview_generation_id") or ""
            ),
            requirement_set_id=str(requirement_set.get("id") or ""),
        ),
        structure_identity=RequirementTreeStructureIdentity(
            effective_snapshot_id=str(
                requirement_set.get("structure_snapshot_id") or ""
            ),
            effective_tree_sha256=str(
                requirement_set.get("structure_tree_sha256") or ""
            ),
            revision=max(int(requirement_set.get("structure_revision") or 0), 0),
        ),
        mode=RequirementTreeMode.REQUIREMENT_SET,
        state=RequirementTreeState.REQUIRES_REEXTRACTION,
        root_node_ids=(),
        nodes=(),
    )


def _tree_object_key(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
) -> str:
    return (
        "requirement-trees/"
        f"{quote(project_id, safe='')}/"
        f"{quote(document_id, safe='')}/"
        f"{quote(requirement_set_id, safe='')}/"
        "requirement-tree.v1.json"
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


__all__ = [
    "RequirementTreeSnapshotError",
    "clear_verified_requirement_tree_snapshot_cache",
    "clone_requirement_set_tree_snapshot",
    "delete_requirement_set_tree_object",
    "delete_unowned_requirement_set_tree_object",
    "load_requirement_set_tree_snapshot",
    "missing_requirement_set_tree_snapshot",
    "persist_published_requirement_tree_snapshot",
    "persist_requirement_tree_snapshot",
]
