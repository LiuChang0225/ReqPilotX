from __future__ import annotations

# Test runtime isolation must be activated before importing project modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from scripts.document_requirement_agent_e2e import (  # noqa: E402
    DocumentTarget,
    GateFailure,
    _assert_receipt_text_free,
    _repeatability,
    main,
    run_acceptance,
)


PROJECT_ID = "project-e2e"
DOCUMENT_ID = "document-e2e"
LOGICAL_ID = "DOC-PDS-OIL-PUMP"
SOURCE_SHA256 = "a" * 64
DEFAULT_MANIFEST = PROTOTYPE_ROOT / "eval/public/document_e2e_manifest.json"
ACTIVE_SET_ID = "requirement-set-active"
DRAFT_SET_ID = "requirement-set-draft"
SESSION_ID = "llm-review-" + "1" * 32
PREDICTION_ID = "llm-prediction-" + "2" * 32
STRUCTURE_ID = "effective-structure-" + "3" * 32
RUN_ID = "atomization-run-" + "4" * 32
ATOMIC_ID_A = "atomic-obligation-" + "5" * 32
ATOMIC_ID_B = "atomic-obligation-" + "6" * 32
ATOM_NODE_A = "atomization-node-" + "7" * 32
ATOM_NODE_B = "atomization-node-" + "8" * 32
SUGGESTION_ID = "refinement-suggestion-" + "9" * 32
PREVIEW_ID = "requirement-publication-preview-" + "a" * 32
ENTRY_ID_A = "requirement-publication-entry-" + "b" * 32
ENTRY_ID_B = "requirement-publication-entry-" + "c" * 32
UNIT_ID = "semantic-unit-" + "d" * 32
OWNER_ID = "structure-" + "e" * 32
COVERAGE_SHA256 = "f" * 64
LINEAGE_SHA256 = "1" * 64
PUBLICATION_KEY = "2" * 64
SOURCE_STATEMENT = "Component shall detect A and report B."
SPLIT_OFFSET = SOURCE_STATEMENT.index(" and ") + 5
ATOMIC_STATEMENT_A = "The component shall detect A."
ATOMIC_STATEMENT_B = "The component shall report B."
REFINED_STATEMENT = "The component shall detect condition A."


class _FakeTransport:
    """Stateful, source-text-bearing fake; only its receipt may leave the runner."""

    def __init__(
        self,
        *,
        transient_atomization_failure: bool = False,
        blocked_coverage: bool = False,
        active_drift: bool = False,
        second_publish_mismatch: bool = False,
        publication_span_mismatch: bool = False,
        top_status: str = "READY",
        receipt_provider_version: str = "provider/v1",
        runtime_ready: bool = True,
        llm_ready: bool = True,
    ) -> None:
        self.transient_atomization_failure = transient_atomization_failure
        self.blocked_coverage = blocked_coverage
        self.active_drift = active_drift
        self.second_publish_mismatch = second_publish_mismatch
        self.publication_span_mismatch = publication_span_mismatch
        self.top_status = top_status
        self.receipt_provider_version = receipt_provider_version
        self.runtime_ready = runtime_ready
        self.llm_ready = llm_ready
        self.calls: list[tuple[str, str]] = []
        self.atomization_post_count = 0
        self.publish_count = 0
        self.refinement_status = "DRAFT"
        self.refinement_revision = 0
        self.atomization_complete = False

    @property
    def root(self) -> str:
        return f"/api/projects/{PROJECT_ID}/sor-documents/{DOCUMENT_ID}"

    @property
    def review_root(self) -> str:
        return f"{self.root}/extraction/llm-reviews/{SESSION_ID}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> Any:
        del query
        method = method.upper()
        self.calls.append((method, path))
        if method == "GET" and path == "/api/runtime-readiness":
            return {
                "status": "not_ready",
                "ready": False,
                "runtime_ready": self.runtime_ready,
                "checks": {
                    name: {"status": "ok"}
                    for name in ("startup", "storage", "sqlite")
                },
            }
        if method == "GET" and path == "/api/runtime/ai-provider-setup":
            return {
                "profiles": [
                    {
                        "id": "aliyun",
                        "assessment_mode": "llm_shadow",
                        "llm_ready": self.llm_ready,
                        "ready": False,
                        "runtime_status": "error",
                    }
                ]
            }
        if method == "GET" and path == f"/api/projects/{PROJECT_ID}/sor-documents":
            active = (
                "requirement-set-drifted"
                if self.active_drift and self.publish_count >= 2
                else ACTIVE_SET_ID
            )
            return [
                {
                    "id": DOCUMENT_ID,
                    "source_sha256": SOURCE_SHA256,
                    "active_requirement_set_id": active,
                }
            ]
        if (
            method == "POST"
            and path == f"{self.root}/extraction/llm-document/start"
        ):
            assert body == {"visual_provider": "off"}
            return self._completed_extraction()
        if method == "GET" and path == f"{self.root}/extraction/structure":
            return self._structure()
        if method == "GET" and path == self.review_root:
            return {
                "project_id": PROJECT_ID,
                "document_id": DOCUMENT_ID,
                "serving_impact": "NONE",
                "llm_review": self._review(),
            }
        if method == "GET" and path == f"{self.review_root}/enrichment":
            return self._enrichment()
        if method == "POST" and path == f"{self.review_root}/atomization-runs":
            self.atomization_post_count += 1
            if (
                self.transient_atomization_failure
                and self.atomization_post_count == 1
            ):
                raise GateFailure(
                    "synthetic_atomization_interruption",
                    stage="http",
                    http_status=503,
                )
            self.atomization_complete = True
            return {
                "schema_version": "requirement-atomization-run-response/v1",
                "project_id": PROJECT_ID,
                "document_id": DOCUMENT_ID,
                "serving_impact": "NONE",
                "atomization_run": self._atomization_run(),
            }
        if (
            method == "POST"
            and path == f"{self.review_root}/refinement-suggestions"
        ):
            assert body is not None and body["atomic_node_id"] == ATOMIC_ID_A
            return {
                "schema_version": "requirement-refinement-suggestion-response/v1",
                "project_id": PROJECT_ID,
                "document_id": DOCUMENT_ID,
                "serving_impact": "NONE",
                "refinement_suggestion": self._refinement(),
            }
        if (
            method == "PATCH"
            and path
            == f"{self.review_root}/refinement-suggestions/{SUGGESTION_ID}"
        ):
            assert body is not None and body["operation"] == "ACCEPT"
            self.refinement_status = "ACCEPTED"
            self.refinement_revision += 1
            return {
                "schema_version": "requirement-refinement-decision-response/v1",
                "project_id": PROJECT_ID,
                "document_id": DOCUMENT_ID,
                "serving_impact": "NONE",
                "refinement_suggestion": self._refinement(),
            }
        if (
            method == "POST"
            and path == f"{self.review_root}/publication-preview"
        ):
            return {
                "schema_version": (
                    "requirement-enrichment-publication-preview-response/v1"
                ),
                "project_id": PROJECT_ID,
                "document_id": DOCUMENT_ID,
                "serving_impact": "NONE",
                "publication_preview": self._preview(),
            }
        if method == "POST" and path == f"{self.review_root}/publish-draft":
            self.publish_count += 1
            set_id = (
                "requirement-set-unexpected"
                if self.second_publish_mismatch and self.publish_count == 2
                else DRAFT_SET_ID
            )
            return {
                "schema_version": "requirement-enrichment-publish-draft-response/v1",
                "project_id": PROJECT_ID,
                "document_id": DOCUMENT_ID,
                "serving_impact": "DRAFT_ONLY",
                "requirement_set_created": self.publish_count == 1,
                "requirement_set_activated": False,
                "publication": self._publication(set_id),
            }
        if method == "GET" and path == f"{self.root}/requirement-sets":
            return {
                "project_id": PROJECT_ID,
                "document_id": DOCUMENT_ID,
                "active_requirement_set_id": ACTIVE_SET_ID,
                "requirement_sets": [
                    {"id": ACTIVE_SET_ID, "status": "DRAFT"},
                    {"id": DRAFT_SET_ID, "status": "DRAFT"},
                ],
            }
        if method == "GET" and path == f"{self.root}/workspace":
            return self._workspace()
        raise AssertionError(f"unexpected fake request: {method} {path}")

    def _job(self) -> dict[str, Any]:
        return {
            "status": "COMPLETED",
            "stage": "completed",
            "job_id": "llm-extraction-" + "0" * 32,
            "source_sha256": SOURCE_SHA256,
            "progress": {
                "total_clauses": 1,
                "total_targets": 1,
                "total_chunks": 1,
                "completed_chunks": 1,
                "completed_targets": 1,
                "successful_chunks": 1,
                "unresolved_chunks": 0,
                "blocked_chunks": 0,
                "current_chunk_index": None,
                "current_chunk_target_count": 0,
            },
            "error_code": "",
            "error_stage": "",
            "error_cause_code": "",
            "created_at_utc": "2026-07-27T00:00:00+00:00",
            "started_at_utc": "2026-07-27T00:00:01+00:00",
            "finished_at_utc": "2026-07-27T00:00:02+00:00",
        }

    def _visual_strategy(self) -> dict[str, Any]:
        return {
            "schema_version": "extraction-visual-strategy/v1",
            "mode": "OFF",
            "provider_name": "off",
            "provider_id": None,
            "provider_version": None,
            "route_id": None,
            "route_sha256": None,
            "routing_policy_id": None,
            "authorized_page_count": 0,
            "source_ir_adapter_version": "pdf-source-ir/v1",
        }

    def _coverage(self) -> dict[str, Any]:
        blocked = 1 if self.blocked_coverage else 0
        total = 2 if blocked else 1
        return {
            "schema_version": "requirement-enrichment-coverage/v1",
            "total_units": total,
            "blocked_unit_count": blocked,
            "terminal_receipt_count": 1,
            "unresolved_unit_count": 0,
            "planning_runtime_version": "semantic-planning/v1",
            "classifier_runtime_version": "semantic-classifier/v1",
            "assembler_runtime_version": "requirement-assembler/v1",
            "document_runtime_version": "requirement-document/v1",
            "coverage_sha256": COVERAGE_SHA256,
        }

    def _terminal_receipt(self) -> dict[str, Any]:
        return {
            "schema_version": "semantic-unit-terminal-receipt/v1",
            "semantic_unit_id": UNIT_ID,
            "status": "RESOLVED",
            "planning_runtime_version": "semantic-planning/v1",
        }

    def _prediction(self) -> dict[str, Any]:
        return {
            "schema_version": "llm-requirement-prediction/v5",
            "prediction_id": PREDICTION_ID,
            "source_sha256": SOURCE_SHA256,
            "structure_snapshot_id": STRUCTURE_ID,
            "nodes": [
                {
                    "semantic_unit_id": UNIT_ID,
                    "review_text": SOURCE_STATEMENT,
                }
            ],
        }

    def _reviewed(self) -> dict[str, Any]:
        return {
            "schema_version": "llm-requirement-review/v4",
            "prediction_id": PREDICTION_ID,
            "revision": 0,
            "nodes": [
                {
                    "review_node_id": "review-node-1",
                    "include_in_view": True,
                    "semantic_class": "REQUIREMENT",
                    "decision_mode": "DIRECT",
                    "source_spans": [
                        {
                            "semantic_unit_id": UNIT_ID,
                            "owner_structure_node_id": OWNER_ID,
                            "start_offset": 0,
                            "end_offset": len(SOURCE_STATEMENT),
                        }
                    ],
                }
            ],
        }

    def _review(self) -> dict[str, Any]:
        return {
            "schema_version": "llm-requirement-review-session/v4",
            "persistence": {
                "status": "PERSISTED",
                "session_id": SESSION_ID,
                "revision": 0,
            },
            "model_prediction": self._prediction(),
            "reviewed_result": self._reviewed(),
            "delta": {},
        }

    def _result(self) -> dict[str, Any]:
        coverage = self._coverage()
        return {
            "schema_version": "extraction-llm-shadow-preview/v5",
            "status": self.top_status,
            "serving_impact": "NONE",
            "requirement_set_id": None,
            "requirement_set_created": False,
            "requirement_set_activated": False,
            "source_identity_match": True,
            "source_identity": {
                "status": "MATCHED",
                "source_sha256": SOURCE_SHA256,
                "stored_hash_available": True,
                "match": True,
            },
            "visual_strategy": self._visual_strategy(),
            "pipeline": {
                "semantic": {
                    "status": "COMPLETE",
                    "planning_runtime_version": coverage[
                        "planning_runtime_version"
                    ],
                    "classifier_runtime_version": coverage[
                        "classifier_runtime_version"
                    ],
                    "assembler_runtime_version": coverage[
                        "assembler_runtime_version"
                    ],
                    "document_runtime_version": coverage[
                        "document_runtime_version"
                    ],
                }
            },
            "semantic_coverage": coverage,
            "semantic_terminal_receipts": [self._terminal_receipt()],
            "llm_review": self._review(),
            "llm_batch": {
                "provider": {
                    "id": "synthetic-provider",
                    "version": self.receipt_provider_version,
                    "model_id": "synthetic-model",
                }
            },
            "counts": {
                "requirement": 1,
                "needs_classification": 0,
                "information": 0,
                "requirement_groups": 1,
                "review_groups": 1,
                "semantic_annotations": 1,
            },
        }

    def _completed_extraction(self) -> dict[str, Any]:
        return {
            "project_id": PROJECT_ID,
            "document_id": DOCUMENT_ID,
            "job": self._job(),
            "result": self._result(),
            "partial_result": None,
        }

    def _structure(self) -> dict[str, Any]:
        return {
            "project_id": PROJECT_ID,
            "document_id": DOCUMENT_ID,
            "job": self._job(),
            "structure_agent": {"editable": True},
            "serving_impact": "NONE",
            "structure_workbench": {
                "schema_version": "structure-workbench/v0",
                "source": {
                    "source_sha256": SOURCE_SHA256,
                    "source_ir_id": "source-ir-" + "1" * 32,
                    "source_ir_adapter_version": "pdf-source-ir/v1",
                    "page_count": 1,
                },
                "structure": {
                    "state": "STABLE",
                    "editable": True,
                    "base_snapshot_id": "structure-" + "2" * 32,
                    "effective_snapshot_id": STRUCTURE_ID,
                    "effective_tree_sha256": "3" * 64,
                    "revision_chain_sha256": "4" * 64,
                    "revision": 0,
                    "nodes": [{"id": OWNER_ID}],
                },
                "agent": {
                    "status": "COMPLETED",
                    "editable": True,
                },
                "workflow": {
                    "review": {
                        "state": "READY",
                        "session_id": SESSION_ID,
                        "revision": 0,
                    },
                    "coverage": {
                        "state": "READY",
                        "blocked_unit_count": 0,
                        "unresolved_unit_count": 0,
                    },
                },
                "readiness": {
                    "blocking_issue_codes": [],
                    "can_atomize": True,
                },
                "issues": [],
            },
        }

    def _enrichment_coverage(self) -> dict[str, Any]:
        return {
            **self._coverage(),
            "terminal_receipts": [self._terminal_receipt()],
            "eligible": not self.blocked_coverage,
        }

    def _enrichment(self) -> dict[str, Any]:
        if self.atomization_complete:
            state = "COMPLETED"
            completed = 1
            resumable = False
            run: dict[str, Any] | None = self._atomization_run()
        elif self.transient_atomization_failure and self.atomization_post_count:
            state = "PARTIAL"
            completed = 1
            resumable = True
            run = None
        else:
            state = "NOT_STARTED"
            completed = 0
            resumable = False
            run = None
        return {
            "schema_version": "requirement-enrichment-state/v1",
            "project_id": PROJECT_ID,
            "document_id": DOCUMENT_ID,
            "session_id": SESSION_ID,
            "review_revision": 0,
            "coverage": self._enrichment_coverage(),
            "atomization_progress": {
                "schema_version": "requirement-atomization-progress/v1",
                "state": state,
                "completed_direct_node_count": completed,
                "total_direct_node_count": 1,
                "resumable": resumable,
            },
            "atomization_run": run,
        }

    def _atomization_run(self) -> dict[str, Any]:
        return {
            "schema_version": "requirement-atomization-run/v3",
            "atomization_run_id": RUN_ID,
            "revision": 0,
            "algorithm_version": "source-bound-provider-atomization/v3",
            "lineage": {
                "session_id": SESSION_ID,
                "source_sha256": SOURCE_SHA256,
                "review_revision": 0,
                "coverage_sha256": COVERAGE_SHA256,
                "lineage_sha256": LINEAGE_SHA256,
            },
            "nodes": [
                {
                    "node_id": ATOM_NODE_A,
                    "atomic_node_id": ATOMIC_ID_A,
                    "kind": "ATOMIC_OBLIGATION",
                    "parent_node_id": None,
                    "review_node_id": "review-node-1",
                    "source_spans": [
                        {
                            "semantic_unit_id": UNIT_ID,
                            "owner_structure_node_id": OWNER_ID,
                            "start_offset": 0,
                            "end_offset": SPLIT_OFFSET,
                        }
                    ],
                    "source_order": 1,
                    "decision_mode": "DIRECT",
                    "requirement_subtype": "FUNCTIONAL",
                    "source_text": SOURCE_STATEMENT[:SPLIT_OFFSET],
                    "atomic_statement": ATOMIC_STATEMENT_A,
                },
                {
                    "node_id": ATOM_NODE_B,
                    "atomic_node_id": ATOMIC_ID_B,
                    "kind": "ATOMIC_OBLIGATION",
                    "parent_node_id": None,
                    "review_node_id": "review-node-1",
                    "source_spans": [
                        {
                            "semantic_unit_id": UNIT_ID,
                            "owner_structure_node_id": OWNER_ID,
                            "start_offset": SPLIT_OFFSET,
                            "end_offset": len(SOURCE_STATEMENT),
                        }
                    ],
                    "source_order": 1,
                    "decision_mode": "DIRECT",
                    "requirement_subtype": "FUNCTIONAL",
                    "source_text": SOURCE_STATEMENT[SPLIT_OFFSET:],
                    "atomic_statement": ATOMIC_STATEMENT_B,
                },
            ],
            "provider_runs": [
                {
                    "provider_run_id": "semantic-run-" + "1" * 32,
                    "provider_id": "synthetic-provider",
                    "model_id": "synthetic-model",
                    "status": "SUCCESS",
                }
            ],
        }

    def _refinement(self) -> dict[str, Any]:
        return {
            "schema_version": "requirement-refinement-suggestion/v1",
            "suggestion_id": SUGGESTION_ID,
            "project_id": PROJECT_ID,
            "document_id": DOCUMENT_ID,
            "session_id": SESSION_ID,
            "atomization_run_id": RUN_ID,
            "atomization_revision": 0,
            "atomic_node_id": ATOMIC_ID_A,
            "revision": self.refinement_revision,
            "status": self.refinement_status,
            "original_statement": ATOMIC_STATEMENT_A,
            "suggested_statement": REFINED_STATEMENT,
            "provider_id": "synthetic-provider",
            "provider_version": "provider/v1",
            "model_id": "synthetic-model",
            "prompt_version": "refinement-prompt/v1",
            "provider_run_id": "semantic-run-" + "2" * 32,
            "reason_codes": ["clarified_subject"],
            "lineage_sha256": "5" * 64,
        }

    def _preview_entries(self) -> list[dict[str, Any]]:
        common_span = [
            {
                "semantic_unit_id": UNIT_ID,
                "owner_structure_node_id": OWNER_ID,
                "start_offset": 0,
                "end_offset": len(SOURCE_STATEMENT),
            }
        ]
        first_span = [
            {
                "semantic_unit_id": UNIT_ID,
                "owner_structure_node_id": OWNER_ID,
                "start_offset": 0,
                "end_offset": SPLIT_OFFSET,
            }
        ]
        second_span = [
            {
                "semantic_unit_id": UNIT_ID,
                "owner_structure_node_id": OWNER_ID,
                "start_offset": SPLIT_OFFSET,
                "end_offset": len(SOURCE_STATEMENT),
            }
        ]
        if self.publication_span_mismatch:
            first_span = common_span
            second_span = common_span
        return [
            {
                "publication_entry_id": ENTRY_ID_A,
                "atomization_node_id": ATOM_NODE_A,
                "atomic_node_id": ATOMIC_ID_A,
                "parent_publication_entry_id": None,
                "review_node_id": "review-node-1",
                "entry_kind": "REQUIREMENT",
                "requirement_type": "FUNCTIONAL",
                "decision_mode": "DIRECT",
                "req_code": "AI-0001",
                "source_order": 1,
                "published_statement": REFINED_STATEMENT,
                "refinement_suggestion_id": SUGGESTION_ID,
                "refinement_revision": self.refinement_revision,
                "source_spans": first_span,
            },
            {
                "publication_entry_id": ENTRY_ID_B,
                "atomization_node_id": ATOM_NODE_B,
                "atomic_node_id": ATOMIC_ID_B,
                "parent_publication_entry_id": None,
                "review_node_id": "review-node-1",
                "entry_kind": "REQUIREMENT",
                "requirement_type": "FUNCTIONAL",
                "decision_mode": "DIRECT",
                "req_code": "AI-0002",
                "source_order": 1,
                "published_statement": ATOMIC_STATEMENT_B,
                "refinement_suggestion_id": None,
                "refinement_revision": None,
                "source_spans": second_span,
            },
        ]

    def _preview(self) -> dict[str, Any]:
        return {
            "schema_version": "requirement-enrichment-publication-preview/v1",
            "preview_id": PREVIEW_ID,
            "project_id": PROJECT_ID,
            "document_id": DOCUMENT_ID,
            "session_id": SESSION_ID,
            "atomization_run_id": RUN_ID,
            "atomization_revision": 0,
            "base_active_requirement_set_id": ACTIVE_SET_ID,
            "lineage_sha256": LINEAGE_SHA256,
            "publication_key": PUBLICATION_KEY,
            "refinement_bindings": [
                {
                    "suggestion_id": SUGGESTION_ID,
                    "atomic_node_id": ATOMIC_ID_A,
                    "revision": self.refinement_revision,
                    "status": "ACCEPTED",
                    "lineage_sha256": "5" * 64,
                }
            ],
            "entries": self._preview_entries(),
        }

    def _publication(self, requirement_set_id: str) -> dict[str, Any]:
        return {
            "schema_version": "requirement-enrichment-publication-result/v1",
            "project_id": PROJECT_ID,
            "document_id": DOCUMENT_ID,
            "preview_id": PREVIEW_ID,
            "publication_key": PUBLICATION_KEY,
            "lineage_sha256": LINEAGE_SHA256,
            "requirement_set_id": requirement_set_id,
            "requirement_set_version": 2,
            "requirement_set_status": "DRAFT",
            "is_active": False,
            "active_requirement_set_id": ACTIVE_SET_ID,
            "created": self.publish_count == 1,
        }

    def _requirement_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, entry in enumerate(self._preview_entries()):
            rows.append(
                {
                    "req_code": entry["req_code"],
                    "decision_reference": {
                        "schema_version": "requirement-enrichment-trace/v1",
                        "publication_key": PUBLICATION_KEY,
                        "preview_id": PREVIEW_ID,
                        "lineage_sha256": LINEAGE_SHA256,
                        "publication_entry_id": entry["publication_entry_id"],
                        "parent_publication_entry_id": entry[
                            "parent_publication_entry_id"
                        ],
                        "publication_order": index,
                        "source_order": entry["source_order"],
                        "entry_kind": entry["entry_kind"],
                        "atomization_node_id": entry["atomization_node_id"],
                        "atomic_node_id": entry["atomic_node_id"],
                        "review_node_id": entry["review_node_id"],
                        "decision_mode": entry["decision_mode"],
                        "source_spans": entry["source_spans"],
                        "refinement_suggestion_id": entry[
                            "refinement_suggestion_id"
                        ],
                        "refinement_revision": entry["refinement_revision"],
                    },
                }
            )
        return rows

    def _workspace(self) -> dict[str, Any]:
        return {
            "document": {
                "requirement_set_id": DRAFT_SET_ID,
                "requirement_set_is_active": False,
                "active_requirement_set_id": ACTIVE_SET_ID,
            },
            "selected_requirement_set": {
                "id": DRAFT_SET_ID,
                "status": "DRAFT",
                "is_active": False,
            },
            "read_only_reason": "NON_ACTIVE_REQUIREMENT_SET_MUST_BE_ACTIVATED",
            "requirements": self._requirement_rows(),
            "summary": {"order_source": "enrichment_publication"},
            "actions": {
                "can_lock_extraction": False,
                "can_export": False,
                "can_export_draft": False,
                "can_export_formal": False,
                "can_run_ai_analysis": False,
            },
        }


def _run(fake: _FakeTransport, *, rehearsals: int = 1) -> dict[str, Any]:
    return run_acceptance(
        fake,
        base_url="http://127.0.0.1:8765",
        project_id=PROJECT_ID,
        manifest_path=DEFAULT_MANIFEST,
        logical_ids=(LOGICAL_ID,),
        rehearsals=rehearsals,
        poll_interval_seconds=0,
        extraction_timeout_seconds=1,
        atomization_timeout_seconds=1,
        atomization_attempts=4,
    )


class DocumentRequirementAgentE2ETests(unittest.TestCase):
    def test_full_flow_resumes_and_emits_text_free_receipt(self) -> None:
        fake = _FakeTransport(transient_atomization_failure=True)

        receipt = _run(fake)

        self.assertEqual(receipt["status"], "PASS")
        document = receipt["rehearsals"][0]["documents"][0]
        self.assertEqual(document["atomization"]["resume_attempt_count"], 1)
        self.assertEqual(
            document["atomization"]["multi_atom_review_node_count"],
            1,
        )
        self.assertTrue(document["publication"]["idempotent"])
        self.assertFalse(document["publication"]["activated"])
        self.assertTrue(document["selected_draft"]["mutating_actions_disabled"])
        encoded = json.dumps(receipt, ensure_ascii=True)
        for forbidden in (
            SOURCE_STATEMENT,
            ATOMIC_STATEMENT_A,
            ATOMIC_STATEMENT_B,
            REFINED_STATEMENT,
            "source_text",
            "atomic_statement",
            "published_statement",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertFalse(
            any(path.endswith("/activate") for _, path in fake.calls),
        )

    def test_incomplete_coverage_fails_before_enrichment_mutations(self) -> None:
        fake = _FakeTransport(blocked_coverage=True)

        with self.assertRaises(GateFailure) as raised:
            _run(fake)

        self.assertEqual(raised.exception.code, "semantic_coverage_blocked")
        mutation_paths = [
            path
            for method, path in fake.calls
            if method in {"POST", "PATCH"}
            and "llm-document/start" not in path
        ]
        self.assertEqual(mutation_paths, [])

    def test_global_rag_degradation_does_not_block_ready_extraction_runtime(
        self,
    ) -> None:
        fake = _FakeTransport(runtime_ready=True, llm_ready=True)

        receipt = _run(fake)

        self.assertEqual(receipt["status"], "PASS")
        self.assertIn(("GET", "/api/runtime-readiness"), fake.calls)
        self.assertIn(("GET", "/api/runtime/ai-provider-setup"), fake.calls)
        self.assertNotIn(("GET", "/api/health"), fake.calls)
        self.assertNotIn(("GET", "/api/readiness"), fake.calls)

    def test_extraction_runtime_or_semantic_llm_not_ready_fails_before_start(
        self,
    ) -> None:
        for fake, expected_code in (
            (
                _FakeTransport(runtime_ready=False),
                "runtime_readiness_gate_failed",
            ),
            (
                _FakeTransport(llm_ready=False),
                "semantic_llm_not_ready",
            ),
        ):
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(GateFailure) as raised:
                    _run(fake)
                self.assertEqual(raised.exception.code, expected_code)
                self.assertFalse(
                    any(
                        path.endswith("/llm-document/start")
                        for _, path in fake.calls
                    )
                )

    def test_publish_replay_must_reuse_the_same_draft(self) -> None:
        fake = _FakeTransport(second_publish_mismatch=True)

        with self.assertRaises(GateFailure) as raised:
            _run(fake)

        self.assertEqual(raised.exception.code, "publication_not_idempotent")

    def test_active_requirement_set_drift_is_a_hard_failure(self) -> None:
        fake = _FakeTransport(active_drift=True)

        with self.assertRaises(GateFailure) as raised:
            _run(fake)

        self.assertEqual(raised.exception.code, "active_requirement_set_drifted")

    def test_review_required_can_continue_when_exact_gates_are_clear(self) -> None:
        receipt = _run(_FakeTransport(top_status="REVIEW_REQUIRED"))

        document = receipt["rehearsals"][0]["documents"][0]
        self.assertEqual(
            document["extraction"]["result_status"],
            "REVIEW_REQUIRED",
        )
        self.assertEqual(
            document["extraction"]["semantic_coverage"]["blocked_unit_count"],
            0,
        )
        self.assertEqual(
            document["extraction"]["semantic_coverage"]["unresolved_unit_count"],
            0,
        )

    def test_publication_must_preserve_atomization_source_spans(self) -> None:
        with self.assertRaises(GateFailure) as raised:
            _run(_FakeTransport(publication_span_mismatch=True))

        self.assertEqual(
            raised.exception.code,
            "publication_source_spans_mismatch",
        )
        self.assertEqual(raised.exception.stage, "publication_preview")

    def test_two_rehearsals_preserve_input_content_and_shape(self) -> None:
        fake = _FakeTransport()

        receipt = _run(fake, rehearsals=2)

        self.assertEqual(receipt["rehearsal_count"], 2)
        self.assertTrue(receipt["repeatability"]["all_runs_passed"])
        self.assertTrue(
            receipt["repeatability"]["all_runtime_bindings_stable"],
        )
        self.assertTrue(
            receipt["repeatability"]["all_input_bindings_stable"],
        )
        self.assertTrue(
            receipt["repeatability"]["all_content_digests_stable"],
        )
        self.assertTrue(
            receipt["repeatability"]["all_result_shapes_stable"],
        )
        self.assertTrue(
            receipt["repeatability"]["all_publications_idempotent"],
        )
        self.assertEqual(fake.publish_count, 4)

    def test_fresh_audit_ids_and_timestamps_may_vary(self) -> None:
        receipt = _run(_FakeTransport(), rehearsals=2)
        rehearsals = copy.deepcopy(receipt["rehearsals"])
        document = rehearsals[1]["documents"][0]
        document["started_at_utc"] = "2026-07-27T01:00:00+00:00"
        document["finished_at_utc"] = "2026-07-27T01:01:00+00:00"
        document["extraction"]["job_id"] = "llm-extraction-" + "a" * 32
        document["extraction"]["created_at_utc"] = (
            "2026-07-27T01:00:00+00:00"
        )
        document["extraction"]["started_at_utc"] = (
            "2026-07-27T01:00:01+00:00"
        )
        document["extraction"]["finished_at_utc"] = (
            "2026-07-27T01:00:02+00:00"
        )
        review = document["extraction"]["review_session"]
        review["session_id"] = "llm-review-" + "a" * 32
        review["prediction_id"] = "llm-prediction-" + "b" * 32
        review["structure_snapshot_id"] = "effective-structure-" + "c" * 32
        document["atomization"]["atomization_run_id"] = (
            "atomization-run-" + "d" * 32
        )
        document["atomization"]["lineage_sha256"] = "e" * 64
        document["publication_preview"]["preview_id"] = (
            "requirement-publication-preview-" + "f" * 32
        )
        document["publication_preview"]["publication_key"] = "a" * 64
        document["publication_preview"]["lineage_sha256"] = "b" * 64
        document["publication"]["requirement_set_id"] = (
            "requirement-set-fresh"
        )
        document["selected_draft"]["requirement_set_id"] = (
            "requirement-set-fresh"
        )

        repeatability = _repeatability(rehearsals)

        self.assertTrue(repeatability["all_input_bindings_stable"])
        self.assertTrue(repeatability["all_content_digests_stable"])
        self.assertTrue(repeatability["all_result_shapes_stable"])
        self.assertTrue(repeatability["all_publications_idempotent"])
        self.assertTrue(repeatability["audit_instance_ids_varied"])
        self.assertEqual(
            repeatability["documents"][0]["audit_instance_identity_count"],
            2,
        )

    def test_repeatability_rejects_content_digest_drift(self) -> None:
        receipt = _run(_FakeTransport(), rehearsals=2)
        rehearsals = copy.deepcopy(receipt["rehearsals"])
        rehearsals[1]["documents"][0]["publication_preview"][
            "content_sha256"
        ] = "0" * 64

        repeatability = _repeatability(rehearsals)

        self.assertFalse(repeatability["all_content_digests_stable"])
        self.assertFalse(
            repeatability["documents"][0]["content_digest_stable"],
        )

    def test_receipt_rejects_free_form_text_under_an_allowed_key(self) -> None:
        with self.assertRaises(GateFailure) as raised:
            _assert_receipt_text_free(
                {"provider_version": "Customer shall reveal secret text"}
            )

        self.assertEqual(raised.exception.code, "receipt_string_not_text_free")

    def test_failure_preserves_prior_document_receipt_and_stage(self) -> None:
        first = DocumentTarget(
            logical_id=LOGICAL_ID,
            application_document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
            visual_provider="off",
            active_requirement_set_id=ACTIVE_SET_ID,
        )
        second = DocumentTarget(
            logical_id="DOC-F06-ACTIVE-SUSPENSION",
            application_document_id=DOCUMENT_ID,
            source_sha256="0" * 64,
            visual_provider="off",
            active_requirement_set_id=ACTIVE_SET_ID,
        )
        with (
            patch(
                "scripts.document_requirement_agent_e2e.resolve_targets",
                return_value=[first, second],
            ),
            self.assertRaises(GateFailure) as raised,
        ):
            run_acceptance(
                _FakeTransport(),
                base_url="http://127.0.0.1:8765",
                project_id=PROJECT_ID,
                manifest_path=DEFAULT_MANIFEST,
                logical_ids=(LOGICAL_ID, "DOC-F06-ACTIVE-SUSPENSION"),
                rehearsals=1,
                poll_interval_seconds=0,
                extraction_timeout_seconds=1,
                atomization_timeout_seconds=1,
                atomization_attempts=4,
            )

        self.assertEqual(raised.exception.stage, "extraction")
        self.assertEqual(
            raised.exception.logical_document_id,
            "DOC-F06-ACTIVE-SUSPENSION",
        )
        self.assertEqual(len(raised.exception.partial_rehearsals), 1)
        partial = raised.exception.partial_rehearsals[0]
        self.assertEqual(partial["status"], "FAIL")
        self.assertEqual(len(partial["documents"]), 1)
        self.assertEqual(
            partial["documents"][0]["logical_document_id"],
            LOGICAL_ID,
        )

    def test_post_run_repeatability_failure_preserves_completed_results(
        self,
    ) -> None:
        original_repeatability = _repeatability

        def drifting_repeatability(
            rehearsals: list[dict[str, Any]],
        ) -> dict[str, Any]:
            result = original_repeatability(rehearsals)
            result["all_content_digests_stable"] = False
            return result

        with (
            patch(
                "scripts.document_requirement_agent_e2e._repeatability",
                side_effect=drifting_repeatability,
            ),
            self.assertRaises(GateFailure) as raised,
        ):
            _run(_FakeTransport(), rehearsals=2)

        self.assertEqual(
            raised.exception.code,
            "repeatability_content_digest_drift",
        )
        self.assertEqual(raised.exception.stage, "repeatability")
        self.assertEqual(len(raised.exception.partial_rehearsals), 2)
        self.assertTrue(
            all(
                rehearsal["status"] == "PASS"
                for rehearsal in raised.exception.partial_rehearsals
            )
        )

    def test_receipt_guard_failure_preserves_completed_results(self) -> None:
        fake = _FakeTransport(
            receipt_provider_version="Customer shall reveal secret text",
        )

        with self.assertRaises(GateFailure) as raised:
            _run(fake)

        self.assertEqual(
            raised.exception.code,
            "receipt_string_not_text_free",
        )
        self.assertEqual(raised.exception.stage, "receipt")
        self.assertEqual(len(raised.exception.partial_rehearsals), 1)
        self.assertEqual(
            len(raised.exception.partial_rehearsals[0]["documents"]),
            1,
        )

    def test_cli_writes_minimal_receipt_when_full_failure_receipt_is_unsafe(
        self,
    ) -> None:
        fake = _FakeTransport(
            receipt_provider_version="Customer shall reveal secret text",
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "failure-receipt.json"
            with patch(
                "scripts.document_requirement_agent_e2e.JsonHttpClient",
                return_value=fake,
            ):
                exit_code = main(
                    [
                        "--manifest",
                        str(DEFAULT_MANIFEST),
                        "--base-url",
                        "http://127.0.0.1:8765",
                        "--project-id",
                        PROJECT_ID,
                        "--out",
                        str(destination),
                        "--document-id",
                        LOGICAL_ID,
                        "--poll-interval",
                        "0",
                        "--extraction-timeout",
                        "1",
                        "--atomization-timeout",
                        "1",
                        "--atomization-attempts",
                        "4",
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertTrue(destination.is_file())
            failure_receipt = json.loads(destination.read_text())
            self.assertTrue(failure_receipt["minimal_failure_receipt"])
            self.assertEqual(failure_receipt["failure"]["stage"], "receipt")
            self.assertEqual(failure_receipt["rehearsal_count"], 1)
            self.assertEqual(
                failure_receipt["rehearsals"][0][
                    "completed_document_count"
                ],
                1,
            )
            self.assertNotIn(
                "Customer shall reveal secret text",
                destination.read_text(),
            )


if __name__ == "__main__":
    unittest.main()
