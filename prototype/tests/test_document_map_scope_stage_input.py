from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
from pathlib import Path
import tempfile
import unittest

from app.adapters.agents.codex_app_server.document_map_scope_stage_input import (
    CanonicalDocumentMapScopeStageInputAttestationV1,
    materialize_document_map_scope_stage_input,
)
from app.application.agent_workflow.document_map_scope import (
    DocumentMapDetailScopeKind,
    DocumentMapHeadingCandidateRefV1,
    DocumentMapScopeRequestV1,
    DocumentMapSourceEndpointV1,
    DocumentMapSourceSpanV1,
)
from app.application.agent_workflow.document_understanding import (
    canonical_json_sha256,
)
from app.application.agent_workflow.native_heading_skeleton import (
    NativeHeadingCandidateState,
)


class DocumentMapScopeStageInputTests(unittest.TestCase):
    def test_materializes_private_content_and_content_free_attestation(self) -> None:
        request = _request()
        with tempfile.TemporaryDirectory() as raw:
            result = materialize_document_map_scope_stage_input(
                session_root=Path(raw),
                request=request,
            )

            self.assertEqual(result.request_path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(result.manifest_path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(
                hashlib.sha256(result.request_path.read_bytes()).hexdigest(),
                result.attestation.file.sha256,
            )
            serialized = result.attestation.model_dump_json()
            self.assertNotIn("第一章客户原文", serialized)
            self.assertEqual(
                DocumentMapScopeRequestV1.model_validate_json(
                    result.request_path.read_bytes()
                ),
                request,
            )

    def test_rejects_tampered_manifest_digest(self) -> None:
        request = _request()
        with tempfile.TemporaryDirectory() as raw:
            result = materialize_document_map_scope_stage_input(
                session_root=Path(raw),
                request=request,
            )
            payload = result.attestation.model_dump(mode="json")
            payload["manifest_sha256"] = "f" * 64

            with self.assertRaisesRegex(ValueError, "manifest SHA drifted"):
                CanonicalDocumentMapScopeStageInputAttestationV1.model_validate(
                    payload
                )


def _request() -> DocumentMapScopeRequestV1:
    span = DocumentMapSourceSpanV1(
        start=DocumentMapSourceEndpointV1(
            source_node_id="node-" + "1" * 32,
            offset=0,
        ),
        end=DocumentMapSourceEndpointV1(
            source_node_id="node-" + "2" * 32,
            offset=0,
        ),
    )
    values = {
        "run_id": "agent-run-" + "3" * 32,
        "source_sha256": "4" * 64,
        "source_ir_id": "source-ir-test",
        "source_ir_canonical_sha256": "5" * 64,
        "evidence_workspace_bundle_sha256": "6" * 64,
        "survey_compilation_sha256": "7" * 64,
        "visual_coverage_receipt_sha256": "8" * 64,
        "heading_skeleton_sha256": "9" * 64,
        "scope_id": "document-map-detail-scope-" + "a" * 32,
        "kind": DocumentMapDetailScopeKind.CHAPTER,
        "advisory_number": "1",
        "advisory_title": "第一章客户原文",
        "suggested_source_span": span,
        "source_node_ids": ("node-" + "1" * 32,),
        "page_numbers": (9,),
        "physical_table_ids": (),
        "asset_ids": (),
        "heading_candidates": (
            DocumentMapHeadingCandidateRefV1(
                candidate_id="heading-candidate-" + "b" * 32,
                source_node_id="node-" + "1" * 32,
                source_order=1,
                page_number=9,
                number="1",
                title="第一章客户原文",
                level=1,
                advisory_parent_candidate_id=None,
                advisory_state=NativeHeadingCandidateState.PROPOSED,
            ),
        ),
        "preceding_context_source_node_ids": (),
        "following_context_source_node_ids": ("node-" + "2" * 32,),
    }
    draft = DocumentMapScopeRequestV1.model_construct(
        **values,
        request_sha256="0" * 64,
    )
    request_sha256 = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"request_sha256"})
    )
    return DocumentMapScopeRequestV1(
        **values,
        request_sha256=request_sha256,
    )


if __name__ == "__main__":
    unittest.main()
