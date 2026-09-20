from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from pathlib import Path
import tempfile
from types import SimpleNamespace

from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.agent_workflow.d3_parent_provisions import canonical_json_sha256
from app.application.agent_workflow.native_parent_provision_jobs import (
    NativeParentProvisionResultArtifactV1,
    _estimated_source_window_count,
    _has_complete_working_decisions,
    _load_reconciliation_checkpoints,
    _load_window_checkpoints,
    _NativeParentProgress,
    _parent_result_artifact,
    _reconciliation_checkpoint_sink,
    _window_checkpoint_sink,
)
from app.adapters.agents.codex_app_server.parent_provision_reconciliation_driver import (
    ParentProvisionObjectReconciliationStageDriver,
)
from app.application.agent_workflow.native_structure_baseline import (
    NativeStructureAcceptedBaselineV1,
)

if __package__:
    from .test_parent_provision_object_workflow import (
        OBJECT_TASK_SHA256,
        _DualSession,
        _object_outcomes,
        _orchestrator,
        _workflow_fixture,
    )
    from .test_parent_provision_reconciliation_driver import (
        _Session as _ReconciliationSession,
        _QueueTransport,
        _candidate_for_group,
        _compiler_for_activation,
        _dual_activation,
        _initial_result,
        _reconciliation_plan,
    )
else:
    from test_parent_provision_object_workflow import (
        OBJECT_TASK_SHA256,
        _DualSession,
        _object_outcomes,
        _orchestrator,
        _workflow_fixture,
    )
    from test_parent_provision_reconciliation_driver import (
        _Session as _ReconciliationSession,
        _QueueTransport,
        _candidate_for_group,
        _compiler_for_activation,
        _dual_activation,
        _initial_result,
        _reconciliation_plan,
    )


class NativeParentProvisionJobTests(unittest.TestCase):
    def test_window_professional_result_forms_replayable_private_artifact(self) -> None:
        fixture = _workflow_fixture()
        self.addCleanup(fixture.temporary_directory.cleanup)
        workflow = _orchestrator(
            fixture,
            object_transport=_QueueTransport(_object_outcomes(fixture)),
            reconciliation_transport=_QueueTransport(()),
        ).run(
            _DualSession(
                fixture.object_activation,
                fixture.reconciliation_activation,
            ),
            object_task_binding_sha256=OBJECT_TASK_SHA256,
        )
        self.assertTrue(workflow.publication_ready)
        assert workflow.professional_result is not None
        baseline = _baseline_for(fixture)

        artifact = _parent_result_artifact(
            baseline=baseline,
            workflow=workflow,
            professional=workflow.professional_result,
        )

        self.assertIsInstance(artifact, NativeParentProvisionResultArtifactV1)
        self.assertEqual(artifact.baseline, baseline)
        self.assertTrue(_has_complete_working_decisions(artifact.proposal))
        self.assertEqual(
            NativeParentProvisionResultArtifactV1.model_validate_json(
                artifact.model_dump_json()
            ),
            artifact,
        )

    def test_private_window_checkpoint_round_trips_for_resume(self) -> None:
        fixture = _workflow_fixture()
        self.addCleanup(fixture.temporary_directory.cleanup)
        workflow = _orchestrator(
            fixture,
            object_transport=_QueueTransport(_object_outcomes(fixture)),
            reconciliation_transport=_QueueTransport(()),
        ).run(
            _DualSession(
                fixture.object_activation,
                fixture.reconciliation_activation,
            ),
            object_task_binding_sha256=OBJECT_TASK_SHA256,
        )
        baseline = _baseline_for(fixture)
        receipt_by_id = {
            item.batch_id: item for item in workflow.object_result.batch_receipts
        }
        values = tuple(
            (receipt_by_id[item.batch_id], item)
            for item in workflow.object_result.compilations
        )
        with tempfile.TemporaryDirectory() as raw:
            store = LocalObjectStore(Path(raw) / "objects")
            context = _Context()
            progress = _NativeParentProgress(context)
            _window_checkpoint_sink(
                object_store=store,
                baseline=baseline,
                plan=workflow.object_result.plan,
                progress=progress,
            )(values)

            receipts, compilations = _load_window_checkpoints(
                store,
                records=progress.window_checkpoints,
                baseline=baseline,
                plan=workflow.object_result.plan,
            )

        self.assertEqual(receipts, tuple(item[0] for item in values))
        self.assertEqual(compilations, tuple(item[1] for item in values))
        self.assertEqual(context.progress[-1]["completed_window_count"], 1)
        self.assertEqual(context.progress[-1]["checkpoint_count"], 1)
        self.assertEqual(
            context.progress[-1]["validated_batch_count"],
            len(values),
        )

    def test_remaining_window_estimate_excludes_validated_batches(self) -> None:
        fixture = _workflow_fixture()
        self.addCleanup(fixture.temporary_directory.cleanup)
        plan = fixture.compiler.build_plan()
        model_targets = tuple(
            item for item in plan.targets if item.execution_mode.value == "MODEL_TARGET"
        )

        full = _estimated_source_window_count(
            plan.targets,
            max_targets=1,
            max_source_characters=25_000,
        )
        remaining = _estimated_source_window_count(
            plan.targets,
            max_targets=1,
            max_source_characters=25_000,
            excluded_batch_ids=frozenset(item.batch_id for item in model_targets),
        )

        self.assertEqual(full, len(model_targets))
        self.assertEqual(remaining, 0)

    def test_private_reconciliation_checkpoint_round_trips_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "activation").mkdir()
            object_activation, reconciliation_activation = _dual_activation(
                root / "activation"
            )
            fixture, compiler = _compiler_for_activation(object_activation)
            initial = _initial_result(
                activation=object_activation,
                compiler=compiler,
                fixture=fixture,
                marked_model_indexes={1},
            )
            plan = _reconciliation_plan(initial, compiler)
            result = ParentProvisionObjectReconciliationStageDriver(
                transport=_QueueTransport(
                    (_candidate_for_group(fixture, plan.groups[0]),)
                ),
                object_compiler=compiler,
            ).run(
                _ReconciliationSession(reconciliation_activation),
                initial_result=initial,
                task_binding_sha256=OBJECT_TASK_SHA256,
            )
            baseline = _baseline_for(
                SimpleNamespace(
                    compiler=compiler,
                    object_activation=object_activation,
                )
            )
            store = LocalObjectStore(root / "objects")
            context = _Context()
            progress = _NativeParentProgress(context)
            receipt = result.group_receipts_in_completion_order[0]
            compilation = result.group_compilations_in_completion_order[0]
            _reconciliation_checkpoint_sink(
                object_store=store,
                baseline=baseline,
                object_plan=initial.plan,
                progress=progress,
            )(
                result.reconciliation_plan,
                result.reconciliation_plan.groups[0],
                receipt,
                compilation,
            )

            receipts, compilations = _load_reconciliation_checkpoints(
                store,
                records=progress.reconciliation_checkpoints,
                baseline=baseline,
                object_plan=initial.plan,
            )

        self.assertEqual(receipts, (receipt,))
        self.assertEqual(compilations, (compilation,))
        self.assertEqual(
            context.progress[-1]["reconciliation_checkpoint_count"],
            1,
        )


class _Context:
    def __init__(self) -> None:
        self.progress: list[dict[str, object]] = []

    def heartbeat(self, progress=None):  # type: ignore[no-untyped-def]
        if progress:
            self.progress.append(dict(progress))
        return SimpleNamespace(progress=progress or {})


def _baseline_for(fixture) -> NativeStructureAcceptedBaselineV1:  # type: ignore[no-untyped-def]
    identity = fixture.compiler.d2_identity
    stage_input = fixture.object_activation.d3_stage_input_attestation
    assert stage_input is not None
    values = {
        "status": "ACCEPTED",
        "project_id": "project-parent-job",
        "document_id": "document-parent-job",
        "source_sha256": identity.source_sha256,
        "native_artifact_id": "native-parent-job-fixture",
        "native_projection_sha256": "1" * 64,
        "source_ir_id": identity.source_ir_id,
        "source_ir_canonical_sha256": identity.source_ir_canonical_sha256,
        "kit_identity": identity.kit_identity,
        "accepted_structure_artifact_id": identity.artifact_id,
        "accepted_structure_artifact_sha256": identity.artifact_sha256,
        "accepted_structure_validator_receipt_sha256": (
            identity.validator_receipt_sha256
        ),
        "d3_stage_input_manifest_id": "d3-region-stage-input-" + "2" * 32,
        "d3_stage_input_manifest_sha256": "2" * 64,
        "d3_source_scope_sha256": identity.source_scope_sha256,
        "evidence_state": "SUFFICIENT",
        "decision_policy": "NONE",
        "logical_object_count": len(identity.logical_object_ids),
        "exact_source_segment_count": len(identity.source_segment_ids),
        "physical_table_count": 0,
        "table_region_count": 0,
    }
    draft = NativeStructureAcceptedBaselineV1.model_construct(
        **values,
        baseline_id="native-structure-baseline-" + "0" * 32,
        baseline_sha256="0" * 64,
    )
    digest = canonical_json_sha256(
        draft.model_dump(
            mode="json",
            exclude={"baseline_id", "baseline_sha256"},
        )
    )
    return NativeStructureAcceptedBaselineV1(
        **values,
        baseline_id=f"native-structure-baseline-{digest[:32]}",
        baseline_sha256=digest,
    )


if __name__ == "__main__":
    unittest.main()
