from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import os
from pathlib import Path
import tempfile
import unittest

from app.adapters.agents.codex_app_server.patch_mcp import (
    PatchMcpError,
    PatchToolSession,
    StructurePatchCandidateV1,
    TOOL_GET_CONTEXT,
    TOOL_GET_NODES,
    TOOL_VALIDATE,
    load_patch_descriptor,
    write_patch_descriptor,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.revise_structure import get_base_effective_structure
from app.domain.extraction.structure import StructureNodeKind
from app.domain.extraction.structure_revision import ReparentStructureOperationV1
from tests.test_structure_revision import _source_ir_fixture


class CodexPatchMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="reqpilotx-patch-mcp-")
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        self.source_ir = _source_ir_fixture()
        self.base_tree = build_structure_tree(self.source_ir)
        self.current = get_base_effective_structure(self.source_ir, self.base_tree)
        target = next(
            node
            for node in self.base_tree.nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.parent_structure_node_id is not None
        )
        parent = next(
            node
            for node in self.base_tree.nodes
            if node.kind is StructureNodeKind.SECTION
            and node.structure_node_id != target.parent_structure_node_id
        )
        self.operation = ReparentStructureOperationV1(
            structure_node_id=target.structure_node_id,
            new_parent_structure_node_id=parent.structure_node_id,
            new_sibling_index=0,
        )
        self.descriptor_path = self.root / "descriptor.json"
        self.descriptor = write_patch_descriptor(
            destination=self.descriptor_path,
            facts_sha256="8" * 64,
            source_ir=self.source_ir,
            base_structure=self.base_tree,
            revisions=(),
            effective_structure=self.current,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_descriptor_is_private_and_round_trips_current_identity(self) -> None:
        loaded = load_patch_descriptor(self.descriptor_path)

        self.assertEqual(loaded.source_ir, self.source_ir)
        self.assertEqual(loaded.effective_structure, self.current)
        self.assertEqual(oct(self.descriptor_path.stat().st_mode & 0o777), "0o600")

    def test_complete_tool_loop_validates_domain_patch_without_writes(self) -> None:
        session = PatchToolSession(self.descriptor)
        context = session.call(
            TOOL_GET_CONTEXT,
            {"run_handle": self.descriptor.run_handle},
        )
        for chunk_index in range(context["chunk_count"]):
            chunk = session.call(
                TOOL_GET_NODES,
                {
                    "document_handle": self.descriptor.document_handle,
                    "chunk_index": chunk_index,
                },
            )
            self.assertTrue(chunk["nodes"])
        candidate = StructurePatchCandidateV1(
            status="PROPOSED",
            reason_code="reparent_source_bound_content",
            source_sha256=self.source_ir.source_sha256,
            facts_sha256="8" * 64,
            expected_revision_number=0,
            expected_effective_snapshot_id=self.current.effective_snapshot_id,
            operations=(self.operation,),
        )
        validated = session.call(
            TOOL_VALIDATE,
            {
                "document_handle": self.descriptor.document_handle,
                "candidate": candidate.model_dump(mode="json"),
            },
        )

        self.assertEqual(validated["candidate_status"], "PROPOSED")
        self.assertEqual(validated["write_audit"]["total_writes"], 0)
        self.assertEqual(
            validated["domain_proposal"]["operations"][0]["operation"],
            "re_parent",
        )

    def test_no_proposal_is_valid_only_after_complete_coverage(self) -> None:
        session = PatchToolSession(self.descriptor)
        session.call(TOOL_GET_CONTEXT, {"run_handle": self.descriptor.run_handle})
        candidate = StructurePatchCandidateV1(
            status="NO_PROPOSAL",
            reason_code="no_source_bound_change",
            source_sha256=self.source_ir.source_sha256,
            facts_sha256="8" * 64,
            expected_revision_number=0,
            expected_effective_snapshot_id=self.current.effective_snapshot_id,
        )
        with self.assertRaisesRegex(PatchMcpError, "STRUCTURE_COVERAGE_INCOMPLETE"):
            session.call(
                TOOL_VALIDATE,
                {
                    "document_handle": self.descriptor.document_handle,
                    "candidate": candidate.model_dump(mode="json"),
                },
            )
        for chunk_index in range(session.chunk_count):
            session.call(
                TOOL_GET_NODES,
                {
                    "document_handle": self.descriptor.document_handle,
                    "chunk_index": chunk_index,
                },
            )
        result = session.call(
            TOOL_VALIDATE,
            {
                "document_handle": self.descriptor.document_handle,
                "candidate": candidate.model_dump(mode="json"),
            },
        )
        self.assertEqual(result["candidate_status"], "NO_PROPOSAL")

    def test_unknown_handle_extra_path_and_duplicate_chunk_fail_closed(self) -> None:
        session = PatchToolSession(self.descriptor)
        with self.assertRaisesRegex(PatchMcpError, "TOOL_ARGUMENTS_INVALID"):
            session.call(
                TOOL_GET_CONTEXT,
                {"run_handle": self.descriptor.run_handle, "path": "/etc/passwd"},
            )
        session = PatchToolSession(self.descriptor)
        session.call(TOOL_GET_CONTEXT, {"run_handle": self.descriptor.run_handle})
        arguments = {
            "document_handle": self.descriptor.document_handle,
            "chunk_index": 0,
        }
        session.call(TOOL_GET_NODES, arguments)
        with self.assertRaisesRegex(PatchMcpError, "NODE_CHUNK_ALREADY_READ"):
            session.call(TOOL_GET_NODES, arguments)


if __name__ == "__main__":
    unittest.main()
