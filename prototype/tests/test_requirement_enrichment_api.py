from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Response

from app.application.errors import ApplicationError as HTTPException

from app import db
from app.adapters.storage.local_object_store import LocalObjectStore
if __package__:
    from .application_under_test import main
else:
    from application_under_test import main
from app.main import app as http_app

from app.application.extraction.llm_requirement_review import (
    evaluate_llm_requirement_review,
    initial_llm_requirement_review,
)
from app.application.extraction.requirement_enrichment import (
    build_requirement_enrichment_lineage,
    build_requirement_atomization_run,
    build_requirement_publication_preview,
)
from app.application.extraction.serialize_structure_workbench import (
    _serialize_enrichment_workflow,
)
from app.application.extraction.requirement_tree import (
    build_requirement_tree_snapshot,
)
from app.application.extraction.requirement_tree_snapshots import (
    load_requirement_set_tree_snapshot,
    persist_requirement_tree_snapshot,
)
from app.application.runtime import RUNTIME_STATE
from app.domain.extraction.requirement_enrichment import (
    RequirementPublicationEntry,
    RequirementPublicationEntryKind,
    RequirementPublicationPreview,
    stable_publication_key,
    stable_publication_preview_id,
)
from app.domain.extraction.requirement_tree import (
    RequirementTreeMode,
    RequirementTreeState,
)
from app.domain.extraction.requirement_view import RequirementDecisionMode
from app.domain.extraction.semantic_provider import stable_semantic_provider_run_id
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderError,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationProviderResult,
)
from app.services.llm_extraction_result_cache import LlmExtractionResultCache
from tests.test_llm_requirement_review import _prediction
from tests.test_requirement_enrichment import (
    _AtomizationProvider,
    _RefinementProvider,
    _coverage,
    _session_with_split_direct_nodes,
)


main.app = http_app


_TEST_STRUCTURE_ROOT_ID = "structure-node-" + "9" * 32


def _persist_test_requirement_tree_snapshot(
    *,
    project_id: str,
    document_id: str,
    requirement_set_id: str,
    expected_source_sha256: str,
    expected_structure_snapshot_id: str,
    **_kwargs,
):
    requirements = db.list_requirements(
        project_id,
        document_id,
        requirement_set_id=requirement_set_id,
    )
    workbench = {
        "project_id": project_id,
        "document_id": document_id,
        "source": {
            "project_id": project_id,
            "document_id": document_id,
            "source_ir_id": "source-ir-enrichment-test",
            "source_sha256": expected_source_sha256,
            "preview_generation_id": "enrichment-test-preview",
        },
        "agent": {"status": "COMPLETED", "result_state": "STABLE"},
        "structure": {
            "base_snapshot_id": expected_structure_snapshot_id,
            "effective_snapshot_id": expected_structure_snapshot_id,
            "effective_tree_sha256": "d" * 64,
            "revision": 0,
            "nodes": [
                {
                    "id": _TEST_STRUCTURE_ROOT_ID,
                    "parent_id": "",
                    "kind": "DOCUMENT",
                    "order": 0,
                    "label": "测试需求结构",
                    "source_refs": [],
                }
            ],
        },
    }
    entries = [
        {
            **row,
            "requirement_entry_id": row["id"],
            "owner_structure_node_id": _TEST_STRUCTURE_ROOT_ID,
            "source_order": index + 1,
            "source_refs": [
                {
                    "kind": "TEXT_RANGE",
                    "source_id": f"source-{index + 1}",
                    "owner_structure_node_id": _TEST_STRUCTURE_ROOT_ID,
                    "start_offset": 0,
                    "end_offset": max(
                        len(str(row.get("display_text") or "")),
                        1,
                    ),
                }
            ],
        }
        for index, row in enumerate(requirements)
    ]
    snapshot = build_requirement_tree_snapshot(
        workbench=workbench,
        mode=RequirementTreeMode.REQUIREMENT_SET,
        requirement_set_id=requirement_set_id,
        requirement_entries=entries,
        forced_state=RequirementTreeState.READY,
    )
    persist_requirement_tree_snapshot(
        requirement_set_id=requirement_set_id,
        snapshot=snapshot,
    )
    return snapshot


class _RecordingAtomizationProvider(_AtomizationProvider):
    def __init__(self, *, fail_review_node_id: str | None = None) -> None:
        super().__init__()
        self.fail_review_node_id = fail_review_node_id
        self.request_review_node_ids: list[str] = []
        self.failed = False

    def atomize(
        self,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult:
        self.request_review_node_ids.append(request.review_node_id)
        if request.review_node_id == self.fail_review_node_id and not self.failed:
            self.failed = True
            raise RuntimeError("synthetic atomization interruption")
        result = super().atomize(request)
        request_sha256 = hashlib.sha256(
            request.review_node_id.encode("utf-8")
        ).hexdigest()
        response_sha256 = hashlib.sha256(
            f"response:{request.review_node_id}".encode("utf-8")
        ).hexdigest()
        run = result.run.model_copy(
            update={
                "provider_run_id": stable_semantic_provider_run_id(
                    operation=result.run.operation,
                    provider_id=result.run.provider_id,
                    model_id=result.run.model_id,
                    request_sha256=request_sha256,
                    response_sha256=response_sha256,
                    status=result.run.status,
                    started_at=result.run.started_at,
                ),
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
            }
        )
        return result.model_copy(update={"run": run})


def _multi_group_publication_preview(
    base: RequirementPublicationPreview,
) -> RequirementPublicationPreview:
    source_spans = base.entries[0].source_spans
    specs = (
        ("AI-7000", "HEADING", 10, None, "Group A"),
        ("AI-7001", "REQUIREMENT", 11, 1, "Group A child one"),
        ("AI-7002", "REQUIREMENT", 12, 1, "Group A child two"),
        ("AI-1000", "REQUIREMENT", 20, None, "Independent top-level X"),
        ("AI-8000", "HEADING", 30, None, "Group B"),
        ("AI-8001", "REQUIREMENT", 31, 5, "Group B child one"),
        ("AI-0500", "REQUIREMENT", 40, None, "Independent top-level Y"),
    )
    entries: list[RequirementPublicationEntry] = []
    entry_id_by_position = {
        position: f"requirement-publication-entry-{position:032x}"
        for position in range(1, len(specs) + 1)
    }
    for position, (
        req_code,
        kind,
        source_order,
        parent_position,
        statement,
    ) in enumerate(specs, start=1):
        is_heading = kind == "HEADING"
        entries.append(
            RequirementPublicationEntry(
                publication_entry_id=entry_id_by_position[position],
                atomization_node_id=f"atomization-node-{position:032x}",
                atomic_node_id=(
                    None if is_heading else f"atomic-obligation-{position:032x}"
                ),
                parent_publication_entry_id=(
                    entry_id_by_position[parent_position]
                    if parent_position is not None
                    else None
                ),
                review_node_id=f"review-node-{position}",
                entry_kind=(
                    RequirementPublicationEntryKind.HEADING
                    if is_heading
                    else RequirementPublicationEntryKind.REQUIREMENT
                ),
                req_code=req_code,
                requirement_type="FUNCTIONAL",
                decision_mode=(
                    RequirementDecisionMode.ROLLUP
                    if is_heading
                    else RequirementDecisionMode.DIRECT
                ),
                original_statement=statement,
                published_statement=statement,
                source_spans=source_spans,
                source_order=source_order,
            )
        )
    frozen_entries = tuple(entries)
    publication_key = stable_publication_key(
        lineage_sha256=base.lineage_sha256,
        entries=frozen_entries,
    )
    return RequirementPublicationPreview(
        preview_id=stable_publication_preview_id(
            publication_key=publication_key,
        ),
        project_id=base.project_id,
        document_id=base.document_id,
        session_id=base.session_id,
        atomization_run_id=base.atomization_run_id,
        atomization_revision=base.atomization_revision,
        base_active_requirement_set_id=base.base_active_requirement_set_id,
        lineage_sha256=base.lineage_sha256,
        publication_key=publication_key,
        refinement_bindings=base.refinement_bindings,
        entries=frozen_entries,
        created_at_utc=base.created_at_utc,
    )


class RequirementEnrichmentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        self.exit_stack.enter_context(patch.object(main, "STORAGE_DIR", self.root))
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()
        db.init_db()
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        self.project = db.create_project(
            name="Enrichment API",
            customer_name="Customer",
        )
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name=source.name,
            file_type="pdf",
            file_path=str(source),
            extracted_text="",
            source_sha256="b" * 64,
            source_size=source.stat().st_size,
            declared_extension=".pdf",
            detected_format="pdf",
            preflight_schema_version="document-preflight/v1",
        )
        self.document = db.update_sor_document_preview_state(
            self.document["id"],
            preview_generation_id="enrichment-test-preview",
        )
        active = db.create_requirement_set(
            self.project["id"],
            self.document["id"],
            source_hash="b" * 64,
            preview_generation_id="enrichment-test-preview",
        )
        self.active = db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            active["id"],
        )
        prediction = _prediction()
        reviewed = initial_llm_requirement_review(prediction)
        bound = main._bind_llm_requirement_review_session(
            project_id=self.project["id"],
            document_id=self.document["id"],
            shadow={
                "llm_review": {
                    "model_prediction": prediction.model_dump(mode="json"),
                    "reviewed_result": reviewed.model_dump(mode="json"),
                    "delta": evaluate_llm_requirement_review(
                        prediction,
                        reviewed,
                    ).model_dump(mode="json"),
                }
            },
        )
        self.session_id = bound["llm_review"]["persistence"]["session_id"]

    def tearDown(self) -> None:
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()
        self.exit_stack.close()
        self.temporary.cleanup()

    def test_full_flow_publishes_idempotent_non_active_draft(self) -> None:
        review_repository = main._llm_requirement_review_repository()
        enrichment_repository = main._requirement_enrichment_repository()
        self.exit_stack.enter_context(
            patch.object(
                RUNTIME_STATE,
                "container",
                SimpleNamespace(
                    object_store=LocalObjectStore(self.root / "objects"),
                    llm_review_repository=review_repository,
                    requirement_enrichment_repository=enrichment_repository,
                ),
            )
        )
        self.exit_stack.enter_context(
            patch(
                "app.application.extraction.requirement_tree_snapshots."
                "persist_published_requirement_tree_snapshot",
                side_effect=_persist_test_requirement_tree_snapshot,
            )
        )
        session = main._llm_requirement_review_repository().get(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session_id,
        )
        assert session is not None
        self.exit_stack.enter_context(
            patch.object(
                main,
                "_load_current_requirement_enrichment_coverage",
                return_value=_coverage(session),
            )
        )
        atomization_provider = _AtomizationProvider()
        self.exit_stack.enter_context(
            patch.object(
                main,
                "_build_requirement_atomization_provider",
                return_value=atomization_provider,
            )
        )
        atomized = main.create_requirement_atomization_run(
            self.project["id"],
            self.document["id"],
            self.session_id,
            main.RequirementAtomizationRunRequest(
                expected_review_revision=0,
            ),
            Response(),
        )
        repeated_atomized = main.create_requirement_atomization_run(
            self.project["id"],
            self.document["id"],
            self.session_id,
            main.RequirementAtomizationRunRequest(
                expected_review_revision=0,
            ),
            Response(),
        )
        self.assertEqual(
            repeated_atomized["atomization_run"]["atomization_run_id"],
            atomized["atomization_run"]["atomization_run_id"],
        )
        self.assertEqual(atomization_provider.call_count, 1)
        run = atomized["atomization_run"]
        self.assertEqual(run["provider_runs"][0]["status"], "SUCCESS")
        self.assertEqual(run["provider_runs"][0]["request_count"], 1)
        atomic_node_id = next(
            item["atomic_node_id"]
            for item in run["nodes"]
            if item["kind"] == "ATOMIC_OBLIGATION"
        )
        provider = _RefinementProvider()
        with patch.object(
            main,
            "_build_requirement_refinement_provider",
            return_value=provider,
        ):
            suggested = main.create_requirement_refinement_suggestion(
                self.project["id"],
                self.document["id"],
                self.session_id,
                main.RequirementRefinementSuggestionRequest(
                    expected_review_revision=0,
                    atomization_run_id=run["atomization_run_id"],
                    atomic_node_id=atomic_node_id,
                ),
                Response(),
            )
        suggestion = suggested["refinement_suggestion"]
        accepted = main.decide_requirement_refinement_suggestion(
            self.project["id"],
            self.document["id"],
            self.session_id,
            suggestion["suggestion_id"],
            main.RequirementRefinementDecisionRequest(
                expected_refinement_revision=0,
                operation="ACCEPT",
                decision_note="Use the independently reviewable wording.",
            ),
            Response(),
        )
        self.assertEqual(
            accepted["refinement_suggestion"]["status"],
            "ACCEPTED",
        )
        previewed = main.create_requirement_publication_preview(
            self.project["id"],
            self.document["id"],
            self.session_id,
            main.RequirementPublicationPreviewRequest(
                expected_review_revision=0,
                atomization_run_id=run["atomization_run_id"],
            ),
            Response(),
        )
        preview = previewed["publication_preview"]
        request = main.RequirementPublishDraftRequest(
            expected_review_revision=0,
            atomization_run_id=run["atomization_run_id"],
            preview_id=preview["preview_id"],
            publication_key=preview["publication_key"],
        )
        first = main.publish_requirement_enrichment_draft(
            self.project["id"],
            self.document["id"],
            self.session_id,
            request,
            Response(),
        )
        second = main.publish_requirement_enrichment_draft(
            self.project["id"],
            self.document["id"],
            self.session_id,
            request,
            Response(),
        )

        self.assertTrue(first["requirement_set_created"])
        self.assertFalse(second["requirement_set_created"])
        self.assertEqual(
            first["publication"]["requirement_set_id"],
            second["publication"]["requirement_set_id"],
        )
        published_set_id = first["publication"]["requirement_set_id"]
        document = db.get_sor_document(self.document["id"])
        self.assertEqual(
            document["active_requirement_set_id"],
            self.active["id"],
        )

        selected_workspace = main.get_document_workspace(
            self.project["id"],
            self.document["id"],
            include_preview=False,
            requirement_set_id=published_set_id,
        )
        self.assertEqual(
            selected_workspace["document"]["requirement_set_id"],
            published_set_id,
        )
        self.assertFalse(selected_workspace["document"]["requirement_set_is_active"])
        self.assertEqual(
            selected_workspace["document"]["active_requirement_set_id"],
            self.active["id"],
        )
        self.assertEqual(
            selected_workspace["read_only_reason"],
            "NON_ACTIVE_REQUIREMENT_SET_MUST_BE_ACTIVATED",
        )
        self.assertEqual(len(selected_workspace["requirements"]), 2)
        published_set = db.get_requirement_set(published_set_id)
        self.assertEqual(
            published_set["preview_generation_id"],
            "enrichment-test-preview",
        )
        self.assertIs(
            load_requirement_set_tree_snapshot(published_set).state,
            RequirementTreeState.READY,
        )
        self.assertFalse(selected_workspace["actions"]["can_export_draft"])

        main.activate_document_requirement_set(
            self.project["id"],
            self.document["id"],
            published_set_id,
        )
        editable_workspace = main.get_document_workspace(
            self.project["id"],
            self.document["id"],
            include_preview=False,
            requirement_set_id=published_set_id,
        )
        self.assertTrue(editable_workspace["document"]["requirement_set_is_active"])
        self.assertIsNone(editable_workspace["read_only_reason"])

    def test_terminal_retry_blocks_v1_v2_atomization_and_refinement_before_provider(
        self,
    ) -> None:
        LlmExtractionResultCache(self.root).save(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            payload={
                "source_sha256": self.document["source_sha256"],
                "structure_agent": {"editable": True},
            },
        )
        v1_payload = main.RequirementAtomizationRunRequest(
            expected_review_revision=0,
        )
        v2_payload = main.RequirementAtomizationScopeRunRequest(
            command_id="blocked-selected",
            operation="APPLY",
            scope="SELECTED",
            expected_review_revision=0,
            expected_scope_revision=0,
            expected_entry_classification_snapshot_id="accepted-1",
            expected_entry_classification_revision=1,
            expected_entry_classification_sha256="a" * 64,
            expected_working_tree_id="working-tree-1",
            expected_working_tree_sha256="b" * 64,
            expected_effective_structure_id="effective-structure-1",
            target_review_node_ids=("review-node-1",),
            target_scope_sha256="c" * 64,
        )
        refinement_payload = main.RequirementRefinementSuggestionRequest(
            expected_review_revision=0,
            atomization_run_id="atomization-run-" + "1" * 32,
            atomic_node_id="atomic-obligation-" + "2" * 32,
        )

        for status in ("FAILED", "INTERRUPTED", "CANCELLED"):
            with self.subTest(status=status):
                with main._LLM_EXTRACTION_JOB_LOCK:
                    main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                        "job_id": f"llm-extraction-{status.lower()}",
                        "project_id": self.project["id"],
                        "document_id": self.document["id"],
                        "source_sha256": self.document["source_sha256"],
                        "status": status,
                        "stage": status.lower(),
                        "result": None,
                        "partial_result": None,
                    }
                with (
                    patch.object(
                        main,
                        "_build_requirement_atomization_execution_provider",
                        side_effect=AssertionError("atomization provider started"),
                    ) as atomization_provider,
                    patch.object(
                        main,
                        "_build_requirement_refinement_provider",
                        side_effect=AssertionError("refinement provider started"),
                    ) as refinement_provider,
                    self.assertRaises(HTTPException) as v1_blocked,
                ):
                    main.create_requirement_atomization_run(
                        self.project["id"],
                        self.document["id"],
                        self.session_id,
                        v1_payload,
                        Response(),
                    )
                with self.assertRaises(HTTPException) as v2_blocked:
                    main.create_requirement_atomization_run(
                        self.project["id"],
                        self.document["id"],
                        self.session_id,
                        v2_payload,
                        Response(),
                    )
                with self.assertRaises(HTTPException) as refinement_blocked:
                    main.create_requirement_refinement_suggestion(
                        self.project["id"],
                        self.document["id"],
                        self.session_id,
                        refinement_payload,
                        Response(),
                    )

                atomization_provider.assert_not_called()
                refinement_provider.assert_not_called()
                for caught in (
                    v1_blocked.exception,
                    v2_blocked.exception,
                    refinement_blocked.exception,
                ):
                    self.assertEqual(caught.status_code, 409)
                    self.assertEqual(
                        caught.detail["code"],
                        "structure_result_read_only",
                    )
                    self.assertEqual(
                        caught.detail["read_only_reason"],
                        "structure_derivation_rerun_not_complete",
                    )
                self.assertIsNone(
                    main._requirement_enrichment_repository().get_latest_atomization(
                        project_id=self.project["id"],
                        document_id=self.document["id"],
                        session_id=self.session_id,
                    )
                )

    def test_unconfigured_remote_provider_fails_closed_without_local_result(
        self,
    ) -> None:
        session = main._llm_requirement_review_repository().get(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session_id,
        )
        assert session is not None
        unavailable = HTTPException(
            status_code=503,
            detail={
                "code": "llm_extraction_provider_not_configured",
                "message": "Remote atomization is not configured.",
            },
        )
        self.exit_stack.enter_context(
            patch.object(
                main,
                "_load_current_requirement_enrichment_coverage",
                return_value=_coverage(session),
            )
        )
        self.exit_stack.enter_context(
            patch.object(
                main,
                "_build_requirement_atomization_provider",
                side_effect=unavailable,
            )
        )
        with self.assertRaises(HTTPException) as caught:
            main.create_requirement_atomization_run(
                self.project["id"],
                self.document["id"],
                self.session_id,
                main.RequirementAtomizationRunRequest(
                    expected_review_revision=0,
                ),
                Response(),
            )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["message"],
            "Remote atomization is not configured.",
        )
        self.assertEqual(
            db.get_sor_document(self.document["id"])[
                "active_requirement_set_id"
            ],
            self.active["id"],
        )

    def test_configured_provider_failure_does_not_fall_back_locally(self) -> None:
        session = main._llm_requirement_review_repository().get(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session_id,
        )
        assert session is not None

        class _FailingProvider:
            def atomize(
                self,
                request: RequirementAtomizationProviderRequest,
            ) -> RequirementAtomizationProviderResult:
                raise RequirementAtomizationProviderError(
                    "atomization_provider_timeout",
                    "The configured provider timed out.",
                )

        with (
            patch.object(
                main,
                "_load_current_requirement_enrichment_coverage",
                return_value=_coverage(session),
            ),
            patch.object(
                main,
                "_build_requirement_atomization_provider",
                return_value=_FailingProvider(),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            main.create_requirement_atomization_run(
                self.project["id"],
                self.document["id"],
                self.session_id,
                main.RequirementAtomizationRunRequest(
                    expected_review_revision=0,
                ),
                Response(),
            )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail["code"],
            "atomization_provider_timeout",
        )
        self.assertIsNone(
            main._requirement_enrichment_repository().get_latest_atomization(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session_id,
            )
        )

    def test_selected_draft_preserves_multi_group_publication_order(self) -> None:
        session = main._llm_requirement_review_repository().get(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session_id,
        )
        assert session is not None
        coverage = _coverage(session)
        repository = main._requirement_enrichment_repository()
        run = repository.create_or_get_atomization(
            build_requirement_atomization_run(
                session,
                coverage=coverage,
                provider=_AtomizationProvider(),
            )
        )
        base = build_requirement_publication_preview(
            session=session,
            run=run,
            coverage=coverage,
            refinements=(),
            base_active_requirement_set_id=self.active["id"],
        )
        preview = repository.create_or_get_publication_preview(
            _multi_group_publication_preview(base)
        )
        publication = repository.publish_draft(preview)
        expected_codes = [
            "AI-7000",
            "AI-7001",
            "AI-7002",
            "AI-1000",
            "AI-8000",
            "AI-8001",
            "AI-0500",
        ]

        database_order = [
            row["req_code"]
            for row in db.list_requirements(
                self.project["id"],
                self.document["id"],
                requirement_set_id=publication.requirement_set_id,
            )
        ]
        self.assertNotEqual(database_order, expected_codes)

        workspace = main.get_document_workspace(
            self.project["id"],
            self.document["id"],
            include_preview=False,
            requirement_set_id=publication.requirement_set_id,
        )
        self.assertEqual(
            [row["req_code"] for row in workspace["requirements"]],
            expected_codes,
        )
        self.assertEqual(
            workspace["summary"]["order_source"],
            "enrichment_publication",
        )

    def test_legacy_rows_keep_page_and_natural_anchor_sorting(self) -> None:
        rows = [
            {
                "req_code": "LEG-10",
                "source_page": 3,
                "source_anchor": "Main > 10",
                "requirement_text": "Tenth source clause",
            },
            {
                "req_code": "LEG-2",
                "source_page": 3,
                "source_anchor": "Main > 2",
                "requirement_text": "Second source clause",
            },
            {
                "req_code": "LEG-1",
                "source_page": 4,
                "source_anchor": "Main > 1",
                "requirement_text": "Later page clause",
            },
        ]

        ordered, diagnostics = main._sort_requirements_for_display(rows)

        self.assertEqual(
            [row["req_code"] for row in ordered],
            ["LEG-2", "LEG-10", "LEG-1"],
        )
        self.assertEqual(diagnostics["order_source"], "fallback_page_anchor")

    def test_atomization_rejects_missing_current_coverage(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            main.create_requirement_atomization_run(
                self.project["id"],
                self.document["id"],
                self.session_id,
                main.RequirementAtomizationRunRequest(
                    expected_review_revision=0,
                ),
                Response(),
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "requirement_enrichment_coverage_unavailable",
        )

    def test_atomization_checkpoint_resumes_and_reports_all_progress_states(
        self,
    ) -> None:
        stored_session = main._llm_requirement_review_repository().get(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session_id,
        )
        assert stored_session is not None
        session = _session_with_split_direct_nodes(stored_session, count=3)
        coverage = _coverage(session)
        lineage = build_requirement_enrichment_lineage(
            session,
            coverage=coverage,
        )
        repository = main._requirement_enrichment_repository()
        direct_review_node_ids = tuple(
            item.review_node_id
            for item in session.reviewed_result.nodes
            if item.decision_mode is RequirementDecisionMode.DIRECT
        )

        not_started = main._serialize_requirement_enrichment_state(
            session,
            coverage=coverage,
        )
        self.assertEqual(
            not_started["atomization_progress"],
            {
                "schema_version": "requirement-atomization-progress/v1",
                "state": "NOT_STARTED",
                "completed_direct_node_count": 0,
                "total_direct_node_count": 3,
                "resumable": False,
            },
        )

        first_provider = _RecordingAtomizationProvider(
            fail_review_node_id=direct_review_node_ids[1],
        )
        first_wrapper = main._CheckpointingRequirementAtomizationProvider(
            first_provider,  # type: ignore[arg-type]
            repository,
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session_id,
            review_revision=session.reviewed_result.revision,
            lineage_sha256=lineage.lineage_sha256,
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "synthetic atomization interruption",
        ):
            build_requirement_atomization_run(
                session,
                coverage=coverage,
                provider=first_wrapper,
                max_concurrent_provider_calls=1,
            )
        self.assertEqual(
            first_provider.request_review_node_ids,
            list(direct_review_node_ids[:2]),
        )

        partial = main._serialize_requirement_enrichment_state(
            session,
            coverage=coverage,
        )
        self.assertEqual(partial["atomization_progress"]["state"], "PARTIAL")
        self.assertEqual(
            partial["atomization_progress"]["completed_direct_node_count"],
            1,
        )
        self.assertTrue(partial["atomization_progress"]["resumable"])
        partial_workbench = _serialize_enrichment_workflow(
            project_id=self.project["id"],
            document_id=self.document["id"],
            editable=True,
            state={
                **partial,
                "provider_available": True,
                "atomization_provider_available": True,
            },
        )
        self.assertEqual(
            partial_workbench["workflow"]["atomization"]["state"],
            "QUESTION",
        )
        self.assertTrue(partial_workbench["workflow"]["atomization"]["resumable"])
        self.assertIn(
            "ATOMIZATION_PARTIAL_RESUMABLE",
            [item["code"] for item in partial_workbench["issues"]],
        )
        self.assertIn(
            "atomization_run_url",
            partial_workbench["actions"],
        )

        main._set_requirement_atomization_active(
            lineage.lineage_sha256,
            active=True,
        )
        try:
            running = main._serialize_requirement_enrichment_state(
                session,
                coverage=coverage,
            )
        finally:
            main._set_requirement_atomization_active(
                lineage.lineage_sha256,
                active=False,
            )
        self.assertEqual(running["atomization_progress"]["state"], "RUNNING")
        self.assertTrue(running["atomization_progress"]["resumable"])
        running_workbench = _serialize_enrichment_workflow(
            project_id=self.project["id"],
            document_id=self.document["id"],
            editable=True,
            state={
                **running,
                "provider_available": True,
                "atomization_provider_available": True,
            },
        )
        self.assertEqual(
            running_workbench["workflow"]["atomization"]["state"],
            "RUNNING",
        )
        self.assertNotIn(
            "atomization_run_url",
            running_workbench["actions"],
        )

        second_provider = _RecordingAtomizationProvider()
        second_wrapper = main._CheckpointingRequirementAtomizationProvider(
            second_provider,  # type: ignore[arg-type]
            repository,
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session_id,
            review_revision=session.reviewed_result.revision,
            lineage_sha256=lineage.lineage_sha256,
        )
        run = repository.create_or_get_atomization(
            build_requirement_atomization_run(
                session,
                coverage=coverage,
                provider=second_wrapper,
                max_concurrent_provider_calls=1,
            )
        )
        self.assertEqual(
            second_provider.request_review_node_ids,
            list(direct_review_node_ids[1:]),
        )
        self.assertEqual(len(run.atomic_nodes), 3)
        self.assertEqual(
            repository.count_atomization_checkpoints(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session_id,
                review_revision=session.reviewed_result.revision,
                lineage_sha256=lineage.lineage_sha256,
            ),
            3,
        )

        completed = main._serialize_requirement_enrichment_state(
            session,
            coverage=coverage,
        )
        self.assertEqual(
            completed["atomization_progress"],
            {
                "schema_version": "requirement-atomization-progress/v1",
                "state": "COMPLETED",
                "completed_direct_node_count": 3,
                "total_direct_node_count": 3,
                "resumable": False,
            },
        )
        completed_workbench = _serialize_enrichment_workflow(
            project_id=self.project["id"],
            document_id=self.document["id"],
            editable=True,
            state={
                **completed,
                "provider_available": True,
                "atomization_provider_available": True,
            },
        )
        self.assertEqual(
            completed_workbench["workflow"]["atomization"]["state"],
            "COMPLETED",
        )
        self.assertEqual(
            completed_workbench["workflow"]["atomization"][
                "completed_direct_node_count"
            ],
            3,
        )
        self.assertFalse(completed_workbench["workflow"]["atomization"]["resumable"])

    def test_atomization_rejects_unresolved_coverage(self) -> None:
        session = main._llm_requirement_review_repository().get(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session_id,
        )
        assert session is not None
        with (
            patch.object(
                main,
                "_load_current_requirement_enrichment_coverage",
                return_value=_coverage(session, unresolved=True),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            main.create_requirement_atomization_run(
                self.project["id"],
                self.document["id"],
                self.session_id,
                main.RequirementAtomizationRunRequest(
                    expected_review_revision=0,
                ),
                Response(),
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "requirement_enrichment_coverage_incomplete",
        )

    def test_coverage_parser_rejects_count_or_runtime_drift(self) -> None:
        session = main._llm_requirement_review_repository().get(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session_id,
        )
        assert session is not None
        coverage = _coverage(session)
        summary = {
            "total_units": coverage.total_units,
            "blocked_unit_count": coverage.blocked_unit_count,
            "terminal_receipt_count": len(coverage.terminal_receipts),
            "unresolved_unit_count": coverage.unresolved_unit_count,
            "planning_runtime_version": coverage.planning_runtime_version,
            "classifier_runtime_version": coverage.classifier_runtime_version,
            "assembler_runtime_version": coverage.assembler_runtime_version,
            "document_runtime_version": coverage.document_runtime_version,
        }
        result = {
            "semantic_coverage": summary,
            "semantic_terminal_receipts": [
                item.model_dump(mode="json") for item in coverage.terminal_receipts
            ],
            "pipeline": {
                "semantic": {
                    key: value
                    for key, value in summary.items()
                    if key.endswith("_runtime_version")
                }
            },
        }
        parsed = main._parse_requirement_enrichment_coverage(result)
        self.assertEqual(parsed.coverage_sha256, coverage.coverage_sha256)

        with self.assertRaisesRegex(ValueError, "count_mismatch"):
            main._parse_requirement_enrichment_coverage(
                {
                    **result,
                    "semantic_coverage": {
                        **summary,
                        "terminal_receipt_count": len(coverage.terminal_receipts) + 1,
                    },
                }
            )
        with self.assertRaisesRegex(ValueError, "runtime_mismatch"):
            main._parse_requirement_enrichment_coverage(
                {
                    **result,
                    "semantic_coverage": {
                        **summary,
                        "classifier_runtime_version": "drifted-runtime",
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
