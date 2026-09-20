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

from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    build_synthetic_pdf,
)
from app.adapters.agents.document_evidence_host import (
    DocumentEvidenceToolHost,
    prepare_document_evidence,
)
from app.adapters.agents.document_parent_provision_evidence_recovery import (
    DocumentParentProvisionEvidenceRecoveryExecutor,
    PublishedParentProvisionEvidenceRevision,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.application.agent_workflow.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceGap,
    ParentProvisionEvidenceRecoverability,
    ParentProvisionEvidenceRecoveryOutcome,
    ParentProvisionEvidenceRecoveryState,
    ParentProvisionEvidenceRecoveryStrategy,
    build_parent_provision_evidence_recovery_session,
    stable_parent_provision_evidence_recovery_attempt_id,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectBatchPlanV1,
)
from app.application.agent_workflow.parent_provision_evidence_recovery import (
    canonical_json_sha256,
)
from app.ports.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceRecoveryExecutionRequestV1,
    ParentProvisionEvidenceRecoveryExecutionTargetV1,
    ParentProvisionEvidenceRecoveryExecutorError,
)

if __package__:
    from .test_parent_provision_evidence_recovery import (
        NEW_STAGE_INPUT_SHA256,
        RECOVERY_OBJECT_ID,
        _object_plans,
        _recovery_plan,
    )
else:
    from test_parent_provision_evidence_recovery import (
        NEW_STAGE_INPUT_SHA256,
        RECOVERY_OBJECT_ID,
        _object_plans,
        _recovery_plan,
    )


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class DocumentParentProvisionEvidenceRecoveryExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / "recovery.pdf"
        build_synthetic_pdf(self.source, include_prompt_injection=True)
        self.source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        kit = load_portable_agent_kit(REPOSITORY_ROOT / "agent-kit")
        self.source_handle = "source-handle-" + "b" * 32
        self.prepared = prepare_document_evidence(
            self.source,
            expected_source_sha256=self.source_sha256,
            document_role="CUSTOMER_SOR",
            kit_identity=kit.identity,
            stage_context_sha256="e" * 64,
            run_id="agent-run-" + "a" * 32,
            opaque_source_handle=self.source_handle,
        )
        original, _new_plan = _object_plans()
        self.old_plan = _rehashed_source_plan(original, self.source_sha256)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_native_text_and_page_render_use_real_source_bound_tools(self) -> None:
        cases = (
            (
                ParentProvisionEvidenceGap.SCANNED_OR_NO_NATIVE_TEXT,
                ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT,
                "page_text",
            ),
            (
                ParentProvisionEvidenceGap.MISSING_PAGE_RENDER,
                ParentProvisionEvidenceRecoveryStrategy.RERENDER_SOURCE_PAGES,
                "page_renders",
            ),
        )
        for gap, strategy, acquisition_field in cases:
            with self.subTest(strategy=strategy):
                published_acquisitions = []

                def publish(request, acquisition):  # type: ignore[no-untyped-def]
                    published_acquisitions.append(acquisition)
                    return PublishedParentProvisionEvidenceRevision(
                        new_stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
                        new_target_evidence_sha256=(
                            request.session.plan.original_target_evidence_sha256
                        ),
                    )

                request = _request(
                    old_plan=self.old_plan,
                    source_handle=self.source_handle,
                    gap=gap,
                    strategy=strategy,
                    page_numbers=(1,),
                )
                result = DocumentParentProvisionEvidenceRecoveryExecutor(
                    host=DocumentEvidenceToolHost(self.prepared),
                    publish_revision=publish,
                ).execute(request, cancellation_requested=lambda: False)

                self.assertEqual(
                    result.outcome,
                    ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
                )
                self.assertEqual(len(published_acquisitions), 1)
                evidence = getattr(published_acquisitions[0], acquisition_field)
                self.assertTrue(evidence)
                self.assertEqual(len(published_acquisitions[0].evidence_sha256), 64)
                if (
                    strategy
                    is ParentProvisionEvidenceRecoveryStrategy.RERENDER_SOURCE_PAGES
                ):
                    rendered = evidence[0]
                    self.assertEqual(
                        hashlib.sha256(rendered.image_bytes).hexdigest(),
                        rendered.receipt.image_sha256,
                    )

    def test_empty_native_text_switches_strategy_without_publishing(self) -> None:
        published = []
        request = _request(
            old_plan=self.old_plan,
            source_handle=self.source_handle,
            gap=ParentProvisionEvidenceGap.SCANNED_OR_NO_NATIVE_TEXT,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT,
            page_numbers=(2,),
        )
        result = DocumentParentProvisionEvidenceRecoveryExecutor(
            host=DocumentEvidenceToolHost(self.prepared),
            publish_revision=lambda *_args: published.append(True),  # type: ignore[arg-type,return-value]
        ).execute(request, cancellation_requested=lambda: False)

        self.assertEqual(result.state, ParentProvisionEvidenceRecoveryState.FAILED)
        self.assertEqual(result.failure_code, "NATIVE_TEXT_STILL_EMPTY")
        self.assertEqual(published, [])

    def test_table_recovery_reads_complete_structured_cell_evidence(self) -> None:
        table_ids = self.prepared.page_tables[1]
        self.assertTrue(table_ids)
        published_acquisitions = []

        def publish(request, acquisition):  # type: ignore[no-untyped-def]
            published_acquisitions.append(acquisition)
            return PublishedParentProvisionEvidenceRevision(
                new_stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
                new_target_evidence_sha256=(
                    request.session.plan.original_target_evidence_sha256
                ),
            )

        request = _request(
            old_plan=self.old_plan,
            source_handle=self.source_handle,
            gap=ParentProvisionEvidenceGap.MISSING_TABLE_EVIDENCE,
            strategy=(ParentProvisionEvidenceRecoveryStrategy.REBUILD_TABLE_EVIDENCE),
            page_numbers=(1,),
            table_ids=table_ids,
        )
        result = DocumentParentProvisionEvidenceRecoveryExecutor(
            host=DocumentEvidenceToolHost(self.prepared),
            publish_revision=publish,
        ).execute(request, cancellation_requested=lambda: False)

        self.assertEqual(
            result.outcome,
            ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
        )
        maps = published_acquisitions[0].table_maps
        self.assertEqual(tuple(item.table_id for item in maps), table_ids)
        self.assertTrue(all(item.cells_included for item in maps))
        self.assertGreater(sum(len(item.cells) for item in maps), 0)

    def test_table_recovery_rejects_a_table_outside_the_object_pages(self) -> None:
        table_ids = self.prepared.page_tables[1]
        self.assertTrue(table_ids)
        request = _request(
            old_plan=self.old_plan,
            source_handle=self.source_handle,
            gap=ParentProvisionEvidenceGap.MISSING_TABLE_EVIDENCE,
            strategy=(ParentProvisionEvidenceRecoveryStrategy.REBUILD_TABLE_EVIDENCE),
            page_numbers=(2,),
            table_ids=table_ids,
        )
        executor = DocumentParentProvisionEvidenceRecoveryExecutor(
            host=DocumentEvidenceToolHost(self.prepared),
            publish_revision=lambda *_args: self.fail(
                "out-of-scope table must not be published"
            ),  # type: ignore[arg-type,return-value]
        )

        with self.assertRaisesRegex(
            ParentProvisionEvidenceRecoveryExecutorError,
            "RECOVERY_TABLE_SCOPE_MISMATCH",
        ):
            executor.execute(request, cancellation_requested=lambda: False)

    def test_unconnected_visual_recovery_is_explicitly_unavailable(self) -> None:
        published = []
        request = _request(
            old_plan=self.old_plan,
            source_handle=self.source_handle,
            gap=ParentProvisionEvidenceGap.SCANNED_OR_NO_NATIVE_TEXT,
            strategy=(
                ParentProvisionEvidenceRecoveryStrategy.AUTHORIZED_VISUAL_TEXT_OCR
            ),
            page_numbers=(2,),
        )
        result = DocumentParentProvisionEvidenceRecoveryExecutor(
            host=DocumentEvidenceToolHost(self.prepared),
            publish_revision=lambda *_args: published.append(True),  # type: ignore[arg-type,return-value]
        ).execute(request, cancellation_requested=lambda: False)

        self.assertEqual(
            result.failure_code,
            "RECOVERY_VISUAL_CAPABILITY_UNAVAILABLE",
        )
        self.assertEqual(published, [])


def _request(
    *,
    old_plan: ParentProvisionObjectBatchPlanV1,
    source_handle: str,
    gap: ParentProvisionEvidenceGap,
    strategy: ParentProvisionEvidenceRecoveryStrategy,
    page_numbers: tuple[int, ...],
    table_ids: tuple[str, ...] = (),
) -> ParentProvisionEvidenceRecoveryExecutionRequestV1:
    session = build_parent_provision_evidence_recovery_session(
        plan=_recovery_plan(
            old_plan,
            gap=gap,
            recoverability=ParentProvisionEvidenceRecoverability.AUTOMATIC,
            strategies=(strategy,),
            max_attempts=1,
        )
    )
    attempt_id = stable_parent_provision_evidence_recovery_attempt_id(
        recovery_session_id=session.recovery_session_id,
        recovery_plan_sha256=session.plan.recovery_plan_sha256,
        attempt_ordinal=1,
        strategy=strategy,
        feedback_sha256=None,
    )
    return ParentProvisionEvidenceRecoveryExecutionRequestV1(
        session=session,
        target=ParentProvisionEvidenceRecoveryExecutionTargetV1(
            source_handle=source_handle,
            workspace_bundle_sha256=old_plan.stage_input_bundle_sha256,
            logical_object_id=RECOVERY_OBJECT_ID,
            page_numbers=page_numbers,
            table_ids=table_ids,
        ),
        attempt_id=attempt_id,
        attempt_ordinal=1,
        strategy=strategy,
        feedback_sha256=None,
    )


def _rehashed_source_plan(
    plan: ParentProvisionObjectBatchPlanV1,
    source_sha256: str,
) -> ParentProvisionObjectBatchPlanV1:
    payload = plan.model_dump(mode="python", exclude={"plan_id", "plan_sha256"})
    payload["source_sha256"] = source_sha256
    payload["targets"] = plan.targets
    draft = ParentProvisionObjectBatchPlanV1.model_construct(
        plan_id="parent-provision-object-plan-" + "0" * 32,
        plan_sha256="0" * 64,
        **payload,
    )
    digest = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"plan_id", "plan_sha256"})
    )
    return ParentProvisionObjectBatchPlanV1(
        plan_id="parent-provision-object-plan-" + digest[:32],
        plan_sha256=digest,
        **payload,
    )


if __name__ == "__main__":
    unittest.main()
