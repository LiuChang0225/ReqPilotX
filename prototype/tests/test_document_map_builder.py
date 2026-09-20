from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

from app.adapters.persistence.memory_agent_structure_loop import (
    InMemoryAgentStructureLoopRepository,
)
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.agent_workflow.document_map_builder import (
    assemble_and_publish_document_map_working_revision,
    project_document_map_scope_replacement,
    restore_document_map_scope_projections,
)
from app.application.agent_workflow.document_map_replacement import (
    DocumentMapContentDisposition,
    compile_document_map_scope_replacement,
)
from app.application.agent_workflow.native_structure_bridge import (
    get_native_structure_reference,
)
from app.application.agent_workflow.native_structure_working_revision_bridge import (
    get_native_structure_working_revision,
    get_native_structure_working_revision_head,
    get_native_structure_working_revision_table_preview,
)
from tests.document_map_replacement_support import build_replacement_fixture


class _Response:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


class DocumentMapBuilderTests(unittest.TestCase):
    def test_agent_non_business_decision_is_kept_in_audit_not_in_readable_body(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = build_replacement_fixture(Path(raw) / "evidence")
            original = fixture.candidate.text_ownership_overrides[0]
            candidate = fixture.candidate.model_copy(
                update={
                    "text_ownership_overrides": (
                        original.model_copy(
                            update={
                                "disposition": DocumentMapContentDisposition.NON_BUSINESS
                            }
                        ),
                    ),
                }
            )
            compilation = compile_document_map_scope_replacement(
                request=fixture.request,
                candidate=candidate,
                evidence=fixture.evidence,
                opened_high_resolution_page_numbers=(1, 2),
                visual_receipt_root_sha256="f" * 64,
            )
            projection = project_document_map_scope_replacement(
                compilation=compilation, evidence=fixture.evidence
            )
            body = [
                content["text"]
                for node in projection.nodes
                for content in node["source_content"]
            ]
            self.assertNotIn("Closing statement for the chapter.", body)
            self.assertEqual(
                projection.nodes[0]["non_business_source_refs"][0]["source_node_id"],
                original.source_node_ids[0],
            )

    def test_survey_navigation_does_not_cut_a_chapter_at_a_shared_page(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            fixture = build_replacement_fixture(
                state / "evidence", split_survey_units=True
            )
            compilation = compile_document_map_scope_replacement(
                request=fixture.request,
                candidate=fixture.candidate,
                evidence=fixture.evidence,
                opened_high_resolution_page_numbers=(1, 2),
                visual_receipt_root_sha256="f" * 64,
            )
            projection = project_document_map_scope_replacement(
                compilation=compilation, evidence=fixture.evidence
            )
            revision = assemble_and_publish_document_map_working_revision(
                project_id="project-synthetic",
                document_id="document-synthetic",
                run_id=fixture.request.run_id,
                source_sha256=fixture.request.source_sha256,
                source_ir_canonical_sha256=fixture.request.scope_request.source_ir_canonical_sha256,
                evidence_workspace_bundle_sha256=fixture.request.scope_request.evidence_workspace_bundle_sha256,
                survey=fixture.survey,
                scope_replacements=(projection,),
                repository=InMemoryAgentStructureLoopRepository(),
                object_store=LocalObjectStore(state / "objects"),
                kit_bundle_sha256="3" * 64,
            )
            main_root = revision.document_tree["nodes"][0]
            self.assertEqual(main_root["pages"], [1, 2])
            chapter = next(
                item for item in revision.document_tree["nodes"] if item["label"] == "1"
            )
            self.assertEqual(chapter["parent_id"], main_root["node_id"])
            self.assertEqual(revision.page_ownership[0]["pages"], [1])
            other_root = next(
                node
                for node in revision.document_tree["nodes"]
                if node["node_id"] == revision.document_tree["root_ids"][1]
            )
            self.assertEqual(other_root["kind"], "DOCUMENT_UNIT")

    def test_projection_preserves_agent_table_group_and_exact_page_geometry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = build_replacement_fixture(Path(raw) / "evidence")
            compilation = compile_document_map_scope_replacement(
                request=fixture.request,
                candidate=fixture.candidate,
                evidence=fixture.evidence,
                opened_high_resolution_page_numbers=(1, 2),
                visual_receipt_root_sha256="f" * 64,
            )
            result = project_document_map_scope_replacement(
                compilation=compilation,
                evidence=fixture.evidence,
            )

            self.assertEqual(len(result.nodes), 2)
            self.assertEqual(len(result.logical_tables), 1)
            self.assertEqual(len(result.table_previews[0]["fragments"]), 2)
            self.assertEqual(
                [
                    item["column_count"]
                    for item in result.table_previews[0]["fragments"]
                ],
                [3, 4],
            )
            root = result.nodes[0]
            self.assertEqual(root["pages"], [1, 2])
            self.assertEqual(
                [item["text"] for item in root["source_content"]],
                [
                    "1 Scope",
                    "Lead text with exact trailing space. ",
                    "Closing statement for the chapter.",
                ],
            )
            self.assertEqual(
                root["source_content"][1]["source_bounds"], [0.0, 0.0, 612.0, 792.0]
            )
            child = result.nodes[1]
            self.assertEqual(len(child["content_blocks"]), 1)
            self.assertEqual(len(child["asset_placements"]), 1)
            self.assertEqual(
                child["source_anchors"][-1]["placement_id"],
                fixture.evidence.records[-1].source_node_id,
            )

    def test_partial_working_revision_serves_tables_without_fake_full_coverage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            fixture = build_replacement_fixture(state / "evidence")
            compilation = compile_document_map_scope_replacement(
                request=fixture.request,
                candidate=fixture.candidate,
                evidence=fixture.evidence,
                opened_high_resolution_page_numbers=(1, 2),
                visual_receipt_root_sha256="f" * 64,
            )
            projection = project_document_map_scope_replacement(
                compilation=compilation,
                evidence=fixture.evidence,
            )
            object_store = LocalObjectStore(state / "objects")
            repository = InMemoryAgentStructureLoopRepository()
            revision = assemble_and_publish_document_map_working_revision(
                project_id="project-synthetic",
                document_id="document-synthetic",
                run_id=fixture.request.run_id,
                source_sha256=fixture.request.source_sha256,
                source_ir_canonical_sha256=fixture.request.scope_request.source_ir_canonical_sha256,
                evidence_workspace_bundle_sha256=fixture.request.scope_request.evidence_workspace_bundle_sha256,
                survey=fixture.survey,
                scope_replacements=(projection,),
                repository=repository,
                object_store=object_store,
                kit_bundle_sha256="3" * 64,
            )
            self.assertEqual(revision.validator_kind, "D2_HOST_COMPILER")
            self.assertEqual(revision.state, "WORKING")
            self.assertEqual(revision.document_tree["nodes"][0]["source_anchors"], [])
            self.assertEqual(
                revision.document_tree["nodes"][0]["title_authority"],
                "AGENT_SURVEY_LABEL",
            )
            self.assertEqual(revision.visual_summary["receipts"][0]["sha256"], "f" * 64)
            restored = restore_document_map_scope_projections(
                revision=revision,
                compilations=(compilation,),
                survey=fixture.survey,
                kit_bundle_sha256="3" * 64,
                object_store=object_store,
            )
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0].compilation, compilation)
            self.assertEqual(
                restored[0].nodes[0]["source_content"],
                projection.nodes[0]["source_content"],
            )
            self.assertEqual(len(restored[0].table_previews[0]["fragments"]), 2)
            with self.assertRaisesRegex(ValueError, "compiler receipt drifted"):
                restore_document_map_scope_projections(
                    revision=revision,
                    compilations=(compilation,),
                    survey=fixture.survey,
                    kit_bundle_sha256="e" * 64,
                    object_store=object_store,
                )
            document = {
                "id": "document-synthetic",
                "project_id": "project-synthetic",
                "file_name": "synthetic.pdf",
                "source_sha256": fixture.request.source_sha256,
            }
            with (
                patch(
                    "app.application.agent_workflow.native_structure_working_revision_bridge._require_project_document",
                    return_value=document,
                ),
                patch(
                    "app.application.agent_workflow.native_structure_working_revision_bridge._runtime_repository",
                    return_value=repository,
                ),
                patch(
                    "app.application.agent_workflow.native_structure_working_revision_bridge._runtime_object_store",
                    return_value=object_store,
                ),
            ):
                head = get_native_structure_working_revision_head(
                    "project-synthetic", "document-synthetic", _Response()
                )
                self.assertEqual(head["head"]["validator_kind"], "D2_HOST_COMPILER")
                full = get_native_structure_working_revision(
                    "project-synthetic",
                    "document-synthetic",
                    revision.run_id,
                    1,
                    _Response(),
                    expected_revision_sha256=revision.revision_sha256,
                )
                self.assertEqual(len(full["document_tree"]["nodes"]), 3)
                preview = get_native_structure_working_revision_table_preview(
                    "project-synthetic",
                    "document-synthetic",
                    revision.run_id,
                    1,
                    projection.logical_tables[0]["scope_id"],
                    _Response(),
                    expected_revision_sha256=revision.revision_sha256,
                )
                self.assertEqual(len(preview["fragments"]), 2)

            with (
                patch(
                    "app.application.agent_workflow.native_structure_bridge._require_project_document",
                    return_value=document,
                ),
                patch(
                    "app.application.agent_workflow.native_structure_bridge._structure_loop_repository",
                    return_value=repository,
                ),
                patch(
                    "app.application.agent_workflow.native_structure_working_revision_bridge._runtime_object_store",
                    return_value=object_store,
                ),
                patch(
                    "app.application.agent_workflow.native_structure_bridge._read_feedbacks",
                    return_value=[],
                ),
                patch(
                    "app.application.agent_workflow.native_structure_bridge._read_proposal_decisions",
                    return_value=[],
                ),
                patch(
                    "app.application.agent_workflow.native_structure_bridge._read_handoffs",
                    return_value=[],
                ),
                patch(
                    "app.application.agent_workflow.native_structure_bridge._read_handoff_events",
                    return_value=[],
                ),
                patch(
                    "app.application.agent_workflow.native_structure_bridge._read_all_handoff_attempts",
                    return_value=[],
                ),
            ):
                public = get_native_structure_reference(
                    "project-synthetic", "document-synthetic", _Response()
                )
                self.assertEqual(public["coverage_status"], "PARTIAL")
                self.assertEqual(len(public["logical_tables"]), 1)
                self.assertEqual(
                    public["working_revision_head"],
                    revision.head().model_dump(mode="json"),
                )
                self.assertNotEqual(
                    public["structure_projection"]["projection_sha256"],
                    revision.base_projection_sha256,
                )
                self.assertEqual(public["visual_summary"]["opened_page_count"], 2)
                self.assertEqual(public["repeatability"]["status"], "NOT_RUN")
                self.assertEqual(
                    public["oracle_diagnostic"]["authority"], "NOT_APPLICABLE"
                )
            next_revision = assemble_and_publish_document_map_working_revision(
                project_id="project-synthetic",
                document_id="document-synthetic",
                run_id=fixture.request.run_id,
                source_sha256=fixture.request.source_sha256,
                source_ir_canonical_sha256=fixture.request.scope_request.source_ir_canonical_sha256,
                evidence_workspace_bundle_sha256="e" * 64,
                survey=fixture.survey,
                scope_replacements=restored,
                repository=repository,
                object_store=object_store,
                kit_bundle_sha256="3" * 64,
                revision=2,
                previous=revision,
            )
            self.assertEqual(
                next_revision.evidence_artifact_id, revision.evidence_artifact_id
            )
            self.assertEqual(
                next_revision.previous_revision_sha256, revision.revision_sha256
            )


if __name__ == "__main__":
    unittest.main()
