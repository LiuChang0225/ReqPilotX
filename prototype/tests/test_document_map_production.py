from __future__ import annotations

# ruff: noqa: E402
from .runtime_isolation import activate_test_runtime

activate_test_runtime()

from pathlib import Path
import tempfile
import unittest

from app.adapters.persistence.memory_agent_structure_loop import (
    InMemoryAgentStructureLoopRepository,
)
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.agent_workflow.document_map_builder import (
    assemble_and_publish_document_map_working_revision,
    project_document_map_scope_replacement,
)
from app.application.agent_workflow.document_map_replacement import (
    compile_document_map_scope_replacement,
)
from app.application.agent_workflow.document_map_scope import (
    DocumentMapScopeRequestV1,
    DocumentMapHeadingScopeCandidateV1,
    compile_document_map_heading_scope,
)
from app.application.agent_workflow.native_structure_working_revision import (
    canonical_json_sha256,
)
from tests.document_map_replacement_support import build_replacement_fixture
from tests.test_document_map_scope import (
    _fixtures,
    _chapter_requests,
    _accepted_candidate,
)


class DocumentMapProductionTests(unittest.TestCase):
    def test_surveyed_unit_can_contain_multiple_real_root_chapters(self):
        source, survey, visual, skeleton = _fixtures()
        request, _ = _chapter_requests(
            source=source, survey=survey, visual=visual, skeleton=skeleton
        )
        values = request.model_dump(mode="json", exclude={"request_sha256"})
        values["kind"] = "SECTION_GROUP"
        request = DocumentMapScopeRequestV1(
            **values, request_sha256=canonical_json_sha256(values)
        )
        candidate = _accepted_candidate(request).model_dump(mode="json")
        for decision in candidate["heading_decisions"]:
            ref = next(
                item
                for item in request.heading_candidates
                if item.candidate_id == decision["candidate_id"]
            )
            if ref.number == "20":
                decision["disposition"] = "HEADING"
        compilation = compile_document_map_heading_scope(
            request=request,
            candidate=DocumentMapHeadingScopeCandidateV1.model_validate(candidate),
        )
        self.assertEqual(sum(item.level == 1 for item in compilation.heading_nodes), 2)

    def test_global_source_coverage_rejects_duplicate_and_missing_scope_claims(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = build_replacement_fixture(root / "evidence")
            compiled = compile_document_map_scope_replacement(
                request=fixture.request,
                candidate=fixture.candidate,
                evidence=fixture.evidence,
                opened_high_resolution_page_numbers=(1, 2),
                visual_receipt_root_sha256="f" * 64,
            )
            projection = project_document_map_scope_replacement(
                compilation=compiled, evidence=fixture.evidence
            )
            values = dict(
                project_id="project-synthetic",
                document_id="document-synthetic",
                run_id=fixture.request.run_id,
                source_sha256=fixture.request.source_sha256,
                source_ir_canonical_sha256=fixture.request.scope_request.source_ir_canonical_sha256,
                evidence_workspace_bundle_sha256=fixture.request.scope_request.evidence_workspace_bundle_sha256,
                survey=fixture.survey,
                repository=InMemoryAgentStructureLoopRepository(),
                object_store=LocalObjectStore(root / "objects"),
                kit_bundle_sha256="3" * 64,
            )
            with self.assertRaisesRegex(ValueError, "more than once"):
                assemble_and_publish_document_map_working_revision(
                    **values, scope_replacements=(projection, projection)
                )
            with self.assertRaisesRegex(ValueError, "exact source ledger"):
                assemble_and_publish_document_map_working_revision(
                    **values,
                    scope_replacements=(projection,),
                    complete_source_node_ids=("node-" + "0" * 32,),
                )


if __name__ == "__main__":
    unittest.main()
