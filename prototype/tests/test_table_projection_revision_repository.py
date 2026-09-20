from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.adapters.persistence.table_projection_revisions import (
    _lock_lineage,
    append_table_projection_revision,
    decode_projection_revision,
    get_max_persisted_projection_revision,
    list_effective_table_projection_revisions,
)
from app.application.extraction.contracts import (
    TableRecordProjectionRowCoordinate,
    TableRecordProjectionRowGroup,
    TableRecordProjectionRevisionRequest,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.errors import ApplicationError
from app.application.extraction.table_evidence_projection import (
    build_table_evidence_bindings,
)
from app.application.extraction.table_projection_overrides import (
    apply_table_projection_overrides,
)
from app.application.extraction.table_record_projection import (
    build_manual_row_group_projection,
    build_table_record_projections,
)
from app.application.extraction.table_row_group_review_index import (
    TABLE_ROW_GROUP_REVIEW_INDEX_KEY,
    build_table_row_group_review_index,
)
from app.application.extraction.table_projection_revisions import (
    get_document_table_record_projection_row_groups,
    revise_document_table_record_projection,
)
from app.application.extraction.revise_structure import (
    get_base_effective_structure,
)
from app.domain.extraction.table_records import TableRecordProjectionMode
from tests.test_table_record_projection_builder import (
    _source_with_tables,
    _table_spec,
)


class TableProjectionRevisionRepositoryTests(unittest.TestCase):
    def test_append_is_optimistic_and_exact_retry_is_idempotent(self) -> None:
        scope = {
            "project_id": "project-table-revision",
            "document_id": "document-table-revision",
            "source_sha256": "a" * 64,
            "structure_snapshot_id": "effective-structure-" + "b" * 32,
            "logical_table_id": "structure-node-" + "c" * 32,
        }
        projection = {
            "projection_id": "table-record-projection-" + "d" * 32,
            "projection_sha256": "e" * 64,
            "projection_revision": 7,
        }
        first, created = append_table_projection_revision(
            **scope,
            expected_revision=0,
            action="SPLIT_ROWS",
            projection=projection,
            evidence_bindings=[{"logical_table_id": scope["logical_table_id"]}],
        )
        self.assertTrue(created)
        self.assertEqual(first["revision"], 1)

        replay, replay_created = append_table_projection_revision(
            **scope,
            expected_revision=0,
            action="SPLIT_ROWS",
            projection=projection,
            evidence_bindings=[{"logical_table_id": scope["logical_table_id"]}],
        )
        self.assertFalse(replay_created)
        self.assertEqual(replay["id"], first["id"])

        with self.assertRaises(ValueError):
            append_table_projection_revision(
                **scope,
                expected_revision=0,
                action="KEEP_WHOLE",
                projection=projection,
                evidence_bindings=[],
            )

        restored, restored_created = append_table_projection_revision(
            **scope,
            expected_revision=1,
            action="RESTORE_AUTO",
            projection=None,
            evidence_bindings=[],
        )
        self.assertTrue(restored_created)
        self.assertEqual(restored["revision"], 2)
        decoded_projection, decoded_evidence = decode_projection_revision(
            restored
        )
        self.assertIsNone(decoded_projection)
        self.assertEqual(decoded_evidence, [])
        self.assertEqual(
            get_max_persisted_projection_revision(**scope),
            7,
        )

        split_after_restore = {
            **projection,
            "projection_id": "table-record-projection-" + "f" * 32,
            "projection_revision": 8,
        }
        next_row, next_created = append_table_projection_revision(
            **scope,
            expected_revision=2,
            action="SPLIT_ROWS",
            projection=split_after_restore,
            evidence_bindings=[{"logical_table_id": scope["logical_table_id"]}],
        )
        self.assertTrue(next_created)
        self.assertEqual(next_row["revision"], 3)
        self.assertEqual(
            get_max_persisted_projection_revision(**scope),
            8,
        )
        heads = list_effective_table_projection_revisions(
            project_id=scope["project_id"],
            document_id=scope["document_id"],
            source_sha256=scope["source_sha256"],
            structure_snapshot_id=scope["structure_snapshot_id"],
        )
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0]["action"], "SPLIT_ROWS")

    def test_restore_auto_rejects_replacement_payload(self) -> None:
        scope = {
            "project_id": "project-table-invalid-restore",
            "document_id": "document-table-invalid-restore",
            "source_sha256": "1" * 64,
            "structure_snapshot_id": "effective-structure-" + "2" * 32,
            "logical_table_id": "structure-node-" + "3" * 32,
        }
        with self.assertRaises(ValueError):
            append_table_projection_revision(
                **scope,
                expected_revision=0,
                action="RESTORE_AUTO",
                projection={"projection_revision": 1},
                evidence_bindings=[],
            )

    def test_postgresql_uses_transaction_advisory_lock(self) -> None:
        class RecordingConnection:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[str, ...] | None]] = []

            def execute(self, sql, parameters=None):
                self.calls.append((str(sql), parameters))

        connection = RecordingConnection()
        with patch(
            "app.adapters.persistence.table_projection_revisions."
            "db.is_postgresql_runtime",
            return_value=True,
        ):
            _lock_lineage(
                connection,
                project_id="project",
                document_id="document",
                source_sha256="a" * 64,
                structure_snapshot_id="effective-structure-" + "b" * 32,
                logical_table_id="structure-node-" + "c" * 32,
            )

        self.assertEqual(len(connection.calls), 1)
        statement, parameters = connection.calls[0]
        self.assertIn("pg_advisory_xact_lock", statement)
        self.assertNotIn("BEGIN IMMEDIATE", statement)
        self.assertEqual(len(parameters or ()), 1)

    def test_directional_merge_contract_requires_record_id(self) -> None:
        with self.assertRaises(ValidationError):
            TableRecordProjectionRevisionRequest(
                action="MERGE_NEXT",
                expected_projection_revision=0,
            )
        request = TableRecordProjectionRevisionRequest(
            action="MERGE_PREVIOUS",
            expected_projection_revision=1,
            record_id="table-record-" + "4" * 32,
        )
        self.assertEqual(request.action, "MERGE_PREVIOUS")

    def test_v2_split_contract_requires_exact_groups_and_bindings(self) -> None:
        with self.assertRaises(ValidationError):
            TableRecordProjectionRevisionRequest(
                schema_version="table-record-projection-revision-request/v2",
                action="SPLIT_ROWS",
                expected_projection_revision=0,
            )
        with self.assertRaises(ValidationError):
            TableRecordProjectionRevisionRequest(
                action="SPLIT_ROWS",
                expected_projection_revision=0,
                row_groups=(
                    TableRecordProjectionRowGroup(
                        rows=(
                            TableRecordProjectionRowCoordinate(
                                table_id="table-1",
                                row_index=1,
                            ),
                        )
                    ),
                ),
            )
        request = TableRecordProjectionRevisionRequest(
            schema_version="table-record-projection-revision-request/v2",
            action="SPLIT_ROWS",
            expected_projection_revision=2,
            expected_source_sha256="a" * 64,
            expected_structure_snapshot_id=(
                "effective-structure-" + "b" * 32
            ),
            row_groups=(
                TableRecordProjectionRowGroup(
                    rows=(
                        TableRecordProjectionRowCoordinate(
                            table_id="table-1",
                            row_index=1,
                        ),
                    )
                ),
            ),
        )
        self.assertEqual(request.row_groups[0].rows[0].row_index, 1)

    def test_override_replaces_evidence_and_restore_recovers_auto(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "要求"),
                        ("1", "第一条"),
                        ("2", "第二条"),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        structure = build_structure_tree(source)
        automatic = build_table_record_projections(source, structure)[0]
        table_node = next(
            node for node in structure.nodes if node.kind.value == "table"
        )
        table_id = automatic.table_fact.table_ids[0]
        table_payload = {"id": table_id, "page_number": 1}
        automatic_evidence = build_table_evidence_bindings(
            table_record_projections=(automatic,),
            tables=(table_payload,),
        )
        manual = build_manual_row_group_projection(
            source,
            structure,
            table_structure_node_id=table_node.structure_node_id,
            row_groups=(),
            projection_revision=automatic.projection_revision + 1,
        )
        manual_evidence = build_table_evidence_bindings(
            table_record_projections=(manual,),
            tables=(table_payload,),
        )
        scope = {
            "project_id": "project-table-override",
            "document_id": "document-table-override",
            "source_sha256": source.source_sha256,
            "structure_snapshot_id": (
                automatic.table_fact.structure_snapshot_id
            ),
            "logical_table_id": automatic.table_fact.logical_table_id,
        }
        append_table_projection_revision(
            **scope,
            expected_revision=0,
            action="KEEP_WHOLE",
            projection=manual.model_dump(mode="json"),
            evidence_bindings=[
                item.model_dump(mode="json") for item in manual_evidence
            ],
        )
        payload = {
            "table_record_projections": [
                automatic.model_dump(mode="json")
            ],
            "table_evidence_bindings": [
                item.model_dump(mode="json")
                for item in automatic_evidence
            ],
            "tables": [table_payload],
        }

        overridden = apply_table_projection_overrides(
            project_id=scope["project_id"],
            document_id=scope["document_id"],
            source_sha256=scope["source_sha256"],
            payload=payload,
        )

        projection = overridden["table_record_projections"][0]
        self.assertEqual(projection["projection_id"], manual.projection_id)
        self.assertEqual(
            projection["mode"],
            TableRecordProjectionMode.WHOLE_TABLE.value,
        )
        self.assertEqual(
            {
                item["projection_id"]
                for item in overridden["table_evidence_bindings"]
            },
            {manual.projection_id},
        )
        self.assertEqual(
            [item["record_id"] for item in overridden["table_evidence_bindings"]],
            [None],
        )

        append_table_projection_revision(
            **scope,
            expected_revision=1,
            action="RESTORE_AUTO",
            projection=None,
            evidence_bindings=[],
        )
        restored = apply_table_projection_overrides(
            project_id=scope["project_id"],
            document_id=scope["document_id"],
            source_sha256=scope["source_sha256"],
            payload=payload,
        )
        self.assertEqual(
            restored["table_record_projections"][0]["projection_id"],
            automatic.projection_id,
        )
        self.assertEqual(
            {
                item["projection_id"]
                for item in restored["table_evidence_bindings"]
            },
            {automatic.projection_id},
        )

    def test_http_original_command_replay_uses_prior_projection_alias(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "要求"),
                        ("1", "第一条"),
                        ("2", "第二条"),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        base_structure = build_structure_tree(source)
        effective_structure = get_base_effective_structure(
            source,
            base_structure,
        )
        automatic = build_table_record_projections(
            source,
            effective_structure,
        )[0]
        table_payload = {
            "id": automatic.table_fact.table_ids[0],
            "page_number": 1,
        }
        automatic_evidence = build_table_evidence_bindings(
            table_record_projections=(automatic,),
            tables=(table_payload,),
        )
        result = {
            "table_record_projections": [
                automatic.model_dump(mode="json")
            ],
            "table_evidence_bindings": [
                item.model_dump(mode="json")
                for item in automatic_evidence
            ],
            "tables": [table_payload],
        }
        project_id = "project-table-http-replay"
        document_id = "document-table-http-replay"
        document = {"source_sha256": source.source_sha256}
        request = TableRecordProjectionRevisionRequest(
            action="SPLIT_ROWS",
            expected_projection_revision=automatic.projection_revision,
        )
        patchers = (
            patch(
                "app.application.extraction.review_support."
                "_require_project_document",
                return_value=document,
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_structure_agent_job_payload",
                return_value=(
                    {},
                    result,
                    {
                        "effective_structure_identity": {
                            "snapshot_id": (
                                effective_structure.effective_snapshot_id
                            )
                        }
                    },
                ),
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_rebuild_structure_revision_context",
                return_value=(source, base_structure, ()),
            ),
            patch(
                "app.application.extraction.document_jobs."
                "_llm_extraction_result_matches_runtime",
                return_value=True,
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        first = revise_document_table_record_projection(
            project_id,
            document_id,
            automatic.projection_id,
            request,
            SimpleNamespace(headers={}, status_code=200),
        )
        replay = revise_document_table_record_projection(
            project_id,
            document_id,
            automatic.projection_id,
            request,
            SimpleNamespace(headers={}, status_code=200),
        )

        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(replay["revision"], first["revision"])
        self.assertEqual(replay["projection"], first["projection"])

        with self.assertRaises(ApplicationError) as raised:
            revise_document_table_record_projection(
                project_id,
                document_id,
                automatic.projection_id,
                TableRecordProjectionRevisionRequest(
                    action="KEEP_WHOLE",
                    expected_projection_revision=(
                        automatic.projection_revision
                    ),
                ),
                SimpleNamespace(headers={}, status_code=200),
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_v2_exact_groups_bind_normal_replay_and_stale_source(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("项目", "要求"),
                        ("A", "第一条"),
                        ("", "第一条补充"),
                        ("B", "第二条"),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        base_structure = build_structure_tree(source)
        effective_structure = get_base_effective_structure(
            source,
            base_structure,
        )
        automatic = build_table_record_projections(
            source,
            effective_structure,
        )[0]
        table_id = automatic.table_fact.table_ids[0]
        table_payload = {"id": table_id, "page_number": 1}
        result = {
            "table_record_projections": [
                automatic.model_dump(mode="json")
            ],
            "table_evidence_bindings": [],
            "tables": [table_payload],
        }
        project_id = "project-table-v2-exact"
        document_id = "document-table-v2-exact"
        document = {"source_sha256": source.source_sha256}
        request = TableRecordProjectionRevisionRequest(
            schema_version="table-record-projection-revision-request/v2",
            action="SPLIT_ROWS",
            expected_projection_revision=automatic.projection_revision,
            expected_source_sha256=source.source_sha256,
            expected_structure_snapshot_id=(
                effective_structure.effective_snapshot_id
            ),
            row_groups=(
                TableRecordProjectionRowGroup(
                    rows=(
                        TableRecordProjectionRowCoordinate(
                            table_id=table_id,
                            row_index=1,
                        ),
                        TableRecordProjectionRowCoordinate(
                            table_id=table_id,
                            row_index=2,
                        ),
                    )
                ),
                TableRecordProjectionRowGroup(
                    rows=(
                        TableRecordProjectionRowCoordinate(
                            table_id=table_id,
                            row_index=3,
                        ),
                    )
                ),
            ),
        )
        patchers = (
            patch(
                "app.application.extraction.review_support."
                "_require_project_document",
                return_value=document,
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_structure_agent_job_payload",
                return_value=(
                    {},
                    result,
                    {
                        "effective_structure_identity": {
                            "snapshot_id": (
                                effective_structure.effective_snapshot_id
                            )
                        }
                    },
                ),
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_rebuild_structure_revision_context",
                return_value=(source, base_structure, ()),
            ),
            patch(
                "app.application.extraction.document_jobs."
                "_llm_extraction_result_matches_runtime",
                return_value=True,
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        descriptor = get_document_table_record_projection_row_groups(
            project_id,
            document_id,
            automatic.projection_id,
            SimpleNamespace(headers={}, status_code=200),
        )
        self.assertEqual(
            descriptor["expected_source_sha256"],
            source.source_sha256,
        )
        self.assertEqual(len(descriptor["eligible_rows"]), 3)
        self.assertEqual(descriptor["current_row_groups"], [])
        self.assertEqual(
            list_effective_table_projection_revisions(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source.source_sha256,
                structure_snapshot_id=(
                    effective_structure.effective_snapshot_id
                ),
            ),
            [],
        )

        first = revise_document_table_record_projection(
            project_id,
            document_id,
            automatic.projection_id,
            request,
            SimpleNamespace(headers={}, status_code=200),
        )
        self.assertEqual(
            [
                len(record["row_bindings"])
                for record in first["projection"]["records"]
            ],
            [2, 1],
        )
        replay = revise_document_table_record_projection(
            project_id,
            document_id,
            automatic.projection_id,
            request,
            SimpleNamespace(headers={}, status_code=200),
        )
        self.assertFalse(replay["created"])
        self.assertEqual(replay["projection"], first["projection"])

        changed_groups = request.model_copy(
            update={
                "row_groups": (
                    TableRecordProjectionRowGroup(
                        rows=(
                            TableRecordProjectionRowCoordinate(
                                table_id=table_id,
                                row_index=1,
                            ),
                        )
                    ),
                    TableRecordProjectionRowGroup(
                        rows=(
                            TableRecordProjectionRowCoordinate(
                                table_id=table_id,
                                row_index=2,
                            ),
                            TableRecordProjectionRowCoordinate(
                                table_id=table_id,
                                row_index=3,
                            ),
                        )
                    ),
                )
            }
        )
        with self.assertRaises(ApplicationError) as changed:
            revise_document_table_record_projection(
                project_id,
                document_id,
                automatic.projection_id,
                changed_groups,
                SimpleNamespace(headers={}, status_code=200),
            )
        self.assertEqual(changed.exception.status_code, 409)

        with self.assertRaises(ApplicationError) as stale:
            revise_document_table_record_projection(
                project_id,
                document_id,
                first["projection"]["projection_id"],
                request.model_copy(
                    update={"expected_source_sha256": "f" * 64}
                ),
                SimpleNamespace(headers={}, status_code=200),
            )
        self.assertEqual(stale.exception.status_code, 409)
        self.assertEqual(
            stale.exception.detail["code"],
            "table_projection_source_binding_mismatch",
        )
        with self.assertRaises(ApplicationError) as stale_structure:
            revise_document_table_record_projection(
                project_id,
                document_id,
                first["projection"]["projection_id"],
                request.model_copy(
                    update={
                        "expected_structure_snapshot_id": (
                            "effective-structure-" + "f" * 32
                        )
                    }
                ),
                SimpleNamespace(headers={}, status_code=200),
            )
        self.assertEqual(stale_structure.exception.status_code, 409)
        self.assertEqual(
            stale_structure.exception.detail["code"],
            "table_projection_structure_binding_mismatch",
        )

    def test_row_group_review_index_fast_path_never_rebuilds_source(
        self,
    ) -> None:
        source, base_structure, effective_structure, automatic = (
            self._row_group_review_fixture()
        )
        result = {
            "table_record_projections": [
                automatic.model_dump(mode="json")
            ],
            TABLE_ROW_GROUP_REVIEW_INDEX_KEY: (
                build_table_row_group_review_index(
                    source,
                    effective_structure,
                    automatic_projections=(automatic,),
                )
            ),
        }
        document = {"source_sha256": source.source_sha256}
        agent = {
            "effective_structure_identity": {
                "snapshot_id": effective_structure.effective_snapshot_id
            }
        }
        with (
            patch(
                "app.application.extraction.review_support."
                "_require_project_document",
                return_value=document,
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_structure_agent_job_payload",
                return_value=({}, result, agent),
            ),
            patch(
                "app.application.extraction.document_jobs."
                "_llm_extraction_result_matches_runtime",
                return_value=True,
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_rebuild_structure_revision_context",
                side_effect=AssertionError("durable descriptor rebuilt PDF"),
            ) as rebuild,
        ):
            descriptor = get_document_table_record_projection_row_groups(
                "project-fast-row-groups",
                "document-fast-row-groups",
                automatic.projection_id,
                SimpleNamespace(headers={}, status_code=200),
            )

        rebuild.assert_not_called()
        self.assertTrue(descriptor["editable"])
        self.assertEqual(len(descriptor["eligible_rows"]), 3)
        self.assertEqual(
            descriptor["expected_source_sha256"],
            source.source_sha256,
        )
        self.assertEqual(
            descriptor["expected_structure_snapshot_id"],
            effective_structure.effective_snapshot_id,
        )

    def test_row_group_review_index_tamper_and_legacy_fall_back_to_rebuild(
        self,
    ) -> None:
        source, base_structure, effective_structure, automatic = (
            self._row_group_review_fixture()
        )
        valid_index = build_table_row_group_review_index(
            source,
            effective_structure,
            automatic_projections=(automatic,),
        )
        variants = ({}, copy.deepcopy(valid_index))
        variants[1]["entries"][0]["eligible_rows"][0]["display_text"] = (
            "tampered"
        )
        document = {"source_sha256": source.source_sha256}
        for variant in variants:
            with self.subTest(sidecar=bool(variant)):
                result = {
                    "table_record_projections": [
                        automatic.model_dump(mode="json")
                    ],
                    TABLE_ROW_GROUP_REVIEW_INDEX_KEY: variant,
                }
                with (
                    patch(
                        "app.application.extraction.review_support."
                        "_require_project_document",
                        return_value=document,
                    ),
                    patch(
                        "app.application.extraction.structure_workspace."
                        "_structure_agent_job_payload",
                        return_value=(
                            {},
                            result,
                            {
                                "effective_structure_identity": {
                                    "snapshot_id": (
                                        effective_structure.effective_snapshot_id
                                    )
                                }
                            },
                        ),
                    ),
                    patch(
                        "app.application.extraction.document_jobs."
                        "_llm_extraction_result_matches_runtime",
                        return_value=True,
                    ),
                    patch(
                        "app.application.extraction.structure_workspace."
                        "_rebuild_structure_revision_context",
                        return_value=(source, base_structure, ()),
                    ) as rebuild,
                ):
                    descriptor = (
                        get_document_table_record_projection_row_groups(
                            "project-fallback-row-groups",
                            "document-fallback-row-groups",
                            automatic.projection_id,
                            SimpleNamespace(headers={}, status_code=200),
                        )
                    )
                rebuild.assert_called_once()
                self.assertEqual(len(descriptor["eligible_rows"]), 3)
                self.assertNotEqual(
                    descriptor["eligible_rows"][0]["display_text"],
                    "tampered",
                )

    def test_row_group_review_index_identity_mismatch_falls_back(
        self,
    ) -> None:
        source, base_structure, effective_structure, automatic = (
            self._row_group_review_fixture()
        )
        index = build_table_row_group_review_index(
            source,
            effective_structure,
            automatic_projections=(automatic,),
        )
        # This stale identity is deliberately not re-signed.  The important
        # behavior is fail-closed fallback even for a self-consistent artifact
        # whose identity no longer matches the automatic table fact.
        index["structure_snapshot_id"] = (
            "effective-structure-" + "f" * 32
        )
        unsigned = {
            key: value
            for key, value in index.items()
            if key != "canonical_sha256"
        }
        index["canonical_sha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        result = {
            "table_record_projections": [
                automatic.model_dump(mode="json")
            ],
            TABLE_ROW_GROUP_REVIEW_INDEX_KEY: index,
        }
        with (
            patch(
                "app.application.extraction.review_support."
                "_require_project_document",
                return_value={"source_sha256": source.source_sha256},
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_structure_agent_job_payload",
                return_value=(
                    {},
                    result,
                    {
                        "effective_structure_identity": {
                            "snapshot_id": (
                                effective_structure.effective_snapshot_id
                            )
                        }
                    },
                ),
            ),
            patch(
                "app.application.extraction.document_jobs."
                "_llm_extraction_result_matches_runtime",
                return_value=True,
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_rebuild_structure_revision_context",
                return_value=(source, base_structure, ()),
            ) as rebuild,
        ):
            get_document_table_record_projection_row_groups(
                "project-bound-row-groups",
                "document-bound-row-groups",
                automatic.projection_id,
                SimpleNamespace(headers={}, status_code=200),
            )
        rebuild.assert_called_once()

    @staticmethod
    def _row_group_review_fixture():
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("项目", "要求"),
                        ("A", "第一条"),
                        ("", "第一条补充"),
                        ("B", "第二条"),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        base_structure = build_structure_tree(source)
        effective_structure = get_base_effective_structure(
            source,
            base_structure,
        )
        automatic = build_table_record_projections(
            source,
            effective_structure,
        )[0]
        return source, base_structure, effective_structure, automatic


if __name__ == "__main__":
    unittest.main()
