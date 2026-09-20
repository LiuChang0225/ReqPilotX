from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from datetime import UTC, datetime, timedelta

from app.application.agent_workflow.native_structure_working_revision import (
    NativeStructureWorkingRevisionError,
    build_accepted_reference_replay_revisions,
    canonical_json_sha256,
    compile_native_structure_working_revision,
    NativeStructureWorkingRevisionV1,
)


def _node(
    node_id: str,
    *,
    parent_id: str | None = None,
    order: int = 0,
    page: int = 1,
    table_scope_id: str = "",
) -> dict[str, object]:
    return {
        "node_id": node_id,
        "parent_id": parent_id,
        "order": order,
        "kind": "DOCUMENT_ROOT" if parent_id is None else "SECTION",
        "label": "",
        "title": node_id,
        "title_authority": "VISIBLE_TITLE",
        "pages": [page],
        "content_blocks": (
            [
                {
                    "kind": "LOGICAL_TABLE",
                    "logical_table_scope_id": table_scope_id,
                }
            ]
            if table_scope_id
            else []
        ),
        "finding_codes": [],
        "source_anchors": [],
        "source_content": [],
    }


def _accepted_reference() -> dict[str, object]:
    nodes = [
        _node("root-a", page=1),
        _node("a-1", parent_id="root-a", order=0, page=1),
        _node(
            "a-2",
            parent_id="root-a",
            order=1,
            page=2,
            table_scope_id="table-a",
        ),
        _node("root-b", order=1, page=3),
        _node("b-1", parent_id="root-b", order=0, page=3),
        _node("root-c", order=2, page=4),
        _node("c-1", parent_id="root-c", order=0, page=4),
    ]
    tree = {
        "schema_version": "native-document-structure-tree/v1",
        "root_ids": ["root-a", "root-b", "root-c"],
        "nodes": nodes,
    }
    return {
        "schema_version": "native-structure-reference-public/v1",
        "artifact_id": "accepted-artifact-1",
        "source_sha256": "1" * 64,
        "document_tree": tree,
        "structure_projection": {
            "schema_version": "native-structure-projection-state/v1",
            "state": "ACCEPTED",
            "base_artifact_id": "accepted-artifact-1",
            "attempt_id": None,
            "attempt_ordinal": None,
            "affected_scope_ids": [],
            "pending_rendered_scope_ids": [],
            "validation_status": "PASS",
            "projection_sha256": canonical_json_sha256(tree),
        },
        "page_ownership": [
            {"unit_id": "unit-a", "title": "A", "pages": [1, 2]},
            {"unit_id": "unit-b", "title": "B", "pages": [3]},
            {"unit_id": "unit-c", "title": "C", "pages": [4]},
        ],
        "logical_tables": [
            {
                "scope_id": "table-a",
                "title": "A table",
                "owner_unit_id": "unit-a",
                "role": "REQUIREMENT_RECORDS",
                "pages": [2],
                "fragments": [
                    {"table_token": "T0001", "page": 2, "row_range": [1, 2]}
                ],
                "source_numbering_anomaly": "",
                "capture_gap": "",
            }
        ],
        "structure_proposals": [
            {
                "proposal_id": "proposal-a-2",
                "node_id": "a-2",
                "recommended_target_node_id": "a-2",
                "page": 2,
            },
            {
                "proposal_id": "proposal-c-1",
                "node_id": "c-1",
                "recommended_target_node_id": "c-1",
                "page": 4,
            },
        ],
        "unresolved": [
            {"code": "unresolved-a", "scope": "P2", "detail": "A", "pages": [2]},
            {"code": "unresolved-c", "scope": "P4", "detail": "C", "pages": [4]},
        ],
        "visual_summary": {
            "opened_page_count": 4,
            "page_count": 4,
            "receipts": [
                {
                    "page": page,
                    "path": f"renders/high/P{page:04d}.png",
                    "sha256": str(page) * 64,
                    "state": "OPENED",
                    "purpose": "test",
                }
                for page in range(1, 5)
            ],
        },
    }


def _table_bindings(reference=None):
    accepted = reference or _accepted_reference()
    return ({
        "scope_id": "table-a", "preview_sha256": "a" * 64,
        "object_key": "tables/a.json", "object_sha256": "a" * 64,
        "object_size": 1,
        "media_type": "application/vnd.reqpilotx.native-structure-table-preview+json",
        "source_sha256": accepted["source_sha256"],
        "base_artifact_id": accepted["artifact_id"],
        "base_projection_sha256": accepted["structure_projection"]["projection_sha256"],
    },)


def _table_previews(reference=None):
    accepted = reference or _accepted_reference()
    return ({
        "schema_version": "native-structure-table-preview/v1",
        "source_sha256": accepted["source_sha256"],
        "artifact_id": accepted["artifact_id"],
        "logical_table_scope_id": "table-a", "title": "A table",
        "fragments": accepted["logical_tables"][0]["fragments"],
    },)


class NativeStructureWorkingRevisionTests(unittest.TestCase):
    def test_forged_derived_identity_receipt_and_first_diff_are_rejected(self) -> None:
        accepted = _accepted_reference()
        revision = build_accepted_reference_replay_revisions(
            accepted, run_id="agent-run-" + "f" * 32,
            evidence_artifact_id="evidence-artifact-1",
            table_preview_bindings=_table_bindings(accepted),
        )[0]
        payload = revision.model_dump(mode="python")
        for update, message in (
            ({"revision_id": "native-structure-working-revision-" + "0" * 32}, "derived identity"),
            ({"validator_receipt_sha256": "0" * 64}, "validator receipt"),
            ({"inserted_node_ids": ()}, "first working revision diff"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                NativeStructureWorkingRevisionV1.model_validate({**payload, **update})

    def test_replay_builds_three_host_verified_revisions_and_exact_final_sha(self) -> None:
        accepted = _accepted_reference()
        revisions = build_accepted_reference_replay_revisions(
            accepted,
            run_id="agent-run-" + "a" * 32,
            evidence_artifact_id="evidence-artifact-1",
            table_preview_bindings=_table_bindings(accepted),
            clock=lambda: datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        )

        self.assertEqual(len(revisions), 3)
        self.assertEqual(
            [len(item.document_tree["nodes"]) for item in revisions],
            [3, 5, 7],
        )
        self.assertEqual([item.revision for item in revisions], [1, 2, 3])
        self.assertEqual(revisions[0].state, "WORKING")
        self.assertEqual(revisions[1].state, "WORKING")
        self.assertEqual(revisions[2].state, "ACCEPTED")
        self.assertEqual(
            revisions[2].revision_sha256,
            revisions[2].canonical_revision_sha256(),
        )
        self.assertEqual(
            canonical_json_sha256(revisions[2].document_tree),
            accepted["structure_projection"]["projection_sha256"],
        )
        self.assertEqual(
            revisions[1].previous_revision_sha256,
            revisions[0].revision_sha256,
        )
        self.assertEqual(
            revisions[2].previous_revision_sha256,
            revisions[1].revision_sha256,
        )
        self.assertEqual(revisions[0].inserted_node_ids, ("root-a", "root-b", "root-c"))
        self.assertEqual(revisions[1].inserted_node_ids, ("a-1", "a-2"))
        self.assertEqual(revisions[2].inserted_node_ids, ("b-1", "c-1"))
        self.assertEqual(revisions[0].logical_tables, ())
        self.assertEqual(revisions[1].logical_tables[0]["scope_id"], "table-a")
        self.assertEqual(
            tuple(revisions[1].node_states.values()),
            ("WORKING_VERIFIED",) * 5,
        )
        self.assertEqual(
            tuple(revisions[2].node_states.values()),
            ("ACCEPTED",) * 7,
        )

    def test_created_at_is_excluded_from_revision_identity(self) -> None:
        accepted = _accepted_reference()
        first = build_accepted_reference_replay_revisions(
            accepted,
            run_id="agent-run-" + "a" * 32,
            evidence_artifact_id="evidence-artifact-1",
            table_preview_bindings=_table_bindings(accepted),
            clock=lambda: datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        )
        second = build_accepted_reference_replay_revisions(
            accepted,
            run_id="agent-run-" + "a" * 32,
            evidence_artifact_id="evidence-artifact-1",
            table_preview_bindings=_table_bindings(accepted),
            clock=lambda: datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
            + timedelta(days=1),
        )

        self.assertEqual(
            [item.revision_sha256 for item in first],
            [item.revision_sha256 for item in second],
        )

    def test_replay_rejects_raw_tree_nodes_not_in_the_accepted_projection(self) -> None:
        accepted = _accepted_reference()
        raw_tree = {
            **accepted["document_tree"],
            "nodes": [
                *accepted["document_tree"]["nodes"],
                _node("raw-only-attachment-1", order=3, page=4),
                _node("raw-only-attachment-2", order=4, page=4),
                _node("raw-only-attachment-3", order=5, page=4),
            ],
            "root_ids": [
                *accepted["document_tree"]["root_ids"],
                "raw-only-attachment-1",
                "raw-only-attachment-2",
                "raw-only-attachment-3",
            ],
        }

        with self.assertRaisesRegex(
            NativeStructureWorkingRevisionError,
            "accepted public projection",
        ):
            compile_native_structure_working_revision(
                run_id="agent-run-" + "b" * 32,
                source_sha256="1" * 64,
                base_artifact_id="accepted-artifact-1",
                base_projection_sha256=accepted["structure_projection"][
                    "projection_sha256"
                ],
                evidence_artifact_id="evidence-artifact-1",
                revision=1,
                previous=None,
                validator_kind="ACCEPTED_REFERENCE_REPLAY",
                state="WORKING",
                candidate_document_tree=raw_tree,
                page_ownership=accepted["page_ownership"],
                logical_tables=accepted["logical_tables"],
                table_preview_bindings=_table_bindings(accepted),
                structure_proposals=accepted["structure_proposals"],
                unresolved=accepted["unresolved"],
                visual_summary=accepted["visual_summary"],
                accepted_public_reference=accepted,
                clock=lambda: datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
            )

    def test_invalid_parent_and_dangling_table_refs_fail_closed(self) -> None:
        accepted = _accepted_reference()
        broken_parent = {
            **accepted["document_tree"],
            "nodes": [
                {**accepted["document_tree"]["nodes"][0]},
                {**accepted["document_tree"]["nodes"][1], "parent_id": "missing"},
            ],
            "root_ids": ["root-a"],
        }
        with self.assertRaisesRegex(NativeStructureWorkingRevisionError, "parent"):
            compile_native_structure_working_revision(
                run_id="agent-run-" + "c" * 32,
                source_sha256="1" * 64,
                base_artifact_id="accepted-artifact-1",
                base_projection_sha256=accepted["structure_projection"][
                    "projection_sha256"
                ],
                evidence_artifact_id="evidence-artifact-1",
                revision=1,
                previous=None,
                validator_kind="ACCEPTED_REFERENCE_REPLAY",
                state="WORKING",
                candidate_document_tree=broken_parent,
                page_ownership=(),
                logical_tables=(),
                table_preview_bindings=(),
                structure_proposals=(),
                unresolved=(),
                visual_summary={
                    "opened_page_count": 0,
                    "page_count": 4,
                    "receipts": [],
                },
                accepted_public_reference=accepted,
                clock=lambda: datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
            )

    def test_d2_host_compiler_is_disabled_without_typed_evidence_receipt(self) -> None:
        accepted = _accepted_reference()
        with self.assertRaisesRegex(
            NativeStructureWorkingRevisionError,
            "D2_HOST_COMPILER_REQUIRES_TYPED_SOURCE_EVIDENCE_RECEIPT",
        ):
            compile_native_structure_working_revision(
                run_id="agent-run-" + "e" * 32,
                source_sha256="1" * 64,
                base_artifact_id="accepted-artifact-1",
                base_projection_sha256=accepted["structure_projection"]["projection_sha256"],
                evidence_artifact_id="evidence-artifact-1",
                revision=1,
                previous=None,
                validator_kind="D2_HOST_COMPILER",
                state="WORKING",
                candidate_document_tree=accepted["document_tree"],
                page_ownership=accepted["page_ownership"],
                logical_tables=accepted["logical_tables"],
                table_preview_bindings=_table_bindings(accepted),
                structure_proposals=accepted["structure_proposals"],
                unresolved=accepted["unresolved"],
                visual_summary=accepted["visual_summary"],
            )

        dangling_table_tree = {
            **accepted["document_tree"],
            "nodes": [
                accepted["document_tree"]["nodes"][0],
                accepted["document_tree"]["nodes"][2],
            ],
            "root_ids": ["root-a"],
        }
        with self.assertRaisesRegex(
            NativeStructureWorkingRevisionError,
            "logical table",
        ):
            compile_native_structure_working_revision(
                run_id="agent-run-" + "d" * 32,
                source_sha256="1" * 64,
                base_artifact_id="accepted-artifact-1",
                base_projection_sha256=accepted["structure_projection"][
                    "projection_sha256"
                ],
                evidence_artifact_id="evidence-artifact-1",
                revision=1,
                previous=None,
                validator_kind="ACCEPTED_REFERENCE_REPLAY",
                state="WORKING",
                candidate_document_tree=dangling_table_tree,
                page_ownership=(),
                logical_tables=(),
                table_preview_bindings=(),
                structure_proposals=(),
                unresolved=(),
                visual_summary={
                    "opened_page_count": 0,
                    "page_count": 4,
                    "receipts": [],
                },
                accepted_public_reference=accepted,
                clock=lambda: datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
