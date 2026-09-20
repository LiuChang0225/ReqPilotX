from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from app.application.agent_workflow.activity import (
    AgentActivityActor,
    AgentActivityDisplayV2,
    AgentActivityEventV2,
    AgentActivityFamily,
    AgentActivityPayloadV2,
    WorkflowAcknowledgementPolicy,
    WorkflowCapability,
    WorkflowCapabilityDecision,
    WorkflowConditionDisposition,
    WorkflowConditionDisplayV1,
    WorkflowConditionState,
    WorkflowConditionV1,
    WorkflowRecoveryV1,
    WorkflowRunOutcomeV2,
    WorkflowScopeV1,
    append_activity_event,
    derive_workflow_gate_snapshot,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage


RUN_ID = "agent-run-" + "a" * 32
SOURCE_SHA = "b" * 64


def _condition(
    *,
    token: str,
    code: str,
    disposition: WorkflowConditionDisposition,
    blocked: tuple[WorkflowCapability, ...] = (),
    recovery: WorkflowRecoveryV1 | None = None,
    acknowledgement: WorkflowAcknowledgementPolicy = (
        WorkflowAcknowledgementPolicy.NONE
    ),
) -> WorkflowConditionV1:
    return WorkflowConditionV1(
        condition_id="workflow-condition-" + token * 32,
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA,
        stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
        code=code,
        disposition=disposition,
        state=(
            WorkflowConditionState.RECOVERING
            if recovery is not None
            else WorkflowConditionState.ACTIVE
        ),
        scope=WorkflowScopeV1(kind="PAGE", ref_id="page-23", page_numbers=(23,)),
        blocked_capabilities=blocked,
        acknowledgement_policy=acknowledgement,
        recovery=recovery,
        display=WorkflowConditionDisplayV1(
            title="页面证据状态",
            impact="只影响当前页面或发布结论。",
            agent_action="Agent 将继续安全范围并保留风险。",
        ),
        evidence_refs=("page-23",),
        available_actions=("VIEW_EVIDENCE",),
        raised_sequence=1,
    )


class AgentActivityV2Tests(unittest.TestCase):
    def test_hard_blocker_is_host_limited_and_cannot_be_acknowledged(self) -> None:
        hard = _condition(
            token="1",
            code="source_identity_drift",
            disposition=WorkflowConditionDisposition.HARD_BLOCKER,
            blocked=tuple(WorkflowCapability),
        )
        snapshot = derive_workflow_gate_snapshot(
            run_id=RUN_ID,
            source_sha256=SOURCE_SHA,
            conditions=(hard,),
        )

        decisions = {item.capability: item.decision for item in snapshot.decisions}
        self.assertEqual(
            decisions[WorkflowCapability.READ_AUTHORIZED_SOURCE],
            WorkflowCapabilityDecision.BLOCK,
        )
        with self.assertRaisesRegex(ValidationError, "Host-approved"):
            _condition(
                token="2",
                code="visual_page_uncertain",
                disposition=WorkflowConditionDisposition.HARD_BLOCKER,
                blocked=(WorkflowCapability.EXECUTE_SCOPE,),
            )

    def test_each_hard_code_enforces_its_host_capability_matrix(self) -> None:
        all_capabilities = tuple(WorkflowCapability)
        write_capabilities = (
            WorkflowCapability.APPLY_PROPOSAL,
            WorkflowCapability.PUBLISH_DRAFT,
            WorkflowCapability.LOCK_FINAL,
        )
        cases = (
            ("source_not_authorized", all_capabilities),
            ("source_identity_drift", all_capabilities),
            ("source_input_corrupt", all_capabilities),
            ("source_input_unavailable", all_capabilities),
            ("proposal_ungrounded", write_capabilities),
            ("irreversible_write_conflict", write_capabilities),
        )
        for index, (code, blocked) in enumerate(cases):
            token = "abcdef"[index]
            with self.subTest(code=code):
                hard = _condition(
                    token=token,
                    code=code,
                    disposition=WorkflowConditionDisposition.HARD_BLOCKER,
                    blocked=blocked,
                )
                snapshot = derive_workflow_gate_snapshot(
                    run_id=RUN_ID,
                    source_sha256=SOURCE_SHA,
                    conditions=(hard,),
                )
                decisions = {
                    item.capability: item.decision for item in snapshot.decisions
                }
                for capability in blocked:
                    self.assertEqual(
                        decisions[capability],
                        WorkflowCapabilityDecision.BLOCK,
                    )
                with self.assertRaisesRegex(
                    ValidationError,
                    "exact Host capability",
                ):
                    _condition(
                        token="012345"[index],
                        code=code,
                        disposition=WorkflowConditionDisposition.HARD_BLOCKER,
                        blocked=blocked[:-1],
                    )
                if blocked != all_capabilities:
                    with self.assertRaisesRegex(
                        ValidationError,
                        "exact Host capability",
                    ):
                        _condition(
                            token="6789ab"[index],
                            code=code,
                            disposition=WorkflowConditionDisposition.HARD_BLOCKER,
                            blocked=(*blocked, WorkflowCapability.EXECUTE_SCOPE),
                        )
        with self.assertRaisesRegex(ValidationError, "exact Host capability"):
            _condition(
                token="8",
                code="source_not_authorized",
                disposition=WorkflowConditionDisposition.HARD_BLOCKER,
                blocked=(WorkflowCapability.LOCK_FINAL,),
            )

    def test_risk_allows_execution_and_only_marks_publish_lock(self) -> None:
        risk = _condition(
            token="3",
            code="table_continuation_uncertain",
            disposition=WorkflowConditionDisposition.CONTINUE_WITH_RISK,
            acknowledgement=WorkflowAcknowledgementPolicy.FINAL_CONFIRMATION,
        )
        snapshot = derive_workflow_gate_snapshot(
            run_id=RUN_ID,
            source_sha256=SOURCE_SHA,
            conditions=(risk,),
        )
        decisions = {item.capability: item.decision for item in snapshot.decisions}

        self.assertEqual(
            decisions[WorkflowCapability.EXECUTE_SCOPE],
            WorkflowCapabilityDecision.ALLOW,
        )
        self.assertEqual(
            decisions[WorkflowCapability.PUBLISH_DRAFT],
            WorkflowCapabilityDecision.ALLOW_WITH_RISKS,
        )
        self.assertEqual(
            decisions[WorkflowCapability.LOCK_FINAL],
            WorkflowCapabilityDecision.ALLOW_WITH_RISKS,
        )

    def test_recoverable_failure_waits_only_for_affected_capability(self) -> None:
        recoverable = _condition(
            token="4",
            code="render_tool_timeout",
            disposition=WorkflowConditionDisposition.RECOVERABLE_FAILURE,
            blocked=(WorkflowCapability.EXECUTE_SCOPE,),
            recovery=WorkflowRecoveryV1(attempt=1, max_attempts=2, exhausted=False),
        )
        snapshot = derive_workflow_gate_snapshot(
            run_id=RUN_ID,
            source_sha256=SOURCE_SHA,
            conditions=(recoverable,),
        )
        decisions = {item.capability: item.decision for item in snapshot.decisions}

        self.assertEqual(
            decisions[WorkflowCapability.EXECUTE_SCOPE],
            WorkflowCapabilityDecision.WAIT_RECOVERY,
        )
        self.assertEqual(
            decisions[WorkflowCapability.PUBLISH_DRAFT],
            WorkflowCapabilityDecision.ALLOW,
        )
        self.assertEqual(
            WorkflowRunOutcomeV2.PARTIAL_CONTINUABLE.value,
            "PARTIAL_CONTINUABLE",
        )
        with self.assertRaisesRegex(ValidationError, "must transition"):
            _condition(
                token="9",
                code="render_tool_timeout",
                disposition=WorkflowConditionDisposition.RECOVERABLE_FAILURE,
                blocked=(WorkflowCapability.EXECUTE_SCOPE,),
                recovery=WorkflowRecoveryV1(
                    attempt=2,
                    max_attempts=2,
                    exhausted=True,
                ),
            )

    def test_activity_event_shows_authorized_evidence_but_rejects_secrets_and_cot(self) -> None:
        event = AgentActivityEventV2(
            event_id="agent-activity-" + "5" * 32,
            run_id=RUN_ID,
            source_sha256=SOURCE_SHA,
            sequence=1,
            occurred_at_utc=datetime.now(timezone.utc),
            actor=AgentActivityActor.TOOL,
            family=AgentActivityFamily.TOOL,
            kind="COMPLETED",
            stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
            display=AgentActivityDisplayV2(
                title="已查看第23页匹配评估表",
                summary="页面、表格和区域证据已绑定。",
            ),
            evidence_refs=("page-23", "table-T0024"),
            payload=AgentActivityPayloadV2(
                tool_id="workspace_tools.render-region",
                status="COMPLETED",
                authorized_parameters={
                    "filename": "客户SOR.pdf",
                    "page": 23,
                    "bbox": [10.0, 20.0, 300.0, 500.0],
                    "table_id": "T0024",
                },
            ),
        )

        self.assertEqual(event.payload.authorized_parameters["page"], 23)
        with self.assertRaisesRegex(ValidationError, "forbidden secret or CoT"):
            AgentActivityPayloadV2(
                authorized_parameters={"oauth_token": "do-not-store"}
            )
        for forbidden_key in (
            "access_token",
            "accessToken",
            "publicAccessToken",
            "refresh_token",
            "refreshToken",
            "client_secret",
            "clientSecret",
            "authorization_header",
            "authorizationHeader",
            "credentials",
            "x_api_key",
            "xApiKey",
            "dbCredential",
            "secretKey",
            "prod_secret",
            "apiToken",
            "tokenValue",
        ):
            with self.subTest(forbidden_key=forbidden_key):
                with self.assertRaisesRegex(
                    ValidationError,
                    "forbidden secret or CoT",
                ):
                    AgentActivityPayloadV2(
                        authorized_parameters={forbidden_key: "do-not-store"}
                    )
        with self.assertRaises(ValidationError):
            AgentActivityEventV2.model_validate(
                {**event.model_dump(mode="json"), "raw_chain_of_thought": "hidden"}
            )
        event.payload.authorized_parameters["access_token"] = "mutated"
        with self.assertRaisesRegex(ValueError, "forbidden secret or CoT"):
            event.model_dump_json()

    def test_gate_rejects_mixed_identity_and_duplicate_conditions(self) -> None:
        risk = _condition(
            token="a",
            code="table_continuation_uncertain",
            disposition=WorkflowConditionDisposition.CONTINUE_WITH_RISK,
        )
        with self.assertRaisesRegex(ValueError, "run and source"):
            derive_workflow_gate_snapshot(
                run_id="agent-run-" + "c" * 32,
                source_sha256=SOURCE_SHA,
                conditions=(risk,),
            )
        with self.assertRaisesRegex(ValueError, "unique identities"):
            derive_workflow_gate_snapshot(
                run_id=RUN_ID,
                source_sha256=SOURCE_SHA,
                conditions=(risk, risk),
            )

    def test_append_only_activity_ledger_requires_contiguous_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "activity.jsonl"
            path.touch(mode=0o600)
            first = AgentActivityEventV2(
                event_id="agent-activity-" + "6" * 32,
                run_id=RUN_ID,
                source_sha256=SOURCE_SHA,
                sequence=1,
                occurred_at_utc=datetime.now(timezone.utc),
                actor=AgentActivityActor.AGENT,
                family=AgentActivityFamily.PLAN,
                kind="PUBLISHED",
                display=AgentActivityDisplayV2(title="计划已生成"),
            )
            append_activity_event(
                path,
                first,
                expected_parent=path.parent,
                expected_run_id=RUN_ID,
                expected_source_sha256=SOURCE_SHA,
            )
            with self.assertRaisesRegex(ValueError, "contiguous"):
                append_activity_event(
                    path,
                    first.model_copy(
                        update={
                            "event_id": "agent-activity-" + "7" * 32,
                            "sequence": 3,
                        }
                    ),
                    expected_parent=path.parent,
                    expected_run_id=RUN_ID,
                    expected_source_sha256=SOURCE_SHA,
                )
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_activity_ledger_rejects_corrupt_history_and_mixed_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "activity.jsonl"
            path.touch(mode=0o600)
            first = AgentActivityEventV2(
                event_id="agent-activity-" + "a" * 32,
                run_id=RUN_ID,
                source_sha256=SOURCE_SHA,
                sequence=1,
                occurred_at_utc=datetime.now(timezone.utc),
                actor=AgentActivityActor.AGENT,
                family=AgentActivityFamily.RUN,
                kind="STARTED",
                display=AgentActivityDisplayV2(title="已开始"),
            )
            third = first.model_copy(
                update={
                    "event_id": "agent-activity-" + "b" * 32,
                    "sequence": 3,
                }
            )
            path.write_text(
                first.model_dump_json() + "\n" + third.model_dump_json() + "\n",
                encoding="utf-8",
            )
            fourth = third.model_copy(
                update={
                    "event_id": "agent-activity-" + "c" * 32,
                    "sequence": 4,
                }
            )
            with self.assertRaisesRegex(ValueError, "history must be contiguous"):
                append_activity_event(
                    path,
                    fourth,
                    expected_parent=path.parent,
                    expected_run_id=RUN_ID,
                    expected_source_sha256=SOURCE_SHA,
                )

            foreign_first = first.model_copy(
                update={
                    "event_id": "agent-activity-" + "d" * 32,
                    "source_sha256": "e" * 64,
                }
            )
            path.write_text(foreign_first.model_dump_json() + "\n", encoding="utf-8")
            second = first.model_copy(
                update={
                    "event_id": "agent-activity-" + "e" * 32,
                    "sequence": 2,
                }
            )
            with self.assertRaisesRegex(ValueError, "source identities"):
                append_activity_event(
                    path,
                    second,
                    expected_parent=path.parent,
                    expected_run_id=RUN_ID,
                    expected_source_sha256=SOURCE_SHA,
                )

    def test_activity_ledger_rejects_torn_tail_and_untrusted_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            trusted = root / "trusted"
            trusted.mkdir()
            path = trusted / "activity.jsonl"
            path.write_text("{}", encoding="utf-8")
            event = AgentActivityEventV2(
                event_id="agent-activity-" + "e" * 32,
                run_id=RUN_ID,
                source_sha256=SOURCE_SHA,
                sequence=1,
                occurred_at_utc=datetime.now(timezone.utc),
                actor=AgentActivityActor.HOST,
                family=AgentActivityFamily.RUN,
                kind="STARTED",
                display=AgentActivityDisplayV2(title="已开始"),
            )
            with self.assertRaisesRegex(ValueError, "torn tail"):
                append_activity_event(
                    path,
                    event,
                    expected_parent=trusted,
                    expected_run_id=RUN_ID,
                    expected_source_sha256=SOURCE_SHA,
                )

            outside = root / "outside"
            outside.mkdir()
            (outside / "activity.jsonl").touch()
            linked = root / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "regular file"):
                append_activity_event(
                    linked / "activity.jsonl",
                    event,
                    expected_parent=trusted,
                    expected_run_id=RUN_ID,
                    expected_source_sha256=SOURCE_SHA,
                )

    def test_empty_ledger_rejects_event_outside_host_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "activity.jsonl"
            path.touch()
            foreign = AgentActivityEventV2(
                event_id="agent-activity-" + "f" * 32,
                run_id="agent-run-" + "1" * 32,
                source_sha256="2" * 64,
                sequence=1,
                occurred_at_utc=datetime.now(timezone.utc),
                actor=AgentActivityActor.HOST,
                family=AgentActivityFamily.RUN,
                kind="STARTED",
                display=AgentActivityDisplayV2(title="foreign"),
            )
            with self.assertRaisesRegex(ValueError, "Host-pinned"):
                append_activity_event(
                    path,
                    foreign,
                    expected_parent=path.parent,
                    expected_run_id=RUN_ID,
                    expected_source_sha256=SOURCE_SHA,
                )
            self.assertEqual(path.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
