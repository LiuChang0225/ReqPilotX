from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.application.agent_workflow import source_requirement_reference_bridge as bridge
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
    compile_source_requirement_baseline,
)
from app.application.errors import ApplicationError
from tests.test_source_requirement_baseline import (
    _fragment,
    _public_reference,
    _table_preview,
)


class SourceRequirementReferenceBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="reqpilotx-source-requirement-bridge-"
        )
        self.root = Path(self.temporary.name)
        self.reference_root = self.root / "references"
        self.feedback_root = self.root / "feedback"
        self.run_root = self.reference_root / "run-1"
        self.run_root.mkdir(parents=True)
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
                        rows=(
                            ("序号", "名称"),
                            ("1", "后减振器"),
                            ("", "前减振器"),
                        ),
                    ),
                ),
            ),
        )
        candidate = SourceRequirementSliceCandidateV1(
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
        baseline, receipt = compile_source_requirement_baseline(
            document_map=document_map,
            requests=(request,),
            candidates=(candidate,),
        )
        payloads = {
            "document-map.json": document_map.model_dump(mode="json"),
            "source-requirement-baseline.json": baseline.model_dump(mode="json"),
            "validation-receipt.json": receipt.model_dump(mode="json"),
            "run-receipts.json": [
                {
                    "logical_table_scope_id": "lt-bom",
                    "record_count": 2,
                    "model_id": "gpt-5.6-sol",
                    "reasoning_effort": "high",
                    "elapsed_milliseconds": 1200,
                    "tool_names": [],
                }
            ],
        }
        files = []
        for filename, payload in payloads.items():
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            (self.run_root / filename).write_bytes(encoded)
            files.append(
                {
                    "path": filename,
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "size": len(encoded),
                }
            )
        manifest = {
            "schema_version": (
                bridge.SOURCE_REQUIREMENT_REFERENCE_MANIFEST_SCHEMA_VERSION
            ),
            "source_sha256": document_map.source_sha256,
            "document_map_artifact_id": document_map.artifact_id,
            "document_map_artifact_sha256": document_map.artifact_sha256,
            "baseline_artifact_id": baseline.artifact_id,
            "baseline_artifact_sha256": baseline.artifact_sha256,
            "covered_scope_ids": list(baseline.covered_scope_ids),
            "status": baseline.status,
            "authority": "AI_REVIEWED_DEVELOPMENT_REFERENCE_NOT_GOLD",
            "is_serving_activation_gate": False,
            "files": files,
        }
        (self.run_root / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        self.document_map = document_map
        self.baseline = baseline

    def tearDown(self) -> None:
        bridge._load_reference_cached.cache_clear()
        self.temporary.cleanup()

    def test_scope_revision_projection_exposes_only_reversible_summary(self) -> None:
        receipt = SimpleNamespace(
            operation=SimpleNamespace(value="REPLACE"),
            receipt_sha256="a" * 64,
            affected_scope_ids=("text:section-1.2",),
            comparison_scope_ids=("text:section-2.3",),
            feedback_ids=("source-requirement-feedback-" + "b" * 32,),
        )

        projection = bridge._scope_revision_public_projection(
            {
                "product_baseline_artifact": SimpleNamespace(
                    scope_revision_receipt=receipt
                ),
                "product_predecessor_pointer": {"object_key": "private"},
            }
        )

        self.assertTrue(projection["available"])
        self.assertTrue(projection["can_undo"])
        self.assertEqual(projection["operation"], "REPLACE")
        self.assertEqual(projection["affected_scope_ids"], ["text:section-1.2"])
        self.assertNotIn("object_key", json.dumps(projection))

    def test_loads_hash_bound_reference_and_projects_activity(self) -> None:
        with patch.object(bridge, "REFERENCE_ROOT", self.reference_root):
            bundle = bridge._load_latest_reference(self.document_map.source_sha256)
        self.assertEqual(bundle["baseline"], self.baseline)
        activity = bridge._public_run_activity(bundle["run_receipts"])
        self.assertEqual(activity[0]["scope_id"], "lt-bom")
        self.assertIn("2 条原文条目", activity[0]["summary"])

    def test_projects_table_as_structure_with_source_item_children(self) -> None:
        ledger = bridge._project_source_item_ledger(
            document_map=self.document_map,
            artifact=self.baseline,
        )
        rows = {item["row_id"]: item for item in ledger["rows"]}
        table_row = rows["bom-node"]
        self.assertTrue(table_row["is_table_structure"])
        self.assertFalse(table_row["is_one_to_one_item"])
        self.assertEqual(
            table_row["child_row_ids"],
            [item.source_record_id for item in self.baseline.records],
        )
        self.assertTrue(
            all(
                rows[item]["row_role"] == "SOURCE_ITEM"
                for item in table_row["child_row_ids"]
            )
        )
        self.assertEqual(ledger["interchange"]["target"], "REQIF_1_2")
        self.assertRegex(ledger["ledger_sha256"], r"^[0-9a-f]{64}$")

    def test_projects_one_to_one_text_item_on_the_structure_row(self) -> None:
        node = SimpleNamespace(
            node_id="clause-1",
            parent_id=None,
            order=0,
            content_blocks=(),
            source_anchors=(),
            pages=(1,),
        )
        record = SimpleNamespace(
            node_id="clause-1",
            source_order=0,
            source_record_id="source-record-" + "9" * 32,
            requirementness=Requirementness.REQUIREMENT,
            source_form=SimpleNamespace(value="NUMBERED_CLAUSE"),
            logical_table_scope_id="",
            table_fragments=(),
            pages=(1,),
        )
        ledger = bridge._project_source_item_ledger(
            document_map=SimpleNamespace(
                artifact_id="document-map-" + "7" * 32,
                artifact_sha256="7" * 64,
                root_node_ids=("clause-1",),
                nodes=(node,),
            ),
            artifact=SimpleNamespace(
                artifact_id="source-requirement-baseline-" + "8" * 32,
                artifact_sha256="8" * 64,
                records=(record,),
            ),
        )
        self.assertEqual(len(ledger["rows"]), 1)
        self.assertEqual(ledger["rows"][0]["source_item_id"], record.source_record_id)
        self.assertTrue(ledger["rows"][0]["is_one_to_one_item"])
        self.assertEqual(ledger["rows"][0]["child_row_ids"], [])

    def test_places_direct_text_item_before_later_structure_children(self) -> None:
        parent = SimpleNamespace(
            node_id="section-3.2.2",
            parent_id=None,
            order=0,
            content_blocks=(),
            source_anchors=(SimpleNamespace(page=3, bbox=(40, 100, 500, 120)),),
            pages=(3,),
        )
        child = SimpleNamespace(
            node_id="section-3.2.2-item-1",
            parent_id=parent.node_id,
            order=1,
            content_blocks=(),
            source_anchors=(SimpleNamespace(page=3, bbox=(40, 300, 500, 320)),),
            pages=(3,),
        )
        record = SimpleNamespace(
            node_id=parent.node_id,
            source_order=0,
            source_record_id="source-record-" + "9" * 32,
            requirementness=Requirementness.REQUIREMENT,
            source_form=SimpleNamespace(value="NUMBERED_CLAUSE"),
            logical_table_scope_id="",
            table_fragments=(),
            text_bindings=(
                SimpleNamespace(
                    page=3,
                    bbox=(40, 200, 500, 240),
                    start_offset=0,
                ),
            ),
            pages=(3,),
        )
        ledger = bridge._project_source_item_ledger(
            document_map=SimpleNamespace(
                artifact_id="document-map-" + "7" * 32,
                artifact_sha256="7" * 64,
                root_node_ids=(parent.node_id,),
                nodes=(parent, child),
            ),
            artifact=SimpleNamespace(
                artifact_id="source-requirement-baseline-" + "8" * 32,
                artifact_sha256="8" * 64,
                records=(record,),
            ),
        )
        rows = {item["row_id"]: item for item in ledger["rows"]}
        self.assertFalse(rows[parent.node_id]["is_one_to_one_item"])
        self.assertEqual(
            rows[parent.node_id]["child_row_ids"],
            [record.source_record_id, child.node_id],
        )

    def test_public_text_label_uses_source_heading_and_marks_direct_body(self) -> None:
        record = SimpleNamespace(
            label="5.7",
            source_form=SimpleNamespace(value="NUMBERED_CLAUSE"),
        )
        plain_node = SimpleNamespace(
            label="5.7",
            title="集成验证要求",
            content_blocks=(),
        )
        self.assertEqual(
            bridge._source_item_display_label(
                record,
                node=plain_node,
                has_structure_children=False,
            ),
            "5.7 集成验证要求",
        )
        self.assertEqual(
            bridge._source_item_display_label(
                record,
                node=plain_node,
                has_structure_children=True,
            ),
            "集成验证要求 · 直接正文",
        )

    def test_manifest_tamper_fails_closed(self) -> None:
        path = self.run_root / "source-requirement-baseline.json"
        path.write_text("{}", encoding="utf-8")
        bridge._load_reference_cached.cache_clear()
        with (
            patch.object(bridge, "REFERENCE_ROOT", self.reference_root),
            self.assertRaises(ApplicationError),
        ):
            bridge._load_latest_reference(self.document_map.source_sha256)

    def test_feedback_is_scope_bound_idempotent_and_conflict_safe(self) -> None:
        payload = bridge.SourceRequirementFeedbackRequestV1(
            command_id="feedback-command",
            expected_source_sha256=self.document_map.source_sha256,
            expected_document_map_artifact_sha256=(
                self.document_map.artifact_sha256
            ),
            expected_baseline_artifact_id=self.baseline.artifact_id,
            source_record_id=self.baseline.records[0].source_record_id,
            message="保持产品结构分支。",
        )
        values = {
            "project_id": "project",
            "document_id": "document",
            "source_sha256": self.document_map.source_sha256,
            "document_map_artifact_sha256": self.document_map.artifact_sha256,
            "baseline_artifact_id": self.baseline.artifact_id,
            "payload": payload,
        }
        with patch.object(bridge, "FEEDBACK_ROOT", self.feedback_root):
            first = bridge._append_feedback(**values)
            replay = bridge._append_feedback(**values)
            self.assertEqual(replay, first)
            with self.assertRaises(ApplicationError):
                bridge._append_feedback(
                    **{
                        **values,
                        "payload": payload.model_copy(
                            update={"message": "同一命令不能改内容。"}
                        ),
                    }
                )
            revised_baseline_id = "source-requirement-baseline-" + "c" * 32
            bridge._carry_feedbacks_to_baseline(
                source_sha256=self.document_map.source_sha256,
                from_baseline_artifact_id=self.baseline.artifact_id,
                to_baseline_artifact_id=revised_baseline_id,
                feedback_ids=(first["feedback_id"],),
                status="CONSUMED",
            )
            bridge._carry_feedbacks_to_baseline(
                source_sha256=self.document_map.source_sha256,
                from_baseline_artifact_id=self.baseline.artifact_id,
                to_baseline_artifact_id=revised_baseline_id,
                feedback_ids=(first["feedback_id"],),
                status="CONSUMED",
            )
            carried = bridge._read_feedbacks(
                source_sha256=self.document_map.source_sha256,
                baseline_artifact_id=revised_baseline_id,
            )
            self.assertEqual(len(carried), 1)
            self.assertEqual(carried[0]["status"], "CONSUMED")
            self.assertEqual(
                carried[0]["carried_from_baseline_artifact_id"],
                self.baseline.artifact_id,
            )


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
