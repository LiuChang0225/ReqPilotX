from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from pathlib import Path
import json
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from canonical_d2_app_server_canary import (
    _adapt_region_parent_runtime_for_canary,
    _activate_candidate_stages,
    _build_region_stage_input_request,
    _compare_region_structure_summaries,
    _d2_region_summary,
    _object_parent_flow_summary,
    _object_parent_flow_incomplete_summary,
    _object_parent_plan_summary,
    _object_parent_flow_task_bindings,
    _region_parent_runtime_safe_summary,
    _safe_object_parent_failure_code,
    _synthetic_region_structure_oracle,
    _validate_canary_modes,
    _validate_dynamic_tool_probe_activity,
)
from app.adapters.agents.codex_app_server.canonical_dynamic_tools import (
    TOOL_STRUCTURE_SELECT_FAILURE_PATTERNS,
)
from app.application.agent_workflow.d2_decision_candidate_v3 import (
    compile_and_validate_d2_logical_structure_candidate_v3,
)
from app.application.agent_workflow.d3_region_stage_input import (
    materialize_d3_region_stage_input,
)
from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.application.agent_workflow.parent_provision_batch_receipts import (
    ParentProvisionRecognitionBatchStatus,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectExecutionMode,
)
from app.application.agent_workflow.d2_logical_structure import D2LogicalObjectKind
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
)

if __package__:
    from .test_canonical_structure_v3_driver import _complete_candidate
    from .test_d2_decision_candidate_v3 import (
        _d1_identity,
        _kit_identity,
        _source_fixture,
    )
else:
    from test_canonical_structure_v3_driver import _complete_candidate
    from test_d2_decision_candidate_v3 import (
        _d1_identity,
        _kit_identity,
        _source_fixture,
    )


class CanonicalD2AppServerCanaryHelperTests(unittest.TestCase):
    def test_dynamic_tool_probe_requires_exact_successful_selector_call(self) -> None:
        _validate_dynamic_tool_probe_activity(
            dynamic_tool_probe=True,
            tool_activity_summary={
                "dynamic_tools": {
                    "enabled": True,
                    "call_count": 1,
                    "success_count": 1,
                    "tool_call_counts": {TOOL_STRUCTURE_SELECT_FAILURE_PATTERNS: 1},
                }
            },
        )
        _validate_dynamic_tool_probe_activity(
            dynamic_tool_probe=False,
            tool_activity_summary={},
        )

    def test_dynamic_tool_probe_rejects_missing_or_extra_calls(self) -> None:
        invalid_summaries = (
            {},
            {"dynamic_tools": {"enabled": True, "call_count": 0}},
            {
                "dynamic_tools": {
                    "enabled": True,
                    "call_count": 2,
                    "success_count": 2,
                    "tool_call_counts": {
                        TOOL_STRUCTURE_SELECT_FAILURE_PATTERNS: 1,
                        "reqpilotx_d2_get_logical_table": 1,
                    },
                }
            },
        )
        for summary in invalid_summaries:
            with (
                self.subTest(summary=summary),
                self.assertRaisesRegex(
                    LifecycleProbeError,
                    "DYNAMIC_TOOL_PROBE_NOT_OBSERVED",
                ),
            ):
                _validate_dynamic_tool_probe_activity(
                    dynamic_tool_probe=True,
                    tool_activity_summary=summary,
                )

    def test_candidate_kit_selects_legacy_or_region_d2_contract_explicitly(
        self,
    ) -> None:
        source_kit = Path(__file__).resolve().parents[2] / "agent-kit"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy = root / "legacy"
            region = root / "region"
            shutil.copytree(source_kit, legacy)
            shutil.copytree(source_kit, region)

            _activate_candidate_stages(
                legacy,
                through_d3=True,
                region_object_parent_flow=False,
            )
            _activate_candidate_stages(
                region,
                through_d3=True,
                region_object_parent_flow=True,
            )

            legacy_stage = _d2_manifest_stage(legacy)
            region_stage = _d2_manifest_stage(region)
            self.assertEqual(
                legacy_stage["runner_output_schema_id"],
                "d2-logical-structure-decision-candidate/v2",
            )
            self.assertEqual(
                legacy_stage["artifact_schema_id"],
                "logical-structure-proposal/v1",
            )
            self.assertEqual(
                region_stage["runner_output_schema_id"],
                "d2-logical-structure-decision-candidate/v3",
            )
            self.assertEqual(
                region_stage["artifact_schema_id"],
                "logical-structure-proposal/v2",
            )
            self.assertEqual(
                next(
                    item
                    for item in load_portable_agent_kit(legacy).manifest.stages
                    if item.stage is AgentExtractionStage.LOGICAL_STRUCTURE
                ).runner_output_schema_id,
                "d2-logical-structure-decision-candidate/v2",
            )
            self.assertEqual(
                next(
                    item
                    for item in load_portable_agent_kit(region).manifest.stages
                    if item.stage is AgentExtractionStage.LOGICAL_STRUCTURE
                ).runner_output_schema_id,
                "d2-logical-structure-decision-candidate/v3",
            )

    def test_region_mode_is_explicit_and_does_not_change_legacy_modes(self) -> None:
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "REGION_OBJECT_PARENT_FLOW_REQUIRES_THROUGH_D3",
        ):
            _validate_canary_modes(
                through_d3=False,
                object_parent_flow=False,
                region_object_parent_flow=True,
            )
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "OBJECT_PARENT_FLOW_MODES_ARE_MUTUALLY_EXCLUSIVE",
        ):
            _validate_canary_modes(
                through_d3=True,
                object_parent_flow=True,
                region_object_parent_flow=True,
            )
        _validate_canary_modes(
            through_d3=True,
            object_parent_flow=False,
            region_object_parent_flow=True,
            object_parent_plan_only=True,
        )
        _validate_canary_modes(
            through_d3=True,
            object_parent_flow=True,
            region_object_parent_flow=False,
        )

    def test_plan_only_requires_object_flow_and_reports_execution_modes(self) -> None:
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "OBJECT_PARENT_PLAN_ONLY_REQUIRES_OBJECT_FLOW",
        ):
            _validate_canary_modes(
                through_d3=True,
                object_parent_flow=False,
                object_parent_plan_only=True,
            )
        plan = SimpleNamespace(
            plan_sha256="a" * 64,
            targets=(
                SimpleNamespace(
                    execution_mode=ParentProvisionObjectExecutionMode.MODEL_TARGET,
                    object_kind=D2LogicalObjectKind.TABLE,
                    segment_ordinals=(0, 1),
                ),
                SimpleNamespace(
                    execution_mode=(
                        ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER
                    ),
                    object_kind=D2LogicalObjectKind.DOCUMENT_UNIT,
                    segment_ordinals=(),
                ),
                SimpleNamespace(
                    execution_mode=(
                        ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED
                    ),
                    object_kind=D2LogicalObjectKind.ATTACHMENT,
                    segment_ordinals=(),
                ),
            ),
        )

        summary = _object_parent_plan_summary(plan)

        self.assertEqual(summary["model_target_count"], 1)
        self.assertEqual(summary["host_empty_count"], 1)
        self.assertEqual(summary["recovery_required_count"], 1)
        self.assertEqual(summary["source_segment_count"], 2)

    def test_incomplete_summary_preserves_real_nonaccepted_reason(self) -> None:
        result = SimpleNamespace(
            object_result=SimpleNamespace(
                plan=SimpleNamespace(
                    plan_sha256="1" * 64,
                    targets=(
                        SimpleNamespace(
                            execution_mode=(
                                ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER
                            )
                        ),
                        SimpleNamespace(
                            execution_mode=(
                                ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED
                            )
                        ),
                    ),
                ),
                batch_receipts=(
                    SimpleNamespace(
                        status=(
                            ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER
                        ),
                        attempts=(),
                    ),
                    SimpleNamespace(
                        status=ParentProvisionRecognitionBatchStatus.BLOCKED,
                        attempts=(),
                    ),
                ),
                execution_receipt=SimpleNamespace(receipt_sha256="2" * 64),
            ),
            coordination_result=None,
            reason_codes=("OBJECT_BATCH_EVIDENCE_RECOVERY_REQUIRED",),
        )

        summary = _object_parent_flow_incomplete_summary(result)

        self.assertEqual(summary["host_empty_count"], 1)
        self.assertEqual(summary["recovery_required_count"], 1)
        self.assertEqual(
            summary["reason_codes"],
            ("OBJECT_BATCH_EVIDENCE_RECOVERY_REQUIRED",),
        )

    def test_object_parent_flow_requires_through_d3(self) -> None:
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "OBJECT_PARENT_FLOW_REQUIRES_THROUGH_D3",
        ):
            _validate_canary_modes(
                through_d3=False,
                object_parent_flow=True,
            )
        _validate_canary_modes(through_d3=True, object_parent_flow=True)
        _validate_canary_modes(through_d3=False, object_parent_flow=False)

    def test_object_and_reconciliation_task_bindings_are_distinct_and_stable(
        self,
    ) -> None:
        values = {
            "run_id": "agent-run-" + "1" * 32,
            "source_sha256": "2" * 64,
            "d2_artifact_sha256": "3" * 64,
            "source_scope_sha256": "4" * 64,
            "stage_input_bundle_sha256": "5" * 64,
            "plan_id": "parent-provision-object-plan-" + "6" * 32,
            "plan_sha256": "7" * 64,
        }

        first = _object_parent_flow_task_bindings(**values)
        second = _object_parent_flow_task_bindings(**values)

        self.assertEqual(first, second)
        self.assertNotEqual(first[0], first[1])
        self.assertTrue(all(len(item) == 64 for item in first))

    def test_failure_reason_is_content_free_machine_code(self) -> None:
        self.assertEqual(
            _safe_object_parent_failure_code("RETRY_BUDGET_EXHAUSTED"),
            "D3_OBJECT_PARENT_FLOW_RETRY_BUDGET_EXHAUSTED",
        )
        self.assertEqual(
            _safe_object_parent_failure_code("unsafe source text"),
            "D3_OBJECT_PARENT_FLOW_FAILED",
        )

    def test_region_structure_summary_has_stable_content_free_fingerprint(
        self,
    ) -> None:
        source = _source_fixture()
        kit = _kit_identity()
        d1 = _d1_identity(kit)
        table_map = AgentD1TableMapArtifactIdentityV1(
            source_sha256=source.source_sha256,
            artifact_id="d1-table-map-" + "4" * 32,
            artifact_sha256="5" * 64,
        )
        compiled = compile_and_validate_d2_logical_structure_candidate_v3(
            _complete_candidate(source),
            source_ir=source,
            expected_d1_artifact=d1,
            expected_d1_table_map_artifact=table_map,
            expected_kit_identity=kit,
        )
        proposal = compiled.final_compilation.proposal
        validation_receipt = compiled.final_compilation.validation_receipt
        result = SimpleNamespace(
            proposal=proposal,
            host_validation_receipt=validation_receipt,
            stage_artifact=SimpleNamespace(
                status=SimpleNamespace(value="VALIDATED"),
                artifact_id=validation_receipt.artifact_id,
                payload_sha256=validation_receipt.artifact_sha256,
            ),
            execution_receipt=SimpleNamespace(receipt_sha256="e" * 64),
        )

        first = _d2_region_summary(result)
        second = _d2_region_summary(result)

        self.assertEqual(
            first["repeatability_fingerprint"], second["repeatability_fingerprint"]
        )
        fingerprint = first["repeatability_fingerprint"]
        self.assertEqual(fingerprint["physical_table_count"], 2)
        self.assertEqual(len(fingerprint["fingerprint_sha256"]), 64)
        serialized = str(first).lower()
        self.assertNotIn("raw_text", serialized)
        self.assertNotIn("source text", serialized)
        self.assertNotIn("/users/", serialized)
        self.assertTrue(first["logical_object_profiles"])
        self.assertEqual(len(first["table_profiles"]), 2)
        self.assertEqual(len(first["region_profiles"]), 2)
        profile = first["logical_object_profiles"][0]
        self.assertEqual(
            set(profile),
            {
                "ordinal",
                "origin",
                "kind",
                "parent_ordinal",
                "source_order",
                "pages",
                "segment_count",
                "segment_kind_runs",
                "segment_kind_counts",
                "nested_evidence_count",
                "supplemental_evidence_count",
            },
        )

    def test_region_repeatability_separates_object_and_table_region_drift(
        self,
    ) -> None:
        object_profile = {
            "ordinal": 0,
            "origin": "RETAINED_BASE",
            "kind": "SECTION",
            "parent_ordinal": None,
            "source_order": 0,
            "pages": [1],
            "segment_count": 1,
            "segment_kind_runs": [{"kind": "BASE_SOURCE_SEGMENT", "count": 1}],
            "segment_kind_counts": {"BASE_SOURCE_SEGMENT": 1},
            "nested_evidence_count": 0,
            "supplemental_evidence_count": 0,
        }
        table_profiles = (
            {
                "ordinal": 0,
                "source_order": 9,
                "region_count": 1,
                "empty_table_disposition": None,
                "supplemental_dispositions": ["ABSENT", "ABSENT", "ABSENT"],
            },
        )
        region_profiles = (
            {
                "table_ordinal": 0,
                "region_ordinal": 0,
                "disposition": "LOGICAL_SCOPE_PART",
                "owner_object_ordinal": 0,
                "nested_owner_position": None,
            },
        )

        first = _comparison_summary(
            objects=tuple(
                {**object_profile, "ordinal": ordinal} for ordinal in range(5)
            ),
            tables=table_profiles,
            regions=region_profiles,
        )
        second = _comparison_summary(
            objects=tuple(
                {**object_profile, "ordinal": ordinal} for ordinal in range(8)
            ),
            tables=table_profiles,
            regions=region_profiles,
        )

        drift = _compare_region_structure_summaries(first, second)
        stable = _compare_region_structure_summaries(first, first)

        self.assertEqual(drift["verdict"], "FAIL")
        self.assertEqual(drift["logical_object_verdict"], "FAIL")
        self.assertEqual(drift["table_region_verdict"], "PASS")
        self.assertEqual(drift["first_logical_object_count"], 5)
        self.assertEqual(drift["second_logical_object_count"], 8)
        self.assertEqual(drift["logical_object_count_delta"], 3)
        self.assertIn("LOGICAL_OBJECT_COUNT_DRIFT", drift["reason_codes"])
        self.assertEqual(stable["verdict"], "PASS")
        self.assertEqual(stable["reason_codes"], ())

    def test_synthetic_region_oracle_accepts_grounded_heading_topology(self) -> None:
        objects = (
            _object_profile(0, "DOCUMENT_UNIT", None, 0, [1], 1),
            _object_profile(1, "SECTION", 0, 1, [1], 1),
            _object_profile(2, "SECTION", 1, 2, [1], 2),
            _object_profile(3, "SECTION", 0, 4, [1], 1),
            _object_profile(4, "SECTION", 3, 5, [1], 2),
            _object_profile(5, "SECTION", 3, 7, [1], 3),
            _object_profile(
                6,
                "TABLE",
                5,
                14,
                [1],
                1,
                segment_kind="TABLE_REGION",
            ),
            _object_profile(7, "SECTION", 0, 15, [2], 0),
            _object_profile(8, "SECTION", 7, 15, [2], 1),
        )
        summary = {
            "artifact_status": "PARTIAL",
            "logical_object_profiles": objects,
            "table_profiles": (
                {
                    "ordinal": 0,
                    "source_order": 14,
                    "region_count": 1,
                    "empty_table_disposition": None,
                    "supplemental_dispositions": [
                        "ABSENT",
                        "ABSENT",
                        "OWNED_BY_SCOPE",
                    ],
                },
            ),
            "region_profiles": (
                {
                    "table_ordinal": 0,
                    "region_ordinal": 0,
                    "disposition": "LOGICAL_SCOPE_PART",
                    "owner_object_ordinal": 6,
                    "nested_owner_position": None,
                },
            ),
        }

        accepted = _synthetic_region_structure_oracle(summary)
        caption_section = {**objects[5], "source_order": 9}
        rejected = _synthetic_region_structure_oracle(
            {**summary, "logical_object_profiles": (*objects, caption_section)}
        )

        self.assertEqual(accepted["verdict"], "PASS")
        self.assertEqual(accepted["reason_codes"], ())
        self.assertEqual(rejected["verdict"], "FAIL")
        self.assertIn(
            "SYNTHETIC_TABLE_CAPTION_PROMOTED_TO_SECTION",
            rejected["reason_codes"],
        )

    def test_region_runtime_summary_exposes_only_counts_and_hashes(self) -> None:
        runtime = SimpleNamespace(
            stage_input_status=SimpleNamespace(value="COMPLETE"),
            stage_input_manifest_id="d3-region-stage-input-" + "1" * 32,
            stage_input_manifest_sha256="2" * 64,
            adapter_sha256="3" * 64,
            logical_objects=(object(), object()),
            source_segments=(object(), object(), object()),
            evidence_ledger=(object(),),
            nested_evidence_ledger=(),
            selected_parent_assignment_ledger=(),
            unresolved_scopes=(),
            d2_identity=SimpleNamespace(source_scope_sha256="4" * 64),
        )

        summary = _region_parent_runtime_safe_summary(
            runtime,
            workspace_bundle_sha256="5" * 64,
        )

        self.assertEqual(summary["logical_object_count"], 2)
        self.assertEqual(summary["exact_source_segment_count"], 3)
        serialized = str(summary).lower()
        self.assertNotIn("text", serialized)
        self.assertNotIn("path", serialized)
        self.assertNotIn("/users/", serialized)

    def test_published_summary_reports_nested_evidence_assignment_closure(
        self,
    ) -> None:
        closure = SimpleNamespace(
            assignments=(object(), object()),
            unassigned_evidence_ids=("nested-evidence-1",),
            closure_sha256="a" * 64,
        )
        professional = SimpleNamespace(
            proposal=SimpleNamespace(
                entries=(),
                declared_logical_object_ids=(),
                unresolved_logical_object_ids=(),
            ),
            stage_artifact=SimpleNamespace(
                status=SimpleNamespace(value="VALIDATED"),
                payload_sha256="b" * 64,
                validator_receipt_sha256="c" * 64,
            ),
            professional_policy_attestation=SimpleNamespace(
                policy_attested=True,
                model_dump=lambda **_: {"policy_attested": True},
            ),
            professional_proof=SimpleNamespace(proof_sha256="d" * 64),
            professional_execution_receipt=SimpleNamespace(
                model_dump=lambda **_: {"receipt": "professional"}
            ),
            host_validation_receipt=SimpleNamespace(receipt_sha256="e" * 64),
            final_receipt=SimpleNamespace(model_dump=lambda **_: {"receipt": "final"}),
            nested_evidence_assignment_closure=closure,
        )
        result = SimpleNamespace(
            professional_result=professional,
            finalized_execution_receipt=SimpleNamespace(
                finalization_mode=SimpleNamespace(value="DIRECT"),
                receipt_sha256="f" * 64,
            ),
            object_result=SimpleNamespace(
                plan=SimpleNamespace(plan_sha256="1" * 64, targets=()),
                batch_receipts=(),
                execution_receipt=SimpleNamespace(receipt_sha256="2" * 64),
            ),
            coordination_result=None,
        )

        summary = _object_parent_flow_summary(result)

        self.assertEqual(summary["nested_evidence_assignment_count"], 2)
        self.assertEqual(summary["nested_evidence_unassigned_count"], 1)
        self.assertEqual(
            summary["nested_evidence_assignment_closure_sha256"],
            closure.closure_sha256,
        )

    def test_region_request_workspace_replay_and_runtime_adapter_connect(self) -> None:
        source = _source_fixture()
        kit = _kit_identity()
        d1 = _d1_identity(kit)
        table_map = AgentD1TableMapArtifactIdentityV1(
            source_sha256=source.source_sha256,
            artifact_id="d1-table-map-" + "4" * 32,
            artifact_sha256="5" * 64,
        )
        compiled = compile_and_validate_d2_logical_structure_candidate_v3(
            _complete_candidate(source),
            source_ir=source,
            expected_d1_artifact=d1,
            expected_d1_table_map_artifact=table_map,
            expected_kit_identity=kit,
        )
        result = SimpleNamespace(
            proposal=compiled.final_compilation.proposal,
            host_validation_receipt=(compiled.final_compilation.validation_receipt),
            compilation=compiled,
        )
        request = _build_region_stage_input_request(
            source_ir=source,
            d2_result=result,
        )
        stage_input = materialize_d3_region_stage_input(
            source,
            proposal=request.d2_proposal,
            validation_receipt=request.d2_validation_receipt,
            parent_input=request.parent_input,
            parent_input_receipt=request.parent_input_receipt,
            expected_lineage=request.expected_lineage,
        )
        workspace = SimpleNamespace(
            stage_input_manifest=stage_input.manifest,
            bundle_sha256="e" * 64,
        )

        runtime = _adapt_region_parent_runtime_for_canary(
            request=request,
            workspace_attestation=workspace,
            expected_kit_identity=kit,
        )

        self.assertEqual(
            runtime.stage_input_manifest_sha256,
            stage_input.manifest.manifest_sha256,
        )
        self.assertEqual(runtime.workspace_bundle_sha256, workspace.bundle_sha256)
        self.assertEqual(
            runtime.d2_identity.artifact_id,
            compiled.final_compilation.validation_receipt.artifact_id,
        )
        self.assertTrue(runtime.source_segments)
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "D3_REGION_STAGE_INPUT_WORKSPACE_REPLAY_DRIFT",
        ):
            _adapt_region_parent_runtime_for_canary(
                request=request,
                workspace_attestation=SimpleNamespace(
                    stage_input_manifest=stage_input.manifest.model_copy(
                        update={"manifest_sha256": "f" * 64}
                    )
                ),
                expected_kit_identity=kit,
            )


def _d2_manifest_stage(kit_root: Path) -> dict[str, object]:
    manifest = json.loads((kit_root / "manifest.json").read_text(encoding="utf-8"))
    return next(
        item for item in manifest["stages"] if item["stage"] == "D2_LOGICAL_STRUCTURE"
    )


def _object_profile(
    ordinal: int,
    kind: str,
    parent_ordinal: int | None,
    source_order: int,
    pages: list[int],
    segment_count: int,
    *,
    segment_kind: str = "BASE_SOURCE_SEGMENT",
) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "origin": ("REGION_LOGICAL_SCOPE" if kind == "TABLE" else "RETAINED_BASE"),
        "kind": kind,
        "parent_ordinal": parent_ordinal,
        "source_order": source_order,
        "pages": pages,
        "segment_count": segment_count,
        "segment_kind_runs": (
            [{"kind": segment_kind, "count": segment_count}] if segment_count else []
        ),
        "segment_kind_counts": ({segment_kind: segment_count} if segment_count else {}),
        "nested_evidence_count": 0,
        "supplemental_evidence_count": 1 if kind == "TABLE" else 0,
    }


def _comparison_summary(
    *,
    objects: tuple[dict[str, object], ...],
    tables: tuple[dict[str, object], ...],
    regions: tuple[dict[str, object], ...],
) -> dict[str, object]:
    return {
        "artifact_status": "PARTIAL",
        "unresolved_region_count": 1,
        "unresolved_empty_table_count": 0,
        "unresolved_supplemental_evidence_count": 0,
        "logical_object_profiles": objects,
        "table_profiles": tables,
        "region_profiles": regions,
    }


if __name__ == "__main__":
    unittest.main()
