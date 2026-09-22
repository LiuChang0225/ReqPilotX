from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import ast
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import db
from app.application.errors import ApplicationError
from app.application.extraction import document_jobs
from app.application.extraction import entry_classification as service
from app.application.extraction.entry_classification import (
    EntryClassificationAcceptanceRequest,
    EntryClassificationPatchRequest,
    _read_current_requirement_tree,
    accept_document_entry_classifications,
    get_document_entry_classifications,
    patch_document_entry_classifications,
)
from app.application.extraction.entry_classification_repository import (
    EntryClassificationSnapshotCorrupted,
    count_entry_classification_snapshots,
)
from app.domain.entry_classification import (
    build_initial_entry_classification_snapshot,
)
from app.domain.extraction.requirement_tree import (
    RequirementTreeMode,
    RequirementTreeNodeV1,
    RequirementTreeSnapshotV1,
    RequirementTreeSourceIdentity,
    RequirementTreeState,
    RequirementTreeStructureIdentity,
)


class EntryClassificationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db.init_db()

    def setUp(self) -> None:
        with document_jobs._LLM_EXTRACTION_JOB_LOCK:
            document_jobs._LLM_EXTRACTION_JOBS.clear()
        self.project = db.create_project("Classification API", "Customer")
        self.document = db.add_sor_document(
            self.project["id"],
            "source.pdf",
            "pdf",
            "uploads/source.pdf",
            "customer source body must not enter command hashes",
            source_sha256="a" * 64,
            source_size=1,
            declared_extension="pdf",
            detected_format="pdf",
        )
        self.tree = _tree(
            project_id=str(self.project["id"]),
            document_id=str(self.document["id"]),
        )
        self.response = SimpleNamespace(headers={}, status_code=200)

    def tearDown(self) -> None:
        with document_jobs._LLM_EXTRACTION_JOB_LOCK:
            document_jobs._LLM_EXTRACTION_JOBS.clear()

    def test_get_uses_only_the_existing_read_path_and_does_not_persist(self) -> None:
        workbench_response = {
            "structure_workbench": {
                "requirement_tree": self.tree.model_dump(mode="json")
            }
        }
        with patch(
            "app.application.extraction.entry_classification."
            "structure_workspace.get_document_structure_agent",
            return_value=workbench_response,
        ) as read_tree:
            result = get_document_entry_classifications(
                self.project["id"],
                self.document["id"],
                self.response,
            )

        read_tree.assert_called_once()
        self.assertIsNone(read_tree.call_args.kwargs.get("request"))
        self.assertEqual(result["snapshot"]["revision"], 0)
        self.assertEqual(result["counts"]["unclassified"], 2)
        self.assertFalse(result["can_accept"])
        self.assertFalse(result["can_publish"])
        self.assertEqual(result["serving_impact"], "NONE")
        self.assertEqual(
            count_entry_classification_snapshots(
                build_initial_entry_classification_snapshot(self.tree)
            ),
            0,
        )
        self.assertNotIn("customer requirement", json.dumps(result))
        self.assertEqual(self.response.headers["Cache-Control"], "private, no-store")

    def test_patch_single_batch_clear_noop_and_command_replay(self) -> None:
        with patch.object(
            service,
            "_read_current_requirement_tree",
            return_value=self.tree,
        ):
            initial = get_document_entry_classifications(
                self.project["id"], self.document["id"], self.response
            )
            first_request = _patch_request(
                initial,
                command_id="patch-single",
                overrides={"workflow-1": "TECHNICAL"},
            )
            first = patch_document_entry_classifications(
                self.project["id"],
                self.document["id"],
                first_request,
                self.response,
            )
            replay = patch_document_entry_classifications(
                self.project["id"],
                self.document["id"],
                first_request,
                self.response,
            )
            with self.assertRaises(ApplicationError) as command_conflict:
                patch_document_entry_classifications(
                    self.project["id"],
                    self.document["id"],
                    _patch_request(
                        initial,
                        command_id="patch-single",
                        overrides={"workflow-1": "INFORMATION"},
                    ),
                    self.response,
                )
            second = patch_document_entry_classifications(
                self.project["id"],
                self.document["id"],
                _patch_request(
                    first,
                    command_id="patch-batch",
                    overrides={
                        "workflow-1": "TECHNICAL",
                        "workflow-2": "INFORMATION",
                    },
                ),
                self.response,
            )
            cleared = patch_document_entry_classifications(
                self.project["id"],
                self.document["id"],
                _patch_request(
                    second,
                    command_id="patch-clear",
                    overrides={"workflow-1": None},
                ),
                self.response,
            )
            with self.assertRaises(ApplicationError) as superseded_replay:
                patch_document_entry_classifications(
                    self.project["id"],
                    self.document["id"],
                    first_request,
                    self.response,
                )
            noop_request = _patch_request(
                cleared,
                command_id="patch-noop",
                overrides={"workflow-2": "INFORMATION"},
            )
            noop = patch_document_entry_classifications(
                self.project["id"],
                self.document["id"],
                noop_request,
                self.response,
            )
            noop_replay = patch_document_entry_classifications(
                self.project["id"],
                self.document["id"],
                noop_request,
                self.response,
            )
            with self.assertRaises(ApplicationError) as noop_command_conflict:
                patch_document_entry_classifications(
                    self.project["id"],
                    self.document["id"],
                    _patch_request(
                        cleared,
                        command_id="patch-noop",
                        overrides={"workflow-2": "NON_TECHNICAL"},
                    ),
                    self.response,
                )
            after_noop = patch_document_entry_classifications(
                self.project["id"],
                self.document["id"],
                _patch_request(
                    cleared,
                    command_id="patch-after-noop",
                    overrides={"workflow-2": "NON_TECHNICAL"},
                ),
                self.response,
            )
            with self.assertRaises(ApplicationError) as old_noop_replay:
                patch_document_entry_classifications(
                    self.project["id"],
                    self.document["id"],
                    noop_request,
                    self.response,
                )

        self.assertEqual(first, replay)
        self.assertEqual(command_conflict.exception.status_code, 409)
        self.assertEqual(superseded_replay.exception.status_code, 409)
        self.assertEqual(second["snapshot"]["revision"], 2)
        self.assertEqual(cleared["snapshot"]["revision"], 3)
        self.assertEqual(cleared["counts"]["unclassified"], 1)
        self.assertEqual(noop["snapshot"]["revision"], 3)
        self.assertEqual(noop_replay, noop)
        self.assertEqual(noop_command_conflict.exception.status_code, 409)
        self.assertEqual(after_noop["snapshot"]["revision"], 4)
        self.assertEqual(old_noop_replay.exception.status_code, 409)
        self.assertEqual(
            count_entry_classification_snapshots(
                build_initial_entry_classification_snapshot(self.tree)
            ),
            4,
        )
        with db.get_conn() as conn:
            receipt_count = conn.execute(
                """
                SELECT COUNT(*) AS receipt_count
                FROM entry_classification_command_receipts
                WHERE project_id = ? AND sor_document_id = ?
                """,
                (self.project["id"], self.document["id"]),
            ).fetchone()
        self.assertEqual(int(receipt_count["receipt_count"]), 1)

    def test_accept_blocks_missing_then_accepts_and_replays_without_side_effects(self) -> None:
        ledger = {"tree_read": 0}

        def read_tree(_project_id: str, _document_id: str):
            ledger["tree_read"] += 1
            return self.tree

        blocked_calls: list[tuple[str, str]] = []
        blocked_functions = {
            (
                "app.application.extraction.document_jobs",
                "start_document_extraction",
            ),
            (
                "app.application.extraction.document_jobs",
                "start_llm_document_extraction",
            ),
            (
                "app.application.extraction.structure_optimization_jobs",
                "start_document_structure_optimization",
            ),
            (
                "app.application.extraction.enrichment_workspace",
                "create_requirement_atomization_run",
            ),
            (
                "app.application.assessment",
                "start_document_ai_analysis",
            ),
        }

        def request_ledger(frame, event, _arg):
            if event == "call":
                identity = (
                    str(frame.f_globals.get("__name__") or ""),
                    str(frame.f_code.co_name),
                )
                if identity in blocked_functions:
                    blocked_calls.append(identity)

        previous_profile = sys.getprofile()
        sys.setprofile(request_ledger)
        try:
            with patch.object(
                service,
                "_read_current_requirement_tree",
                side_effect=read_tree,
            ):
                initial = get_document_entry_classifications(
                    self.project["id"], self.document["id"], self.response
                )
                with self.assertRaises(ApplicationError) as missing:
                    accept_document_entry_classifications(
                        self.project["id"],
                        self.document["id"],
                        _accept_request(initial, command_id="accept-missing"),
                        self.response,
                    )
                working = patch_document_entry_classifications(
                    self.project["id"],
                    self.document["id"],
                    _patch_request(
                        initial,
                        command_id="patch-all",
                        overrides={
                            "workflow-1": "TECHNICAL",
                            "workflow-2": "NON_TECHNICAL",
                        },
                    ),
                    self.response,
                )
                accept_request = _accept_request(
                    working,
                    command_id="accept-all",
                )
                accepted = accept_document_entry_classifications(
                    self.project["id"],
                    self.document["id"],
                    accept_request,
                    self.response,
                )
                replay = accept_document_entry_classifications(
                    self.project["id"],
                    self.document["id"],
                    accept_request,
                    self.response,
                )
        finally:
            sys.setprofile(previous_profile)

        self.assertEqual(missing.exception.status_code, 422)
        self.assertEqual(accepted, replay)
        self.assertEqual(accepted["snapshot"]["status"], "ACCEPTED")
        self.assertEqual(accepted["snapshot"]["accepted_by"], "LOCAL_USER")
        self.assertFalse(accepted["can_accept"])
        self.assertTrue(accepted["can_publish"])
        self.assertGreaterEqual(ledger["tree_read"], 1)
        self.assertEqual(blocked_calls, [])
        service_syntax = ast.parse(
            Path(service.__file__).read_text(encoding="utf-8")
        )
        called_names = {
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else node.func.id
            for node in ast.walk(service_syntax)
            if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Attribute, ast.Name))
        }
        self.assertTrue(
            {
                "start_document_extraction",
                "start_llm_document_extraction",
                "start_document_structure_optimization",
                "create_requirement_atomization_run",
                "start_document_ai_analysis",
            }.isdisjoint(called_names)
        )

    def test_cached_ready_tree_is_read_only_for_patch_and_accept_after_terminal_retry(
        self,
    ) -> None:
        cached_ready = {
            "job": {
                "job_id": "llm-extraction-serving-cache",
                "status": "COMPLETED",
                "stage": "cached_previous_result",
            },
            "structure_agent": {
                "editable": False,
                "read_only_reason": "structure_derivation_rerun_not_complete",
            },
            "structure_workbench": {
                "agent": {"status": "COMPLETED", "editable": False},
                "structure": {"state": "STABLE", "editable": False},
                "requirement_tree": self.tree.model_dump(mode="json"),
            },
        }
        initial = {
            "snapshot": build_initial_entry_classification_snapshot(
                self.tree
            ).model_dump(mode="json")
        }

        for status in ("FAILED", "INTERRUPTED", "CANCELLED"):
            with self.subTest(status=status):
                with document_jobs._LLM_EXTRACTION_JOB_LOCK:
                    document_jobs._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                        "job_id": f"llm-extraction-latest-{status.lower()}",
                        "project_id": self.project["id"],
                        "document_id": self.document["id"],
                        "source_sha256": self.document["source_sha256"],
                        "status": status,
                        "stage": status.lower(),
                        "result": None,
                        "partial_result": None,
                    }
                with patch(
                    "app.application.extraction.entry_classification."
                    "structure_workspace.get_document_structure_agent",
                    return_value=cached_ready,
                ):
                    browsed = get_document_entry_classifications(
                        self.project["id"],
                        self.document["id"],
                        self.response,
                    )
                    with self.assertRaises(ApplicationError) as patched:
                        patch_document_entry_classifications(
                            self.project["id"],
                            self.document["id"],
                            _patch_request(
                                initial,
                                command_id=f"blocked-patch-{status.lower()}",
                                overrides={"workflow-1": "TECHNICAL"},
                            ),
                            self.response,
                        )
                    with self.assertRaises(ApplicationError) as accepted:
                        accept_document_entry_classifications(
                            self.project["id"],
                            self.document["id"],
                            _accept_request(
                                initial,
                                command_id=f"blocked-accept-{status.lower()}",
                            ),
                            self.response,
                        )

                self.assertEqual(browsed["snapshot"]["revision"], 0)
                for caught in (patched.exception, accepted.exception):
                    self.assertEqual(caught.status_code, 409)
                    self.assertEqual(
                        caught.detail["code"],
                        "structure_result_read_only",
                    )
                    self.assertEqual(
                        caught.detail["read_only_reason"],
                        "structure_derivation_rerun_not_complete",
                    )
                self.assertEqual(
                    count_entry_classification_snapshots(
                        build_initial_entry_classification_snapshot(self.tree)
                    ),
                    0,
                )

    def test_stale_corrupt_and_post_write_tree_race_are_409(self) -> None:
        with patch.object(
            service,
            "_read_current_requirement_tree",
            return_value=self.tree,
        ):
            initial = get_document_entry_classifications(
                self.project["id"], self.document["id"], self.response
            )
            stale = _patch_request(
                initial,
                command_id="patch-stale",
                overrides={"workflow-1": "TECHNICAL"},
            ).model_copy(update={"expected_snapshot_sha256": "f" * 64})
            with self.assertRaises(ApplicationError) as stale_error:
                patch_document_entry_classifications(
                    self.project["id"],
                    self.document["id"],
                    stale,
                    self.response,
                )
        self.assertEqual(stale_error.exception.status_code, 409)

        with (
            patch.object(
                service,
                "_read_current_requirement_tree",
                return_value=self.tree,
            ),
            patch.object(
                service,
                "get_entry_classification_snapshot_or_initial",
                side_effect=EntryClassificationSnapshotCorrupted("corrupt"),
            ),
        ):
            with self.assertRaises(ApplicationError) as corrupt_error:
                get_document_entry_classifications(
                    self.project["id"], self.document["id"], self.response
                )
        self.assertEqual(corrupt_error.exception.status_code, 409)

        changed_tree = _tree(
            project_id=str(self.project["id"]),
            document_id=str(self.document["id"]),
            effective_structure_id="effective-2",
        )
        with patch.object(
            service,
            "_read_current_requirement_tree",
            side_effect=[self.tree, changed_tree],
        ):
            with self.assertRaises(ApplicationError) as race_error:
                patch_document_entry_classifications(
                    self.project["id"],
                    self.document["id"],
                    _patch_request(
                        initial,
                        command_id="patch-race",
                        overrides={"workflow-1": "TECHNICAL"},
                    ),
                    self.response,
                )
        self.assertEqual(race_error.exception.status_code, 409)
        self.assertEqual(
            count_entry_classification_snapshots(
                build_initial_entry_classification_snapshot(self.tree)
            ),
            1,
        )

    def test_reader_rejects_non_working_or_non_ready_tree_and_router_is_registered(self) -> None:
        invalid = self.tree.model_copy(
            update={"mode": RequirementTreeMode.REQUIREMENT_SET}
        )
        with patch(
            "app.application.extraction.entry_classification."
            "structure_workspace.get_document_structure_agent",
            return_value={
                "structure_workbench": {
                    "requirement_tree": invalid.model_dump(mode="json")
                }
            },
        ):
            with self.assertRaises(ApplicationError) as caught:
                _read_current_requirement_tree(
                    self.project["id"], self.document["id"]
                )
        self.assertEqual(caught.exception.status_code, 409)

        router_source = (
            Path(__file__).resolve().parents[2]
            / "apps/server/src/reqpilotx_server"
            / "api"
            / "routers"
            / "extraction.py"
        ).read_text(encoding="utf-8")
        self.assertIn('f"{ROOT}/entry-classifications"', router_source)
        self.assertIn('f"{ROOT}/entry-classifications/acceptance"', router_source)
        self.assertIn("patch_document_entry_classifications", router_source)
        self.assertIn("accept_document_entry_classifications", router_source)

    def test_fastapi_get_patch_put_validation_and_document_404(self) -> None:
        app = FastAPI()
        root = (
            "/api/projects/{project_id}/sor-documents/{document_id}"
            "/extraction"
        )

        @app.exception_handler(ApplicationError)
        async def application_error_handler(_request, exc: ApplicationError):
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )

        @app.get(f"{root}/entry-classifications")
        def get_route(project_id: str, document_id: str, response: Response):
            return get_document_entry_classifications(
                project_id,
                document_id,
                response,
            )

        @app.patch(f"{root}/entry-classifications")
        def patch_route(
            project_id: str,
            document_id: str,
            payload: EntryClassificationPatchRequest,
            response: Response,
        ):
            return patch_document_entry_classifications(
                project_id,
                document_id,
                payload,
                response,
            )

        @app.put(f"{root}/entry-classifications/acceptance")
        def accept_route(
            project_id: str,
            document_id: str,
            payload: EntryClassificationAcceptanceRequest,
            response: Response,
        ):
            return accept_document_entry_classifications(
                project_id,
                document_id,
                payload,
                response,
            )

        client = TestClient(app)
        url = (
            f"/api/projects/{self.project['id']}/sor-documents/"
            f"{self.document['id']}/extraction/entry-classifications"
        )
        with patch.object(
            service,
            "_read_current_requirement_tree",
            return_value=self.tree,
        ):
            initial_response = client.get(url)
            initial = initial_response.json()
            patch_payload = _patch_request(
                initial,
                command_id="http-patch",
                overrides={
                    "workflow-1": "TECHNICAL",
                    "workflow-2": "INFORMATION",
                },
            ).model_dump(mode="json")
            patch_response = client.patch(url, json=patch_payload)
            working = patch_response.json()
            accept_response = client.put(
                f"{url}/acceptance",
                json=_accept_request(
                    working,
                    command_id="http-accept",
                ).model_dump(mode="json"),
            )
            invalid_command = dict(patch_payload)
            invalid_command["command_id"] = "unsafe command text"
            invalid_response = client.patch(url, json=invalid_command)
            extra_payload = {**patch_payload, "unexpected": "field"}
            extra_response = client.patch(url, json=extra_payload)

        self.assertEqual(initial_response.status_code, 200)
        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(accept_response.status_code, 200)
        self.assertEqual(
            accept_response.json()["snapshot"]["status"],
            "ACCEPTED",
        )
        self.assertEqual(invalid_response.status_code, 422)
        self.assertEqual(extra_response.status_code, 422)

        with patch.object(
            service,
            "_read_current_requirement_tree",
            side_effect=ApplicationError(
                status_code=404,
                detail={"code": "document_not_found", "message": "missing"},
            ),
        ):
            missing_response = client.get(url)
        self.assertEqual(missing_response.status_code, 404)


def _tree(
    *,
    project_id: str,
    document_id: str,
    effective_structure_id: str = "effective-1",
) -> RequirementTreeSnapshotV1:
    return RequirementTreeSnapshotV1(
        source_identity=RequirementTreeSourceIdentity(
            project_id=project_id,
            document_id=document_id,
            source_ir_id="source-ir-1",
            source_sha256="a" * 64,
        ),
        structure_identity=RequirementTreeStructureIdentity(
            base_snapshot_id="base-1",
            effective_snapshot_id=effective_structure_id,
            effective_tree_sha256="b" * 64,
            revision=1,
        ),
        mode=RequirementTreeMode.WORKING,
        state=RequirementTreeState.READY,
        root_node_ids=("entry-1", "entry-2"),
        nodes=(
            RequirementTreeNodeV1(
                id="entry-1",
                kind="CLAUSE",
                depth=0,
                source_order=0,
                label="customer label one",
                summary="customer requirement one",
                workflow_node_id="workflow-1",
                projection_role="REQUIREMENT",
            ),
            RequirementTreeNodeV1(
                id="entry-2",
                kind="CLAUSE",
                depth=0,
                source_order=1,
                label="customer label two",
                summary="customer requirement two",
                workflow_node_id="workflow-2",
                projection_role="REQUIREMENT",
            ),
        ),
    )


def _patch_request(result, *, command_id: str, overrides: dict[str, str | None]):
    snapshot = result["snapshot"]
    return EntryClassificationPatchRequest(
        command_id=command_id,
        expected_revision=snapshot["revision"],
        expected_snapshot_sha256=snapshot["payload_sha256"],
        expected_source_sha256=snapshot["source_sha256"],
        expected_working_tree_sha256=snapshot["working_tree_sha256"],
        expected_effective_structure_id=snapshot["effective_structure_id"],
        expected_policy_version=snapshot["policy_version"],
        overrides=overrides,
    )


def _accept_request(result, *, command_id: str):
    snapshot = result["snapshot"]
    return EntryClassificationAcceptanceRequest(
        command_id=command_id,
        expected_revision=snapshot["revision"],
        expected_snapshot_sha256=snapshot["payload_sha256"],
        expected_source_sha256=snapshot["source_sha256"],
        expected_working_tree_sha256=snapshot["working_tree_sha256"],
        expected_effective_structure_id=snapshot["effective_structure_id"],
        expected_policy_version=snapshot["policy_version"],
    )


if __name__ == "__main__":
    unittest.main()
