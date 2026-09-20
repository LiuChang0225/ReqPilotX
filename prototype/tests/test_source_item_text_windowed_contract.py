from __future__ import annotations

# Test runtime isolation must be active before application imports.

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from pathlib import Path
import unittest

from app.adapters.agents.codex_app_server.native_source_item_text_windowed import (
    SourceItemTextBoundaryAgentCandidateV1,
    SourceItemTextBoundaryAgentRecordV1,
    SourceItemTextBoundaryAgentStructureOnlySpanV1,
    SourceItemTextClassificationAgentCandidateV1,
    boundary_validation_feedback,
    build_source_item_text_classification_windows,
    compile_source_item_text_boundary_plan,
)
from app.adapters.agents.codex_app_server.native_source_requirement_windowed_reference import (
    SourceRequirementClassificationDecisionV1,
)
from app.application.agent_workflow.source_requirement_baseline import (
    RequirementKind,
    Requirementness,
    SourceItemTextSpanV1,
    SourceRecordForm,
    SourceRequirementBaselineError,
    build_source_item_text_scope_request,
    compile_document_map_artifact,
)
from scripts.source_item_text_windowed_benchmark import (
    SourceItemTextWindowedBenchmarkInputV1,
    evaluate_source_item_text_windowed_benchmark,
)
from tests.test_source_requirement_baseline import _reference_with_text_node


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class SourceItemTextWindowedContractTests(unittest.TestCase):
    def test_boundary_schema_has_no_abf_and_host_normalizes_stable_record_key(
        self,
    ) -> None:
        text = "供应商应提交验证报告。"
        document_map = compile_document_map_artifact(
            _reference_with_text_node(
                node_id="clause-stable",
                label="3.1",
                title="交付责任",
                contents=(("content-stable", text, False),),
            )
        )
        request = build_source_item_text_scope_request(
            document_map,
            node_id="clause-stable",
        )
        split = SourceItemTextBoundaryAgentCandidateV1(
            scope_id=request.scope_id,
            records=(
                SourceItemTextBoundaryAgentRecordV1(
                    label="3.1 交付验证报告",
                    source_form=SourceRecordForm.NUMBERED_CLAUSE,
                    text_spans=(
                        SourceItemTextSpanV1(
                            content_id="content-stable",
                            start_offset=0,
                            end_offset=4,
                        ),
                        SourceItemTextSpanV1(
                            content_id="content-stable",
                            start_offset=4,
                            end_offset=len(text),
                        ),
                    ),
                ),
            ),
        )
        whole = SourceItemTextBoundaryAgentCandidateV1(
            scope_id=request.scope_id,
            records=(
                SourceItemTextBoundaryAgentRecordV1(
                    label="不同展示标签不改变机器身份",
                    source_form=SourceRecordForm.NUMBERED_CLAUSE,
                    text_spans=(
                        SourceItemTextSpanV1(
                            content_id="content-stable",
                            start_offset=0,
                            end_offset=len(text),
                        ),
                    ),
                ),
            ),
        )
        split_plan = compile_source_item_text_boundary_plan(
            request=request,
            candidate=split,
        )
        whole_plan = compile_source_item_text_boundary_plan(
            request=request,
            candidate=whole,
        )

        self.assertEqual(
            split_plan.records[0].record_key,
            whole_plan.records[0].record_key,
        )
        record_schema = SourceItemTextBoundaryAgentRecordV1.model_json_schema()[
            "properties"
        ]
        for forbidden in (
            "record_key",
            "requirementness",
            "requirement_kinds",
            "non_requirement_role",
            "confidence",
            "decision_summary",
            "reason_codes",
        ):
            self.assertNotIn(forbidden, record_schema)

    def test_title_must_be_structure_only_before_classification(self) -> None:
        text = "4、适用范围"
        document_map = compile_document_map_artifact(
            _reference_with_text_node(
                node_id="clause-title",
                label="4",
                title="适用范围",
                contents=(("content-title", text, True),),
            )
        )
        request = build_source_item_text_scope_request(
            document_map,
            node_id="clause-title",
        )
        span = SourceItemTextSpanV1(
            content_id="content-title",
            start_offset=0,
            end_offset=len(text),
        )
        with self.assertRaisesRegex(
            SourceRequirementBaselineError,
            "TITLE_MUST_BE_STRUCTURE_ONLY",
        ):
            compile_source_item_text_boundary_plan(
                request=request,
                candidate=SourceItemTextBoundaryAgentCandidateV1(
                    scope_id=request.scope_id,
                    records=(
                        SourceItemTextBoundaryAgentRecordV1(
                            label=text,
                            source_form=SourceRecordForm.NUMBERED_CLAUSE,
                            text_spans=(span,),
                        ),
                    ),
                ),
            )

        plan = compile_source_item_text_boundary_plan(
            request=request,
            candidate=SourceItemTextBoundaryAgentCandidateV1(
                scope_id=request.scope_id,
                structure_only_spans=(
                    SourceItemTextBoundaryAgentStructureOnlySpanV1(
                        text_span=span,
                    ),
                ),
            ),
        )
        self.assertEqual(plan.records, ())
        self.assertEqual(len(plan.structure_only_spans), 1)

    def test_owned_text_content_cannot_also_be_shared_by_same_record(self) -> None:
        text = "1.2 供应商应提交验证报告。"
        document_map = compile_document_map_artifact(
            _reference_with_text_node(
                node_id="clause-owned-shared-overlap",
                label="1.2",
                title="提交验证报告",
                contents=(("content-owned-shared", text, False),),
            )
        )
        request = build_source_item_text_scope_request(
            document_map,
            node_id="clause-owned-shared-overlap",
        )

        with self.assertRaisesRegex(
            SourceRequirementBaselineError,
            "^SOURCE_ITEM_TEXT_SHARED_CONTENT_OVERLAPS_OWNED_SPAN$",
        ) as caught:
            compile_source_item_text_boundary_plan(
                request=request,
                candidate=SourceItemTextBoundaryAgentCandidateV1(
                    scope_id=request.scope_id,
                    records=(
                        SourceItemTextBoundaryAgentRecordV1(
                            label=text,
                            source_form=SourceRecordForm.NUMBERED_CLAUSE,
                            text_spans=(
                                SourceItemTextSpanV1(
                                    content_id="content-owned-shared",
                                    start_offset=0,
                                    end_offset=len(text),
                                ),
                            ),
                            shared_source_content_ids=("content-owned-shared",),
                        ),
                    ),
                ),
            )
        feedback = boundary_validation_feedback(caught.exception, request=request)
        self.assertEqual(
            feedback["failure_code"],
            "SOURCE_ITEM_TEXT_SHARED_CONTENT_OVERLAPS_OWNED_SPAN",
        )
        self.assertIn("remove only the duplicate shared reference", feedback["instruction"])

    def test_text_candidate_reuses_source_requirement_classification_decision(
        self,
    ) -> None:
        decision = SourceRequirementClassificationDecisionV1(
            record_key="text-record-" + "a" * 32,
            requirementness=Requirementness.REQUIREMENT,
            requirement_kinds=(RequirementKind.CONSTRAINT,),
            confidence=0.9,
            decision_summary="规范性提交义务。",
            reason_codes=("supplier_deliverable_constraint",),
        )
        candidate = SourceItemTextClassificationAgentCandidateV1(
            decisions=(decision,)
        )

        self.assertIsInstance(
            candidate.decisions[0],
            SourceRequirementClassificationDecisionV1,
        )

    def test_extreme_record_capacity_is_bounded_or_fails_closed(self) -> None:
        document_map, request, plan = _window_fixture((1,) * 7000)

        representable = build_source_item_text_classification_windows(
            document_map=document_map,
            requests=(request,),
            plans=(plan,),
            max_window_records=55,
        )
        self.assertEqual(len(representable), 128)
        self.assertLessEqual(
            max(len(item.record_keys) for item in representable),
            55,
        )
        with self.assertRaisesRegex(
            SourceRequirementBaselineError,
            "^SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW_CAPACITY_EXCEEDED$",
        ):
            build_source_item_text_classification_windows(
                document_map=document_map,
                requests=(request,),
                plans=(plan,),
                max_window_records=48,
            )

    def test_character_budget_is_never_broken_by_multi_record_window(self) -> None:
        document_map, request, plan = _window_fixture((400,) * 5)

        windows = build_source_item_text_classification_windows(
            document_map=document_map,
            requests=(request,),
            plans=(plan,),
            max_window_characters=1000,
            max_window_records=48,
        )

        self.assertEqual(
            [len(item.record_keys) for item in windows],
            [2, 2, 1],
        )
        self.assertTrue(
            all(item.estimated_character_count <= 1000 for item in windows)
        )

    def test_single_oversized_whole_record_is_isolated_and_deterministic(
        self,
    ) -> None:
        document_map, request, plan = _window_fixture((1500,))

        first = build_source_item_text_classification_windows(
            document_map=document_map,
            requests=(request,),
            plans=(plan,),
            max_window_characters=1000,
            max_window_records=48,
        )
        second = build_source_item_text_classification_windows(
            document_map=document_map,
            requests=(request,),
            plans=(plan,),
            max_window_characters=1000,
            max_window_records=48,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].record_keys, (plan.records[0].record_key,))
        self.assertEqual(first[0].estimated_character_count, 1500)

    def test_benchmark_fixture_encodes_all_acceptance_gates(self) -> None:
        benchmark = SourceItemTextWindowedBenchmarkInputV1.model_validate_json(
            (
                REPOSITORY_ROOT
                / "prototype/tests/fixtures/source_item_text_windowed_benchmark_input.v1.json"
            ).read_bytes()
        )
        result = evaluate_source_item_text_windowed_benchmark(benchmark)

        self.assertEqual(result["status"], "NOT_RUN")
        maxima = {
            item["sample_id"]: item["maximum_seconds"]
            for item in result["results"]
        }
        self.assertEqual(maxima["direct-text-801"], 561.0)
        self.assertAlmostEqual(maxima["direct-text-191"], 191.0 * 1.2)
        self.assertEqual(benchmark.first_boundary_checkpoint_target_seconds, 120.0)
        self.assertEqual(benchmark.subsequent_checkpoint_gap_target_seconds, 90.0)

        def checkpoint_timeline(total: float) -> tuple[float, ...]:
            return tuple(
                [value for value in (100.0, 180.0, 260.0, 340.0, 420.0, 500.0) if value < total]
                + [total]
            )

        observed = benchmark.model_copy(
            update={
                "samples": tuple(
                    item.model_copy(
                        update={
                            "observed_windowed_seconds": (
                                550.0 if item.sample_id == "direct-text-801" else item.baseline_seconds
                            ),
                            "checkpoint_seconds": (
                                (100.0, 180.0, 260.0, 340.0, 420.0, 500.0)
                                if item.sample_id == "direct-text-801"
                                else checkpoint_timeline(item.baseline_seconds)
                            ),
                        }
                    )
                    for item in benchmark.samples
                )
            }
        )
        self.assertEqual(
            evaluate_source_item_text_windowed_benchmark(observed)["status"],
            "PASS",
        )


def _window_fixture(record_lengths: tuple[int, ...]):  # type: ignore[no-untyped-def]
    text = "x" * sum(record_lengths)
    document_map = compile_document_map_artifact(
        _reference_with_text_node(
            node_id="window-capacity",
            label="9",
            title="窗口容量",
            contents=(("window-capacity-content", text, False),),
        )
    )
    request = build_source_item_text_scope_request(
        document_map,
        node_id="window-capacity",
    )
    records = []
    offset = 0
    for index, length in enumerate(record_lengths):
        records.append(
            SourceItemTextBoundaryAgentRecordV1(
                label=f"记录 {index + 1}",
                source_form=SourceRecordForm.NATIVE_LIST,
                text_spans=(
                    SourceItemTextSpanV1(
                        content_id="window-capacity-content",
                        start_offset=offset,
                        end_offset=offset + length,
                    ),
                ),
            )
        )
        offset += length
    plan = compile_source_item_text_boundary_plan(
        request=request,
        candidate=SourceItemTextBoundaryAgentCandidateV1(
            scope_id=request.scope_id,
            records=tuple(records),
        ),
    )
    return document_map, request, plan


if __name__ == "__main__":
    unittest.main()
