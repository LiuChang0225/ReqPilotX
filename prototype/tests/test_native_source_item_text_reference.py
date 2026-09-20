from __future__ import annotations

# Test runtime isolation must be active before application imports.

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.adapters.agents.codex_app_server.native_source_item_text_reference import (
    SourceItemTextBatchCandidateV1,
    run_native_source_item_text_reference,
)
from app.application.agent_workflow.source_requirement_baseline import (
    NonRequirementRole,
    Requirementness,
    SourceItemStructureOnlySpanV1,
    SourceItemTextBoundaryCandidateV1,
    SourceItemTextSliceCandidateV1,
    SourceItemTextSpanV1,
    SourceRecordForm,
    build_source_item_text_scope_request,
    compile_document_map_artifact,
)
from tests.test_source_requirement_baseline import _reference_with_text_node


class NativeSourceItemTextReferenceTests(unittest.TestCase):
    def test_invalid_first_batch_receives_one_bounded_replacement(self) -> None:
        title = "主动前减振器试验大纲"
        metadata = "项目：YX2516\n部门：底盘设计部\n版本：1.0"
        document_map = compile_document_map_artifact(
            _reference_with_text_node(
                node_id="front-outline-meta",
                label="",
                title=title,
                contents=(
                    ("outline-title", title, True),
                    ("outline-metadata", metadata, False),
                ),
            )
        )
        request = build_source_item_text_scope_request(
            document_map,
            node_id="front-outline-meta",
        )
        candidate = SourceItemTextSliceCandidateV1(
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            scope_id=request.scope_id,
            items=(
                SourceItemTextBoundaryCandidateV1(
                    item_key="document-control",
                    label="项目、部门与版本信息",
                    source_form=SourceRecordForm.NATIVE_LIST,
                    text_spans=(
                        SourceItemTextSpanV1(
                            content_id="outline-metadata",
                            start_offset=0,
                            end_offset=len(metadata),
                        ),
                    ),
                    requirementness=Requirementness.NON_REQUIREMENT,
                    requirement_kinds=(),
                    non_requirement_role=NonRequirementRole.METADATA,
                    confidence=0.99,
                    decision_summary="文档控制信息。",
                    reason_codes=("document_control_metadata",),
                ),
            ),
            structure_only_spans=(
                SourceItemStructureOnlySpanV1(
                    text_span=SourceItemTextSpanV1(
                        content_id="outline-title",
                        start_offset=0,
                        end_offset=len(title),
                    ),
                    reason_codes=("source_hierarchy_title",),
                ),
            ),
        )
        valid = SourceItemTextBatchCandidateV1(
            source_sha256=document_map.source_sha256,
            document_map_artifact_id=document_map.artifact_id,
            document_map_artifact_sha256=document_map.artifact_sha256,
            candidates=(candidate,),
        )
        invalid = valid.model_dump(mode="json")
        invalid["candidates"][0]["items"][0]["text_spans"][0]["end_offset"] = (
            len(metadata) + 1
        )
        exec_calls = 0

        def command_runner(command, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal exec_calls
            if "--version" in command:
                return subprocess.CompletedProcess(
                    command, 0, stdout="codex-cli 0.150.0\n", stderr=""
                )
            exec_calls += 1
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text(
                json.dumps(
                    invalid if exec_calls == 1 else valid.model_dump(mode="json"),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"type":"thread.started"}\n'
                    '{"type":"turn.started"}\n'
                    '{"type":"turn.completed"}\n'
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as raw_output:
            result = run_native_source_item_text_reference(
                document_map=document_map,
                requests=(request,),
                output_directory=Path(raw_output) / "result",
                command_runner=command_runner,
            )
        self.assertEqual(exec_calls, 2)
        self.assertEqual(result.batch, valid)
        self.assertEqual(result.run_receipt.source_item_count, 1)
        self.assertEqual(result.run_receipt.structure_only_span_count, 1)


if __name__ == "__main__":
    unittest.main()
