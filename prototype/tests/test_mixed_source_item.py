"""A table-led obligation owns its leading text without creating a second REQ."""
from __future__ import annotations

from io import BytesIO
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .runtime_isolation import activate_test_runtime

activate_test_runtime()

from openpyxl import load_workbook
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_delivery import build_delivery_snapshot, export_delivery_xlsx
from app.application.agent_workflow import document_map_revision
from app.adapters.agents.codex_app_server.native_source_requirement_windowed_reference import (
    SourceRequirementBoundaryAgentCandidateV1, _compile_boundary_plan,
)
from app.adapters.agents.codex_app_server.native_source_item_text_windowed import (
    SourceItemTextBoundaryAgentCandidateV1, SourceItemTextBoundaryAgentRecordV1,
    compile_source_item_text_boundary_plan,
)
from .test_source_requirement_baseline import _public_reference, _table_preview, _fragment, _candidate, _record
from .test_source_requirement_jobs import _Context, _job, _current
from app.adapters.storage.local_object_store import LocalObjectStore
from app.adapters.agents.codex_app_server.native_source_requirement_reference import NativeSourceRequirementRunReceiptV1
from app.adapters.agents.codex_app_server.native_source_item_text_reference import NativeSourceItemTextRunReceiptV1, SourceItemTextBatchCandidateV1
from app.application.agent_workflow.source_requirement_jobs import run_durable_source_requirement_baseline
from app.adapters.agents.codex_app_server.native_source_item_text_windowed import source_item_text_request_set_sha256
from app.adapters.agents.codex_app_server.source_item_text_executor import _validate_reserved_table_owned_spans
from app.ports.source_requirement_window_executor import SourceRequirementWindowExecutionV1
from app.ports.source_item_text_executor import SourceItemTextExecutionV1


def source_reference(*, independent: bool = False):
    reference = _public_reference()
    reference["logical_tables"] = reference["logical_tables"][:1]
    reference["logical_tables"][0]["fragments"][0]["row_range"] = [0, 3]
    reference["document_tree"]["nodes"] = reference["document_tree"]["nodes"][:2]
    node = reference["document_tree"]["nodes"][1]
    lead = node["source_content"][0]["text"]
    if independent:
        separate = "3.2.2 The supplier shall provide a separate service manual."
        node["source_content"].append({
            **node["source_content"][0], "content_id": "other-duty",
            "text": separate, "text_sha256": hashlib.sha256(separate.encode()).hexdigest(),
            "bbox": [0.0, 110.0, 100.0, 130.0],
        })
    return reference


def fixture(*, independent: bool = False):
    reference = source_reference(independent=independent)
    node = reference["document_tree"]["nodes"][1]
    lead = node["source_content"][0]["text"]
    document_map = core.compile_document_map_artifact(reference)
    preview = _table_preview(scope_id="lt-bom", title="Products", fragments=(
        _fragment("bom-fragment", "T0001", page=2,
                  rows=(("Item", "Part"), ("1", "Main body"), ("2", "Fastener"))),
    ))
    table_request = core.build_source_requirement_scope_request(document_map, preview)
    text_request = core.build_source_item_text_scope_request(document_map, node_id="bom-node")
    span = core.SourceItemTextSpanV1(content_id="bom-context", start_offset=0, end_offset=len(lead))
    row_group = core.SourceRequirementRowGroupV1(fragment_id="bom-fragment", row_start=1, row_end=3)
    table_record = _record("complete-bom", "Complete BOM", core.SourceRecordForm.TABLE_RECORD, (row_group,))
    table_record = core.SourceRequirementBoundaryCandidateV1.model_validate({
        **table_record.model_dump(mode="json"), "owning_text_spans": [span.model_dump(mode="json")],
    })
    header = core.SourceRequirementContextRowGroupV1(
        role=core.ContextRowRole.HEADER,
        row_group=core.SourceRequirementRowGroupV1(fragment_id="bom-fragment", row_start=0, row_end=1),
        reason_codes=("header",),
    )
    table_candidate = _candidate(table_request, records=(table_record,), context=(header,))
    items = ()
    if independent:
        content = text_request.source_content[1]
        items = (core.SourceItemTextBoundaryCandidateV1(
            item_key="service-manual", label="Service manual", source_form="NUMBERED_CLAUSE",
            text_spans=(core.SourceItemTextSpanV1(content_id=content.content_id,
                         start_offset=0, end_offset=len(content.text)),),
            requirementness="REQUIREMENT", requirement_kinds=("CONSTRAINT",),
            confidence=0.95, decision_summary="Independent manual duty.",
            reason_codes=("separate_number",),
        ),)
    text_candidate = core.SourceItemTextSliceCandidateV1(
        source_sha256=text_request.source_sha256,
        document_map_artifact_id=text_request.document_map_artifact_id,
        document_map_artifact_sha256=text_request.document_map_artifact_sha256,
        request_sha256=text_request.request_sha256, scope_id=text_request.scope_id,
        items=items, table_owned_spans=(core.SourceItemTableOwnedTextSpanV1(
            logical_table_scope_id="lt-bom", record_key="complete-bom", text_span=span,
        ),),
    )
    return document_map, preview, table_request, table_candidate, text_request, text_candidate


def compile_fixture(values):
    document_map, _, table_request, table_candidate, text_request, text_candidate = values
    return core.compile_source_requirement_baseline(
        document_map=document_map, requests=(table_request,), candidates=(table_candidate,),
        text_requests=(text_request,), text_candidates=(text_candidate,), status="COMPLETE",
    )


class MixedSourceItemTests(unittest.TestCase):
    def test_windowed_worker_publishes_table_and_text_as_one_product_revision(self):
        from .test_source_requirement_baseline import SourceRequirementBaselineTests
        existing = SourceRequirementBaselineTests()
        existing.setUp()
        document_map = existing.document_map
        table_request = existing.bom_request
        text_request = core.build_source_item_text_scope_request(document_map, node_id="bom-node")
        lead = text_request.source_content[0]
        span = core.SourceItemTextSpanV1(content_id=lead.content_id, start_offset=0, end_offset=len(lead.text))
        table_candidate = _candidate(table_request, records=(
            core.SourceRequirementBoundaryCandidateV1.model_validate({
                **_record("whole-bom", "Complete BOM", core.SourceRecordForm.TABLE_RECORD, (
                    core.SourceRequirementRowGroupV1(fragment_id="bom-fragment", row_start=1, row_end=5),
                )).model_dump(mode="json"),
                "owning_text_spans": [span.model_dump(mode="json")],
            }),
        ), context=(core.SourceRequirementContextRowGroupV1(
            role=core.ContextRowRole.HEADER,
            row_group=core.SourceRequirementRowGroupV1(fragment_id="bom-fragment", row_start=0, row_end=1),
            reason_codes=("header",),
        ),))
        dvp_candidate = _candidate(existing.dvp_request, records=(
            _record("dvp-previous", "DVP", core.SourceRecordForm.TABLE_RECORD, (
                core.SourceRequirementRowGroupV1(fragment_id="dvp-one", row_start=1, row_end=4),
                core.SourceRequirementRowGroupV1(fragment_id="dvp-two", row_start=0, row_end=2),
            )),
        ), context=(core.SourceRequirementContextRowGroupV1(
            role=core.ContextRowRole.HEADER,
            row_group=core.SourceRequirementRowGroupV1(fragment_id="dvp-one", row_start=0, row_end=1),
            reason_codes=("header",),
        ),))
        previous, previous_receipt = core.compile_source_requirement_baseline(
            document_map=document_map, requests=(existing.dvp_request,), candidates=(dvp_candidate,),
        )
        table_receipt = NativeSourceRequirementRunReceiptV1(
            source_sha256=table_request.source_sha256,
            document_map_artifact_id=table_request.document_map_artifact_id,
            document_map_artifact_sha256=table_request.document_map_artifact_sha256,
            request_sha256=table_request.request_sha256,
            logical_table_scope_id=table_request.logical_table_scope_id,
            model_id="synthetic", reasoning_effort="high", codex_version="synthetic",
            elapsed_milliseconds=1, event_counts=(), tool_names=(), record_count=1,
            context_row_group_count=1,
            candidate_sha256=core.canonical_json_sha256(table_candidate.model_dump(mode="json")),
        )

        class TableExecutor:
            adapter_id = "synthetic-table"
            adapter_version = "1"
            transport_kind = "SYNTHETIC"
            model_id = "synthetic"
            reasoning_effort = "high"

            def execute(self, task, **kwargs):
                return SourceRequirementWindowExecutionV1(
                    candidate=table_candidate.model_dump(mode="json"),
                    run_receipt=table_receipt.model_dump(mode="json"),
                )

        class TextExecutor:
            def __init__(self, fail=False):
                self.fail = fail

            def readiness_reason(self):
                return None

            def execute(self, task, **kwargs):
                if self.fail:
                    raise RuntimeError("synthetic text model interruption")
                ownership = task.execution_context["table_owned_spans"]
                candidate = core.SourceItemTextSliceCandidateV1(
                    source_sha256=text_request.source_sha256,
                    document_map_artifact_id=text_request.document_map_artifact_id,
                    document_map_artifact_sha256=text_request.document_map_artifact_sha256,
                    request_sha256=text_request.request_sha256,
                    scope_id=text_request.scope_id,
                    table_owned_spans=tuple(core.SourceItemTableOwnedTextSpanV1.model_validate(item) for item in ownership),
                )
                batch = SourceItemTextBatchCandidateV1(
                    source_sha256=document_map.source_sha256,
                    document_map_artifact_id=document_map.artifact_id,
                    document_map_artifact_sha256=document_map.artifact_sha256,
                    candidates=(candidate,),
                )
                receipt = NativeSourceItemTextRunReceiptV1(
                    source_sha256=document_map.source_sha256,
                    document_map_artifact_id=document_map.artifact_id,
                    document_map_artifact_sha256=document_map.artifact_sha256,
                    request_set_sha256=source_item_text_request_set_sha256((text_request,), execution_context=task.execution_context),
                    model_id="synthetic", reasoning_effort="high", codex_version="synthetic",
                    elapsed_milliseconds=1, event_counts=(), tool_names=(), scope_count=1,
                    scope_ids=(text_request.scope_id,), source_item_count=0,
                    structure_only_span_count=0,
                    candidate_sha256=core.canonical_json_sha256(batch.model_dump(mode="json")),
                )
                return SourceItemTextExecutionV1(batch=batch.model_dump(mode="json"), run_receipt=receipt.model_dump(mode="json"))

        with tempfile.TemporaryDirectory() as raw:
            container = SimpleNamespace(
                object_store=LocalObjectStore(Path(raw) / "objects"),
                source_requirement_window_executor=TableExecutor(),
                source_item_text_executor=TextExecutor(),
            )
            current = {**_current(document_map, table_request),
                       "source_requirement_base_bundle": {"synthetic": True}}
            job = _job(document_map, table_request)
            job.payload["executor_id"] = TableExecutor.adapter_id
            job.payload["executor_version"] = TableExecutor.adapter_version
            job.payload["transport_kind"] = TableExecutor.transport_kind
            job.payload["model_id"] = TableExecutor.model_id
            context = _Context(job)
            container.source_item_text_executor = TextExecutor(fail=True)
            with (patch("app.application.runtime.RUNTIME_STATE.container", container),
                  patch("app.application.agent_workflow.source_requirement_jobs._current_scope_context", return_value=current),
                  patch("app.application.agent_workflow.source_requirement_jobs.resolve_effective_source_requirement_bundle", return_value={"baseline": previous, "receipt": previous_receipt, "product_run_receipts": ()}),
                  patch("app.application.agent_workflow.source_item_manual_publication.attach_rebound_baseline_manual_contexts")):
                with self.assertRaisesRegex(RuntimeError, "synthetic text model interruption"):
                    run_durable_source_requirement_baseline(job, context)
            self.assertEqual(context.publication_count, 0)
            container.source_item_text_executor = TextExecutor()
            context = _Context(job)
            with (patch("app.application.runtime.RUNTIME_STATE.container", container),
                  patch("app.application.agent_workflow.source_requirement_jobs._current_scope_context", return_value=current),
                  patch("app.application.agent_workflow.source_requirement_jobs.resolve_effective_source_requirement_bundle", return_value={"baseline": previous, "receipt": previous_receipt, "product_run_receipts": ()}),
                  patch("app.application.agent_workflow.source_item_manual_publication.attach_rebound_baseline_manual_contexts")):
                result = run_durable_source_requirement_baseline(job, context)
            self.assertEqual(result["next_action"], "BASELINE_REVISION_PUBLISHED")
            self.assertEqual(result["mixed_text_scope_ids"], [text_request.scope_id])
            self.assertEqual(result["baseline_revision_artifact"]["record_count"], 2)
            self.assertEqual(context.publication_count, 1)

    def test_windowed_model_boundaries_preserve_exact_table_text_ownership(self):
        document_map, _, table_request, _, text_request, text_candidate = fixture(independent=True)
        owned = text_candidate.table_owned_spans[0]
        table_plan = _compile_boundary_plan(
            document_map=document_map,
            request=table_request,
            candidate=SourceRequirementBoundaryAgentCandidateV1.model_validate({
                "records": [{
                    "label": "Complete BOM", "source_form": "TABLE_RECORD",
                    "row_groups": [{"fragment_id": "bom-fragment", "row_start": 1, "row_count": 2}],
                    "owning_text_spans": [owned.text_span.model_dump(mode="json")],
                }],
                "context_row_groups": [{
                    "role": "HEADER", "row_group": {
                        "fragment_id": "bom-fragment", "row_start": 0, "row_count": 1,
                    }, "reason_codes": ["header"],
                }],
            }),
        )
        table_key = table_plan.records[0].record_key
        independent = text_candidate.items[0]
        text_plan = compile_source_item_text_boundary_plan(
            request=text_request,
            candidate=SourceItemTextBoundaryAgentCandidateV1(
                scope_id=text_request.scope_id,
                records=(SourceItemTextBoundaryAgentRecordV1(
                    label=independent.label, source_form=independent.source_form,
                    text_spans=independent.text_spans,
                ),),
                table_owned_spans=(owned.model_copy(update={"record_key": table_key}),),
            ),
        )
        self.assertEqual(table_plan.records[0].owning_text_spans, (owned.text_span,))
        self.assertEqual(text_plan.table_owned_spans[0].record_key, table_key)
        self.assertEqual(len(text_plan.records), 1)
        reserved = [text_plan.table_owned_spans[0].model_dump(mode="json")]
        _validate_reserved_table_owned_spans(
            request=text_request, plan=text_plan,
            execution_context={
                "schema_version": "source-item-text-table-ownership/v1",
                "scope_id": text_request.scope_id,
                "source_sha256": text_request.source_sha256,
                "document_map_artifact_sha256": text_request.document_map_artifact_sha256,
                "table_owned_spans": reserved,
            },
        )
        with self.assertRaisesRegex(ValueError, "TABLE_OWNERSHIP_MISMATCH"):
            _validate_reserved_table_owned_spans(
                request=text_request, plan=text_plan,
                execution_context={
                    "schema_version": "source-item-text-table-ownership/v1",
                    "scope_id": text_request.scope_id,
                    "source_sha256": text_request.source_sha256,
                    "document_map_artifact_sha256": text_request.document_map_artifact_sha256,
                    "table_owned_spans": [{**reserved[0], "record_key": "wrong-record"}],
                },
            )

    def test_complete_bom_is_one_cited_requirement_in_snapshot_and_excel(self):
        values = fixture()
        baseline, receipt = compile_fixture(values)
        self.assertEqual((len(baseline.records), receipt.requirement_count), (1, 1))
        record = baseline.records[0]
        self.assertEqual(record.requirement_display_id, "REQ-0001")
        self.assertEqual(len(record.table_fragments), 1)
        self.assertEqual(len(record.text_bindings), 1)
        self.assertEqual(record.text_bindings[0].text, values[0].nodes[1].source_content[0].text)
        self.assertEqual(receipt.owned_text_character_count,
                         sum(not c.isspace() for c in record.text_bindings[0].text))
        restored = core.SourceRequirementBaselineArtifactV1.model_validate_json(baseline.model_dump_json())
        self.assertEqual(restored.artifact_sha256, baseline.artifact_sha256)
        snapshot = build_delivery_snapshot(values[0], baseline, {"lt-bom": values[1]})
        item = next(node for node in snapshot["nodes"] if node.get("source_item"))
        self.assertEqual(item["display_text"], record.text_bindings[0].text)
        self.assertEqual({ref["kind"] for ref in item["source_refs"]}, {"TEXT", "TABLE"})
        self.assertEqual(sum(bool(node.get("requirement_id")) for node in snapshot["nodes"]), 1)
        book = load_workbook(BytesIO(export_delivery_xlsx(snapshot)))
        values_in_book = [str(cell.value) for row in book["ComplianceMatrix"] for cell in row if cell.value]
        self.assertTrue(any(record.text_bindings[0].text in value for value in values_in_book))

    def test_separate_number_stays_separate_and_no_cross_scope_guessing(self):
        values = fixture(independent=True)
        baseline, receipt = compile_fixture(values)
        self.assertEqual((len(baseline.records), receipt.requirement_count), (2, 2))
        self.assertEqual({record.source_form.value for record in baseline.records},
                         {"TABLE_RECORD", "NUMBERED_CLAUSE"})
        _, _, _, table_candidate, _, text_candidate = values
        omitted = text_candidate.model_copy(update={"table_owned_spans": ()})
        with self.assertRaisesRegex(core.SourceRequirementBaselineError, "DISPOSITION_MISMATCH"):
            compile_fixture((*values[:5], omitted))
        wrong_owner = text_candidate.model_copy(update={"table_owned_spans": (
            text_candidate.table_owned_spans[0].model_copy(update={"record_key": "another-record"}),
        )})
        with self.assertRaisesRegex(core.SourceRequirementBaselineError, "DISPOSITION_MISMATCH"):
            compile_fixture((*values[:5], wrong_owner))
        duplicated = table_candidate.model_copy(update={"records": (
            table_candidate.records[0].model_copy(update={"owning_text_spans":
                table_candidate.records[0].owning_text_spans * 2}),
        )})
        with self.assertRaisesRegex(core.SourceRequirementBaselineError, "OWNED_TEXT_DUPLICATE"):
            compile_fixture((*values[:3], duplicated, *values[4:]))

    def test_partial_revision_cannot_detach_a_mixed_record(self):
        values = fixture()
        baseline, receipt = compile_fixture(values)
        revision = core.build_source_item_scope_revision(
            document_map=values[0], current_artifact=baseline,
            current_validation_receipt=receipt, operation=core.SourceItemScopeRevisionOperation.REPLACE,
            affected_scope_ids=("lt-bom",), comparison_scope_ids=(), feedback_ids=("feedback-mixed",),
            requests=(values[2],), candidates=(values[3],),
        )
        with self.assertRaisesRegex(core.SourceRequirementBaselineError, "MIXED_OWNER_SCOPES_MUST_MOVE_TOGETHER"):
            core.compile_source_item_scope_revision(
                document_map=values[0], current_artifact=baseline,
                current_validation_receipt=receipt, scope_revision=revision,
                requests=(values[2],), candidates=(values[3],),
            )

    def test_joint_revision_preserves_identity_and_reviewed_judgment(self):
        values = fixture()
        baseline, _ = compile_fixture(values)
        reviewed = baseline.records[0].model_copy(update={
            "decision_origin": "USER_OVERRIDE", "review_state": "REVIEWED",
            "decision_summary": "Engineer accepted the complete BOM boundary.",
        })
        draft = baseline.model_copy(update={"records": (reviewed,)})
        digest = core.canonical_json_sha256(core._baseline_hash_payload(draft))
        baseline = core.SourceRequirementBaselineArtifactV1.model_validate(draft.model_copy(update={
            "artifact_id": "source-requirement-baseline-" + digest[:32], "artifact_sha256": digest,
        }).model_dump(mode="json"))
        receipt = core._compile_validation_receipt_from_artifact(document_map=values[0], artifact=baseline)
        revision = core.build_source_item_scope_revision(
            document_map=values[0], current_artifact=baseline,
            current_validation_receipt=receipt, operation=core.SourceItemScopeRevisionOperation.REPLACE,
            affected_scope_ids=("lt-bom", "text:bom-node"), comparison_scope_ids=(),
            feedback_ids=("feedback-mixed",), requests=(values[2],), candidates=(values[3],),
            text_requests=(values[4],), text_candidates=(values[5],),
        )
        revised, validation, _ = core.compile_source_item_scope_revision(
            document_map=values[0], current_artifact=baseline,
            current_validation_receipt=receipt, scope_revision=revision,
            requests=(values[2],), candidates=(values[3],),
            text_requests=(values[4],), text_candidates=(values[5],),
        )
        self.assertEqual(revised.records[0].source_record_id, reviewed.source_record_id)
        self.assertEqual(revised.records[0].source_display_id, reviewed.source_display_id)
        self.assertEqual(revised.records[0].decision_origin, "USER_OVERRIDE")
        self.assertEqual(revised.records[0].decision_summary, reviewed.decision_summary)
        self.assertEqual(validation.owned_text_character_count, receipt.owned_text_character_count)

    def test_map_rebind_replays_both_owned_sources(self):
        values = fixture()
        baseline, receipt = compile_fixture(values)
        document_map_revision._replay(values[0], baseline, {"lt-bom": values[1]})
        proposal = core.compile_document_map_artifact(source_reference())
        successor_map = document_map_revision.compile_document_map_revision(values[0], proposal)["document_map"]
        rebound = document_map_revision.rebind_baseline_for_document_map_revision(
            values[0], successor_map, baseline, receipt, {"lt-bom": values[1]})
        self.assertEqual(len(rebound["baseline"]["records"]), 1)

    def test_sealed_binding_cannot_claim_words_absent_from_the_map(self):
        values = fixture()
        baseline, _ = compile_fixture(values)
        record = baseline.records[0]
        binding = record.text_bindings[0]
        forged = binding.model_copy(update={
            "text": "X" + binding.text[1:],
            "text_sha256": core.canonical_text_sha256("X" + binding.text[1:]),
        })
        with self.assertRaisesRegex(core.SourceRequirementBaselineError, "TEXT_BINDING_DRIFT"):
            core._scope_partition_snapshot(
                document_map=values[0],
                records=(record.model_copy(update={"text_bindings": (forged,)}),),
                context_dispositions=baseline.context_dispositions,
                text_context_dispositions=baseline.text_context_dispositions,
                scope_ids=set(baseline.covered_scope_ids), subject="TEST",
            )


if __name__ == "__main__":
    unittest.main()
