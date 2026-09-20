from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import hashlib
import inspect
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import db
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.errors import ApplicationError
from app.application.extraction.entry_classification_overlay import (
    apply_accepted_entry_classification_overlay,
)
from app.application.extraction.entry_classification_repository import (
    append_entry_classification_snapshot,
    entry_classification_lineage_values,
    get_entry_classification_snapshot_or_initial,
)
from app.application.extraction.processing_pipeline import (
    LOCAL_STRUCTURE_PROJECTION_MODE,
)
from app.application.extraction.requirement_tree import (
    build_requirement_tree_snapshot,
)
from app.application.extraction.requirement_tree_snapshots import (
    RequirementTreeSnapshotError,
    clear_verified_requirement_tree_snapshot_cache,
    load_requirement_set_tree_snapshot,
    persist_requirement_tree_snapshot,
)
from app.application.extraction.working_tree_publication import (
    _assert_same_tree_projection,
    _working_tree_publication_key,
    publish_working_requirement_tree_draft,
)
from app.application.requirement_matrix import build_requirement_matrix_snapshot
from app.application.requirements import (
    get_document_lock_readiness,
    list_document_requirement_sets,
    lock_document_extraction,
    save_review_entry,
)
from app.application.requirements_contracts import (
    LockExtractionRequest,
    ReviewedRequirementEntry,
)
from app.application.runtime import RUNTIME_STATE
from app.domain.entry_classification import (
    EntryClassificationSnapshotV1,
    EntryClassificationType,
    accept_entry_classification_snapshot,
    build_initial_entry_classification_snapshot,
    revise_entry_classification_snapshot,
)
from app.domain.extraction.requirement_tree import (
    RequirementTreeBilingualContextV1,
    RequirementTreeBilingualSourceSpanV1,
    RequirementTreeMode,
    RequirementTreeState,
)

SOURCE_SHA256 = "a" * 64
TREE_SHA256 = "b" * 64
GEOMETRY_ID = "c" * 64
GEOMETRY_SHA256 = "d" * 64
ROOT_ID = "structure-node-" + "1" * 32
CLAUSE_ID = "structure-node-" + "2" * 32


def _payload() -> dict:
    return {
        "mode": LOCAL_STRUCTURE_PROJECTION_MODE,
        "source_sha256": SOURCE_SHA256,
        "pipeline": {"source_ir": {"status": "COMPLETE"}},
        "items": [
            {
                "id": "item-document-context",
                "target_structure": {"id": ROOT_ID, "kind": "DOCUMENT"},
                "source_order": 0,
                "title": "文档级本地分类依据",
                "text": "该证据只辅助后续语义判断，不制造需求条目。",
                "semantic_class": "TECHNICAL_INFORMATION",
                "reason_codes": ["document_context_signal"],
                "source_refs": [
                    {
                        "kind": "TEXT_RANGE",
                        "source_id": "source-document",
                        "owner_structure_node_id": ROOT_ID,
                        "start_offset": 0,
                        "end_offset": 20,
                    }
                ],
            },
            {
                "id": "item-1",
                "target_structure": {"id": CLAUSE_ID, "kind": "CLAUSE"},
                "source_order": 2,
                "title": "第一项要求",
                "text": "系统应记录第一项结果。",
                "page_start": 1,
                "page_end": 1,
                "source_refs": [
                    {
                        "kind": "TEXT_RANGE",
                        "source_id": "source-1",
                        "owner_structure_node_id": CLAUSE_ID,
                        "start_offset": 0,
                        "end_offset": 11,
                    }
                ],
                "content_blocks": [
                    {
                        "kind": "paragraph",
                        "text": "系统应记录第一项结果。",
                        "source_id": "source-1",
                    }
                ],
            },
            {
                "id": "item-2",
                "target_structure": {"id": CLAUSE_ID, "kind": "CLAUSE"},
                "source_order": 3,
                "title": "第二项要求",
                "text": "系统应记录第二项结果。",
                "page_start": 1,
                "page_end": 1,
                "source_refs": [
                    {
                        "kind": "TEXT_RANGE",
                        "source_id": "source-2",
                        "owner_structure_node_id": CLAUSE_ID,
                        "start_offset": 0,
                        "end_offset": 11,
                    }
                ],
            },
        ],
        "tables": [],
    }


def _workbench() -> dict:
    return {
        "project_id": "project-1",
        "document_id": "document-1",
        "source": {
            "project_id": "project-1",
            "document_id": "document-1",
            "source_ir_id": "source-ir-1",
            "source_sha256": SOURCE_SHA256,
            "preview_generation_id": "preview-1",
        },
        "source_pages": {
            "geometry_index": {
                "geometry_index_id": GEOMETRY_ID,
                "geometry_index_sha256": GEOMETRY_SHA256,
            }
        },
        "structure": {
            "base_snapshot_id": "base-1",
            "effective_snapshot_id": "effective-1",
            "effective_tree_sha256": TREE_SHA256,
            "revision": 0,
            "nodes": [
                {
                    "id": ROOT_ID,
                    "parent_id": None,
                    "kind": "DOCUMENT",
                    "order": 0,
                    "label": "测试文档",
                    "page_start": 1,
                    "page_end": 1,
                    "source_refs": [],
                    "health_codes": [],
                },
                {
                    "id": CLAUSE_ID,
                    "parent_id": ROOT_ID,
                    "kind": "CLAUSE",
                    "order": 1,
                    "label": "1 测试条款",
                    "page_start": 1,
                    "page_end": 1,
                    "source_refs": [],
                    "health_codes": [],
                },
            ],
        },
        "agent": {"status": "COMPLETED", "result_state": "STABLE"},
        "result": {},
    }


def _serialized_workbench(*, workbench: dict, payload: dict) -> dict:
    """Return the serializer contract, including its canonical Working tree."""

    serialized = copy.deepcopy(workbench)
    serialized["requirement_tree"] = build_requirement_tree_snapshot(
        workbench=serialized,
        payload=payload,
    ).model_dump(mode="json")
    return serialized


def _published_snapshot(
    *,
    workbench: dict,
    payload: dict,
    requirement_set_id: str,
    requirement_entry_id: str,
):
    working = build_requirement_tree_snapshot(
        workbench=workbench,
        payload=payload,
    )
    node = next(
        item
        for item in working.nodes
        if item.include_in_view
        and item.projection_role == "STRUCTURE_PROXY"
    )
    return build_requirement_tree_snapshot(
        workbench=workbench,
        payload=payload,
        mode=RequirementTreeMode.REQUIREMENT_SET,
        requirement_set_id=requirement_set_id,
        requirement_entries=[
            {
                "requirement_tree_node_id": node.id,
                "requirement_entry_id": requirement_entry_id,
                "workflow_node_id": node.workflow_node_id,
                "owner_structure_node_id": node.structure_node_id,
                "source_order": node.source_order,
                "title": node.label,
                "source_text": node.summary,
                "page_start": node.page_start,
                "page_end": node.page_end,
                "source_refs": list(node.source_refs),
                "status": node.status,
                "classification": node.classification,
                "projection_role": node.projection_role,
                "deterministic_rule_evidence": list(
                    node.deterministic_rule_evidence
                ),
                "include_in_view": True,
                "content_blocks": list(node.content_blocks),
                "table_refs": list(node.table_refs),
                "figure_refs": list(node.figure_refs),
            }
        ],
        forced_state=RequirementTreeState.READY,
    )


def _accepted_from_initial(
    initial: EntryClassificationSnapshotV1,
    *,
    entry_type: EntryClassificationType = EntryClassificationType.TECHNICAL,
    suffix: str = "fixture",
) -> EntryClassificationSnapshotV1:
    working = revise_entry_classification_snapshot(
        initial,
        overrides={
            entry.workflow_node_id: entry_type for entry in initial.entries
        },
        snapshot_id=f"working-{suffix}",
        created_at="2026-08-07T07:00:00+00:00",
    )
    return accept_entry_classification_snapshot(
        working,
        snapshot_id=f"accepted-{suffix}",
        accepted_at="2026-08-07T07:01:00+00:00",
        accepted_by="LOCAL_USER",
    )


def _persist_classification_head(
    tree,
    *,
    entry_type: EntryClassificationType,
    suffix: str,
    accept: bool,
) -> EntryClassificationSnapshotV1:
    initial = build_initial_entry_classification_snapshot(tree)
    current = get_entry_classification_snapshot_or_initial(initial)
    working = revise_entry_classification_snapshot(
        current,
        overrides={
            entry.workflow_node_id: entry_type for entry in current.entries
        },
        snapshot_id=f"working-{suffix}-{current.revision + 1}",
        created_at="2026-08-07T07:10:00+00:00",
    )
    if working is not current:
        working, _ = append_entry_classification_snapshot(
            base_snapshot=current,
            next_snapshot=working,
            command_id=f"working-{suffix}",
            command_sha256=hashlib.sha256(
                f"working-{suffix}".encode("utf-8")
            ).hexdigest(),
            expected_revision=current.revision,
            expected_snapshot_sha256=current.payload_sha256,
        )
    if not accept:
        return working
    accepted = accept_entry_classification_snapshot(
        working,
        snapshot_id=f"accepted-{suffix}-{working.revision + 1}",
        accepted_at="2026-08-07T07:11:00+00:00",
        accepted_by="LOCAL_USER",
    )
    accepted, _ = append_entry_classification_snapshot(
        base_snapshot=working,
        next_snapshot=accepted,
        command_id=f"accepted-{suffix}",
        command_sha256=hashlib.sha256(
            f"accepted-{suffix}".encode("utf-8")
        ).hexdigest(),
        expected_revision=working.revision,
        expected_snapshot_sha256=working.payload_sha256,
    )
    return accepted


def _bound_workbench(project_id: str, document_id: str) -> dict:
    workbench = _workbench()
    workbench["project_id"] = project_id
    workbench["document_id"] = document_id
    workbench["source"].update(
        {"project_id": project_id, "document_id": document_id}
    )
    return workbench


def _replacement_structure_workbench(original: dict) -> dict:
    replacement = copy.deepcopy(original)
    replacement["structure"].update(
        {
            "effective_snapshot_id": "effective-concurrent-2",
            "effective_tree_sha256": "e" * 64,
            "revision": 1,
        }
    )
    replacement["structure"]["nodes"][1]["label"] = (
        "1 并发替换后的条款"
    )
    return replacement


@contextmanager
def _publication_source(
    *,
    document: dict,
    payload: dict,
    current_workbench,
):
    with (
        patch(
            "app.application.extraction.review_support."
            "_require_project_document",
            return_value=document,
        ),
        patch(
            "app.application.extraction.structure_workspace."
            "_structure_agent_job_payload",
            return_value=(
                {"job_id": "job-1", "status": "COMPLETED"},
                payload,
                {"status": "COMPLETED"},
            ),
        ),
        patch(
            "app.application.extraction.document_jobs."
            "_llm_extraction_result_matches_runtime",
            return_value=True,
        ),
        patch(
            "app.application.extraction.structure_workspace."
            "_rebuild_structure_revision_context",
            return_value=(object(), object(), ()),
        ),
        patch(
            "app.application.extraction.structure_workspace."
            "_serialize_structure_workbench_for_api",
            side_effect=lambda **kwargs: _serialized_workbench(
                workbench=current_workbench(),
                payload=kwargs["payload"],
            ),
        ),
    ):
        yield


class WorkingTreePublicationTests(unittest.TestCase):
    def test_projection_assertion_rejects_removed_bilingual_context(self) -> None:
        working = build_requirement_tree_snapshot(
            workbench=_workbench(),
            payload=_payload(),
        )
        published = _published_snapshot(
            workbench=_workbench(),
            payload=_payload(),
            requirement_set_id="set-bilingual-context-mismatch",
            requirement_entry_id="database-1",
        )
        target = next(
            node for node in working.nodes if node.structure_node_id is not None
        )
        owner_id = str(target.structure_node_id)
        context = RequirementTreeBilingualContextV1(
            counterpart_group_id=f"bilingual-group-{'1' * 32}",
            canonical_structure_node_id=owner_id,
            member_structure_node_ids=(owner_id,),
            source_spans=(
                RequirementTreeBilingualSourceSpanV1(
                    bilingual_span_id=f"bilingual-span-{'2' * 32}",
                    language="en",
                    source_ref={"source_id": "source-bilingual"},
                    owner_structure_node_id=owner_id,
                    start_offset=0,
                    end_offset=10,
                    boundary_basis="SOURCE_BLOCK_BOUNDARY",
                ),
                RequirementTreeBilingualSourceSpanV1(
                    bilingual_span_id=f"bilingual-span-{'3' * 32}",
                    language="zh-CN",
                    source_ref={"source_id": "source-bilingual"},
                    owner_structure_node_id=owner_id,
                    start_offset=10,
                    end_offset=20,
                    boundary_basis="SOURCE_BLOCK_BOUNDARY",
                ),
            ),
        )
        working = working.model_copy(
            update={
                "nodes": tuple(
                    node.model_copy(update={"bilingual_context": context})
                    if node.id == target.id
                    else node
                    for node in working.nodes
                )
            }
        )

        with self.assertRaises(RequirementTreeSnapshotError):
            _assert_same_tree_projection(working, published)

    def test_projection_assertion_rejects_changed_classification(self) -> None:
        working = build_requirement_tree_snapshot(
            workbench=_workbench(),
            payload=_payload(),
        )
        published = _published_snapshot(
            workbench=_workbench(),
            payload=_payload(),
            requirement_set_id="set-classification-mismatch",
            requirement_entry_id="database-1",
        )
        published = published.model_copy(
            update={
                "nodes": tuple(
                    node.model_copy(
                        update={"classification": "TECHNICAL"}
                    )
                    if node.projection_role == "STRUCTURE_PROXY"
                    else node
                    for node in published.nodes
                )
            }
        )

        with self.assertRaises(Exception):
            _assert_same_tree_projection(working, published)

    def test_local_structure_proxies_publish_without_llm_or_activation(self) -> None:
        payload = _payload()
        workbench = _workbench()
        response = SimpleNamespace(headers={})
        inserted_ids: list[str] = []

        def insert_requirement(**kwargs):
            requirement_id = f"database-{len(inserted_ids) + 1}"
            inserted_ids.append(requirement_id)
            return {"id": requirement_id, **kwargs}

        with (
            patch(
                "app.application.extraction.review_support."
                "_require_project_document",
                return_value={
                    "id": "document-1",
                    "project_id": "project-1",
                    "source_sha256": SOURCE_SHA256,
                },
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_structure_agent_job_payload",
                return_value=(
                    {"job_id": "job-1", "status": "COMPLETED"},
                    payload,
                    {"status": "COMPLETED"},
                ),
            ),
            patch(
                "app.application.extraction.document_jobs."
                "_llm_extraction_result_matches_runtime",
                return_value=True,
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_rebuild_structure_revision_context",
                return_value=(object(), object(), ()),
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_serialize_structure_workbench_for_api",
                side_effect=lambda **kwargs: _serialized_workbench(
                    workbench=workbench,
                    payload=kwargs["payload"],
                ),
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "get_entry_classification_snapshot_or_initial",
                side_effect=_accepted_from_initial,
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "db.get_sor_document_publication_currentness_generation",
                return_value=0,
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "db.create_or_get_requirement_set_for_publication",
                return_value=({"id": "set-1", "version": 1}, True),
            ) as create_set,
            patch(
                "app.application.extraction.working_tree_publication."
                "_claim_equivalent_legacy_draft",
                return_value={},
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "db.insert_requirement",
                side_effect=insert_requirement,
            ) as insert,
            patch(
                "app.application.extraction.working_tree_publication."
                "persist_requirement_tree_snapshot",
                return_value={},
            ) as persist_snapshot,
            patch(
                "app.application.extraction.working_tree_publication."
                "db.activate_requirement_set",
            ) as activate,
        ):
            result = publish_working_requirement_tree_draft(
                "project-1",
                "document-1",
                response,
            )

        self.assertEqual(
            result["publication"]["source"],
            "LOCAL_STRUCTURE_PROXY",
        )
        self.assertEqual(result["publication"]["requirement_set_id"], "set-1")
        self.assertFalse(result["requirement_set_activated"])
        self.assertEqual(result["requirement_tree"]["state"], "READY")
        proxies = [
            node
            for node in result["requirement_tree"]["nodes"]
            if node["projection_role"] == "STRUCTURE_PROXY"
        ]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(
            {node["requirement_entry_id"] for node in proxies},
            {"database-1"},
        )
        evidence_bundle = result["requirement_tree"][
            "requirement_evidence_bundle"
        ]
        self.assertEqual(evidence_bundle["source_ir_id"], "source-ir-1")
        self.assertEqual(evidence_bundle["source_sha256"], SOURCE_SHA256)
        self.assertEqual(
            evidence_bundle["structure_snapshot_id"],
            "effective-1",
        )
        self.assertEqual(len(evidence_bundle["requirement_bindings"]), 1)
        evidence_binding = evidence_bundle["requirement_bindings"][0]
        self.assertEqual(
            evidence_binding["requirement_entry_id"],
            "database-1",
        )
        self.assertTrue(evidence_binding["source_refs"])
        self.assertEqual(
            proxies[0]["requirement_evidence_binding_id"],
            evidence_binding["binding_id"],
        )
        self.assertEqual(len(proxies[0]["deterministic_rule_evidence"]), 2)
        document_node = next(
            node
            for node in result["requirement_tree"]["nodes"]
            if node["id"] == ROOT_ID
        )
        self.assertEqual(
            document_node["deterministic_rule_evidence"][0]["authority"],
            "SUGGESTION_ONLY",
        )
        self.assertEqual(
            document_node["deterministic_rule_evidence"][0]["item_id"],
            "item-document-context",
        )
        self.assertFalse(
            any(
                node["kind"] == "REQUIREMENT"
                for node in result["requirement_tree"]["nodes"]
            )
        )
        self.assertEqual(create_set.call_count, 1)
        self.assertEqual(insert.call_count, 1)
        self.assertEqual(insert.call_args.kwargs["entry_kind"], "REFERENCE")
        self.assertEqual(persist_snapshot.call_count, 1)
        activate.assert_not_called()

    def test_failed_snapshot_removes_exact_inactive_draft(self) -> None:
        payload = _payload()
        workbench = _workbench()
        response = SimpleNamespace(headers={})

        with (
            patch(
                "app.application.extraction.review_support."
                "_require_project_document",
                return_value={
                    "id": "document-1",
                    "project_id": "project-1",
                    "source_sha256": SOURCE_SHA256,
                },
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_structure_agent_job_payload",
                return_value=(
                    {"job_id": "job-1", "status": "COMPLETED"},
                    payload,
                    {"status": "COMPLETED"},
                ),
            ),
            patch(
                "app.application.extraction.document_jobs."
                "_llm_extraction_result_matches_runtime",
                return_value=True,
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_rebuild_structure_revision_context",
                return_value=(object(), object(), ()),
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_serialize_structure_workbench_for_api",
                side_effect=lambda **kwargs: _serialized_workbench(
                    workbench=workbench,
                    payload=kwargs["payload"],
                ),
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "get_entry_classification_snapshot_or_initial",
                side_effect=_accepted_from_initial,
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "db.get_sor_document_publication_currentness_generation",
                return_value=0,
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "db.create_or_get_requirement_set_for_publication",
                return_value=({"id": "set-failed", "version": 2}, True),
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "_claim_equivalent_legacy_draft",
                return_value={},
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "db.insert_requirement",
                side_effect=lambda **kwargs: {
                    "id": f"database-{kwargs['req_code']}",
                    **kwargs,
                },
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "persist_requirement_tree_snapshot",
                side_effect=ValueError("snapshot hash mismatch"),
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "delete_requirement_set_tree_object",
            ) as delete_snapshot,
            patch(
                "app.application.extraction.working_tree_publication."
                "db.discard_inactive_requirement_set",
                return_value=True,
            ) as discard_set,
        ):
            with self.assertRaises(ApplicationError) as raised:
                publish_working_requirement_tree_draft(
                    "project-1",
                    "document-1",
                    response,
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "working_tree_snapshot_validation_failed",
        )
        delete_snapshot.assert_called_once_with(
            project_id="project-1",
            document_id="document-1",
            requirement_set_id="set-failed",
        )
        discard_set.assert_called_once_with(
            "document-1",
            "set-failed",
            project_id="project-1",
        )

    def test_publication_key_ignores_regenerated_preview_identity(self) -> None:
        first_workbench = _workbench()
        second_workbench = _workbench()
        second_workbench["source"]["preview_generation_id"] = "preview-2"
        first = build_requirement_tree_snapshot(
            workbench=first_workbench,
            payload=_payload(),
        )
        second = build_requirement_tree_snapshot(
            workbench=second_workbench,
            payload=_payload(),
        )

        self.assertEqual(
            _working_tree_publication_key(first),
            _working_tree_publication_key(second),
        )
        changed_workbench = _workbench()
        changed_workbench["structure"]["effective_tree_sha256"] = "e" * 64
        changed = build_requirement_tree_snapshot(
            workbench=changed_workbench,
            payload=_payload(),
        )
        self.assertNotEqual(
            _working_tree_publication_key(first),
            _working_tree_publication_key(changed),
        )

    def test_publication_key_keeps_pre_extension_v1_node_canonical_shape(self) -> None:
        snapshot = build_requirement_tree_snapshot(
            workbench=_workbench(),
            payload=_payload(),
        )

        presentation_keys = {
            "reading_event_id",
            "source_offset",
            "caption_source_order",
            "caption_source_offset",
        }

        def pre_extension_payload(value):
            if isinstance(value, dict):
                return {
                    key: pre_extension_payload(item)
                    for key, item in value.items()
                    if key not in presentation_keys
                }
            if isinstance(value, list):
                return [pre_extension_payload(item) for item in value]
            return value

        legacy_payload = {
            "schema_version": "working-tree-publication-key/v1",
            "source_sha256": snapshot.source_identity.source_sha256,
            "structure_identity": snapshot.structure_identity.model_dump(
                mode="json"
            ),
            "root_node_ids": list(snapshot.root_node_ids),
            "nodes": [
                pre_extension_payload(
                    node.model_dump(
                        mode="json",
                        exclude={
                            "requirement_entry_id",
                            "requirement_evidence_binding_id",
                            "editable_structure_node_ids",
                            "toc_entries",
                            "source_offset",
                        },
                    )
                )
                for node in snapshot.nodes
            ],
            "table_record_projections": [
                projection.model_dump(mode="json", exclude={"decision"})
                for projection in snapshot.table_record_projections
            ],
            "table_evidence_bindings": [
                binding.model_dump(mode="json")
                for binding in snapshot.table_evidence_bindings
            ],
        }
        legacy_key = hashlib.sha256(
            json.dumps(
                legacy_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        self.assertEqual(_working_tree_publication_key(snapshot), legacy_key)

    def test_publication_key_binds_classification_snapshot_id_and_sha(self) -> None:
        snapshot = build_requirement_tree_snapshot(
            workbench=_workbench(),
            payload=_payload(),
        )
        parameters = inspect.signature(
            _working_tree_publication_key
        ).parameters

        self.assertIn("entry_classification_snapshot_id", parameters)
        self.assertIn("entry_classification_snapshot_sha256", parameters)
        legacy_key = _working_tree_publication_key(snapshot)
        bound_key = _working_tree_publication_key(
            snapshot,
            entry_classification_snapshot_id="accepted-1",
            entry_classification_snapshot_sha256="c" * 64,
        )
        self.assertNotEqual(bound_key, legacy_key)
        self.assertNotEqual(
            bound_key,
            _working_tree_publication_key(
                snapshot,
                entry_classification_snapshot_id="accepted-2",
                entry_classification_snapshot_sha256="c" * 64,
            ),
        )
        self.assertNotEqual(
            bound_key,
            _working_tree_publication_key(
                snapshot,
                entry_classification_snapshot_id="accepted-1",
                entry_classification_snapshot_sha256="d" * 64,
            ),
        )
        with self.assertRaises(ValueError):
            _working_tree_publication_key(
                snapshot,
                entry_classification_snapshot_id="accepted-1",
            )
        with self.assertRaises(ValueError):
            _working_tree_publication_key(
                snapshot,
                entry_classification_snapshot_id="accepted-1",
                entry_classification_snapshot_sha256="not-a-sha",
            )


class RequirementSetPublicationPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_verified_requirement_tree_snapshot_cache()
        self.runtime_lineage_patch = patch(
            "app.application.requirement_matrix."
            "_requirement_set_has_current_runtime_lineage",
            return_value=True,
        )
        self.runtime_lineage_patch.start()
        self.addCleanup(self.runtime_lineage_patch.stop)
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.root = Path(self.temporary_directory.name)
        self.database_patch = patch.object(db, "DB_PATH", self.root / "app.db")
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        self.addCleanup(self.temporary_directory.cleanup)
        self.object_store = LocalObjectStore(self.root / "objects")
        self.container_patch = patch.object(
            RUNTIME_STATE,
            "container",
            SimpleNamespace(object_store=self.object_store),
        )
        self.container_patch.start()
        self.addCleanup(self.container_patch.stop)
        self.addCleanup(clear_verified_requirement_tree_snapshot_cache)
        db.init_db()
        self.project = db.create_project("Publication", "Customer")
        source_path = self.root / "source.pdf"
        source_path.write_bytes(b"source")
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name="source.pdf",
            file_type="pdf",
            file_path=str(source_path),
            extracted_text="",
            source_sha256=SOURCE_SHA256,
        )

    def _publish_accepted_fixture(
        self,
        *,
        suffix: str,
    ) -> tuple[dict, EntryClassificationSnapshotV1, dict, dict]:
        payload = _payload()
        workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        tree = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=payload,
        )
        accepted = _persist_classification_head(
            tree,
            entry_type=EntryClassificationType.TECHNICAL,
            suffix=suffix,
            accept=True,
        )
        with _publication_source(
            document=self.document,
            payload=payload,
            current_workbench=lambda: workbench,
        ):
            result = publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                SimpleNamespace(headers={}),
            )
        return result, accepted, workbench, payload

    def test_publish_rejects_missing_accepted_classification_before_draft(
        self,
    ) -> None:
        payload = _payload()
        workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        response = SimpleNamespace(headers={})

        with _publication_source(
            document=self.document,
            payload=payload,
            current_workbench=lambda: workbench,
        ):
            with self.assertRaises(ApplicationError) as blocked:
                publish_working_requirement_tree_draft(
                    self.project["id"],
                    self.document["id"],
                    response,
                )

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(
            blocked.exception.detail["code"],
            "entry_classification_acceptance_required",
        )
        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]),
            [],
        )

    def test_publish_uses_the_exact_durable_tree_after_replay_validation(
        self,
    ) -> None:
        payload = _payload()
        durable_workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        replay_workbench = copy.deepcopy(durable_workbench)
        durable_workbench["structure"]["nodes"][1].update(
            {
                "label": "1 持久化展示条款",
                "source_refs": [
                    {
                        "kind": "source_node",
                        "source_id": "durable-source-1",
                        "source_kind": "paragraph",
                        "page_number": 1,
                    }
                ],
            }
        )
        replay_workbench["structure"]["nodes"][1].update(
            {
                "label": "1 replay 重建条款",
                "source_refs": [
                    {
                        "kind": "source_node",
                        "source_id": "replay-source-1",
                        "source_kind": "paragraph",
                        "page_number": 1,
                    }
                ],
            }
        )
        durable_tree = build_requirement_tree_snapshot(
            workbench=durable_workbench,
            payload=payload,
        )
        replay_tree = build_requirement_tree_snapshot(
            workbench=replay_workbench,
            payload=payload,
        )
        self.assertNotEqual(
            durable_tree,
            replay_tree,
            "the fixture must exercise divergent presentation projections",
        )
        accepted = _persist_classification_head(
            durable_tree,
            entry_type=EntryClassificationType.TECHNICAL,
            suffix="durable-presentation",
            accept=True,
        )

        def serialize_by_projection_source(**kwargs):
            workbench = (
                durable_workbench
                if kwargs.get("source_ir") is None
                else replay_workbench
            )
            return _serialized_workbench(
                workbench=workbench,
                payload=kwargs["payload"],
            )

        with (
            patch(
                "app.application.extraction.review_support."
                "_require_project_document",
                return_value=self.document,
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_structure_agent_job_payload",
                return_value=(
                    {"job_id": "job-durable", "status": "COMPLETED"},
                    payload,
                    {"status": "COMPLETED"},
                ),
            ),
            patch(
                "app.application.extraction.document_jobs."
                "_llm_extraction_result_matches_runtime",
                return_value=True,
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_rebuild_structure_revision_context",
                return_value=(object(), object(), ()),
            ) as replay_validation,
            patch(
                "app.application.extraction.structure_workspace."
                "_serialize_structure_workbench_for_api",
                side_effect=serialize_by_projection_source,
            ) as serialize,
        ):
            result = publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                SimpleNamespace(headers={}),
            )

        self.assertEqual(
            result["publication"]["entry_classification_snapshot_id"],
            accepted.snapshot_id,
        )
        self.assertEqual(
            result["publication"]["entry_classification_snapshot_sha256"],
            accepted.payload_sha256,
        )
        self.assertGreaterEqual(replay_validation.call_count, 3)
        self.assertTrue(
            all(call.kwargs.get("source_ir") is None for call in serialize.mock_calls)
        )

    def test_expired_publication_is_recovered_in_place_and_not_reported_created(
        self,
    ) -> None:
        payload = _payload()
        workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        tree = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=payload,
        )
        accepted = _persist_classification_head(
            tree,
            entry_type=EntryClassificationType.TECHNICAL,
            suffix="expired-in-place",
            accept=True,
        )
        classified_tree = apply_accepted_entry_classification_overlay(
            tree,
            accepted,
        )
        publication_key = _working_tree_publication_key(
            classified_tree,
            entry_classification_snapshot_id=accepted.snapshot_id,
            entry_classification_snapshot_sha256=accepted.payload_sha256,
        )
        old_owner = "publication-owner-" + "a" * 32
        stranded, created = db.create_or_get_requirement_set_for_publication(
            self.project["id"],
            self.document["id"],
            publication_key=publication_key,
            publication_owner_token=old_owner,
            expected_publication_currentness_generation=0,
            source_hash=SOURCE_SHA256,
            entry_classification_snapshot_id=accepted.snapshot_id,
            entry_classification_snapshot_sha256=accepted.payload_sha256,
            entry_classification_lineage=(
                entry_classification_lineage_values(accepted)
            ),
            publication_lease_seconds=30,
            now_utc=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        self.assertTrue(created)

        with _publication_source(
            document=self.document,
            payload=payload,
            current_workbench=lambda: workbench,
        ):
            result = publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                SimpleNamespace(headers={}),
            )

        self.assertFalse(result["requirement_set_created"])
        self.assertEqual(
            result["publication"]["requirement_set_id"],
            stranded["id"],
        )
        self.assertEqual(
            result["publication"]["version"],
            stranded["version"],
        )

    def test_pre_attach_crash_replays_same_set_rows_and_object_safely(
        self,
    ) -> None:
        owner_a = "publication-owner-" + "a" * 32
        owner_b = "publication-owner-" + "b" * 32
        publication_key = "9" * 64
        now = datetime(2099, 1, 1, tzinfo=timezone.utc)
        requirement_set, created = (
            db.create_or_get_requirement_set_for_publication(
                self.project["id"],
                self.document["id"],
                publication_key=publication_key,
                publication_owner_token=owner_a,
                expected_publication_currentness_generation=0,
                source_hash=SOURCE_SHA256,
                publication_lease_seconds=60,
                now_utc=now,
            )
        )
        self.assertTrue(created)
        requirement_id = "requirement-" + "9" * 32

        def insert(owner: str, at: datetime) -> dict:
            return db.insert_requirement(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                requirement_set_id=requirement_set["id"],
                requirement_id=requirement_id,
                req_code="REQ0001",
                source_page=1,
                source_anchor="source-1",
                requirement_text="系统应记录第一项结果。",
                requirement_type="FUNCTIONAL",
                priority="MEDIUM",
                publication_owner_token=owner,
                now_utc=at,
            )

        inserted = insert(owner_a, now + timedelta(seconds=1))
        workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        snapshot = _published_snapshot(
            workbench=workbench,
            payload=_payload(),
            requirement_set_id=requirement_set["id"],
            requirement_entry_id=inserted["id"],
        )

        def crash_before_attach() -> None:
            raise RuntimeError("simulated hard crash after object write")

        with self.assertRaises(RuntimeError):
            persist_requirement_tree_snapshot(
                requirement_set_id=requirement_set["id"],
                snapshot=snapshot,
                publication_owner_token=owner_a,
                expected_publication_currentness_generation=0,
                before_attach=crash_before_attach,
            )
        stored_paths = [
            path
            for path in (self.root / "objects").rglob("*")
            if path.is_file()
        ]
        self.assertEqual(len(stored_paths), 1)
        stored_bytes = stored_paths[0].read_bytes()

        takeover, takeover_created = (
            db.create_or_get_requirement_set_for_publication(
                self.project["id"],
                self.document["id"],
                publication_key=publication_key,
                publication_owner_token=owner_b,
                expected_publication_currentness_generation=0,
                source_hash=SOURCE_SHA256,
                publication_lease_seconds=60,
                now_utc=now + timedelta(seconds=61),
            )
        )
        self.assertFalse(takeover_created)
        self.assertEqual(takeover["id"], requirement_set["id"])
        replayed = insert(owner_b, now + timedelta(seconds=62))
        self.assertEqual(replayed["id"], requirement_id)

        ready = persist_requirement_tree_snapshot(
            requirement_set_id=requirement_set["id"],
            snapshot=snapshot,
            publication_owner_token=owner_b,
            expected_publication_currentness_generation=0,
        )
        self.assertEqual(ready["publication_state"], "READY")
        self.assertEqual(stored_paths[0].read_bytes(), stored_bytes)
        with self.assertRaises(db.RequirementSetMutationBlocked):
            persist_requirement_tree_snapshot(
                requirement_set_id=requirement_set["id"],
                snapshot=snapshot,
                publication_owner_token=owner_a,
                expected_publication_currentness_generation=0,
            )
        self.assertEqual(stored_paths[0].read_bytes(), stored_bytes)

    def test_publish_rejects_working_classification_head_before_draft(
        self,
    ) -> None:
        payload = _payload()
        workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        tree = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=payload,
        )
        _persist_classification_head(
            tree,
            entry_type=EntryClassificationType.TECHNICAL,
            suffix="working-only",
            accept=False,
        )
        response = SimpleNamespace(headers={})

        with _publication_source(
            document=self.document,
            payload=payload,
            current_workbench=lambda: workbench,
        ):
            with self.assertRaises(ApplicationError) as blocked:
                publish_working_requirement_tree_draft(
                    self.project["id"],
                    self.document["id"],
                    response,
                )

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(
            blocked.exception.detail["code"],
            "entry_classification_acceptance_required",
        )
        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]),
            [],
        )

    def test_publish_rejects_accepted_snapshot_from_stale_tree_lineage(
        self,
    ) -> None:
        payload = _payload()
        accepted_workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        accepted_tree = build_requirement_tree_snapshot(
            workbench=accepted_workbench,
            payload=payload,
        )
        _persist_classification_head(
            accepted_tree,
            entry_type=EntryClassificationType.TECHNICAL,
            suffix="stale-tree",
            accept=True,
        )
        current_workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        current_workbench["structure"]["effective_snapshot_id"] = (
            "effective-2"
        )
        current_workbench["structure"]["effective_tree_sha256"] = "e" * 64
        response = SimpleNamespace(headers={})

        with _publication_source(
            document=self.document,
            payload=payload,
            current_workbench=lambda: current_workbench,
        ):
            with self.assertRaises(ApplicationError) as blocked:
                publish_working_requirement_tree_draft(
                    self.project["id"],
                    self.document["id"],
                    response,
                )

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(
            blocked.exception.detail["code"],
            "entry_classification_acceptance_required",
        )
        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]),
            [],
        )

    def test_accepted_classification_is_bound_to_published_draft(self) -> None:
        payload = _payload()
        workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        raw_tree = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=payload,
        )
        accepted = _persist_classification_head(
            raw_tree,
            entry_type=EntryClassificationType.NON_TECHNICAL,
            suffix="publish-bound",
            accept=True,
        )
        response = SimpleNamespace(headers={})

        with _publication_source(
            document=self.document,
            payload=payload,
            current_workbench=lambda: workbench,
        ):
            result = publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                response,
        )

        publication = result["publication"]
        self.assertIn("entry_classification_snapshot_id", publication)
        self.assertIn("entry_classification_snapshot_sha256", publication)
        self.assertEqual(
            publication["entry_classification_snapshot_id"],
            accepted.snapshot_id,
        )
        self.assertEqual(
            publication["entry_classification_snapshot_sha256"],
            accepted.payload_sha256,
        )
        requirement_set = db.get_requirement_set(
            publication["requirement_set_id"]
        )
        self.assertEqual(
            requirement_set["entry_classification_snapshot_id"],
            accepted.snapshot_id,
        )
        self.assertEqual(
            requirement_set["entry_classification_snapshot_sha256"],
            accepted.payload_sha256,
        )
        published_node = next(
            node
            for node in result["requirement_tree"]["nodes"]
            if node["projection_role"] == "STRUCTURE_PROXY"
        )
        raw_node = next(
            node
            for node in raw_tree.nodes
            if node.projection_role == "STRUCTURE_PROXY"
        )
        self.assertEqual(published_node["classification"], "NON_TECHNICAL")
        for field in (
            "id",
            "parent_id",
            "child_ids",
            "source_order",
            "label",
            "summary",
            "source_refs",
            "projection_role",
        ):
            expected = getattr(raw_node, field)
            if isinstance(expected, tuple):
                expected = list(expected)
            self.assertEqual(published_node[field], expected)
        stored_rows = db.list_requirements(
            self.project["id"],
            self.document["id"],
            requirement_set_id=publication["requirement_set_id"],
        )
        self.assertEqual(stored_rows[0]["entry_kind"], "REFERENCE")
        self.assertEqual(stored_rows[0]["requirement_type"], "FUNCTIONAL")
        loaded = load_requirement_set_tree_snapshot(requirement_set)
        loaded_node = next(
            node
            for node in loaded.nodes
            if node.projection_role == "STRUCTURE_PROXY"
        )
        self.assertEqual(loaded_node.classification, "NON_TECHNICAL")

        for column, tampered_value, original_value in (
            (
                "entry_classification_snapshot_id",
                "accepted-tampered",
                accepted.snapshot_id,
            ),
            (
                "entry_classification_snapshot_sha256",
                "f" * 64,
                accepted.payload_sha256,
            ),
        ):
            with self.subTest(column=column):
                with db.get_conn() as conn:
                    conn.execute(
                        f"UPDATE requirement_sets SET {column} = ? WHERE id = ?",
                        (tampered_value, publication["requirement_set_id"]),
                    )
                clear_verified_requirement_tree_snapshot_cache()
                with self.assertRaises(RequirementTreeSnapshotError):
                    load_requirement_set_tree_snapshot(
                        db.get_requirement_set(
                            publication["requirement_set_id"]
                        )
                    )
                with db.get_conn() as conn:
                    conn.execute(
                        f"UPDATE requirement_sets SET {column} = ? WHERE id = ?",
                        (original_value, publication["requirement_set_id"]),
                    )

    def test_publish_rejects_accepted_snapshot_superseded_during_publish(
        self,
    ) -> None:
        payload = _payload()
        workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        tree = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=payload,
        )
        accepted = _persist_classification_head(
            tree,
            entry_type=EntryClassificationType.TECHNICAL,
            suffix="concurrent-unlock",
            accept=True,
        )

        def supersede_before_publication(
            current_tree,
            current_snapshot,
        ):
            unlocked = revise_entry_classification_snapshot(
                current_snapshot,
                overrides={
                    entry.workflow_node_id:
                        EntryClassificationType.NON_TECHNICAL
                    for entry in current_snapshot.entries
                },
                snapshot_id="working-concurrent-unlock",
                created_at="2026-08-07T08:00:00+00:00",
            )
            append_entry_classification_snapshot(
                base_snapshot=current_snapshot,
                next_snapshot=unlocked,
                command_id="concurrent-unlock",
                command_sha256=hashlib.sha256(
                    b"concurrent-unlock"
                ).hexdigest(),
                expected_revision=current_snapshot.revision,
                expected_snapshot_sha256=current_snapshot.payload_sha256,
            )
            return apply_accepted_entry_classification_overlay(
                current_tree,
                current_snapshot,
            )

        with (
            _publication_source(
                document=self.document,
                payload=payload,
                current_workbench=lambda: workbench,
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "apply_accepted_entry_classification_overlay",
                side_effect=supersede_before_publication,
            ),
            self.assertRaises(ApplicationError) as blocked,
        ):
            publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                SimpleNamespace(headers={}),
            )

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(
            blocked.exception.detail["code"],
            "entry_classification_identity_changed",
        )
        self.assertEqual(accepted.status.value, "ACCEPTED")
        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]),
            [],
        )

    def test_publish_rejects_structure_replaced_after_classification_overlay(
        self,
    ) -> None:
        payload = _payload()
        original_workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        tree = build_requirement_tree_snapshot(
            workbench=original_workbench,
            payload=payload,
        )
        _persist_classification_head(
            tree,
            entry_type=EntryClassificationType.TECHNICAL,
            suffix="concurrent-structure-replacement",
            accept=True,
        )
        current = {"workbench": original_workbench}

        def replace_structure_after_overlay(
            current_tree,
            current_snapshot,
        ):
            overlaid = apply_accepted_entry_classification_overlay(
                current_tree,
                current_snapshot,
            )
            current["workbench"] = _replacement_structure_workbench(
                original_workbench
            )
            return overlaid

        with (
            _publication_source(
                document=self.document,
                payload=payload,
                current_workbench=lambda: current["workbench"],
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "apply_accepted_entry_classification_overlay",
                side_effect=replace_structure_after_overlay,
            ),
            self.assertRaises(ApplicationError) as blocked,
        ):
            publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                SimpleNamespace(headers={}),
            )

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(
            blocked.exception.detail["code"],
            "working_tree_identity_changed",
        )
        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]),
            [],
        )
        with db.get_conn() as conn:
            requirement_count = conn.execute(
                "SELECT COUNT(*) AS count FROM requirements"
            ).fetchone()
        self.assertEqual(int(requirement_count["count"]), 0)
        self.assertEqual(
            [
                path
                for path in (self.root / "objects").rglob("*")
                if path.is_file()
            ],
            [],
        )

    def test_publish_cleans_new_draft_when_structure_changes_after_tree_write(
        self,
    ) -> None:
        payload = _payload()
        original_workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        tree = build_requirement_tree_snapshot(
            workbench=original_workbench,
            payload=payload,
        )
        _persist_classification_head(
            tree,
            entry_type=EntryClassificationType.TECHNICAL,
            suffix="structure-replaced-after-tree-write",
            accept=True,
        )
        current = {"workbench": original_workbench}

        def persist_then_replace_structure(**kwargs):
            before_attach = kwargs.pop("before_attach")

            def replace_then_validate():
                current["workbench"] = _replacement_structure_workbench(
                    original_workbench
                )
                before_attach()

            return persist_requirement_tree_snapshot(
                **kwargs,
                before_attach=replace_then_validate,
            )

        with (
            _publication_source(
                document=self.document,
                payload=payload,
                current_workbench=lambda: current["workbench"],
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "persist_requirement_tree_snapshot",
                side_effect=persist_then_replace_structure,
            ),
            self.assertRaises(ApplicationError) as blocked,
        ):
            publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                SimpleNamespace(headers={}),
            )

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(
            blocked.exception.detail["code"],
            "working_tree_identity_changed",
        )
        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]),
            [],
        )
        with db.get_conn() as conn:
            requirement_count = conn.execute(
                "SELECT COUNT(*) AS count FROM requirements"
            ).fetchone()
        self.assertEqual(int(requirement_count["count"]), 0)
        self.assertEqual(
            [
                path
                for path in (self.root / "objects").rglob("*")
                if path.is_file()
            ],
            [],
        )

    def test_existing_ready_publish_rechecks_structure_before_return(
        self,
    ) -> None:
        result, _accepted, original_workbench, payload = (
            self._publish_accepted_fixture(
                suffix="existing-ready-structure-recheck"
            )
        )
        requirement_set_id = result["publication"]["requirement_set_id"]
        current = {"workbench": original_workbench}

        def load_then_replace_structure(requirement_set):
            snapshot = load_requirement_set_tree_snapshot(requirement_set)
            current["workbench"] = _replacement_structure_workbench(
                original_workbench
            )
            return snapshot

        with (
            _publication_source(
                document=self.document,
                payload=payload,
                current_workbench=lambda: current["workbench"],
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "load_requirement_set_tree_snapshot",
                side_effect=load_then_replace_structure,
            ),
            self.assertRaises(ApplicationError) as blocked,
        ):
            publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                SimpleNamespace(headers={}),
            )

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(
            blocked.exception.detail["code"],
            "working_tree_identity_changed",
        )
        requirement_sets = db.list_requirement_sets(
            self.project["id"], self.document["id"]
        )
        self.assertEqual(
            [item["id"] for item in requirement_sets],
            [requirement_set_id],
        )
        self.assertTrue(
            str(requirement_sets[0]["requirement_tree_object_key"])
        )

    def test_failed_final_check_never_deletes_object_without_owner_cleanup(
        self,
    ) -> None:
        payload = _payload()
        original_workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        tree = build_requirement_tree_snapshot(
            workbench=original_workbench,
            payload=payload,
        )
        _persist_classification_head(
            tree,
            entry_type=EntryClassificationType.TECHNICAL,
            suffix="concurrently-activated-before-cleanup",
            accept=True,
        )
        current = {"workbench": original_workbench}

        def persist_then_replace_structure(**kwargs):
            before_attach = kwargs.pop("before_attach")

            def replace_then_validate():
                current["workbench"] = _replacement_structure_workbench(
                    original_workbench
                )
                before_attach()

            return persist_requirement_tree_snapshot(
                **kwargs,
                before_attach=replace_then_validate,
            )

        def ownership_changed_before_discard(*_args, **_kwargs) -> bool:
            return False

        with (
            _publication_source(
                document=self.document,
                payload=payload,
                current_workbench=lambda: current["workbench"],
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "persist_requirement_tree_snapshot",
                side_effect=persist_then_replace_structure,
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "db.discard_inactive_requirement_set",
                side_effect=ownership_changed_before_discard,
            ),
            self.assertRaises(ApplicationError) as blocked,
        ):
            publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                SimpleNamespace(headers={}),
            )

        self.assertEqual(
            blocked.exception.detail["code"],
            "working_tree_identity_changed",
        )
        retained = db.list_requirement_sets(
            self.project["id"], self.document["id"]
        )
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["publication_state"], "PUBLISHING")
        self.assertEqual(retained[0]["requirement_tree_object_key"], "")
        self.assertEqual(
            len(
                [
                    path
                    for path in (self.root / "objects").rglob("*")
                    if path.is_file()
                ]
            ),
            1,
        )

    def test_loader_rejects_classified_tree_with_removed_publication_key(
        self,
    ) -> None:
        result, _accepted, _workbench, _payload_value = (
            self._publish_accepted_fixture(suffix="removed-key")
        )
        requirement_set_id = result["publication"]["requirement_set_id"]
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE requirement_sets SET publication_key = '' WHERE id = ?",
                (requirement_set_id,),
            )
        clear_verified_requirement_tree_snapshot_cache()

        with self.assertRaises(RequirementTreeSnapshotError):
            load_requirement_set_tree_snapshot(
                db.get_requirement_set(requirement_set_id)
            )

    def test_loader_rejects_missing_or_invalid_classification_receipt(
        self,
    ) -> None:
        result, accepted, _workbench, _payload_value = (
            self._publish_accepted_fixture(suffix="missing-receipt")
        )
        requirement_set_id = result["publication"]["requirement_set_id"]
        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE requirement_sets
                SET entry_classification_snapshot_sha256 = 'short'
                WHERE id = ?
                """,
                (requirement_set_id,),
            )
        clear_verified_requirement_tree_snapshot_cache()
        with self.assertRaises(RequirementTreeSnapshotError):
            load_requirement_set_tree_snapshot(
                db.get_requirement_set(requirement_set_id)
            )

        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE requirement_sets
                SET entry_classification_snapshot_sha256 = ?
                WHERE id = ?
                """,
                (accepted.payload_sha256, requirement_set_id),
            )
            conn.execute(
                "DELETE FROM entry_classification_snapshots WHERE id = ?",
                (accepted.snapshot_id,),
            )
        clear_verified_requirement_tree_snapshot_cache()
        with self.assertRaises(RequirementTreeSnapshotError):
            load_requirement_set_tree_snapshot(
                db.get_requirement_set(requirement_set_id)
            )

    def _create_active_tree_bound_requirement(
        self,
    ) -> tuple[dict, dict]:
        requirement_set = db.create_requirement_set(
            self.project["id"],
            self.document["id"],
            source_hash=SOURCE_SHA256,
            preview_generation_id="preview-1",
        )
        inserted = db.insert_requirement(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            requirement_set_id=requirement_set["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="source-1",
            requirement_text="系统应记录第一项结果。",
            raw_text="系统应记录第一项结果。",
            display_text="系统应记录第一项结果。",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
        )
        workbench = _workbench()
        workbench["project_id"] = self.project["id"]
        workbench["document_id"] = self.document["id"]
        workbench["source"].update(
            {
                "project_id": self.project["id"],
                "document_id": self.document["id"],
            }
        )
        snapshot = _published_snapshot(
            workbench=workbench,
            payload=_payload(),
            requirement_set_id=requirement_set["id"],
            requirement_entry_id=inserted["id"],
        )
        persist_requirement_tree_snapshot(
            requirement_set_id=requirement_set["id"],
            snapshot=snapshot,
        )
        db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            requirement_set["id"],
        )
        return requirement_set, inserted

    def test_concurrent_duplicate_key_returns_one_set_and_changed_key_versions(
        self,
    ) -> None:
        def create(publication_key: str):
            return db.create_or_get_requirement_set_for_publication(
                self.project["id"],
                self.document["id"],
                publication_key=publication_key,
                publication_owner_token=(
                    db.new_requirement_set_publication_owner_token()
                ),
                expected_publication_currentness_generation=0,
                source_hash=SOURCE_SHA256,
                preview_generation_id="preview-regenerated",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(lambda _: create("1" * 64), range(2))
            )

        self.assertEqual(
            {item[0]["id"] for item in results},
            {results[0][0]["id"]},
        )
        self.assertEqual(sorted(item[1] for item in results), [False, True])
        self.assertEqual(len(db.list_requirement_sets(
            self.project["id"], self.document["id"]
        )), 1)

        changed, created = create("2" * 64)
        self.assertTrue(created)
        self.assertEqual(changed["version"], 2)
        self.assertEqual(len(db.list_requirement_sets(
            self.project["id"], self.document["id"]
        )), 2)

    def test_publication_creation_persists_paired_classification_identity(
        self,
    ) -> None:
        parameters = inspect.signature(
            db.create_or_get_requirement_set_for_publication
        ).parameters

        self.assertIn("entry_classification_snapshot_id", parameters)
        self.assertIn("entry_classification_snapshot_sha256", parameters)
        tree = build_requirement_tree_snapshot(
            workbench=_bound_workbench(
                self.project["id"], self.document["id"]
            ),
            payload=_payload(),
        )
        accepted = _persist_classification_head(
            tree,
            entry_type=EntryClassificationType.TECHNICAL,
            suffix="db-identity",
            accept=True,
        )
        created, was_created = (
            db.create_or_get_requirement_set_for_publication(
                self.project["id"],
                self.document["id"],
                publication_key="3" * 64,
                publication_owner_token=(
                    db.new_requirement_set_publication_owner_token()
                ),
                expected_publication_currentness_generation=0,
                source_hash=SOURCE_SHA256,
                entry_classification_snapshot_id=accepted.snapshot_id,
                entry_classification_snapshot_sha256=accepted.payload_sha256,
                entry_classification_lineage=(
                    entry_classification_lineage_values(accepted)
                ),
            )
        )
        replay, replay_created = (
            db.create_or_get_requirement_set_for_publication(
                self.project["id"],
                self.document["id"],
                publication_key="3" * 64,
                publication_owner_token=(
                    db.new_requirement_set_publication_owner_token()
                ),
                expected_publication_currentness_generation=0,
                source_hash=SOURCE_SHA256,
                entry_classification_snapshot_id=accepted.snapshot_id,
                entry_classification_snapshot_sha256=accepted.payload_sha256,
                entry_classification_lineage=(
                    entry_classification_lineage_values(accepted)
                ),
            )
        )

        self.assertTrue(was_created)
        self.assertFalse(replay_created)
        self.assertEqual(replay["id"], created["id"])
        self.assertEqual(
            created["entry_classification_snapshot_id"],
            accepted.snapshot_id,
        )
        self.assertEqual(
            created["entry_classification_snapshot_sha256"],
            accepted.payload_sha256,
        )
        with self.assertRaises(RuntimeError):
            db.create_or_get_requirement_set_for_publication(
                self.project["id"],
                self.document["id"],
                publication_key="3" * 64,
                publication_owner_token=(
                    db.new_requirement_set_publication_owner_token()
                ),
                expected_publication_currentness_generation=0,
                source_hash=SOURCE_SHA256,
                entry_classification_snapshot_id="accepted-db-conflict",
                entry_classification_snapshot_sha256="7" * 64,
                entry_classification_lineage=(
                    entry_classification_lineage_values(accepted)
                ),
            )
        with self.assertRaises(ValueError):
            db.create_or_get_requirement_set_for_publication(
                self.project["id"],
                self.document["id"],
                publication_key="5" * 64,
                publication_owner_token=(
                    db.new_requirement_set_publication_owner_token()
                ),
                expected_publication_currentness_generation=0,
                entry_classification_snapshot_id="accepted-db-2",
            )
        with self.assertRaises(ValueError):
            db.create_or_get_requirement_set_for_publication(
                self.project["id"],
                self.document["id"],
                publication_key="6" * 64,
                publication_owner_token=(
                    db.new_requirement_set_publication_owner_token()
                ),
                expected_publication_currentness_generation=0,
                entry_classification_snapshot_id="accepted-db-3",
                entry_classification_snapshot_sha256="invalid",
            )

    def test_repeated_publish_reuses_exact_receipt_but_not_legacy_draft(
        self,
    ) -> None:
        payload = _payload()
        workbench = _workbench()
        workbench["project_id"] = self.project["id"]
        workbench["document_id"] = self.document["id"]
        workbench["source"].update(
            {
                "project_id": self.project["id"],
                "document_id": self.document["id"],
            }
        )
        raw_tree = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=payload,
        )
        _persist_classification_head(
            raw_tree,
            entry_type=EntryClassificationType.TECHNICAL,
            suffix="repeat-publish",
            accept=True,
        )
        response = SimpleNamespace(headers={})
        patches = (
            patch(
                "app.application.extraction.review_support."
                "_require_project_document",
                return_value=self.document,
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_structure_agent_job_payload",
                return_value=(
                    {"job_id": "job-1", "status": "COMPLETED"},
                    payload,
                    {"status": "COMPLETED"},
                ),
            ),
            patch(
                "app.application.extraction.document_jobs."
                "_llm_extraction_result_matches_runtime",
                return_value=True,
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_rebuild_structure_revision_context",
                return_value=(object(), object(), ()),
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_serialize_structure_workbench_for_api",
                side_effect=lambda **kwargs: _serialized_workbench(
                    workbench=workbench,
                    payload=kwargs["payload"],
                ),
            ),
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patch(
                "app.application.extraction.working_tree_publication."
                "persist_requirement_tree_snapshot",
                wraps=persist_requirement_tree_snapshot,
            ) as persist_snapshot,
        ):
            first = publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                response,
            )
            second = publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                response,
            )

            self.assertTrue(first["requirement_set_created"])
            self.assertFalse(second["requirement_set_created"])
            self.assertEqual(
                first["publication"]["requirement_set_id"],
                second["publication"]["requirement_set_id"],
            )
            self.assertEqual(len(db.list_requirements(
                self.project["id"], self.document["id"]
            )), 0)
            set_id = first["publication"]["requirement_set_id"]
            self.assertEqual(len(db.list_requirements(
                self.project["id"],
                self.document["id"],
                requirement_set_id=set_id,
            )), 1)
            self.assertEqual(len(db.list_requirement_sets(
                self.project["id"], self.document["id"]
            )), 1)
            self.assertEqual(persist_snapshot.call_count, 1)

            # A pre-W3 DRAFT has no accepted classification receipt.  Even
            # with identical tree bytes it must not be claimed as the current
            # classified publication.
            with db.get_conn() as conn:
                conn.execute(
                    """
                    UPDATE requirement_sets SET
                        publication_key = '', publication_state = '',
                        entry_classification_snapshot_id = '',
                        entry_classification_snapshot_sha256 = ''
                    WHERE id = ?
                    """,
                    (set_id,),
                )
            legacy_retry = publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                response,
            )
            self.assertTrue(legacy_retry["requirement_set_created"])
            self.assertNotEqual(
                legacy_retry["publication"]["requirement_set_id"], set_id
            )
            self.assertEqual(len(db.list_requirement_sets(
                self.project["id"], self.document["id"]
            )), 2)
            self.assertEqual(persist_snapshot.call_count, 2)

            changed_workbench = dict(workbench)
            changed_workbench["structure"] = dict(workbench["structure"])
            changed_workbench["structure"][
                "effective_tree_sha256"
            ] = "e" * 64
            workbench = changed_workbench
            changed_tree = build_requirement_tree_snapshot(
                workbench=changed_workbench,
                payload=payload,
            )
            _persist_classification_head(
                changed_tree,
                entry_type=EntryClassificationType.TECHNICAL,
                suffix="changed-publish",
                accept=True,
            )
            changed = publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                response,
            )
            self.assertTrue(changed["requirement_set_created"])
            self.assertEqual(changed["publication"]["version"], 3)
            self.assertEqual(len(db.list_requirement_sets(
                self.project["id"], self.document["id"]
            )), 3)
            self.assertEqual(persist_snapshot.call_count, 3)

    def test_verified_tree_lists_candidates_and_locks_without_legacy_preview(
        self,
    ) -> None:
        requirement_set = db.create_requirement_set(
            self.project["id"],
            self.document["id"],
            source_hash=SOURCE_SHA256,
            preview_generation_id="preview-1",
        )
        inserted = db.insert_requirement(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            requirement_set_id=requirement_set["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="source-1",
            requirement_text="系统应记录第一项结果。",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
        )
        workbench = _workbench()
        workbench["project_id"] = self.project["id"]
        workbench["document_id"] = self.document["id"]
        workbench["source"].update(
            {
                "project_id": self.project["id"],
                "document_id": self.document["id"],
            }
        )
        snapshot = _published_snapshot(
            workbench=workbench,
            payload=_payload(),
            requirement_set_id=requirement_set["id"],
            requirement_entry_id=inserted["id"],
        )
        persist_requirement_tree_snapshot(
            requirement_set_id=requirement_set["id"],
            snapshot=snapshot,
        )
        db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            requirement_set["id"],
        )
        db.update_sor_document_preview_state(
            self.document["id"],
            preview_status="NOT_READY",
        )

        versions = list_document_requirement_sets(
            self.project["id"],
            self.document["id"],
        )
        listed = versions["requirement_sets"][0]
        self.assertEqual(listed["requirement_tree_state"], "READY")
        self.assertEqual(listed["review_candidate_entry_count"], 1)
        self.assertEqual(listed["structure_proxy_count"], 1)
        self.assertEqual(listed["direct_content_count"], 0)
        self.assertEqual(listed["whole_table_count"], 0)
        self.assertEqual(listed["semantic_requirement_count"], 0)
        self.assertEqual(listed["stored_review_entry_count"], 1)
        self.assertTrue(listed["tree_lock_ready"])

        with self.assertRaises(ApplicationError) as changed_selection:
            lock_document_extraction(
                self.project["id"],
                self.document["id"],
                LockExtractionRequest(requirement_set_id="another-set"),
            )
        self.assertEqual(
            changed_selection.exception.detail["code"],
            "requirement_set_selection_changed",
        )
        expected_matrix = build_requirement_matrix_snapshot(
            self.project["id"],
            self.document["id"],
            requirement_set["id"],
        )
        readiness = get_document_lock_readiness(
            self.project["id"],
            self.document["id"],
            requirement_set["id"],
        )
        self.assertEqual(
            readiness["schema_version"],
            "requirement-set-lock-readiness/v1",
        )
        self.assertEqual(len(readiness["readiness_sha256"]), 64)
        self.assertEqual(
            readiness["counts"]["analysis_targets"],
            expected_matrix.counts.analyze_nodes,
        )
        self.assertEqual(
            readiness["next_destination"],
            "ANALYSIS" if expected_matrix.counts.analyze_nodes else "MATRIX",
        )
        self.assertEqual(
            readiness["count_metadata"],
            {
                "content_classification": "LEGACY_UNAVAILABLE",
                "entry_composition": "LEGACY_UNAVAILABLE",
                "source": "LEGACY_MATRIX",
            },
        )
        self.assertNotIn(
            "LEGACY_BREAKDOWN_METADATA_UNAVAILABLE",
            {warning["code"] for warning in readiness["warnings"]},
        )
        self.assertTrue(readiness["can_lock"])
        locked = lock_document_extraction(
            self.project["id"],
            self.document["id"],
            LockExtractionRequest(requirement_set_id=requirement_set["id"]),
        )
        self.assertEqual(
            locked["locked_requirement_set_id"],
            requirement_set["id"],
        )
        self.assertEqual(locked["locked_requirement_set_version"], 1)
        self.assertEqual(
            locked["locked_requirement_count"],
            expected_matrix.counts.analyze_nodes,
        )
        self.assertEqual(
            locked["locked_review_candidate_count"],
            expected_matrix.counts.total_nodes,
        )
        self.assertEqual(
            locked["locked_matrix_counts"],
            expected_matrix.counts.model_dump(mode="json"),
        )
        self.assertEqual(
            locked["readiness"]["lock"]["basis"],
            "REQUIREMENT_MATRIX_SNAPSHOT_V1",
        )

        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE requirement_sets
                SET requirement_tree_sha256 = ?
                WHERE id = ?
                """,
                ("f" * 64, requirement_set["id"]),
            )
        clear_verified_requirement_tree_snapshot_cache()
        invalid = list_document_requirement_sets(
            self.project["id"],
            self.document["id"],
        )["requirement_sets"][0]
        self.assertEqual(invalid["requirement_tree_state"], "INVALID")

        historical = db.create_requirement_set(
            self.project["id"],
            self.document["id"],
            source_hash=SOURCE_SHA256,
        )
        missing_versions = list_document_requirement_sets(
            self.project["id"],
            self.document["id"],
        )["requirement_sets"]
        missing = next(
            item
            for item in missing_versions
            if item["id"] == historical["id"]
        )
        self.assertEqual(missing["requirement_tree_state"], "MISSING")

    def test_excluded_candidate_can_be_restored_without_breaking_tree_binding(
        self,
    ) -> None:
        requirement_set, inserted = self._create_active_tree_bound_requirement()

        excluded = save_review_entry(
            self.project["id"],
            self.document["id"],
            ReviewedRequirementEntry(
                requirement_set_id=requirement_set["id"],
                entry_id=inserted["id"],
                req_code=inserted["req_code"],
                entry_kind="EXCLUDED",
                source_page=1,
                source_anchor="source-1",
                requirement_text="系统应记录第一项结果。",
                display_text="系统应记录第一项结果。",
                raw_text="系统应记录第一项结果。",
                requirement_type="FUNCTIONAL",
                priority="MEDIUM",
                keep=True,
            ),
        )

        self.assertEqual(excluded["review_save"]["action"], "updated")
        excluded_row = db.get_requirement(inserted["id"])
        self.assertIsNotNone(excluded_row)
        self.assertEqual(excluded_row["entry_kind"], "EXCLUDED")
        excluded_version = list_document_requirement_sets(
            self.project["id"],
            self.document["id"],
        )["requirement_sets"][0]
        self.assertEqual(excluded_version["requirement_tree_state"], "READY")
        self.assertTrue(excluded_version["tree_lock_ready"])
        self.assertEqual(excluded_version["included_requirement_count"], 0)
        self.assertEqual(excluded_version["excluded_count"], 1)

        restored = save_review_entry(
            self.project["id"],
            self.document["id"],
            ReviewedRequirementEntry(
                requirement_set_id=requirement_set["id"],
                entry_id=inserted["id"],
                req_code=inserted["req_code"],
                entry_kind="REQUIREMENT",
                source_page=1,
                source_anchor="source-1",
                requirement_text="系统应记录第一项结果。",
                display_text="系统应记录第一项结果。",
                raw_text="系统应记录第一项结果。",
                requirement_type="FUNCTIONAL",
                priority="MEDIUM",
                keep=True,
            ),
        )

        self.assertEqual(restored["review_save"]["action"], "updated")
        restored_row = db.get_requirement(inserted["id"])
        self.assertEqual(restored_row["entry_kind"], "REQUIREMENT")
        restored_version = list_document_requirement_sets(
            self.project["id"],
            self.document["id"],
        )["requirement_sets"][0]
        self.assertEqual(restored_version["requirement_tree_state"], "READY")
        self.assertTrue(restored_version["tree_lock_ready"])
        self.assertEqual(restored_version["included_requirement_count"], 1)
        self.assertEqual(restored_version["excluded_count"], 0)
        locked = lock_document_extraction(
            self.project["id"],
            self.document["id"],
            LockExtractionRequest(requirement_set_id=requirement_set["id"]),
        )
        self.assertEqual(
            locked["locked_requirement_set_id"],
            requirement_set["id"],
        )

    def test_ready_legacy_preview_does_not_replace_missing_requirement_tree(
        self,
    ) -> None:
        requirement_set = db.create_requirement_set(
            self.project["id"],
            self.document["id"],
            source_hash=SOURCE_SHA256,
        )
        db.insert_requirement(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            requirement_set_id=requirement_set["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="source-1",
            requirement_text="系统应记录第一项结果。",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
        )
        db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            requirement_set["id"],
        )

        with patch(
            "app.application.requirements.load_cached_preview",
            return_value=({}, {"preview_status": "READY"}),
        ):
            with self.assertRaises(ApplicationError) as blocked:
                lock_document_extraction(
                    self.project["id"],
                    self.document["id"],
                    LockExtractionRequest(
                        requirement_set_id=requirement_set["id"],
                    ),
                )

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(
            blocked.exception.detail["code"],
            "matrix_requires_reextraction",
        )
        readiness = blocked.exception.detail["readiness"]
        self.assertEqual(readiness["basis"], "REQUIREMENT_MATRIX_SNAPSHOT_V1")
        self.assertEqual(readiness["matrix_state"], "REQUIRES_REEXTRACTION")
        self.assertIn(
            "REQUIREMENT_MATRIX_REQUIRES_REEXTRACTION",
            {item["code"] for item in readiness["hard_blockers"]},
        )
        self.assertEqual(
            db.get_requirement_set(requirement_set["id"])["status"],
            "DRAFT",
        )


if __name__ == "__main__":
    unittest.main()
