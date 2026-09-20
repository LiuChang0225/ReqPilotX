from __future__ import annotations

# Test runtime isolation must be active before application imports.

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.adapters.agents.codex_app_server.native_source_requirement_reference import (
    _effective_reasoning_effort,
    _materialize_candidate,
    _reasoning_risk_signals,
    run_native_source_requirement_slice_reference,
)
from app.application.agent_workflow.source_requirement_baseline import (
    ContextRowRole,
    RequirementKind,
    Requirementness,
    SourceRecordForm,
    SourceRequirementBoundaryCandidateV1,
    SourceRequirementContextRowGroupV1,
    SourceRequirementRowGroupV1,
    SourceRequirementSliceCandidateV1,
    build_source_requirement_scope_request,
    compile_document_map_artifact,
)
from tests.test_source_requirement_baseline import (
    _fragment,
    _public_reference,
    _table_preview,
)


class NativeSourceRequirementReferenceTests(unittest.TestCase):
    def test_compact_materialization_canonicalizes_cross_fragment_row_groups(
        self,
    ) -> None:
        document_map = compile_document_map_artifact(_public_reference())
        request = build_source_requirement_scope_request(
            document_map,
            _table_preview(
                scope_id="lt-dvp",
                title="主动前减振器试验大纲",
                fragments=(
                    _fragment(
                        "dvp-page-10",
                        "T0002",
                        page=10,
                        rows=(("编号", "项目"), ("1", "跨页试验")),
                    ),
                    _fragment(
                        "dvp-page-11",
                        "T0003",
                        page=11,
                        rows=(("", "续行"),),
                    ),
                ),
            ),
        )

        def payload(groups):  # type: ignore[no-untyped-def]
            return {
                "schema_version": "source-requirement-agent-candidate/v1",
                "records": [
                    {
                        "label": "1 跨页试验",
                        "source_form": "TABLE_RECORD",
                        "row_groups": groups,
                        "shared_source_content_ids": [],
                        "requirementness": "REQUIREMENT",
                        "requirement_kinds": ["CONSTRAINT"],
                        "non_requirement_role": None,
                        "confidence": 0.95,
                        "decision_summary": "完整跨页试验记录。",
                        "reason_codes": ["complete_test_record"],
                    }
                ],
                "context_row_groups": [
                    {
                        "role": "HEADER",
                        "row_group": {
                            "fragment_id": "dvp-page-10",
                            "row_start": 0,
                            "row_count": 1,
                        },
                        "reason_codes": ["shared_table_header"],
                    }
                ],
                "diagnostic_codes": [],
            }

        ordered = [
            {"fragment_id": "dvp-page-10", "row_start": 1, "row_count": 1},
            {"fragment_id": "dvp-page-11", "row_start": 0, "row_count": 1},
        ]
        reversed_candidate, _ = _materialize_candidate(
            payload(list(reversed(ordered))),
            request=request,
            execution_profile="compact-adaptive",
        )
        ordered_candidate, _ = _materialize_candidate(
            payload(ordered),
            request=request,
            execution_profile="compact-adaptive",
        )

        self.assertEqual(
            tuple(
                group.fragment_id
                for group in reversed_candidate.records[0].row_groups
            ),
            ("dvp-page-10", "dvp-page-11"),
        )
        self.assertEqual(
            reversed_candidate.records[0].record_key,
            ordered_candidate.records[0].record_key,
        )

    def test_compact_reasoning_preserves_requested_effort_for_numbering_risk(
        self,
    ) -> None:
        public_reference = _public_reference()
        public_reference["logical_tables"][0][
            "source_numbering_anomaly"
        ] = "MISSING_7_BETWEEN_6_AND_8"
        document_map = compile_document_map_artifact(public_reference)
        request = build_source_requirement_scope_request(
            document_map,
            _table_preview(
                scope_id="lt-bom",
                title="产品信息",
                fragments=(
                    _fragment(
                        "bom-fragment",
                        "T0001",
                        page=2,
                        rows=(("序号", "名称"), ("1", "后减振器")),
                    ),
                ),
            ),
        )

        signals = _reasoning_risk_signals(
            document_map=document_map,
            request=request,
        )
        effort, selection = _effective_reasoning_effort(
            request=request,
            requested="high",
            execution_profile="compact-adaptive",
            risk_signals=signals,
        )

        self.assertIn("source_numbering_anomaly", signals)
        self.assertEqual(effort, "high")
        self.assertEqual(selection, "COMPACT_RISK_PRESERVED")

    def test_invalid_first_candidate_receives_one_bounded_replacement_attempt(
        self,
    ) -> None:
        document_map = compile_document_map_artifact(_public_reference())
        request = build_source_requirement_scope_request(
            document_map,
            _table_preview(
                scope_id="lt-bom",
                title="产品信息",
                fragments=(
                    _fragment(
                        "bom-fragment",
                        "T0001",
                        page=2,
                        rows=(("序号", "名称"), ("1", "后减振器"), ("", "前减振器")),
                    ),
                ),
            ),
        )
        valid = SourceRequirementSliceCandidateV1(
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            logical_table_scope_id=request.logical_table_scope_id,
            records=(
                _record("rear", "后减振器", 1, 2),
                _record("front", "前减振器", 2, 3),
            ),
            context_row_groups=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="bom-fragment", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
            diagnostic_codes=(),
        )
        invalid = valid.model_dump(mode="json")
        invalid["context_row_groups"][0]["row_group"]["row_end"] = 0
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
            result = run_native_source_requirement_slice_reference(
                document_map=document_map,
                request=request,
                output_directory=Path(raw_output) / "result",
                command_runner=command_runner,
            )
            feedback = next(
                Path(raw_output).glob("**/validation-feedback.json"),
                None,
            )
        self.assertEqual(exec_calls, 2)
        self.assertEqual(result.candidate, valid)
        self.assertEqual(result.run_receipt.record_count, 2)
        # Validation feedback is intentionally confined to the temporary Agent
        # workspace and disappears after the corrected attempt.
        self.assertIsNone(feedback)

    def test_machine_key_failure_returns_actionable_ascii_feedback(self) -> None:
        document_map = compile_document_map_artifact(_public_reference())
        request = build_source_requirement_scope_request(
            document_map,
            _table_preview(
                scope_id="lt-bom",
                title="产品信息",
                fragments=(
                    _fragment(
                        "bom-fragment",
                        "T0001",
                        page=2,
                        rows=(("序号", "名称"), ("1", "后减振器")),
                    ),
                ),
            ),
        )
        valid = SourceRequirementSliceCandidateV1(
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            logical_table_scope_id=request.logical_table_scope_id,
            records=(_record("rear-damper", "后减振器", 1, 2),),
            context_row_groups=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="bom-fragment", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
            diagnostic_codes=(),
        )
        invalid = valid.model_dump(mode="json")
        invalid["records"][0]["record_key"] = "rear-damper-后减振器"
        exec_calls = 0

        def command_runner(command, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal exec_calls
            if "--version" in command:
                return subprocess.CompletedProcess(
                    command, 0, stdout="codex-cli 0.151.0\n", stderr=""
                )
            exec_calls += 1
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text(
                json.dumps(
                    invalid,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"type":"turn.completed"}\n',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as raw_output:
            result = run_native_source_requirement_slice_reference(
                document_map=document_map,
                request=request,
                output_directory=Path(raw_output) / "result",
                command_runner=command_runner,
            )

        self.assertEqual(exec_calls, 1)
        self.assertEqual(result.candidate.records[0].record_key, "record-0001")
        self.assertEqual(result.candidate.records[0].label, "后减振器")
        self.assertEqual(
            result.candidate.records[0].row_groups, valid.records[0].row_groups
        )
        self.assertEqual(result.run_receipt.runner_version, "1.10.0")
        self.assertEqual(result.run_receipt.normalized_machine_key_count, 1)

    def test_materializes_locked_kit_and_hash_bound_pdf_for_visual_review(self) -> None:
        source_bytes = b"%PDF-1.4\n% visual source fixture\n%%EOF\n"
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        public_reference = _public_reference()
        public_reference["source_sha256"] = source_sha256
        document_map = compile_document_map_artifact(public_reference)
        preview = _table_preview(
            scope_id="lt-bom",
            title="产品信息",
            fragments=(
                _fragment(
                    "bom-fragment",
                    "T0001",
                    page=2,
                    rows=(("序号", "名称"), ("1", "后减振器"), ("", "前减振器")),
                ),
            ),
        )
        preview["source_sha256"] = source_sha256
        request = build_source_requirement_scope_request(document_map, preview)
        valid = SourceRequirementSliceCandidateV1(
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            logical_table_scope_id=request.logical_table_scope_id,
            records=(
                _record("rear", "后减振器", 1, 2),
                _record("front", "前减振器", 2, 3),
            ),
            context_row_groups=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="bom-fragment", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
            diagnostic_codes=(),
        )
        compact = {
            "schema_version": "source-requirement-agent-candidate/v1",
            "records": [
                {
                    key: value
                    for key, value in record.model_dump(mode="json").items()
                    if key != "record_key"
                }
                for record in valid.records
            ],
            "context_row_groups": [
                item.model_dump(mode="json") for item in valid.context_row_groups
            ],
            "diagnostic_codes": [],
        }
        for record in compact["records"]:
            record["row_groups"] = [
                {
                    "fragment_id": group["fragment_id"],
                    "row_start": group["row_start"],
                    "row_count": group["row_end"] - group["row_start"],
                }
                for group in record["row_groups"]
            ]
        for context in compact["context_row_groups"]:
            group = context["row_group"]
            context["row_group"] = {
                "fragment_id": group["fragment_id"],
                "row_start": group["row_start"],
                "row_count": group["row_end"] - group["row_start"],
            }
        observed_prompt = ""

        def command_runner(command, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal observed_prompt
            if "--version" in command:
                return subprocess.CompletedProcess(
                    command, 0, stdout="codex-cli 0.151.0\n", stderr=""
                )
            workspace = Path(command[command.index("--cd") + 1])
            self.assertTrue((workspace / "AGENTS.md").is_file())
            self.assertTrue((workspace / "document-context.json").is_file())
            self.assertFalse((workspace / "document-map.json").exists())
            self.assertEqual(
                json.loads((workspace / "document-map.full.json").read_text()),
                document_map.model_dump(mode="json"),
            )
            self.assertEqual(
                json.loads((workspace / "scope.full.json").read_text()),
                request.model_dump(mode="json"),
            )
            self.assertEqual(
                (workspace / "document-map.full.json").stat().st_mode & 0o777,
                0o400,
            )
            self.assertEqual(
                (workspace / "scope.full.json").stat().st_mode & 0o777,
                0o400,
            )
            self.assertTrue(
                (
                    workspace
                    / ".agents/skills/reqpilotx-parent-provision-recognition/SKILL.md"
                ).is_file()
            )
            self.assertEqual((workspace / "source.pdf").read_bytes(), source_bytes)
            observed_prompt = str(kwargs["input"])
            final_path = Path(command[command.index("--output-last-message") + 1])
            final_path.write_text(
                json.dumps(compact, ensure_ascii=False),
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
            source_pdf = Path(raw_output) / "source.pdf"
            source_pdf.write_bytes(source_bytes)
            result = run_native_source_requirement_slice_reference(
                document_map=document_map,
                request=request,
                output_directory=Path(raw_output) / "result",
                agent_kit_directory=Path(__file__).resolve().parents[2] / "agent-kit",
                source_pdf_path=source_pdf,
                command_runner=command_runner,
            )

        self.assertIn("$reqpilotx-parent-provision-recognition", observed_prompt)
        self.assertIn("render and visually inspect", observed_prompt)
        self.assertIn("Host injects them deterministically", observed_prompt)
        self.assertIn("BEGIN_UNTRUSTED_SOURCE_EVIDENCE_JSON", observed_prompt)
        self.assertIn("document-map.full.json", observed_prompt)
        self.assertEqual(result.run_receipt.kit_version, "0.15.2")
        self.assertNotEqual(result.run_receipt.kit_bundle_sha256, "0" * 64)
        self.assertEqual(result.run_receipt.visual_source_sha256, source_sha256)
        self.assertEqual(result.run_receipt.visual_page_numbers, (2,))
        self.assertEqual(result.run_receipt.execution_profile, "compact-adaptive")
        self.assertEqual(result.run_receipt.host_assigned_machine_key_count, 2)
        self.assertEqual(result.run_receipt.requested_reasoning_effort, "high")
        self.assertEqual(result.run_receipt.attempt_reasoning_efforts, ("medium",))
        self.assertLess(result.run_receipt.evidence_context_bytes, 20_000)
        self.assertEqual(result.run_receipt.evidence_delivery, "INLINE_AND_FILES")
        self.assertEqual(
            result.run_receipt.on_demand_evidence_resources,
            ("document-map.full.json", "scope.full.json", "source.pdf"),
        )
        self.assertEqual(
            result.run_receipt.reasoning_selection,
            "COMPACT_LOW_RISK_MEDIUM",
        )
        self.assertEqual(result.run_receipt.reasoning_risk_signals, ())


def _record(
    key: str,
    label: str,
    start: int,
    end: int,
) -> SourceRequirementBoundaryCandidateV1:
    return SourceRequirementBoundaryCandidateV1(
        record_key=key,
        label=label,
        source_form=SourceRecordForm.TABLE_BRANCH,
        row_groups=(
            SourceRequirementRowGroupV1(
                fragment_id="bom-fragment", row_start=start, row_end=end
            ),
        ),
        requirementness=Requirementness.REQUIREMENT,
        requirement_kinds=(RequirementKind.CONSTRAINT,),
        non_requirement_role=None,
        confidence=0.95,
        decision_summary="完整产品结构分支。",
        reason_codes=("product_structure_branch",),
    )


if __name__ == "__main__":
    unittest.main()
