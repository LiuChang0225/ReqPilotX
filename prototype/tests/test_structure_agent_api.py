from __future__ import annotations

# ruff: noqa: E402

import copy

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import tempfile
import time
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Response
from pydantic import ValidationError

from app import db
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application import runtime as application_runtime
from app.application import processing_profiles
from app.application.errors import ApplicationError as HTTPException

if __package__:
    from .application_under_test import main
else:
    from application_under_test import main
from app.main import app as http_app

from app.application.extraction.build_llm_shadow_preview import (
    _serialize_structure_agent,
)
from app.application.extraction.orchestrate_structure_agent import (
    initialize_structure_repair_without_provider,
)
from app.application.extraction import document_jobs, structure_optimization_jobs
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.structure_presentation import (
    build_structure_presentation_index,
)
from app.application.extraction.revise_structure import (
    create_structure_patch_proposal,
    validate_structure_revision_chain,
)
from app.domain.extraction.processing_profile import (
    ResolvedProviderBindingsV1,
    build_local_rules_profile,
    build_resolved_processing_plan,
)
from app.domain.extraction.source_ir import SourceIRStatus
from app.domain.extraction.structure_revision import (
    ReparentStructureOperationV1,
    StructureRevisionV1,
    canonical_model_sha256,
)
from app.infrastructure.runtime_settings import DatabaseBackend
from app.ports.job_queue import JobStatus
from app.ports.structure_optimizer_provider import (
    StructureOptimizationConfidence,
    StructureOptimizationIssueCategory,
    StructureOptimizationIssueOrigin,
    StructureOptimizationIssueV1,
)
from tests.test_structure_optimizer import _FakeOptimizerProvider
from tests.test_structure_revision import (
    _source_ir_fixture,
    _structure_node_for_source,
)

_REAL_REBUILD_STRUCTURE_REVISION_CONTEXT = main._rebuild_structure_revision_context


main.app = http_app


class StructureAgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        main._clear_structure_revision_context_cache()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        self.exit_stack.enter_context(patch.object(main, "STORAGE_DIR", self.root))
        self.exit_stack.enter_context(
            patch.object(processing_profiles, "require_capabilities")
        )
        self.source_ir = _source_ir_fixture()
        self.base_tree = build_structure_tree(self.source_ir)
        self.exit_stack.enter_context(
            patch.object(
                main,
                "PDF_SOURCE_IR_ADAPTER_VERSION",
                self.source_ir.adapter_version,
            )
        )
        self.exit_stack.enter_context(
            patch.object(
                main,
                "DEFAULT_STRUCTURE_RESOLVER_VERSION",
                self.base_tree.resolver_version,
            )
        )
        self.exit_stack.enter_context(
            patch.object(
                main,
                "_llm_extraction_result_matches_runtime",
                return_value=True,
            )
        )
        self.rebuild_context_mock = self.exit_stack.enter_context(
            patch.object(
                main,
                "_rebuild_structure_revision_context",
                side_effect=self._rebuild_context,
            )
        )
        self.provider_builder_mock = self.exit_stack.enter_context(
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                side_effect=HTTPException(
                    status_code=503,
                    detail={
                        "code": "llm_extraction_provider_not_configured",
                        "message": "Synthetic provider is intentionally unavailable.",
                    },
                ),
            )
        )
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()
        with main._LOCAL_OPTIMIZATION_LOCK:
            main._LOCAL_OPTIMIZATION_JOBS.clear()
        db.init_db()
        source = self.root / "source.pdf"
        source.write_bytes(b"synthetic-structure-agent-api")
        self.project = db.create_project(
            name="Structure Agent API",
            customer_name="Customer",
        )
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name=source.name,
            file_type="pdf",
            file_path=str(source),
            extracted_text="",
            source_sha256=self.source_ir.source_sha256,
            source_size=source.stat().st_size,
            declared_extension=".pdf",
            detected_format="pdf",
            preflight_schema_version="document-preflight/v1",
        )
        self.nodes_by_text = {
            source_node.raw_text: _structure_node_for_source(
                self.base_tree.nodes,
                source_node.source_node_id,
            )
            for source_node in self.source_ir.nodes
        }
        self.sections_by_label = {
            node.explicit_label: node
            for node in self.base_tree.nodes
            if node.explicit_label is not None
        }
        self.base_repair = initialize_structure_repair_without_provider(
            self.source_ir,
            self.base_tree,
            error_code="synthetic_audit_not_run",
        )
        self.agent = _serialize_structure_agent(
            self.base_repair,
            editable=True,
            final=True,
        )
        self.result = {
            "schema_version": "extraction-llm-shadow-preview/v5",
            "source_sha256": self.source_ir.source_sha256,
            "status": "READY",
            "serving_impact": "NONE",
            "items": [
                {
                    "id": "requirement-affected",
                    "target_structure": {
                        "id": self.nodes_by_text[
                            "Alpha one. Alpha two."
                        ].structure_node_id
                    },
                },
                {
                    "id": "requirement-reusable",
                    "target_structure": {
                        "id": self.nodes_by_text["Other content."].structure_node_id
                    },
                },
            ],
            "requirement_groups": [],
            "review_groups": [],
            "pipeline": {
                "semantic": {"status": "COMPLETE"},
                "requirement_view": {"status": "SHADOW_READY"},
            },
            "llm_review": {},
            "structure_agent": self.agent,
            "structure_presentation_index": (
                build_structure_presentation_index(
                    self.source_ir,
                    structure=self.base_repair.effective_structure,
                )
            ),
        }
        profile = build_local_rules_profile(project_id=self.project["id"])
        self.processing_plan = build_resolved_processing_plan(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.source_ir.source_sha256,
            profile=profile,
            document=profile.document,
            llm_provider="aliyun",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                llm_transport_provider="aliyun",
                structure_overview_deployment="test-model",
                structure_repair_deployment="test-model",
                semantic_deployment="test-model",
                atomization_deployment="test-model",
                refinement_deployment="test-model",
            ),
            connection_revision=0,
        )
        self.result["processing_plan"] = self.processing_plan.model_dump(mode="json")
        self._seed_completed(self.result)

    def tearDown(self) -> None:
        main._clear_structure_revision_context_cache()
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()
        with main._LOCAL_OPTIMIZATION_LOCK:
            main._LOCAL_OPTIMIZATION_JOBS.clear()
        self.exit_stack.close()
        self.temporary.cleanup()

    def test_final_patch_marks_affected_stale_and_preserves_reusable_scope(
        self,
    ) -> None:
        response = main.apply_document_structure_patch(
            self.project["id"],
            self.document["id"],
            self._patch_request(),
            Response(),
        )

        self.assertTrue(response["accepted"])
        agent = response["structure_agent"]
        workbench = response["structure_workbench"]
        self._assert_workbench_identity(workbench, revision=1)
        self.assertEqual(workbench["structure"]["state"], "STABLE")
        self.assertTrue(workbench["capabilities"]["can_edit_structure"])
        moved = next(
            item
            for item in workbench["structure"]["nodes"]
            if item["id"]
            == self.nodes_by_text["Alpha one. Alpha two."].structure_node_id
        )
        self.assertEqual(
            moved["parent_id"],
            self.sections_by_label["1.2"].structure_node_id,
        )
        self.assertEqual(workbench["revisions"][-1]["actor_type"], "HUMAN")
        self.assertEqual(workbench["revisions"][-1]["operation"], "RE_PARENT")
        self.assertEqual(
            agent["effective_structure_identity"]["revision_number"],
            1,
        )
        self.assertEqual(
            agent["stale_scope"]["structure_node_ids"],
            [self.nodes_by_text["Alpha one. Alpha two."].structure_node_id],
        )
        self.assertEqual(
            agent["stale_scope"]["reusable_structure_node_ids"],
            [self.nodes_by_text["Other content."].structure_node_id],
        )
        stored = main._LLM_EXTRACTION_JOBS[self.document["id"]]["result"]
        statuses = {
            item["id"]: item["structure_derivation"]["status"]
            for item in stored["items"]
        }
        self.assertEqual(statuses["requirement-affected"], "STALE")
        self.assertEqual(statuses["requirement-reusable"], "REUSED")
        self.assertEqual(agent["rerun"]["mode"], "FULL_DOCUMENT_RECOMPUTE")
        self.assertEqual(
            agent["rerun"]["invalidation_scope"],
            "AFFECTED_STRUCTURE_SCOPE",
        )
        self.assertEqual(agent["rerun"]["execution_scope"], "FULL_DOCUMENT")
        self.assertEqual(agent["rerun"]["status"], "BLOCKED")
        self.assertEqual(
            agent["rerun"]["error_code"],
            "llm_extraction_provider_not_configured",
        )
        self.assertEqual(stored["serving_impact"], "NONE")

    def test_local_patch_persists_runtime_compatible_not_run_stages(self) -> None:
        local_profile = build_local_rules_profile(project_id=self.project["id"])
        local_plan = build_resolved_processing_plan(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.source_ir.source_sha256,
            profile=local_profile,
            document=local_profile.document,
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
            ),
            connection_revision=0,
        )
        effective = self.base_repair.effective_structure
        inspection = self._structure_inspection_payload(
            structural_candidate_count=0,
        )
        local_result = copy.deepcopy(self.result)
        local_result.update(
            {
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "requirement_tree_projection_runtime_version": (
                    document_jobs.REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
                ),
                "processing_plan": local_plan.model_dump(mode="json"),
                "result_runtime_sha256": (
                    document_jobs._document_extraction_result_runtime_sha256(local_plan)
                ),
                "items": [],
                "requirement_groups": [],
                "review_groups": [],
                "table_record_projections": [],
                "table_evidence_bindings": [],
                "llm_review": None,
                "requirement_set_id": None,
                "structure_inspection": inspection,
                "counts": {
                    "items": 0,
                    "requirement_groups": 0,
                    "review_groups": 0,
                    "table_record_projections": 0,
                    "table_evidence_bindings": 0,
                    "table_records": 0,
                },
                "pipeline": {
                    "source_ir": {
                        "id": self.source_ir.source_ir_id,
                        "adapter_id": document_jobs.PDF_SOURCE_IR_ADAPTER_ID,
                        "version": document_jobs.PDF_SOURCE_IR_ADAPTER_VERSION,
                        "status": "COMPLETE",
                    },
                    "structure": {
                        "id": effective.effective_snapshot_id,
                        "base_snapshot_id": self.base_tree.structure_snapshot_id,
                        "version": self.base_tree.resolver_version,
                    },
                    "semantic": {
                        "id": None,
                        "status": "NOT_RUN",
                        "origin": "NONE",
                        "authority": "NONE",
                        "runtime_version": "local-content-role/v1",
                        "structure_snapshot_id": effective.effective_snapshot_id,
                    },
                    "requirement_view": {
                        "id": None,
                        "status": "NOT_RUN",
                        "origin": "NONE",
                        "authority": "NONE",
                        "runtime_version": "local-content-role/v1",
                        "semantic_snapshot_id": None,
                        "structure_snapshot_id": effective.effective_snapshot_id,
                    },
                },
            }
        )
        local_result["structure_agent"]["deterministic_inspection"] = inspection
        semantic_before = copy.deepcopy(local_result["pipeline"]["semantic"])
        requirement_view_before = copy.deepcopy(
            local_result["pipeline"]["requirement_view"]
        )
        self.assertTrue(
            document_jobs._local_structure_extraction_result_matches_runtime(
                local_result
            )
        )
        self._seed_completed(local_result)

        with (
            patch.object(
                main,
                "_llm_extraction_result_matches_runtime",
                side_effect=lambda payload, **_kwargs: (
                    document_jobs._local_structure_extraction_result_matches_runtime(
                        payload
                    )
                ),
            ),
            patch.object(
                main,
                "_queue_structure_derivation_rerun",
                side_effect=lambda **kwargs: kwargs["previous_job"],
            ),
        ):
            applied = main.apply_document_structure_patch(
                self.project["id"],
                self.document["id"],
                self._patch_request(),
                Response(),
            )
            loaded = main.get_document_structure_agent(
                self.project["id"],
                self.document["id"],
                Response(),
            )

        stored = main._LLM_EXTRACTION_JOBS[self.document["id"]]["result"]
        self.assertEqual(stored["pipeline"]["semantic"], semantic_before)
        self.assertEqual(
            stored["pipeline"]["requirement_view"],
            requirement_view_before,
        )
        self.assertTrue(
            document_jobs._local_structure_extraction_result_matches_runtime(stored)
        )
        self.assertEqual(
            applied["structure_agent"]["effective_structure_identity"][
                "revision_number"
            ],
            1,
        )
        self.assertEqual(
            loaded["structure_workbench"]["requirement_tree"]["state"],
            "READY",
        )
        self.assertEqual(loaded["structure_workbench"]["structure"]["revision"], 1)
        self.assertNotIn(
            "SOURCE_TEXT_RESTORATION_REQUIRED",
            {item["code"] for item in loaded["structure_workbench"]["issues"]},
        )

    def test_durable_snapshot_keeps_private_raw_result_cas_token(self) -> None:
        store = LocalObjectStore(self.root / "snapshot-objects")
        container = SimpleNamespace(object_store=store)
        initial_result = {
            **copy.deepcopy(self.result),
            "project_id": self.project["id"],
            "document_id": self.document["id"],
        }
        with patch.object(application_runtime.RUNTIME_STATE, "container", container):
            receipt = document_jobs._persist_document_extraction_result_artifact(
                initial_result,
                project_id=self.project["id"],
                document_id=self.document["id"],
                source_sha256=self.source_ir.source_sha256,
            )
            snapshot = document_jobs._llm_extraction_snapshot_from_durable(
                self._durable_result_job(receipt)
            )

        self.assertEqual(snapshot["result"], initial_result)
        self.assertEqual(
            snapshot[document_jobs._DURABLE_RESULT_CAS_TOKEN_KEY],
            receipt,
        )
        serialized = document_jobs._serialize_llm_extraction_job(snapshot)
        self.assertNotIn(document_jobs._DURABLE_RESULT_CAS_TOKEN_KEY, serialized)
        self.assertNotIn(receipt["storage_key"], str(serialized))

    def test_postgresql_patch_republishes_artifact_receipt_with_receipt_cas(
        self,
    ) -> None:
        store = LocalObjectStore(self.root / "objects")
        container = SimpleNamespace(object_store=store, jobs=None)
        initial_result = {
            **copy.deepcopy(self.result),
            "project_id": self.project["id"],
            "document_id": self.document["id"],
        }
        with patch.object(application_runtime.RUNTIME_STATE, "container", container):
            old_receipt = document_jobs._persist_document_extraction_result_artifact(
                initial_result,
                project_id=self.project["id"],
                document_id=self.document["id"],
                source_sha256=self.source_ir.source_sha256,
            )

        durable = self._durable_result_job(old_receipt)
        cas_calls: list[dict[str, object]] = []

        def compare_and_set_succeeded_result(**kwargs):
            cas_calls.append(copy.deepcopy(kwargs))
            if kwargs["expected_result"] != durable.result:
                return None
            durable.result = copy.deepcopy(kwargs["result"])
            return durable

        queue = SimpleNamespace(
            get_latest=lambda **_kwargs: durable,
            compare_and_set_succeeded_result=compare_and_set_succeeded_result,
        )
        container.jobs = queue
        postgres_settings = replace(
            application_runtime.RUNTIME_SETTINGS,
            database_backend=DatabaseBackend.POSTGRESQL,
        )
        new_parent = self.sections_by_label["2"]
        request = self._patch_request().model_copy(
            update={
                "operations": tuple(
                    ReparentStructureOperationV1(
                        structure_node_id=self.nodes_by_text[text].structure_node_id,
                        new_parent_structure_node_id=new_parent.structure_node_id,
                        new_sibling_index=index,
                    )
                    for index, text in enumerate(
                        (
                            "Alpha one. Alpha two.",
                            "Second continuation.",
                            "Beta content.",
                        )
                    )
                )
            }
        )

        with (
            patch.object(
                application_runtime,
                "RUNTIME_SETTINGS",
                postgres_settings,
            ),
            patch.object(application_runtime.RUNTIME_STATE, "container", container),
            patch.object(
                main,
                "_queue_structure_derivation_rerun",
                side_effect=lambda **kwargs: kwargs["previous_job"],
            ),
        ):
            response = main.apply_document_structure_patch(
                self.project["id"],
                self.document["id"],
                request,
                Response(),
            )

            self.assertTrue(response["accepted"])
            self.assertEqual(len(cas_calls), 1)
            self.assertEqual(cas_calls[0]["expected_result"], old_receipt)
            new_receipt = cas_calls[0]["result"]
            self.assertEqual(
                new_receipt["schema_version"],
                "document-extraction-result-artifact/v1",
            )
            self.assertNotIn("structure_agent", new_receipt)
            restored = document_jobs._load_document_extraction_result_artifact(
                new_receipt,
                expected_project_id=self.project["id"],
                expected_document_id=self.document["id"],
                expected_source_sha256=self.source_ir.source_sha256,
            )

        self.assertEqual(
            restored["structure_agent"]["effective_structure_identity"][
                "revision_number"
            ],
            1,
        )
        self.assertEqual(
            len(restored["structure_agent"]["patch_summaries"][-1]["operations"]),
            3,
        )
        moved_ids = [
            self.nodes_by_text[text].structure_node_id
            for text in (
                "Alpha one. Alpha two.",
                "Second continuation.",
                "Beta content.",
            )
        ]
        section_children = sorted(
            (
                node
                for node in response["structure_workbench"]["structure"]["nodes"]
                if node["parent_id"] == new_parent.structure_node_id
            ),
            key=lambda node: node["order"],
        )
        self.assertEqual(
            [node["id"] for node in section_children],
            [
                *moved_ids,
                self.nodes_by_text["Other content."].structure_node_id,
            ],
        )
        revision_chain = restored["structure_agent"]["revision_chain"]
        self.assertEqual(len(revision_chain), 1)
        self.assertEqual(len(revision_chain[0]["patch"]["operations"]), 3)
        cached = document_jobs.LlmExtractionResultCache(self.root).load(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.source_ir.source_sha256,
        )
        self.assertEqual(cached, restored)

    def test_postgresql_patch_migrates_legacy_inline_result_to_receipt(self) -> None:
        store = LocalObjectStore(self.root / "legacy-objects")
        container = SimpleNamespace(object_store=store, jobs=None)
        initial_result = {
            **copy.deepcopy(self.result),
            "project_id": self.project["id"],
            "document_id": self.document["id"],
        }
        durable = self._durable_result_job(initial_result)
        cas_calls: list[dict[str, object]] = []

        def compare_and_set_succeeded_result(**kwargs):
            cas_calls.append(copy.deepcopy(kwargs))
            if kwargs["expected_result"] != durable.result:
                return None
            durable.result = copy.deepcopy(kwargs["result"])
            return durable

        container.jobs = SimpleNamespace(
            get_latest=lambda **_kwargs: durable,
            compare_and_set_succeeded_result=compare_and_set_succeeded_result,
        )
        postgres_settings = replace(
            application_runtime.RUNTIME_SETTINGS,
            database_backend=DatabaseBackend.POSTGRESQL,
        )

        with (
            patch.object(
                application_runtime,
                "RUNTIME_SETTINGS",
                postgres_settings,
            ),
            patch.object(application_runtime.RUNTIME_STATE, "container", container),
            patch.object(
                main,
                "_queue_structure_derivation_rerun",
                side_effect=lambda **kwargs: kwargs["previous_job"],
            ),
        ):
            response = main.apply_document_structure_patch(
                self.project["id"],
                self.document["id"],
                self._patch_request(),
                Response(),
            )
            migrated = document_jobs._load_document_extraction_result_artifact(
                durable.result,
                expected_project_id=self.project["id"],
                expected_document_id=self.document["id"],
                expected_source_sha256=self.source_ir.source_sha256,
            )

        self.assertTrue(response["accepted"])
        self.assertEqual(len(cas_calls), 1)
        self.assertEqual(cas_calls[0]["expected_result"], initial_result)
        self.assertEqual(
            durable.result["schema_version"],
            "document-extraction-result-artifact/v1",
        )
        self.assertEqual(
            migrated["structure_agent"]["effective_structure_identity"][
                "revision_number"
            ],
            1,
        )

    def test_postgresql_patch_does_not_cas_when_artifact_write_fails(self) -> None:
        store = LocalObjectStore(self.root / "write-failure-objects")
        container = SimpleNamespace(object_store=store, jobs=None)
        initial_result = {
            **copy.deepcopy(self.result),
            "project_id": self.project["id"],
            "document_id": self.document["id"],
        }
        with patch.object(application_runtime.RUNTIME_STATE, "container", container):
            old_receipt = document_jobs._persist_document_extraction_result_artifact(
                initial_result,
                project_id=self.project["id"],
                document_id=self.document["id"],
                source_sha256=self.source_ir.source_sha256,
            )

        durable = self._durable_result_job(old_receipt)
        cas_calls: list[dict[str, object]] = []

        def compare_and_set_succeeded_result(**kwargs):
            cas_calls.append(copy.deepcopy(kwargs))
            return durable

        container.jobs = SimpleNamespace(
            get_latest=lambda **_kwargs: durable,
            compare_and_set_succeeded_result=compare_and_set_succeeded_result,
        )
        postgres_settings = replace(
            application_runtime.RUNTIME_SETTINGS,
            database_backend=DatabaseBackend.POSTGRESQL,
        )

        with (
            patch.object(
                application_runtime,
                "RUNTIME_SETTINGS",
                postgres_settings,
            ),
            patch.object(application_runtime.RUNTIME_STATE, "container", container),
            patch.object(
                store,
                "put",
                side_effect=OSError("synthetic object-store failure"),
            ),
            patch.object(
                document_jobs.LlmExtractionResultCache,
                "save",
            ) as save_cache,
        ):
            with self.assertRaises(OSError):
                main.apply_document_structure_patch(
                    self.project["id"],
                    self.document["id"],
                    self._patch_request(),
                    Response(),
                )

        self.assertEqual(cas_calls, [])
        self.assertEqual(durable.result, old_receipt)
        save_cache.assert_not_called()

    def test_postgresql_patch_never_overwrites_receipt_changed_after_read(
        self,
    ) -> None:
        store = LocalObjectStore(self.root / "conflict-objects")
        container = SimpleNamespace(object_store=store, jobs=None)
        initial_result = {
            **copy.deepcopy(self.result),
            "project_id": self.project["id"],
            "document_id": self.document["id"],
        }
        concurrent_result = {
            **copy.deepcopy(initial_result),
            "concurrent_publication": True,
        }
        with patch.object(application_runtime.RUNTIME_STATE, "container", container):
            old_receipt = document_jobs._persist_document_extraction_result_artifact(
                initial_result,
                project_id=self.project["id"],
                document_id=self.document["id"],
                source_sha256=self.source_ir.source_sha256,
            )
            concurrent_receipt = (
                document_jobs._persist_document_extraction_result_artifact(
                    concurrent_result,
                    project_id=self.project["id"],
                    document_id=self.document["id"],
                    source_sha256=self.source_ir.source_sha256,
                )
            )

        durable = self._durable_result_job(old_receipt)
        current_result = copy.deepcopy(old_receipt)
        get_latest_calls = 0
        cas_calls: list[dict[str, object]] = []

        def get_latest(**_kwargs):
            nonlocal current_result, get_latest_calls
            get_latest_calls += 1
            observed = copy.copy(durable)
            observed.result = copy.deepcopy(current_result)
            if get_latest_calls == 1:
                current_result = copy.deepcopy(concurrent_receipt)
            return observed

        def compare_and_set_succeeded_result(**kwargs):
            nonlocal current_result
            cas_calls.append(copy.deepcopy(kwargs))
            if kwargs["expected_result"] != current_result:
                return None
            current_result = copy.deepcopy(kwargs["result"])
            return durable

        queue = SimpleNamespace(
            get_latest=get_latest,
            compare_and_set_succeeded_result=compare_and_set_succeeded_result,
        )
        container.jobs = queue
        postgres_settings = replace(
            application_runtime.RUNTIME_SETTINGS,
            database_backend=DatabaseBackend.POSTGRESQL,
        )

        with (
            patch.object(
                application_runtime,
                "RUNTIME_SETTINGS",
                postgres_settings,
            ),
            patch.object(application_runtime.RUNTIME_STATE, "container", container),
            patch.object(
                document_jobs.LlmExtractionResultCache,
                "save",
            ) as save_cache,
        ):
            with self.assertRaises(HTTPException) as caught:
                main.apply_document_structure_patch(
                    self.project["id"],
                    self.document["id"],
                    self._patch_request(),
                    Response(),
                )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "structure_revision_conflict",
        )
        self.assertEqual(len(cas_calls), 1)
        self.assertEqual(cas_calls[0]["expected_result"], old_receipt)
        self.assertEqual(current_result, concurrent_receipt)
        save_cache.assert_not_called()

    def test_patch_queues_revision_preserving_full_document_rerun(self) -> None:
        self.provider_builder_mock.side_effect = None
        self.provider_builder_mock.return_value = SimpleNamespace()

        with patch.object(main, "Thread") as thread_type:
            response = main.apply_document_structure_patch(
                self.project["id"],
                self.document["id"],
                self._patch_request(),
                Response(),
            )

        thread_type.return_value.start.assert_called_once_with()
        worker_kwargs = thread_type.call_args.kwargs["kwargs"]
        revisions = worker_kwargs["structure_revisions"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].revision_number, 1)
        self.assertEqual(
            response["rerun"]["invalidation_scope"],
            "AFFECTED_STRUCTURE_SCOPE",
        )
        self.assertEqual(response["rerun"]["execution_scope"], "FULL_DOCUMENT")
        self.assertEqual(response["rerun"]["status"], "QUEUED")
        self.assertEqual(response["rerun"]["job"]["status"], "PENDING")
        self.assertFalse(
            response["structure_workbench"]["capabilities"]["can_edit_structure"]
        )
        self.assertEqual(
            response["structure_workbench"]["structure"]["state"],
            "PARTIAL",
        )
        cached = main._structure_agent_job_payload(
            project_id=self.project["id"],
            document_id=self.document["id"],
            document=self.document,
            require_final=False,
        )
        self.assertEqual(
            cached[2]["effective_structure_identity"]["revision_number"],
            1,
        )
        self.assertFalse(cached[2]["editable"])

    def test_optimistic_conflict_returns_latest_structure_without_writing(self) -> None:
        stale_request = self._patch_request()
        first = main.apply_document_structure_patch(
            self.project["id"],
            self.document["id"],
            stale_request,
            Response(),
        )

        with self.assertRaises(HTTPException) as caught:
            main.apply_document_structure_patch(
                self.project["id"],
                self.document["id"],
                stale_request,
                Response(),
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "structure_revision_conflict",
        )
        self.assertEqual(
            caught.exception.detail["latest_structure_agent"][
                "effective_structure_identity"
            ]["snapshot_id"],
            first["structure_agent"]["effective_structure_identity"]["snapshot_id"],
        )
        latest_workbench = caught.exception.detail["latest_structure_workbench"]
        self._assert_workbench_identity(latest_workbench, revision=1)
        self.assertEqual(
            latest_workbench["structure"]["effective_snapshot_id"],
            first["structure_workbench"]["structure"]["effective_snapshot_id"],
        )
        self.assertEqual(
            len(
                main._LLM_EXTRACTION_JOBS[self.document["id"]]["result"][
                    "structure_agent"
                ]["revision_chain"]
            ),
            1,
        )

    def test_stored_agent_proposal_uses_exact_writer_and_replays_idempotently(
        self,
    ) -> None:
        request = self._patch_request()
        proposal = create_structure_patch_proposal(
            self.source_ir,
            self.base_tree,
            (),
            request.operations,
        )
        with patch.object(main, "Thread"):
            first = main.apply_stored_agent_structure_patch(
                self.project["id"],
                self.document["id"],
                proposal,
                command_id="agent-apply-1",
            )
        replay = main.apply_stored_agent_structure_patch(
            self.project["id"],
            self.document["id"],
            proposal,
            command_id="agent-apply-1",
        )

        self.assertTrue(first.appended)
        self.assertFalse(replay.appended)
        self.assertEqual(first.structure_revision_id, replay.structure_revision_id)
        self.assertEqual(first.structure_revision_number, 1)
        revisions = main._LLM_EXTRACTION_JOBS[self.document["id"]]["result"][
            "structure_agent"
        ]["revision_chain"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(
            revisions[0]["patch"]["accepted_proposal_id"],
            proposal.proposal_id,
        )

    def test_undo_uses_current_identity_and_appends_revision(self) -> None:
        applied = main.apply_document_structure_patch(
            self.project["id"],
            self.document["id"],
            self._patch_request(),
            Response(),
        )
        identity = applied["structure_agent"]["effective_structure_identity"]
        undo = main.StructureUndoRequest(
            source_ir_id=self.source_ir.source_ir_id,
            source_sha256=self.source_ir.source_sha256,
            source_ir_adapter_version=self.source_ir.adapter_version,
            base_structure_snapshot_id=self.base_tree.structure_snapshot_id,
            base_structure_canonical_sha256=(
                self.base_repair.effective_structure.base_structure_canonical_sha256
            ),
            structure_resolver_version=self.base_tree.resolver_version,
            expected_revision_number=identity["revision_number"],
            expected_effective_snapshot_id=identity["snapshot_id"],
        )

        response = main.undo_document_structure_revision(
            self.project["id"],
            self.document["id"],
            undo,
            Response(),
        )

        self.assertEqual(response["action"], "UNDO")
        agent = response["structure_agent"]
        workbench = response["structure_workbench"]
        self._assert_workbench_identity(workbench, revision=2)
        self.assertEqual(workbench["revisions"][-1]["operation"], "UNDO")
        self.assertEqual(workbench["revisions"][-1]["actor_type"], "HUMAN")
        self.assertEqual(workbench["revisions"][-1]["actor_label"], "User")
        self.assertEqual(
            workbench["revisions"][-1]["target_revision_id"],
            applied["structure_agent"]["current_revision"]["revision_id"],
        )
        self.assertEqual(
            agent["effective_structure_identity"]["revision_number"],
            2,
        )
        self.assertEqual(len(agent["revision_chain"]), 2)
        self.assertEqual(agent["current_revision"]["action"], "UNDO")

    def test_structure_change_blocks_stale_review_mutations(self) -> None:
        applied = main.apply_document_structure_patch(
            self.project["id"],
            self.document["id"],
            self._patch_request(),
            Response(),
        )

        with self.assertRaises(HTTPException) as caught:
            main._reject_review_mutation_for_stale_structure(
                project_id=self.project["id"],
                document_id=self.document["id"],
                source_sha256=self.source_ir.source_sha256,
                prediction_structure_snapshot_id=(
                    self.base_repair.effective_structure.effective_snapshot_id
                ),
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "llm_requirement_review_structure_stale",
        )
        self.assertEqual(
            caught.exception.detail["required_effective_snapshot_id"],
            applied["structure_agent"]["effective_structure_identity"]["snapshot_id"],
        )

    def test_partial_and_interrupted_results_are_get_only(self) -> None:
        partial_agent = {
            **self.agent,
            "editable": False,
            "read_only_reason": "partial_result_is_read_only",
        }
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                **self._job_base(),
                "status": "INTERRUPTED",
                "partial_result": {
                    "source_sha256": self.source_ir.source_sha256,
                    "structure_agent": partial_agent,
                },
                "result": None,
            }

        loaded = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )
        self.assertFalse(loaded["structure_agent"]["editable"])
        workbench = loaded["structure_workbench"]
        self._assert_workbench_identity(workbench, revision=0)
        self.assertEqual(workbench["structure"]["state"], "INTERRUPTED")
        self.assertFalse(workbench["structure"]["editable"])
        self.assertFalse(workbench["agent"]["editable"])
        self.assertFalse(workbench["capabilities"]["can_edit_structure"])
        self.assertFalse(workbench["capabilities"]["can_undo_revision"])

        with self.assertRaises(HTTPException) as caught:
            main.apply_document_structure_patch(
                self.project["id"],
                self.document["id"],
                self._patch_request(),
                Response(),
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "structure_result_read_only",
        )

    def test_source_and_runtime_identity_mismatches_fail_closed(self) -> None:
        source_mismatch = self._patch_request().model_copy(
            update={"source_ir_id": "source-ir-wrong"}
        )
        with self.assertRaises(HTTPException) as source_caught:
            main.apply_document_structure_patch(
                self.project["id"],
                self.document["id"],
                source_mismatch,
                Response(),
            )
        self.assertEqual(
            source_caught.exception.detail["code"],
            "structure_source_binding_mismatch",
        )

        runtime_mismatch = self._patch_request().model_copy(
            update={"structure_resolver_version": "resolver-wrong"}
        )
        with self.assertRaises(HTTPException) as runtime_caught:
            main.apply_document_structure_patch(
                self.project["id"],
                self.document["id"],
                runtime_mismatch,
                Response(),
            )
        self.assertEqual(
            runtime_caught.exception.detail["code"],
            "structure_runtime_binding_mismatch",
        )

    def test_exact_visual_source_ir_adapter_is_a_valid_mutation_binding(self) -> None:
        visual_adapter_version = f"{self.source_ir.adapter_version}+visual.{'a' * 12}"
        visual_source_ir = self.source_ir.model_copy(
            update={"adapter_version": visual_adapter_version},
        )
        visual_current = validate_structure_revision_chain(
            visual_source_ir,
            self.base_tree,
            (),
        )
        visual_request = self._patch_request().model_copy(
            update={
                "source_ir_adapter_version": visual_adapter_version,
                "expected_effective_snapshot_id": (
                    visual_current.effective_snapshot_id
                ),
            },
        )

        main._validate_structure_mutation_binding(
            visual_request,
            source_ir=visual_source_ir,
            base_tree=self.base_tree,
            revisions=(),
            latest_structure_agent=self.agent,
            latest_structure_workbench={},
        )

    def test_failed_retry_does_not_hide_previous_successful_working_tree(
        self,
    ) -> None:
        failed = {
            **self._job_base(),
            "job_id": "llm-extraction-" + "f" * 32,
            "status": "FAILED",
            "stage": "failed",
            "result": None,
            "partial_result": None,
            "error_code": "synthetic_retry_failed",
        }
        previous = {
            **self._job_base(),
            "job_id": "llm-extraction-" + "e" * 32,
            "status": "COMPLETED",
            "stage": "completed",
            "result": copy.deepcopy(self.result),
            "partial_result": None,
        }
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS[self.document["id"]] = failed

        with patch.object(
            document_jobs,
            "_get_latest_successful_llm_extraction_job",
            return_value=previous,
        ) as successful_lookup:
            response = main.get_document_structure_agent(
                self.project["id"],
                self.document["id"],
                Response(),
            )

        successful_lookup.assert_called_once_with(
            self.project["id"],
            self.document["id"],
            source_sha256=self.source_ir.source_sha256,
        )
        self.assertEqual(response["job"]["job_id"], failed["job_id"])
        self.assertEqual(response["job"]["status"], "FAILED")
        self.assertEqual(
            response["job"]["error_code"],
            "synthetic_retry_failed",
        )
        self.assertFalse(response["structure_agent"]["editable"])
        self.assertEqual(
            response["structure_agent"]["read_only_reason"],
            "structure_derivation_rerun_not_complete",
        )
        self.assertEqual(
            response["structure_workbench"]["agent"]["status"],
            "COMPLETED",
        )
        self.assertEqual(
            response["structure_workbench"]["structure"]["state"],
            "STABLE",
        )
        self.assertFalse(
            response["structure_workbench"]["capabilities"]["can_edit_structure"]
        )
        self.assertEqual(
            response["structure_workbench"]["requirement_tree"]["state"],
            "READY",
        )
        self.assertGreater(
            len(response["structure_workbench"]["requirement_tree"]["nodes"]),
            0,
        )

    def test_terminal_retry_serves_runtime_bound_cached_tree_read_only(self) -> None:
        document_jobs.LlmExtractionResultCache(self.root).save(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.source_ir.source_sha256,
            payload=self.result,
        )

        for status in ("FAILED", "INTERRUPTED", "CANCELLED"):
            with self.subTest(status=status):
                latest_job_id = f"llm-extraction-{status.lower()}"
                with main._LLM_EXTRACTION_JOB_LOCK:
                    main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                        **self._job_base(),
                        "job_id": latest_job_id,
                        "status": status,
                        "stage": status.lower(),
                        "result": None,
                        "partial_result": None,
                        "error_code": f"synthetic_{status.lower()}",
                    }

                with patch.object(application_runtime, "STORAGE_DIR", self.root):
                    response = main.get_document_structure_agent(
                        self.project["id"],
                        self.document["id"],
                        Response(),
                    )
                    with self.assertRaises(HTTPException) as caught:
                        main.apply_document_structure_patch(
                            self.project["id"],
                            self.document["id"],
                            self._patch_request(),
                            Response(),
                        )

                self.assertEqual(response["job"]["job_id"], latest_job_id)
                self.assertEqual(response["job"]["status"], status)
                self.assertEqual(
                    response["job"]["error_code"],
                    f"synthetic_{status.lower()}",
                )
                self.assertFalse(response["structure_agent"]["editable"])
                self.assertEqual(
                    response["structure_agent"]["read_only_reason"],
                    "structure_derivation_rerun_not_complete",
                )
                workbench = response["structure_workbench"]
                self.assertEqual(workbench["agent"]["status"], "COMPLETED")
                self.assertEqual(workbench["structure"]["state"], "STABLE")
                self.assertFalse(workbench["structure"]["editable"])
                self.assertFalse(workbench["capabilities"]["can_edit_structure"])
                self.assertEqual(workbench["requirement_tree"]["state"], "READY")
                self.assertGreater(len(workbench["requirement_tree"]["nodes"]), 0)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(
                    caught.exception.detail["code"],
                    "structure_result_read_only",
                )

    def test_active_retry_serves_cached_tree_ready_and_read_only(self) -> None:
        document_jobs.LlmExtractionResultCache(self.root).save(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.source_ir.source_sha256,
            payload=self.result,
        )

        for status in ("PENDING", "RUNNING", "CANCEL_REQUESTED"):
            with self.subTest(status=status):
                latest_job_id = f"llm-extraction-{status.lower()}"
                with main._LLM_EXTRACTION_JOB_LOCK:
                    main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                        **self._job_base(),
                        "job_id": latest_job_id,
                        "status": status,
                        "stage": status.lower(),
                        "result": None,
                        "partial_result": None,
                    }

                with patch.object(application_runtime, "STORAGE_DIR", self.root):
                    response = main.get_document_structure_agent(
                        self.project["id"],
                        self.document["id"],
                        Response(),
                    )

                self.assertEqual(response["job"]["job_id"], latest_job_id)
                self.assertEqual(response["job"]["status"], status)
                self.assertFalse(response["structure_agent"]["editable"])
                self.assertEqual(
                    response["structure_agent"]["read_only_reason"],
                    "structure_derivation_rerun_in_progress",
                )
                workbench = response["structure_workbench"]
                self.assertEqual(workbench["agent"]["status"], "COMPLETED")
                self.assertEqual(workbench["structure"]["state"], "STABLE")
                self.assertFalse(workbench["capabilities"]["can_edit_structure"])
                self.assertEqual(
                    workbench["requirement_tree"]["state"],
                    "READY",
                )

    def test_active_retry_serves_latest_runtime_bound_postgres_artifact_read_only(
        self,
    ) -> None:
        previous = {
            **self._job_base(),
            "job_id": "job-" + "b" * 32,
            "status": "COMPLETED",
            "stage": "completed",
            "result": copy.deepcopy(self.result),
            "partial_result": None,
        }
        for status in ("PENDING", "CANCEL_REQUESTED"):
            with self.subTest(status=status):
                active = {
                    **self._job_base(),
                    "job_id": "llm-extraction-" + "a" * 32,
                    "status": status,
                    "stage": "queued" if status == "PENDING" else "cancelling",
                    "result": None,
                    "partial_result": None,
                }
                with main._LLM_EXTRACTION_JOB_LOCK:
                    main._LLM_EXTRACTION_JOBS[self.document["id"]] = active

                with (
                    patch.object(
                        document_jobs,
                        "_get_latest_successful_llm_extraction_job",
                        return_value=previous,
                    ),
                    patch.object(
                        document_jobs,
                        "_llm_extraction_result_matches_runtime",
                        return_value=True,
                    ),
                ):
                    response = main.get_document_structure_agent(
                        self.project["id"],
                        self.document["id"],
                        Response(),
                    )

                self.assertEqual(response["job"]["job_id"], active["job_id"])
                self.assertEqual(response["job"]["status"], status)
                self.assertFalse(response["structure_agent"]["editable"])
                self.assertEqual(
                    response["structure_agent"]["read_only_reason"],
                    "structure_derivation_rerun_in_progress",
                )
                workbench = response["structure_workbench"]
                self.assertEqual(workbench["requirement_tree"]["state"], "READY")
                self.assertFalse(workbench["capabilities"]["can_edit_structure"])

    def test_previous_successful_tree_still_fails_closed_on_runtime_mismatch(
        self,
    ) -> None:
        failed = {
            **self._job_base(),
            "job_id": "llm-extraction-" + "f" * 32,
            "status": "FAILED",
            "stage": "failed",
            "result": None,
            "partial_result": None,
        }
        previous = {
            **self._job_base(),
            "job_id": "llm-extraction-" + "e" * 32,
            "status": "COMPLETED",
            "stage": "completed",
            "result": copy.deepcopy(self.result),
            "partial_result": None,
        }
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS[self.document["id"]] = failed

        with (
            patch.object(
                document_jobs,
                "_get_latest_successful_llm_extraction_job",
                return_value=previous,
            ),
            patch.object(
                main,
                "_llm_extraction_result_matches_runtime",
                return_value=False,
            ),
        ):
            response = main.get_document_structure_agent(
                self.project["id"],
                self.document["id"],
                Response(),
            )

        workbench = response["structure_workbench"]
        self.assertEqual(
            workbench["requirement_tree"]["state"],
            "REQUIRES_REEXTRACTION",
        )
        self.assertEqual(workbench["requirement_tree"]["nodes"], [])
        self.assertFalse(workbench["capabilities"]["can_edit_structure"])

    def test_stored_effective_identity_tamper_fails_closed(self) -> None:
        tampered_agent = copy.deepcopy(self.agent)
        tampered_agent["effective_structure_identity"]["tree_sha256"] = "0" * 64

        with (
            patch.object(
                main,
                "_source_file_sha256",
                return_value=self.source_ir.source_sha256,
            ),
            patch.object(
                main.LocalDocumentPreflight,
                "inspect",
                return_value=SimpleNamespace(
                    source_sha256=self.source_ir.source_sha256
                ),
            ),
            patch.object(
                main,
                "build_source_ir",
                return_value=self.source_ir,
            ),
            patch.object(
                main,
                "build_structure_tree",
                return_value=self.base_tree,
            ),
        ):
            valid_context = _REAL_REBUILD_STRUCTURE_REVISION_CONTEXT(
                document=self.document,
                agent=self.agent,
            )
            with self.assertRaises(HTTPException) as caught:
                _REAL_REBUILD_STRUCTURE_REVISION_CONTEXT(
                    document=self.document,
                    agent=tampered_agent,
                )

        self.assertIs(valid_context[0], self.source_ir)
        self.assertEqual(
            caught.exception.detail["code"],
            "structure_agent_identity_mismatch",
        )

    def test_structure_revision_context_cache_is_identity_bound(self) -> None:
        with (
            patch.object(
                main,
                "_source_file_sha256",
                return_value=self.source_ir.source_sha256,
            ) as source_sha_mock,
            patch.object(
                main.LocalDocumentPreflight,
                "inspect",
                return_value=SimpleNamespace(
                    source_sha256=self.source_ir.source_sha256
                ),
            ) as preflight_mock,
            patch.object(
                main,
                "build_source_ir",
                return_value=self.source_ir,
            ) as source_ir_mock,
            patch.object(
                main,
                "build_structure_tree",
                return_value=self.base_tree,
            ) as structure_mock,
        ):
            first = _REAL_REBUILD_STRUCTURE_REVISION_CONTEXT(
                document=self.document,
                agent=self.agent,
            )
            second = _REAL_REBUILD_STRUCTURE_REVISION_CONTEXT(
                document=self.document,
                agent=self.agent,
            )

        self.assertIs(first, second)
        self.assertEqual(source_sha_mock.call_count, 2)
        preflight_mock.assert_called_once()
        source_ir_mock.assert_called_once()
        structure_mock.assert_called_once()

    def test_structure_revision_context_cache_rechecks_source_bytes(self) -> None:
        with (
            patch.object(
                main,
                "_source_file_sha256",
                side_effect=(self.source_ir.source_sha256, "f" * 64),
            ) as source_sha_mock,
            patch.object(
                main.LocalDocumentPreflight,
                "inspect",
                return_value=SimpleNamespace(
                    source_sha256=self.source_ir.source_sha256
                ),
            ) as preflight_mock,
            patch.object(
                main,
                "build_source_ir",
                return_value=self.source_ir,
            ) as source_ir_mock,
            patch.object(
                main,
                "build_structure_tree",
                return_value=self.base_tree,
            ) as structure_mock,
        ):
            _REAL_REBUILD_STRUCTURE_REVISION_CONTEXT(
                document=self.document,
                agent=self.agent,
            )
            with self.assertRaises(HTTPException) as caught:
                _REAL_REBUILD_STRUCTURE_REVISION_CONTEXT(
                    document=self.document,
                    agent=self.agent,
                )

        self.assertEqual(
            caught.exception.detail["code"],
            "structure_source_binding_mismatch",
        )
        self.assertEqual(source_sha_mock.call_count, 2)
        preflight_mock.assert_called_once()
        source_ir_mock.assert_called_once()
        structure_mock.assert_called_once()

    def test_final_get_projects_persisted_runtime_bound_identity(self) -> None:
        response = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(response["serving_impact"], "NONE")
        self.assertEqual(
            response["structure_agent"]["effective_structure_identity"],
            self.agent["effective_structure_identity"],
        )
        workbench = response["structure_workbench"]
        self._assert_workbench_identity(workbench, revision=0)
        self.assertEqual(workbench["schema_version"], "structure-workbench/v0")
        self.assertEqual(workbench["source"]["file_name"], "source.pdf")
        self.assertEqual(
            workbench["structure"]["effective_tree_sha256"],
            self.base_repair.effective_structure.effective_tree_sha256,
        )
        self.assertGreater(len(workbench["structure"]["nodes"]), 1)
        self.assertTrue(workbench["structure"]["editable"])
        self.assertEqual(workbench["agent"]["status"], "COMPLETED")
        self.assertNotIn("file_path", workbench["source"])
        self.rebuild_context_mock.assert_not_called()

    def test_final_get_projects_persisted_visual_source_ir_adapter(self) -> None:
        visual_adapter_version = f"{self.source_ir.adapter_version}+visual.{'b' * 12}"
        self.result["pipeline"]["source_ir"] = {
            "id": self.source_ir.source_ir_id,
            "version": visual_adapter_version,
            "status": "COMPLETE",
        }
        self.result["visual_strategy"] = {
            "mode": "PAGE_LAYOUT",
            "source_ir_adapter_version": visual_adapter_version,
        }
        self._seed_completed(self.result)

        response = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        workbench = response["structure_workbench"]
        self.assertEqual(
            workbench["source"]["source_ir_adapter_version"],
            visual_adapter_version,
        )
        self.assertEqual(
            workbench["runtime"]["source_ir_adapter_version"],
            visual_adapter_version,
        )
        self.rebuild_context_mock.assert_not_called()

    def test_final_get_restores_persisted_figure_kind_without_content_label(
        self,
    ) -> None:
        target_id = self.nodes_by_text["Other content."].structure_node_id
        presentation = self.result["structure_presentation_index"]
        entry = presentation["nodes"][target_id]
        entry["label"] = ""
        entry["owned_text"] = ""
        for ref in entry["source_refs"]:
            ref["kind"] = "visual_text_observation"
            ref["source_kind"] = "figure"
        self._seed_completed(self.result)

        response = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        node = next(
            item
            for item in response["structure_workbench"]["structure"]["nodes"]
            if item["id"] == target_id
        )
        self.assertEqual(node["kind"], "CONTENT")
        self.assertEqual(node["label"], "图片")
        self.assertEqual(
            {ref["source_kind"] for ref in node["source_refs"]},
            {"figure"},
        )
        self.assertEqual(
            {ref["kind"] for ref in node["source_refs"]},
            {"visual_text_observation"},
        )
        self.rebuild_context_mock.assert_not_called()

    def test_final_get_requires_reextraction_for_legacy_presentation_index(
        self,
    ) -> None:
        self.result["structure_presentation_index"]["schema_version"] = (
            "structure-presentation-index/v2"
        )
        self._seed_completed(self.result)

        response = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        workbench = response["structure_workbench"]
        self.assertEqual(
            workbench["requirement_tree"]["state"],
            "REQUIRES_REEXTRACTION",
        )
        self.assertEqual(workbench["requirement_tree"]["nodes"], [])
        self.assertFalse(workbench["capabilities"]["can_edit_structure"])
        self.rebuild_context_mock.assert_not_called()

    def test_workbench_projects_inspection_without_turning_it_into_a_gate(
        self,
    ) -> None:
        inspection = self._structure_inspection_payload(
            structural_candidate_count=1,
        )
        self.result["structure_inspection"] = inspection
        self.result["structure_agent"]["deterministic_inspection"] = inspection
        self._seed_completed(self.result)

        response = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        workbench = response["structure_workbench"]
        projected = workbench["structure_inspection"]
        self.assertEqual(projected["state"], "READY")
        self.assertEqual(projected["status"], "REVIEW_REQUIRED")
        self.assertFalse(projected["stale"])
        self.assertEqual(
            projected["counts"]["structural_actionable_candidate_count"],
            1,
        )
        self.assertEqual(len(projected["actionable_candidates"]), 1)
        issue = next(
            item
            for item in workbench["issues"]
            if item["code"] == "STRUCTURE_INSPECTION_REVIEW_REQUIRED"
        )
        self.assertEqual(issue["severity"], "WARNING")
        self.assertEqual(issue["candidate_count"], 1)
        self.assertTrue(workbench["capabilities"]["can_edit_structure"])
        self.assertEqual(
            workbench["agent"]["document_health"],
            "STABLE_WITH_ISSUES",
        )

        patched = main.apply_document_structure_patch(
            self.project["id"],
            self.document["id"],
            self._patch_request(),
            Response(),
        )
        stale = patched["structure_workbench"]["structure_inspection"]
        self.assertEqual(stale["basis_revision"], 0)
        self.assertEqual(stale["effective_revision"], 1)
        self.assertTrue(stale["stale"])
        self.assertEqual(
            stale["status"],
            "STALE_AFTER_STRUCTURE_REVISION",
        )
        self.assertEqual(
            stale["counts"]["observed_structural_actionable_candidate_count"],
            1,
        )
        self.assertEqual(
            stale["counts"]["structural_actionable_candidate_count"],
            0,
        )
        self.assertEqual(stale["actionable_candidates"], [])
        self.assertNotIn(
            "STRUCTURE_INSPECTION_REVIEW_REQUIRED",
            {item["code"] for item in patched["structure_workbench"]["issues"]},
        )

    def test_workbench_rejects_mismatched_inspection_copy_without_blocking(
        self,
    ) -> None:
        inspection = self._structure_inspection_payload(
            structural_candidate_count=1,
        )
        self.result["structure_inspection"] = inspection
        self.result["structure_agent"]["deterministic_inspection"] = {
            **inspection,
            "canonical_sha256": "f" * 64,
        }
        self._seed_completed(self.result)

        response = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        workbench = response["structure_workbench"]
        projected = workbench["structure_inspection"]
        self.assertEqual(projected["state"], "UNAVAILABLE")
        self.assertEqual(
            projected["unavailable_reason"],
            "DUPLICATE_RECEIPT_MISMATCH",
        )
        self.assertTrue(workbench["capabilities"]["can_edit_structure"])
        self.assertNotIn(
            "STRUCTURE_INSPECTION_REVIEW_REQUIRED",
            {item["code"] for item in workbench["issues"]},
        )

    def test_final_get_projects_runtime_mismatch_as_reextraction_state(self) -> None:
        main._llm_extraction_result_matches_runtime.return_value = False

        response = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        workbench = response["structure_workbench"]
        tree = workbench["requirement_tree"]
        self.assertEqual(tree["state"], "REQUIRES_REEXTRACTION")
        self.assertEqual(tree["nodes"], [])
        self.assertEqual(tree["root_node_ids"], [])
        self.assertEqual(
            tree["source_identity"]["source_sha256"],
            self.source_ir.source_sha256,
        )
        self.assertFalse(workbench["capabilities"]["can_edit_structure"])
        self.assertEqual(
            response["structure_agent"]["read_only_reason"],
            "requires_reextraction",
        )
        self.assertEqual(
            workbench["recovery"],
            {
                "reason_code": "RESULT_REGENERATION_REQUIRED",
                "detail_code": "RUNTIME_INCOMPATIBLE",
                "recommended_action": "REGENERATE_EXTRACTION_RESULT",
            },
        )
        issue_codes = {item["code"] for item in workbench["issues"]}
        self.assertIn("STRUCTURE_RESULT_REGENERATION_REQUIRED", issue_codes)
        self.assertNotIn("SOURCE_TEXT_RESTORATION_REQUIRED", issue_codes)
        self.rebuild_context_mock.assert_not_called()

    def test_final_get_reserves_visual_recovery_for_source_capture_gap(
        self,
    ) -> None:
        self.result["items"] = []
        self.result["pipeline"]["source_ir"] = {
            "id": self.source_ir.source_ir_id,
            "version": self.source_ir.adapter_version,
            "status": "PARTIAL",
        }
        self._seed_completed(self.result)

        response = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        workbench = response["structure_workbench"]
        self.assertEqual(
            workbench["requirement_tree"]["state"],
            "REQUIRES_REEXTRACTION",
        )
        self.assertEqual(
            workbench["recovery"],
            {
                "reason_code": "SOURCE_CAPTURE_GAP",
                "detail_code": "SOURCE_IR_INCOMPLETE",
                "recommended_action": "VISUAL_RESTORE_AND_REEXTRACT",
            },
        )
        issue_codes = {item["code"] for item in workbench["issues"]}
        self.assertIn("SOURCE_TEXT_RESTORATION_REQUIRED", issue_codes)
        self.assertNotIn("STRUCTURE_RESULT_REGENERATION_REQUIRED", issue_codes)

    def test_completed_job_without_result_requires_reextraction(self) -> None:
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS[self.document["id"]]["result"] = None

        response = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(
            response["structure_workbench"]["requirement_tree"]["state"],
            "REQUIRES_REEXTRACTION",
        )
        self.rebuild_context_mock.assert_not_called()

    def test_ai_optimization_directly_applies_one_revision(self) -> None:
        provider = self._optimizer_provider()
        source_hash_before = canonical_model_sha256(self.source_ir)

        with patch.object(
            main,
            "_build_structure_optimizer_provider",
            return_value=provider,
        ):
            started = main.start_document_structure_optimization(
                self.project["id"],
                self.document["id"],
                Response(),
            )
            terminal = self._await_structure_optimization()

        self.assertEqual(started["job"]["status"], "PENDING")
        self.assertEqual(terminal["job"]["status"], "SUCCEEDED")
        self.assertEqual(terminal["job"]["optimization_status"], "APPLIED")
        self.assertEqual(
            terminal["job"]["result"]["revision"]["revision_number"],
            1,
        )
        self.assertEqual(
            terminal["job"]["result"]["repair"]["operations"],
            ["re_parent"],
        )
        self.assertEqual(
            terminal["job"]["result"]["changes"][0]["operation"],
            "re_parent",
        )
        self.assertEqual(
            terminal["job"]["result"]["changes"][0]["before"][
                "parent_structure_node_id"
            ],
            self.sections_by_label["1.1"].structure_node_id,
        )
        self.assertEqual(
            terminal["job"]["result"]["changes"][0]["after"][
                "parent_structure_node_id"
            ],
            self.sections_by_label["1.2"].structure_node_id,
        )
        self.assertGreater(
            terminal["job"]["result"]["impact_summary"]["affected_node_count"],
            0,
        )
        self.assertEqual(provider.calls, ["overview_structure", "repair_structure"])
        self.assertEqual(canonical_model_sha256(self.source_ir), source_hash_before)

        current = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )
        self.assertEqual(
            current["structure_agent"]["effective_structure_identity"][
                "revision_number"
            ],
            1,
        )
        self.assertEqual(
            current["structure_workbench"]["revisions"][-1]["actor_type"],
            "AI",
        )
        self.assertEqual(
            current["structure_agent"]["proposal_summaries"][-1]["origin"],
            "AI",
        )
        revisions = tuple(
            StructureRevisionV1.model_validate(item)
            for item in current["structure_agent"]["revision_chain"]
        )
        rerun_shadow = {
            "structure_agent": _serialize_structure_agent(
                initialize_structure_repair_without_provider(
                    self.source_ir,
                    self.base_tree,
                    error_code="structure_audit_not_requested",
                    revisions=revisions,
                ),
                editable=True,
                final=True,
            )
        }
        main._retain_structure_revision_provenance(
            rerun_shadow,
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.source_ir.source_sha256,
            structure_revisions=revisions,
        )
        self.assertEqual(
            rerun_shadow["structure_agent"]["proposal_summaries"][-1]["origin"],
            "AI",
        )
        self.assertEqual(
            rerun_shadow["structure_agent"]["optimization"]["revision_id"],
            terminal["job"]["result"]["revision"]["revision_id"],
        )

    def test_ai_optimization_start_requires_complete_source_capture(self) -> None:
        incomplete = self.source_ir.model_copy(
            update={"status": SourceIRStatus.PARTIAL},
        )
        self.rebuild_context_mock.side_effect = lambda **_kwargs: (
            incomplete,
            self.base_tree,
            (),
        )

        with (
            patch.object(main, "_build_structure_optimizer_provider") as builder,
            self.assertRaises(HTTPException) as captured,
        ):
            main.start_document_structure_optimization(
                self.project["id"],
                self.document["id"],
                Response(),
            )

        self.assertEqual(captured.exception.status_code, 409)
        self.assertEqual(
            captured.exception.code,
            "structure_optimization_requires_reextraction",
        )
        self.assertEqual(
            captured.exception.detail,
            {
                "code": "structure_optimization_requires_reextraction",
                "reason_code": "source_ir_not_complete",
                "message": (
                    "Source capture is incomplete. Re-extract the document "
                    "before running Structure optimization."
                ),
                "source_ir_status": "PARTIAL",
                "required_source_ir_status": "COMPLETE",
                "blocked_by": ["source_capture"],
                "next_action": "reextract_source",
            },
        )
        builder.assert_not_called()
        with main._LOCAL_OPTIMIZATION_LOCK:
            self.assertNotIn(
                self.document["id"],
                main._LOCAL_OPTIMIZATION_JOBS,
            )

    def test_queued_optimization_stops_if_source_capture_becomes_partial(
        self,
    ) -> None:
        incomplete = self.source_ir.model_copy(
            update={"status": SourceIRStatus.PARTIAL},
        )
        self.rebuild_context_mock.side_effect = lambda **_kwargs: (
            incomplete,
            self.base_tree,
            (),
        )
        current = validate_structure_revision_chain(
            incomplete,
            self.base_tree,
            (),
        )
        payload = {
            "project_id": self.project["id"],
            "document_id": self.document["id"],
            "source_ir_id": incomplete.source_ir_id,
            "source_sha256": incomplete.source_sha256,
            "base_structure_snapshot_id": self.base_tree.structure_snapshot_id,
            "expected_revision_number": current.revision_number,
            "expected_effective_snapshot_id": current.effective_snapshot_id,
            "expected_effective_tree_sha256": current.effective_tree_sha256,
            "expected_revision_chain_sha256": current.revision_chain_sha256,
            "expected_head_revision_id": current.head_revision_id,
        }
        provider = self._optimizer_provider()
        stages: list[str] = []
        publications: list[bool] = []

        result = main._execute_structure_optimization(
            payload=payload,
            job_id="structure-optimization-incomplete-source",
            provider=provider,
            publish_progress=stages.append,
            begin_publication=lambda: publications.append(True),
            cancellation_requested=lambda: False,
        )

        self.assertEqual(result["status"], "REQUIRES_REEXTRACTION")
        self.assertEqual(result["reason_code"], "source_ir_not_complete")
        self.assertEqual(result["source_ir_status"], "PARTIAL")
        self.assertEqual(result["required_source_ir_status"], "COMPLETE")
        self.assertEqual(result["next_action"], "reextract_source")
        self.assertEqual(provider.calls, [])
        self.assertEqual(stages, [])
        self.assertEqual(publications, [])
        self.assertEqual(current.revision_number, 0)

    def test_optimization_does_not_apply_if_fresh_source_becomes_partial(
        self,
    ) -> None:
        incomplete = self.source_ir.model_copy(
            update={"status": SourceIRStatus.PARTIAL},
        )

        def become_partial_before_apply() -> None:
            self.rebuild_context_mock.side_effect = lambda **_kwargs: (
                incomplete,
                self.base_tree,
                (),
            )

        provider = self._optimizer_provider(
            before_repair=become_partial_before_apply,
        )
        current = validate_structure_revision_chain(
            self.source_ir,
            self.base_tree,
            (),
        )
        payload = {
            "project_id": self.project["id"],
            "document_id": self.document["id"],
            "source_ir_id": self.source_ir.source_ir_id,
            "source_sha256": self.source_ir.source_sha256,
            "base_structure_snapshot_id": self.base_tree.structure_snapshot_id,
            "expected_revision_number": current.revision_number,
            "expected_effective_snapshot_id": current.effective_snapshot_id,
            "expected_effective_tree_sha256": current.effective_tree_sha256,
            "expected_revision_chain_sha256": current.revision_chain_sha256,
            "expected_head_revision_id": current.head_revision_id,
        }
        stages: list[str] = []
        publications: list[bool] = []

        result = main._execute_structure_optimization(
            payload=payload,
            job_id="structure-optimization-partial-before-apply",
            provider=provider,
            publish_progress=stages.append,
            begin_publication=lambda: publications.append(True),
            cancellation_requested=lambda: False,
        )

        self.assertEqual(result["status"], "REQUIRES_REEXTRACTION")
        self.assertEqual(result["reason_code"], "source_ir_not_complete")
        self.assertEqual(result["source_ir_status"], "PARTIAL")
        self.assertEqual(provider.calls, ["overview_structure", "repair_structure"])
        self.assertEqual(stages, ["OVERVIEW"])
        self.assertEqual(publications, [True])
        self.assertIsNone(result["revision"])
        self.assertEqual(len(result["provider_runs"]), 2)
        stored = main._LLM_EXTRACTION_JOBS[self.document["id"]]["result"]
        self.assertEqual(
            stored["structure_agent"]["revision_chain"],
            [],
        )

    def test_ai_optimization_retry_after_committed_cas_reuses_revision(
        self,
    ) -> None:
        provider = self._optimizer_provider()
        with (
            patch.object(
                main,
                "_build_structure_optimizer_provider",
                return_value=provider,
            ),
            patch.object(
                main,
                "_queue_structure_derivation_rerun",
                side_effect=RuntimeError("synthetic crash after Structure CAS"),
            ),
            patch.object(structure_optimization_jobs.logger, "exception"),
        ):
            started = main.start_document_structure_optimization(
                self.project["id"],
                self.document["id"],
                Response(),
            )
            failed = self._await_structure_optimization()

        self.assertEqual(failed["job"]["status"], "FAILED")
        first_calls = list(provider.calls)
        self.assertEqual(
            first_calls,
            ["overview_structure", "repair_structure"],
        )
        with main._LOCAL_OPTIMIZATION_LOCK:
            retry_payload = copy.deepcopy(
                main._LOCAL_OPTIMIZATION_JOBS[self.document["id"]]
            )
        recovered = main._execute_structure_optimization(
            payload=retry_payload,
            job_id=started["job"]["job_id"],
            provider=provider,
            publish_progress=lambda _stage: None,
            begin_publication=lambda: None,
            cancellation_requested=lambda: False,
        )

        self.assertEqual(recovered["status"], "APPLIED")
        self.assertEqual(provider.calls, first_calls)
        self.assertEqual(
            recovered["revision"]["revision_number"],
            1,
        )
        self.assertEqual(
            recovered["rerun"]["status"],
            "BLOCKED",
        )
        current = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )
        self.assertEqual(
            len(current["structure_agent"]["revision_chain"]),
            1,
        )
        self.assertEqual(
            current["structure_agent"]["optimization"]["receipt"]["revision"][
                "revision_id"
            ],
            recovered["revision"]["revision_id"],
        )

    def test_ai_optimization_no_change_does_not_append_revision(self) -> None:
        provider = _FakeOptimizerProvider(
            overview_builder=lambda _request: (),
            repair_builder=None,
        )

        with patch.object(
            main,
            "_build_structure_optimizer_provider",
            return_value=provider,
        ):
            main.start_document_structure_optimization(
                self.project["id"],
                self.document["id"],
                Response(),
            )
            terminal = self._await_structure_optimization()

        self.assertEqual(terminal["job"]["optimization_status"], "NO_CHANGE")
        self.assertEqual(provider.calls, ["overview_structure"])
        current = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )
        self.assertEqual(
            current["structure_agent"]["effective_structure_identity"][
                "revision_number"
            ],
            0,
        )
        self.assertEqual(current["structure_agent"]["revision_chain"], [])

    def test_ai_optimization_stale_result_never_overwrites_user_revision(
        self,
    ) -> None:
        user_revision_applied = Event()

        def before_repair() -> None:
            main.apply_document_structure_patch(
                self.project["id"],
                self.document["id"],
                self._patch_request(),
                Response(),
            )
            user_revision_applied.set()

        provider = self._optimizer_provider(before_repair=before_repair)
        with patch.object(
            main,
            "_build_structure_optimizer_provider",
            return_value=provider,
        ):
            main.start_document_structure_optimization(
                self.project["id"],
                self.document["id"],
                Response(),
            )
            terminal = self._await_structure_optimization()

        self.assertTrue(user_revision_applied.is_set())
        self.assertEqual(terminal["job"]["optimization_status"], "STALE")
        self.assertEqual(
            terminal["job"]["result"]["reason_code"],
            "stale_before_apply",
        )
        current = main.get_document_structure_agent(
            self.project["id"],
            self.document["id"],
            Response(),
        )
        self.assertEqual(
            current["structure_agent"]["effective_structure_identity"][
                "revision_number"
            ],
            1,
        )
        self.assertEqual(
            current["structure_workbench"]["revisions"][-1]["actor_type"],
            "HUMAN",
        )

    def test_duplicate_ai_optimization_start_reuses_same_job(self) -> None:
        overview_started = Event()
        release_overview = Event()

        def hold_overview(request):
            overview_started.set()
            self.assertTrue(release_overview.wait(timeout=2.0))
            return ()

        provider = _FakeOptimizerProvider(
            overview_builder=hold_overview,
            repair_builder=None,
        )
        with patch.object(
            main,
            "_build_structure_optimizer_provider",
            return_value=provider,
        ):
            first = main.start_document_structure_optimization(
                self.project["id"],
                self.document["id"],
                Response(),
            )
            self.assertTrue(overview_started.wait(timeout=2.0))
            second = main.start_document_structure_optimization(
                self.project["id"],
                self.document["id"],
                Response(),
            )
            release_overview.set()
            terminal = self._await_structure_optimization()

        self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])
        self.assertEqual(terminal["job"]["optimization_status"], "NO_CHANGE")
        self.assertEqual(provider.calls, ["overview_structure"])

    def test_legacy_fixture_patch_and_undo_shapes_remain_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            main.StructurePatchRequest.model_validate(
                {
                    "schema_version": "structure-patch/v0",
                    "expected_structure_revision": 0,
                    "operation": "MOVE",
                    "node_id": "node-a",
                    "target_node_id": "node-b",
                    "placement": "inside",
                }
            )
        with self.assertRaises(ValidationError):
            main.StructureUndoRequest.model_validate(
                {
                    "schema_version": "structure-patch/v0",
                    "expected_structure_revision": 1,
                    "operation": "UNDO",
                    "target_revision_id": "revision-1",
                }
            )

    def test_legacy_status_response_shape_remains_compatible(self) -> None:
        response = main._build_llm_extraction_job_response(
            self.project["id"],
            self.document["id"],
            job=main._LLM_EXTRACTION_JOBS[self.document["id"]],
        )

        self.assertEqual(
            set(response),
            {
                "project_id",
                "document_id",
                "job",
                "result",
                "partial_result",
                "visual_repair",
            },
        )
        self.assertIsNone(response["visual_repair"])
        self.assertIsNone(response["partial_result"])
        self.assertEqual(response["result"]["serving_impact"], "NONE")

    def _patch_request(self) -> main.StructurePatchRequest:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        new_parent = self.sections_by_label["1.2"]
        effective = self.base_repair.effective_structure
        return main.StructurePatchRequest(
            source_ir_id=self.source_ir.source_ir_id,
            source_sha256=self.source_ir.source_sha256,
            source_ir_adapter_version=self.source_ir.adapter_version,
            base_structure_snapshot_id=self.base_tree.structure_snapshot_id,
            base_structure_canonical_sha256=(effective.base_structure_canonical_sha256),
            structure_resolver_version=self.base_tree.resolver_version,
            expected_revision_number=effective.revision_number,
            expected_effective_snapshot_id=effective.effective_snapshot_id,
            operations=(
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=new_parent.structure_node_id,
                    new_sibling_index=0,
                ),
            ),
        )

    def _structure_inspection_payload(
        self,
        *,
        structural_candidate_count: int,
        document_capture_gap_count: int = 0,
    ) -> dict[str, object]:
        candidates = [
            {
                "candidate_id": (
                    "structure-inspection-candidate-" + f"{index + 1:032x}"
                ),
                "category": "OUTLINE_PARENT_MISMATCH",
                "source_ref": {
                    "kind": "SOURCE_NODE",
                    "source_id": self.source_ir.nodes[0].source_node_id,
                },
                "structure_node_id": self.base_tree.nodes[0].structure_node_id,
                "start_offset": 0,
                "end_offset": 3,
                "page_number": None,
                "bbox": None,
                "confidence": "HIGH",
                "disposition": "ACTIONABLE",
                "safe_marker": "1.1",
                "reason_codes": ["explicit_parent_mismatch"],
            }
            for index in range(structural_candidate_count)
        ]
        observed_actionable = structural_candidate_count + document_capture_gap_count
        return {
            "schema_version": "structure-inspection/v1",
            "inspection_id": "structure-inspection-" + "a" * 32,
            "canonical_sha256": "b" * 64,
            "source_ir_id": self.source_ir.source_ir_id,
            "source_sha256": self.source_ir.source_sha256,
            "structure_snapshot_id": self.base_tree.structure_snapshot_id,
            "resolver_id": self.base_tree.resolver_id,
            "resolver_version": self.base_tree.resolver_version,
            "basis": "BASE_STRUCTURE",
            "basis_revision": 0,
            "effective_revision": 0,
            "stale": False,
            "status": (
                "REQUIRES_REEXTRACTION"
                if document_capture_gap_count
                else "REVIEW_REQUIRED"
                if structural_candidate_count
                else "COMPLETE"
            ),
            "counts": {
                "inspected_source_node_count": len(self.source_ir.nodes),
                "grounded_source_node_count": len(self.source_ir.nodes),
                "candidate_count": observed_actionable,
                "observed_actionable_candidate_count": observed_actionable,
                "observed_structural_actionable_candidate_count": (
                    structural_candidate_count
                ),
                "observed_document_capture_gap_count": (document_capture_gap_count),
                "observed_ungrounded_source_fact_count": 0,
                "actionable_candidate_count": observed_actionable,
                "structural_actionable_candidate_count": (structural_candidate_count),
                "capture_gap_count": document_capture_gap_count,
                "ungrounded_source_fact_count": 0,
                "suppressed_candidate_count": 0,
                "by_category_and_disposition": {
                    "OUTLINE_PARENT_MISMATCH:ACTIONABLE": (structural_candidate_count)
                },
            },
            "actionable_candidates": candidates,
        }

    def _optimizer_provider(
        self,
        *,
        before_repair=None,
    ) -> _FakeOptimizerProvider:
        def overview_builder(request):
            target = next(
                node
                for node in request.nodes
                if "Alpha one. Alpha two." in node.text_preview
            )
            new_parent = next(
                node for node in request.nodes if node.explicit_label == "1.2"
            )
            return (
                StructureOptimizationIssueV1(
                    category=(StructureOptimizationIssueCategory.MISPLACED_PARENT),
                    target_structure_node_id=target.structure_node_id,
                    related_structure_node_ids=(new_parent.structure_node_id,),
                    evidence_source_refs=target.source_refs,
                    origin=StructureOptimizationIssueOrigin.BASE_RESOLVER,
                    confidence=StructureOptimizationConfidence.HIGH,
                    reason_codes=("synthetic_parent_conflict",),
                ),
            )

        def repair_builder(request):
            if before_repair is not None:
                before_repair()
            issue = request.overview_issues[0]
            return (
                ReparentStructureOperationV1(
                    structure_node_id=issue.target_structure_node_id,
                    new_parent_structure_node_id=(issue.related_structure_node_ids[0]),
                    new_sibling_index=0,
                ),
            )

        return _FakeOptimizerProvider(
            overview_builder=overview_builder,
            repair_builder=repair_builder,
        )

    def _await_structure_optimization(self) -> dict[str, object]:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            status = main.get_document_structure_optimization_status(
                self.project["id"],
                self.document["id"],
                Response(),
            )
            if status["job"]["status"] in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            }:
                return status
            time.sleep(0.01)
        self.fail("Structure optimization did not reach a terminal state.")

    def _assert_workbench_identity(
        self,
        workbench: dict[str, object],
        *,
        revision: int,
    ) -> None:
        source = workbench["source"]
        structure = workbench["structure"]
        actions = workbench["actions"]
        self.assertEqual(source["source_ir_id"], self.source_ir.source_ir_id)
        self.assertEqual(source["source_sha256"], self.source_ir.source_sha256)
        self.assertEqual(
            source["source_ir_adapter_version"],
            self.source_ir.adapter_version,
        )
        self.assertEqual(
            structure["base_snapshot_id"],
            self.base_tree.structure_snapshot_id,
        )
        self.assertEqual(
            structure["base_canonical_sha256"],
            self.base_repair.effective_structure.base_structure_canonical_sha256,
        )
        self.assertEqual(
            structure["resolver_version"],
            self.base_tree.resolver_version,
        )
        self.assertEqual(structure["revision"], revision)
        self.assertRegex(
            structure["effective_snapshot_id"],
            r"^effective-structure-[0-9a-f]{32}$",
        )
        base_url = (
            f"/api/projects/{self.project['id']}/sor-documents/"
            f"{self.document['id']}/extraction/structure"
        )
        self.assertEqual(actions["commit_url"], f"{base_url}/patch")
        self.assertEqual(actions["undo_url"], f"{base_url}/undo")

    def _seed_completed(self, result: dict[str, object]) -> None:
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                **self._job_base(),
                "status": "COMPLETED",
                "stage": "completed",
                "result": result,
                "partial_result": None,
            }

    def _durable_result_job(self, result: dict[str, object]) -> SimpleNamespace:
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            job_id="llm-extraction-" + "b" * 32,
            status=JobStatus.SUCCEEDED,
            payload={
                "project_id": self.project["id"],
                "document_id": self.document["id"],
                "source_sha256": self.source_ir.source_sha256,
            },
            progress={},
            result=copy.deepcopy(result),
            error_code="",
            created_at=now,
            updated_at=now,
        )

    def _job_base(self) -> dict[str, object]:
        return {
            "job_id": "llm-extraction-" + "a" * 32,
            "project_id": self.project["id"],
            "document_id": self.document["id"],
            "source_sha256": self.source_ir.source_sha256,
            "stage": "completed",
            "error_code": "",
        }

    def _rebuild_context(
        self,
        *,
        document: dict[str, object],
        agent: dict[str, object],
    ):
        self.assertEqual(document["id"], self.document["id"])
        raw_revisions = agent.get("revision_chain")
        revisions = tuple(
            StructureRevisionV1.model_validate(item)
            for item in (raw_revisions if isinstance(raw_revisions, list) else ())
        )
        validate_structure_revision_chain(
            self.source_ir,
            self.base_tree,
            revisions,
        )
        return self.source_ir, self.base_tree, revisions


if __name__ == "__main__":
    unittest.main()
