from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from app.adapters.agents.codex_app_server.canonical_dynamic_tools import (
    CanonicalD2StructureDynamicToolHost,
    CanonicalStructureRevisionDraftV1,
    CanonicalStructureRevisionSubmissionInputV1,
    TOOL_D2_RESOLVE_SOURCE_HANDLES,
    TOOL_STRUCTURE_PUBLISH_REVISION,
    TOOL_STRUCTURE_SELECT_FAILURE_PATTERNS,
    TOOL_STRUCTURE_VALIDATE_MATERIALIZATION,
    canonical_dynamic_tool_specs_sha256,
)
from app.adapters.agents.d2_evidence_host import D2EvidenceToolHost
from app.adapters.agents.structure_failure_pattern_host import (
    StructureFailurePatternHost,
)
from app.application.agent_workflow.d2_logical_structure import D2SourceHandleKind
from app.application.agent_workflow.structure_tool_loop import (
    StructureAgentCandidateRevisionV1,
    StructureProjectionOperationKind,
    StructureProjectionOperationV1,
    StructureRevisionBasisRole,
    stable_structure_agent_revision_id,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
)
from tests.test_d2_evidence_host import (
    SOURCE_HANDLE,
    SOURCE_SHA,
    _d1_identity,
    _kit_identity,
    _source_fixture,
)


class CanonicalDynamicToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        source = _source_fixture()
        kit = _kit_identity()
        evidence = D2EvidenceToolHost(
            source_ir=source,
            source_handle=SOURCE_HANDLE,
            kit_identity=kit,
            validated_d1_artifact=_d1_identity(kit),
            d1_table_map_artifact=AgentD1TableMapArtifactIdentityV1(
                source_sha256=SOURCE_SHA,
                artifact_id="d1-table-map-" + "d" * 32,
                artifact_sha256="e" * 64,
            ),
        )
        self.source = source
        self.evidence = evidence
        self.host = CanonicalD2StructureDynamicToolHost(
            evidence_host=evidence,
            failure_pattern_host=StructureFailurePatternHost(),
        )

    def test_catalog_is_small_high_level_and_stably_hashed(self) -> None:
        names = tuple(item.name for item in self.host.specs)

        self.assertEqual(len(names), 5)
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("reqpilotx_d2_get_page", names)
        self.assertNotIn("reqpilotx_d2_get_logical_table_candidates", names)
        self.assertEqual(len(canonical_dynamic_tool_specs_sha256(self.host.specs)), 64)

    def test_resolve_source_handles_executes_one_host_bound_batch(self) -> None:
        paragraph = self.source.nodes[0]
        result = self.host.invoke(
            tool_name=TOOL_D2_RESOLVE_SOURCE_HANDLES,
            arguments={
                "selections": [
                    {
                        "handle_kind": D2SourceHandleKind.SOURCE_NODE.value,
                        "source_handle": paragraph.source_node_id,
                        "start_offset": 0,
                        "end_offset": 7,
                    }
                ],
            },
        )

        self.assertTrue(result.success)
        self.assertEqual(
            result.result["result"]["segments"][0]["exact_text"],
            "Section",
        )
        self.assertEqual(
            self.evidence.call_counts["source.resolve_handles"],
            1,
        )

    def test_failure_patterns_are_signal_triggered_and_capped(self) -> None:
        result = self.host.invoke(
            tool_name=TOOL_STRUCTURE_SELECT_FAILURE_PATTERNS,
            arguments={
                "signals": sorted(
                    [
                        "accepted_action_has_no_materialization_receipt",
                        "cell_text_contains_following_heading",
                        "duplicate_source_handle_across_visible_owners",
                        "image_not_dispositioned",
                    ]
                )
            },
        )

        self.assertTrue(result.success)
        selected = result.result["result"]["selected_patterns"]
        self.assertEqual(len(selected), 3)

    def test_materialization_validator_returns_compact_revision_signals(self) -> None:
        result = self.host.invoke(
            tool_name=TOOL_STRUCTURE_VALIDATE_MATERIALIZATION,
            arguments={
                "observation": {
                    "confirmed_actions": [
                        {
                            "action_id": "action-1",
                            "materialization_receipt_id": "",
                            "verification_receipt_id": "",
                            "rendered_output_reopened": False,
                        }
                    ]
                }
            },
        )

        self.assertTrue(result.success)
        payload = result.result["result"]
        self.assertEqual(payload["validation_report"]["status"], "FAIL")
        self.assertIn(
            "rendered_output_not_reopened",
            payload["observed_signals"],
        )
        self.assertLessEqual(
            len(payload["selected_failure_patterns"]["selected_patterns"]),
            3,
        )

    def test_invalid_arguments_fail_without_calling_the_evidence_host(self) -> None:
        result = self.host.invoke(
            tool_name=TOOL_D2_RESOLVE_SOURCE_HANDLES,
            arguments={"selections": []},
        )

        self.assertFalse(result.success)
        self.assertEqual(result.result["reason_code"], "dynamic_tool_arguments_invalid")
        self.assertEqual(self.evidence.call_counts, {})

    def test_revision_publisher_is_opt_in_host_bound_and_not_an_identity_echo(
        self,
    ) -> None:
        publisher = _RevisionPublisher()
        host = CanonicalD2StructureDynamicToolHost(
            evidence_host=self.evidence,
            failure_pattern_host=StructureFailurePatternHost(),
            revision_publisher=publisher,
        )
        spec = next(
            item for item in host.specs if item.name == TOOL_STRUCTURE_PUBLISH_REVISION
        )
        values = {
            "target_scope_id": "scope-1",
            "basis_role": StructureRevisionBasisRole.CURRENT_MATERIALIZATION,
            "basis_sha256": "a" * 64,
            "revised_candidate_sha256": "b" * 64,
            "operations": (
                StructureProjectionOperationV1(
                    operation=StructureProjectionOperationKind.PRESERVE_SOURCE_LABEL,
                    target_scope_id="scope-1",
                ),
            ),
            "evidence_refs": ("source-handle-1",),
            "tool_call_ids": (),
        }
        revision = StructureAgentCandidateRevisionV1(
            revision_id=stable_structure_agent_revision_id(**values),
            **values,
        )
        result = host.invoke(
            tool_name=TOOL_STRUCTURE_PUBLISH_REVISION,
            arguments={
                "candidate_revisions": [
                    revision.model_dump(
                        mode="json",
                        exclude={"revision_id", "revised_candidate_sha256"},
                    )
                ],
                "evidence_state": "CONFLICTING",
                "current_action": "正在等待局部渲染复验。",
                "public_summary": "Agent 已提交完整替换候选。",
            },
        )

        self.assertEqual(len(host.specs), 6)
        self.assertNotIn("source_sha256", spec.input_schema["properties"])
        self.assertNotIn("handoff_id", spec.input_schema["properties"])
        self.assertNotIn("command_id", spec.input_schema["properties"])
        self.assertTrue(result.success)
        self.assertEqual(result.result["result"]["status"], "VERIFICATION_REQUIRED")
        self.assertEqual(len(publisher.submissions), 1)
        self.assertEqual(publisher.submissions[0].evidence_state, "CONFLICTING")
        self.assertEqual(
            publisher.submissions[0].candidate_revisions,
            (
                CanonicalStructureRevisionDraftV1.model_validate(
                    revision.model_dump(
                        mode="json",
                        exclude={"revision_id", "revised_candidate_sha256"},
                    )
                ),
            ),
        )


class _RevisionPublisher:
    def __init__(self) -> None:
        self.submissions: list[CanonicalStructureRevisionSubmissionInputV1] = []

    def publish(
        self,
        submission: CanonicalStructureRevisionSubmissionInputV1,
    ) -> dict[str, object]:
        self.submissions.append(submission)
        return {
            "attempt_id": "native-structure-handoff-attempt-" + "1" * 32,
            "receipt_sha256": "2" * 64,
            "status": "VERIFICATION_REQUIRED",
            "current_action": submission.current_action,
        }


if __name__ == "__main__":
    unittest.main()
