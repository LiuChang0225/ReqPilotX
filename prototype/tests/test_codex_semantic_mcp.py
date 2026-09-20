from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from app.adapters.agents.codex_app_server.semantic_mcp import (
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    SemanticDocumentSessionRegistry,
    SemanticMcpError,
    SemanticToolSession,
    StdioSemanticMcpServer,
    TOOL_GET_IMAGE,
    TOOL_GET_DOCUMENT_PAGE,
    TOOL_GET_PAGE_LEDGER,
    TOOL_GET_TASK,
    TOOL_NAMES,
    TOOL_VALIDATE_ASSEMBLY,
    TOOL_VALIDATE_CLASSIFICATION,
    TOOL_VALIDATE_JOINT,
    TOOL_VALIDATE_PARENT_BOUNDARY,
    TOOL_VALIDATE_DECOMPOSITION,
    TOOL_VALIDATE_DOCUMENT_DECOMPOSITION,
    load_semantic_descriptor,
    tool_definitions,
    write_semantic_descriptor,
    write_semantic_document_session_registry,
    write_semantic_document_session_images,
    write_semantic_document_session_task,
)
from app.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from app.application.extraction.native_parent_boundary_reference import (
    NativeParentBoundaryRequestV1,
    NativeParentBoundarySourceSegmentV1,
)
from app.application.extraction.document_decomposition import (
    DocumentDecompositionItemReferenceV1,
    DocumentDecompositionItemRequestV1,
    DocumentDecompositionReferenceV1,
    DocumentDecompositionRequestV1,
    DocumentDecompositionSourceSegmentV1,
    document_decomposition_lineage_sha256,
)
from app.application.extraction.native_atomization_reference import (
    NativeAtomizationReferenceStatus,
)
from app.domain.extraction.requirement_view import RequirementProjectionRole
from app.domain.extraction.semantics import RequirementSubtype, SemanticClass
from app.domain.extraction.structure import (
    StructureNodeKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.ports.semantic_provider import (
    RequirementAssemblyDecision,
    RequirementAssemblyNodeInput,
    RequirementAssemblyRequest,
    SemanticClassificationRequest,
    SemanticDecision,
    SemanticEvidenceInput,
    SemanticImageInput,
    SemanticNodeInput,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationObligation,
    RequirementAtomizationRange,
)
from tests.test_native_codex_atomization_reference import (
    _reference as _atomization_reference,
    _request as _atomization_request,
)


TARGET = "structure-node-" + "1" * 32
SOURCE_REF = StructureSourceRef(
    kind=StructureSourceRefKind.SOURCE_NODE,
    source_id="node-" + "2" * 32,
)


def _png(width: int = 1, height: int = 1) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def _evidence() -> SemanticEvidenceInput:
    text = "The component shall satisfy the specified limit."
    return SemanticEvidenceInput(
        source_ref=SOURCE_REF,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def _classification_request(
    *, with_image: bool = False
) -> SemanticClassificationRequest:
    image_bytes = _png()
    images = (
        (
            SemanticImageInput(
                source_sha256="3" * 64,
                page_number=7,
                width_px=1,
                height_px=1,
                image_bytes=image_bytes,
                image_sha256=hashlib.sha256(image_bytes).hexdigest(),
            ),
        )
        if with_image
        else ()
    )
    return SemanticClassificationRequest(
        source_ir_id="source-ir-test",
        source_sha256="3" * 64,
        structure_snapshot_id="structure-test",
        scope_structure_node_ids=(TARGET,),
        nodes=(
            SemanticNodeInput(
                target_structure_node_id=TARGET,
                source_order=0,
                kind=StructureNodeKind.CLAUSE,
                image_page_numbers=(7,) if with_image else (),
                target_source_refs=(SOURCE_REF,),
                evidence=(_evidence(),),
            ),
        ),
        images=images,
    )


def _classification_candidate(
    *,
    source_ref: StructureSourceRef = SOURCE_REF,
) -> dict[str, object]:
    return {
        "decisions": [
            SemanticDecision(
                target_structure_node_id=TARGET,
                semantic_class=SemanticClass.REQUIREMENT,
                requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
                evidence_source_refs=(source_ref,),
                reason_codes=("source_bound_decision",),
            ).model_dump(mode="json")
        ]
    }


def _assembly_request() -> RequirementAssemblyRequest:
    return RequirementAssemblyRequest(
        source_ir_id="source-ir-test",
        structure_snapshot_id="structure-test",
        semantic_snapshot_id="semantic-test",
        scope_structure_node_ids=(TARGET,),
        nodes=(
            RequirementAssemblyNodeInput(
                target_structure_node_id=TARGET,
                source_order=0,
                kind=StructureNodeKind.CLAUSE,
                semantic_annotation_ids=("semantic-annotation-1",),
                semantic_class=SemanticClass.REQUIREMENT,
                requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
                evidence=(_evidence(),),
            ),
        ),
    )


def _assembly_candidate() -> dict[str, object]:
    return {
        "decisions": [
            RequirementAssemblyDecision(
                target_structure_node_id=TARGET,
                include_in_view=True,
                role=RequirementProjectionRole.PRIMARY,
                status=RequirementAssemblyStatus.CANDIDATE,
                reason_codes=("source_bound_assembly",),
            ).model_dump(mode="json")
        ]
    }


def _parent_boundary_request() -> NativeParentBoundaryRequestV1:
    text = "1 Scope\nThe component shall satisfy the specified limit."
    return NativeParentBoundaryRequestV1(
        source_sha256="3" * 64,
        scope_id="scope-1",
        source_segments=(
            NativeParentBoundarySourceSegmentV1(
                segment_index=0,
                page_number=1,
                evidence_channel="NATIVE_TEXT",
                source_ref="source-page-1",
                source_text=text,
                source_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            ),
        ),
    )


def _parent_boundary_candidate() -> dict[str, object]:
    text = _parent_boundary_request().source_segments[0].source_text
    return {
        "schema_version": "native-parent-boundary-reference/v2",
        "source_sha256": "3" * 64,
        "scope_id": "scope-1",
        "status": "OBSERVED",
        "provisions": [
            {
                "provision_id": "native-provision-" + "4" * 32,
                "explicit_label": "1",
                "ancestor_labels": [],
                "source_order": 0,
                "disposition": "REQUIREMENT",
                "body_ranges": [
                    RequirementAtomizationRange(
                        segment_index=0,
                        start_offset=0,
                        end_offset=len(text),
                    ).model_dump(mode="json")
                ],
                "reason_codes": ["complete_parent_provision"],
            }
        ],
        "context_ranges": [],
        "diagnostic_codes": [],
        "is_human_gold": False,
        "is_serving_activation_gate": False,
    }


def _document_decomposition_pair() -> tuple[
    DocumentDecompositionRequestV1,
    DocumentDecompositionReferenceV1,
]:
    text = "The component shall satisfy the specified limit."
    segment = DocumentDecompositionSourceSegmentV1(
        segment_index=0,
        page_number=1,
        source_ref="document-page-1-source",
        source_start_offset=0,
        source_end_offset=len(text),
        source_text=text,
        source_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    parent_id = "native-provision-" + "4" * 32
    lineage = document_decomposition_lineage_sha256(
        parent_provision_id=parent_id,
        explicit_label="1",
        ancestor_labels=(),
        source_order=0,
        source_segments=(segment,),
    )
    request = DocumentDecompositionRequestV1(
        source_ir_id="source-ir-test",
        source_sha256="3" * 64,
        items=(
            DocumentDecompositionItemRequestV1(
                parent_provision_id=parent_id,
                lineage_sha256=lineage,
                explicit_label="1",
                source_order=0,
                source_segments=(segment,),
            ),
        ),
    )
    reference = DocumentDecompositionReferenceV1(
        source_sha256=request.source_sha256,
        items=(
            DocumentDecompositionItemReferenceV1(
                parent_provision_id=parent_id,
                lineage_sha256=lineage,
                status=NativeAtomizationReferenceStatus.OBSERVED,
                obligations=(
                    RequirementAtomizationObligation(
                        source_ranges=(
                            RequirementAtomizationRange(
                                segment_index=0,
                                start_offset=0,
                                end_offset=len(text),
                            ),
                        ),
                        atomic_statement=text,
                        split_basis="single_or_descriptive",
                        reason_codes=("single_source_obligation",),
                    ),
                ),
            ),
        ),
    )
    return request, reference


class CodexSemanticMcpTests(unittest.TestCase):
    def test_document_decomposition_batch_exactly_covers_ordered_parents(self) -> None:
        request, reference = _document_decomposition_pair()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            os.chmod(root, 0o700)
            descriptor = write_semantic_descriptor(
                root / "session.json",
                operation="DOCUMENT_DECOMPOSITION",
                request=request,
            )
            session = SemanticToolSession(descriptor)
            session.call(TOOL_GET_TASK, {"run_handle": descriptor.run_handle})

            result = session.call(
                TOOL_VALIDATE_DOCUMENT_DECOMPOSITION,
                {
                    "run_handle": descriptor.run_handle,
                    "candidate": reference.model_dump(mode="json"),
                },
            )

        self.assertEqual(len(result["validation_receipts"]), 1)
        self.assertEqual(result["validation_receipts"][0]["obligation_count"], 1)

    def test_decomposition_task_reuses_exact_cover_atomization_validator(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            os.chmod(root, 0o700)
            path = root / "session.json"
            descriptor = write_semantic_descriptor(
                path,
                operation="DECOMPOSITION",
                request=_atomization_request(),
            )
            session = SemanticToolSession(descriptor)

            task = session.call(
                TOOL_GET_TASK,
                {"run_handle": descriptor.run_handle},
            )
            result = session.call(
                TOOL_VALIDATE_DECOMPOSITION,
                {
                    "run_handle": descriptor.run_handle,
                    "candidate": _atomization_reference().model_dump(mode="json"),
                },
            )

            self.assertEqual(task["operation"], "DECOMPOSITION")
            self.assertEqual(result["validation_receipt"]["obligation_count"], 1)
            self.assertEqual(result["write_audit"]["total_writes"], 0)

    def test_parent_boundary_task_uses_exact_cover_host_validator(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            os.chmod(root, 0o700)
            path = root / "session.json"
            descriptor = write_semantic_descriptor(
                path,
                operation="PARENT_BOUNDARY",
                request=_parent_boundary_request(),
            )
            session = SemanticToolSession(descriptor)

            task = session.call(
                TOOL_GET_TASK,
                {"run_handle": descriptor.run_handle},
            )
            for _index in range(31):
                session.call(
                    TOOL_GET_TASK,
                    {"run_handle": descriptor.run_handle},
                )
            with self.assertRaisesRegex(
                SemanticMcpError,
                "TOOL_CALL_BUDGET_EXCEEDED",
            ):
                session.call(
                    TOOL_GET_TASK,
                    {"run_handle": descriptor.run_handle},
                )
            result = session.call(
                TOOL_VALIDATE_PARENT_BOUNDARY,
                {
                    "run_handle": descriptor.run_handle,
                    "candidate": _parent_boundary_candidate(),
                },
            )

            self.assertEqual(task["operation"], "PARENT_BOUNDARY")
            self.assertEqual(result["validation_receipt"]["provision_count"], 1)
            self.assertEqual(result["write_audit"]["total_writes"], 0)

    def test_document_session_registry_resolves_multiple_immutable_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            os.chmod(root, 0o700)
            registry_path = write_semantic_document_session_registry(
                root / "document-session.json"
            )
            first = write_semantic_document_session_task(
                registry_path,
                operation="CLASSIFICATION",
                request=_classification_request(),
            )
            second = write_semantic_document_session_task(
                registry_path,
                operation="JOINT",
                request=_classification_request(),
            )
            registry = SemanticDocumentSessionRegistry.load(registry_path)

            first_task = registry.call(
                TOOL_GET_TASK,
                {"run_handle": first.run_handle},
            )
            second_task = registry.call(
                TOOL_GET_TASK,
                {"run_handle": second.run_handle},
            )

            self.assertEqual(first_task["operation"], "CLASSIFICATION")
            self.assertEqual(second_task["operation"], "JOINT")
            self.assertEqual(
                set(registry.sessions), {first.run_handle, second.run_handle}
            )
            descriptor_files = tuple((root / "semantic-tasks").glob("*.json"))
            self.assertEqual(len(descriptor_files), 2)
            self.assertTrue(
                all(
                    stat.S_IMODE(item.stat().st_mode) == 0o600
                    for item in descriptor_files
                )
            )

            with self.assertRaisesRegex(SemanticMcpError, "RUN_HANDLE_UNKNOWN"):
                registry.call(TOOL_GET_TASK, {"run_handle": "rpxs_unknown"})

    def test_document_session_page_ledger_supports_bounded_adaptive_revisit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            os.chmod(root, 0o700)
            registry_path = write_semantic_document_session_registry(
                root / "document-session.json"
            )
            image_request = _classification_request(with_image=True)
            write_semantic_document_session_images(
                registry_path,
                image_request.images,
            )
            task = write_semantic_document_session_task(
                registry_path,
                operation="JOINT",
                request=_classification_request(),
            )
            registry = SemanticDocumentSessionRegistry.load(registry_path)

            with self.assertRaisesRegex(SemanticMcpError, "TOOL_SEQUENCE_INVALID"):
                registry.call(
                    TOOL_GET_DOCUMENT_PAGE,
                    {"run_handle": task.run_handle, "page_number": 7},
                )
            task_payload = registry.call(
                TOOL_GET_TASK,
                {"run_handle": task.run_handle},
            )
            ledger = registry.call(
                TOOL_GET_PAGE_LEDGER,
                {"run_handle": task.run_handle},
            )
            page = registry.call(
                TOOL_GET_DOCUMENT_PAGE,
                {"run_handle": task.run_handle, "page_number": 7},
            )

            self.assertTrue(task_payload["document_page_ledger_available"])
            self.assertEqual(ledger["page_count"], 1)
            self.assertEqual(ledger["pages"][0]["page_number"], 7)
            self.assertEqual(page.pop("_mcp_image_bytes"), _png())
            self.assertEqual(page["image_sha256"], image_request.images[0].image_sha256)
            with self.assertRaisesRegex(SemanticMcpError, "PAGE_NUMBER_OUT_OF_RANGE"):
                registry.call(
                    TOOL_GET_DOCUMENT_PAGE,
                    {"run_handle": task.run_handle, "page_number": 8},
                )

    def test_classification_descriptor_hydrates_private_image_and_exact_cover(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            os.chmod(root, 0o700)
            path = root / "session.json"
            written = write_semantic_descriptor(
                path,
                operation="CLASSIFICATION",
                request=_classification_request(with_image=True),
            )

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            image_files = tuple(root.glob(".semantic-image-*.png"))
            self.assertEqual(len(image_files), 1)
            self.assertEqual(stat.S_IMODE(image_files[0].stat().st_mode), 0o600)
            self.assertEqual(len(written.images), 1)

            descriptor = load_semantic_descriptor(path)
            request = descriptor.request
            self.assertIsInstance(request, SemanticClassificationRequest)
            assert isinstance(request, SemanticClassificationRequest)
            self.assertEqual(request.images[0].image_bytes, _png())
            session = SemanticToolSession(descriptor)
            task = session.call(TOOL_GET_TASK, {"run_handle": descriptor.run_handle})
            image_handle = task["images"][0]["image_handle"]
            self.assertNotIn("image_bytes", json.dumps(task))

            with self.assertRaisesRegex(SemanticMcpError, "TOOL_SEQUENCE_INVALID"):
                session.call(
                    TOOL_VALIDATE_CLASSIFICATION,
                    {
                        "run_handle": descriptor.run_handle,
                        "candidate": _classification_candidate(),
                    },
                )

            image = session.call(
                TOOL_GET_IMAGE,
                {"run_handle": descriptor.run_handle, "image_handle": image_handle},
            )
            self.assertEqual(image.pop("_mcp_image_bytes"), _png())
            reread = session.call(
                TOOL_GET_TASK,
                {"run_handle": descriptor.run_handle},
            )
            self.assertEqual(reread["images"], [])
            self.assertEqual(
                reread["required_next_tool"],
                TOOL_VALIDATE_CLASSIFICATION,
            )
            session.call(TOOL_GET_TASK, {"run_handle": descriptor.run_handle})
            session.call(TOOL_GET_TASK, {"run_handle": descriptor.run_handle})
            with self.assertRaisesRegex(
                SemanticMcpError,
                "TOOL_CALL_BUDGET_EXCEEDED",
            ):
                session.call(
                    TOOL_GET_TASK,
                    {"run_handle": descriptor.run_handle},
                )
            result = session.call(
                TOOL_VALIDATE_CLASSIFICATION,
                {
                    "run_handle": descriptor.run_handle,
                    "candidate": _classification_candidate(),
                },
            )
            self.assertRegex(result["candidate_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(result["write_audit"]["total_writes"], 0)

    def test_session_fails_closed_on_handle_sequence_operation_and_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            os.chmod(root, 0o700)
            path = root / "session.json"
            descriptor = write_semantic_descriptor(
                path,
                operation="CLASSIFICATION",
                request=_classification_request(),
            )
            session = SemanticToolSession(descriptor)
            with self.assertRaisesRegex(SemanticMcpError, "RUN_HANDLE_UNKNOWN"):
                session.call(TOOL_GET_TASK, {"run_handle": "wrong"})
            session = SemanticToolSession(descriptor)
            session.call(TOOL_GET_TASK, {"run_handle": descriptor.run_handle})
            with self.assertRaisesRegex(SemanticMcpError, "OPERATION_MISMATCH"):
                session.call(
                    TOOL_VALIDATE_ASSEMBLY,
                    {
                        "run_handle": descriptor.run_handle,
                        "candidate": _assembly_candidate(),
                    },
                )
            outside = StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id="node-" + "9" * 32,
            )
            with self.assertRaisesRegex(
                SemanticMcpError,
                "PROPOSAL_REJECTED_SEMANTIC_GROUNDING_INVALID",
            ):
                session.call(
                    TOOL_VALIDATE_CLASSIFICATION,
                    {
                        "run_handle": descriptor.run_handle,
                        "candidate": _classification_candidate(source_ref=outside),
                    },
                )

    def test_assembly_descriptor_validates_exact_target_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            os.chmod(root, 0o700)
            path = root / "session.json"
            write_semantic_descriptor(
                path,
                operation="ASSEMBLY",
                request=_assembly_request(),
            )
            loaded = load_semantic_descriptor(path)
            session = SemanticToolSession(loaded)
            task = session.call(TOOL_GET_TASK, {"run_handle": loaded.run_handle})
            self.assertEqual(task["required_next_tool"], TOOL_VALIDATE_ASSEMBLY)
            result = session.call(
                TOOL_VALIDATE_ASSEMBLY,
                {
                    "run_handle": loaded.run_handle,
                    "candidate": _assembly_candidate(),
                },
            )
            self.assertEqual(
                result["validated_candidate"]["decisions"][0][
                    "target_structure_node_id"
                ],
                TARGET,
            )

    def test_joint_descriptor_validates_paired_classification_and_assembly(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            os.chmod(root, 0o700)
            path = root / "session.json"
            descriptor = write_semantic_descriptor(
                path,
                operation="JOINT",
                request=_classification_request(),
            )
            session = SemanticToolSession(descriptor)
            task = session.call(
                TOOL_GET_TASK,
                {"run_handle": descriptor.run_handle},
            )

            self.assertEqual(task["required_next_tool"], TOOL_VALIDATE_JOINT)
            result = session.call(
                TOOL_VALIDATE_JOINT,
                {
                    "run_handle": descriptor.run_handle,
                    "candidate": {
                        "classifications": _classification_candidate()["decisions"],
                        "assemblies": _assembly_candidate()["decisions"],
                    },
                },
            )
            self.assertEqual(
                result["validated_candidate"]["classifications"][0][
                    "target_structure_node_id"
                ],
                TARGET,
            )

            invalid = _assembly_candidate()
            invalid["decisions"][0]["include_in_view"] = False
            invalid["decisions"][0]["role"] = None
            with self.assertRaisesRegex(
                SemanticMcpError,
                "PROPOSAL_REJECTED_JOINT_GROUNDING_INVALID",
            ):
                session.call(
                    TOOL_VALIDATE_JOINT,
                    {
                        "run_handle": descriptor.run_handle,
                        "candidate": {
                            "classifications": _classification_candidate()["decisions"],
                            "assemblies": invalid["decisions"],
                        },
                    },
                )

    def test_toolset_is_closed_and_read_only(self) -> None:
        definitions = tool_definitions()
        self.assertEqual(tuple(item["name"] for item in definitions), TOOL_NAMES)
        for item in definitions:
            self.assertFalse(item["inputSchema"]["additionalProperties"])
            self.assertTrue(item["annotations"]["readOnlyHint"])
            self.assertFalse(item["annotations"]["destructiveHint"])
            self.assertFalse(item["annotations"]["openWorldHint"])

    def test_stdio_server_returns_image_content_without_bytes_in_structured_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            os.chmod(root, 0o700)
            path = root / "session.json"
            descriptor = write_semantic_descriptor(
                path,
                operation="CLASSIFICATION",
                request=_classification_request(with_image=True),
            )
            server = StdioSemanticMcpServer(SemanticToolSession(descriptor))
            output = io.StringIO()
            with redirect_stdout(output):
                server._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {},
                    }
                )
                server._dispatch(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}
                )
                server._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {},
                    }
                )
                server._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": TOOL_GET_TASK,
                            "arguments": {"run_handle": descriptor.run_handle},
                        },
                    }
                )
                server._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": TOOL_GET_IMAGE,
                            "arguments": {
                                "run_handle": descriptor.run_handle,
                                "image_handle": descriptor.images[0].image_handle,
                            },
                        },
                    }
                )
            messages = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(
                messages[0]["result"]["serverInfo"],
                {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
            )
            self.assertEqual(len(messages[1]["result"]["tools"]), len(TOOL_NAMES))
            image_result = messages[3]["result"]
            self.assertEqual(image_result["content"][0]["type"], "image")
            self.assertNotIn("data", image_result["structuredContent"])
            self.assertNotIn("_mcp_image_bytes", json.dumps(image_result))


if __name__ == "__main__":
    unittest.main()
