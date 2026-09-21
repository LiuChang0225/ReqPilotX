from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import db
from app.adapters.persistence.parsing_risk_acknowledgements import (
    get_parsing_risk_acknowledgement,
    record_parsing_risk_acknowledgement,
)
from app.application.errors import ApplicationError
from app.application.extraction.parsing_diagnostics import (
    ParsingRiskAcknowledgementRequest,
    _build_parsing_risk_summary,
    acknowledge_document_parsing_risks,
    get_document_parsing_diagnostics,
)


SOURCE_SHA256 = "a" * 64
SNAPSHOT_ID = "b" * 64
DETAILS_SHA256 = "c" * 64


class ParsingDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db.init_db()

    def test_summary_reuses_server_severity_and_blocking_without_page_scan(self) -> None:
        summary = _build_parsing_risk_summary(
            snapshot=_snapshot(),
            details=_details(),
            details_sha256=DETAILS_SHA256,
        )

        self.assertEqual(summary["hard_blocker_count"], 2)
        self.assertEqual(summary["severe_warning_count"], 2)
        self.assertEqual(summary["risk_count"], 4)
        self.assertEqual(summary["unassigned_risk_count"], 1)
        self.assertEqual(
            [page["page_number"] for page in summary["risk_pages"]],
            [2, 3, 4, 5],
        )
        self.assertNotIn(
            "minor_information",
            {risk["code"] for risk in summary["risks"]},
        )
        self.assertEqual(len(summary["parse_identity_sha256"]), 64)
        self.assertEqual(summary["status"], "BLOCKED")
        self.assertEqual(len(summary["blocking_issue_ids"]), 2)
        self.assertEqual(len(summary["severe_issue_ids"]), 2)
        structure_risk = next(
            risk for risk in summary["risks"] if risk["stage"] == "STRUCTURE"
            and risk["finding_id"] == "finding-warning"
        )
        self.assertEqual(structure_risk["id"], "finding-warning:0")
        self.assertEqual(structure_risk["page_number"], 3)
        self.assertEqual(structure_risk["page_numbers"], [3, 4])
        self.assertEqual(
            structure_risk["locations"],
            [{"page_number": 3}, {"page_number": 4}],
        )
        pages = {page["page_number"]: page for page in summary["risk_pages"]}
        self.assertEqual(pages[3]["risk_ids"], ["finding-warning:0"])
        self.assertEqual(pages[4]["risk_ids"], ["finding-warning:0"])
        self.assertEqual(pages[3]["severe_warning_count"], 1)
        self.assertEqual(pages[4]["severe_warning_count"], 1)

    def test_one_finding_keeps_all_locations_without_counting_same_page_twice(
        self,
    ) -> None:
        summary = _build_parsing_risk_summary(
            snapshot=_snapshot(),
            details={
                "diagnostics": [],
                "findings": [
                    {
                        "id": "multi-location-finding",
                        "code": "visual_requirement",
                        "severity": "WARNING",
                        "blocking": False,
                        "message_key": "visual_requirement",
                        "source_refs": [
                            {
                                "location": {
                                    "format": "pdf",
                                    "page_number": 3,
                                    "bbox": [10, 20, 30, 40],
                                }
                            },
                            {
                                "location": {
                                    "format": "pdf",
                                    "page_number": 3,
                                    "bbox": [50, 60, 70, 80],
                                }
                            },
                            {
                                "location": {
                                    "format": "pdf",
                                    "page_number": 4,
                                    "bbox": [15, 25, 35, 45],
                                }
                            },
                        ],
                    }
                ],
            },
            details_sha256=DETAILS_SHA256,
        )

        self.assertEqual(summary["risk_count"], 1)
        self.assertEqual(summary["severe_warning_count"], 1)
        self.assertEqual(summary["unassigned_risk_count"], 0)
        self.assertEqual(summary["risks"][0]["page_numbers"], [3, 4])
        self.assertEqual(len(summary["risks"][0]["locations"]), 3)
        self.assertEqual(
            [
                (page["page_number"], page["severe_warning_count"])
                for page in summary["risk_pages"]
            ],
            [(3, 1), (4, 1)],
        )

    def test_finding_without_source_refs_remains_one_visible_unassigned_risk(
        self,
    ) -> None:
        summary = _build_parsing_risk_summary(
            snapshot=_snapshot(),
            details={
                "diagnostics": [],
                "findings": [
                    {
                        "id": "unassigned-finding",
                        "code": "structure_gap",
                        "severity": "WARNING",
                        "blocking": False,
                        "message_key": "structure_gap",
                        "source_refs": [],
                    }
                ],
            },
            details_sha256=DETAILS_SHA256,
        )

        self.assertEqual(summary["risk_count"], 1)
        self.assertEqual(summary["unassigned_risk_count"], 1)
        self.assertEqual(summary["risk_pages"], [])
        self.assertEqual(summary["risks"][0]["page_numbers"], [])
        self.assertEqual(summary["risks"][0]["locations"], [])

    def test_summary_uses_ready_and_ready_with_risks_without_new_thresholds(self) -> None:
        warning_details = _details()
        warning_details["diagnostics"] = [warning_details["diagnostics"][1]]
        warning_details["findings"] = []
        warning_summary = _build_parsing_risk_summary(
            snapshot=_snapshot(),
            details=warning_details,
            details_sha256=DETAILS_SHA256,
        )
        ready_summary = _build_parsing_risk_summary(
            snapshot=_snapshot(),
            details={"diagnostics": [], "findings": []},
            details_sha256=DETAILS_SHA256,
        )

        self.assertEqual(warning_summary["status"], "READY_WITH_RISKS")
        self.assertEqual(ready_summary["status"], "READY")

    def test_get_reads_one_verified_details_artifact_and_restores_exact_ack(self) -> None:
        encoded = json.dumps(_details()).encode("utf-8")
        response = SimpleNamespace(headers={}, status_code=200)
        acknowledgement = {
            "parse_artifact_id": SNAPSHOT_ID,
            "severe_warning_count": 2,
            "acknowledged_at": "2026-08-06T08:00:00+00:00",
        }
        with (
            patch(
                "app.application.extraction.parsing_diagnostics._current_document_source_page_snapshot",
                return_value=({}, _snapshot()),
            ),
            patch(
                "app.application.extraction.parsing_diagnostics._read_verified_json_artifact",
                return_value=encoded,
            ) as read_details,
            patch(
                "app.application.extraction.parsing_diagnostics.get_parsing_risk_acknowledgement",
                return_value=acknowledgement,
            ),
        ):
            result = get_document_parsing_diagnostics(
                "project-1",
                "document-1",
                response,
            )

        read_details.assert_called_once()
        self.assertEqual(
            result["risk_acknowledged_artifact_id"],
            SNAPSHOT_ID,
        )
        self.assertEqual(
            result["risk_acknowledged_at"],
            "2026-08-06T08:00:00+00:00",
        )
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertNotIn("storage_key", json.dumps(result))

    def test_acknowledgement_rejects_stale_identity_before_persistence(self) -> None:
        response = SimpleNamespace(headers={}, status_code=200)
        with (
            patch(
                "app.application.extraction.parsing_diagnostics._current_document_source_page_snapshot",
                return_value=({}, _snapshot()),
            ),
            patch(
                "app.application.extraction.parsing_diagnostics._build_current_summary",
                return_value=_ready_summary(hard_blocker_count=0),
            ),
            patch(
                "app.application.extraction.parsing_diagnostics.record_parsing_risk_acknowledgement"
            ) as persist,
        ):
            with self.assertRaises(ApplicationError) as caught:
                acknowledge_document_parsing_risks(
                    "project-1",
                    "document-1",
                    ParsingRiskAcknowledgementRequest(
                        expected_source_sha256=SOURCE_SHA256,
                        expected_parse_artifact_id=SNAPSHOT_ID,
                        expected_parse_identity_sha256="e" * 64,
                    ),
                    response,
                )

        self.assertEqual(caught.exception.code, "parsing_risk_identity_changed")
        persist.assert_not_called()

    def test_acknowledgement_rejects_explicit_hard_blocker(self) -> None:
        response = SimpleNamespace(headers={}, status_code=200)
        summary = _ready_summary(hard_blocker_count=1)
        with (
            patch(
                "app.application.extraction.parsing_diagnostics._current_document_source_page_snapshot",
                return_value=({}, _snapshot()),
            ),
            patch(
                "app.application.extraction.parsing_diagnostics._build_current_summary",
                return_value=summary,
            ),
            patch(
                "app.application.extraction.parsing_diagnostics.record_parsing_risk_acknowledgement"
            ) as persist,
        ):
            with self.assertRaises(ApplicationError) as caught:
                acknowledge_document_parsing_risks(
                    "project-1",
                    "document-1",
                    _matching_request(summary),
                    response,
                )

        self.assertEqual(
            caught.exception.code,
            "parsing_risk_acknowledgement_blocked",
        )
        persist.assert_not_called()

    def test_acknowledgement_is_returned_from_idempotent_repository_receipt(self) -> None:
        response = SimpleNamespace(headers={}, status_code=200)
        summary = _ready_summary(hard_blocker_count=0)
        persisted = {
            "parse_artifact_id": SNAPSHOT_ID,
            "severe_warning_count": 3,
            "acknowledged_at": "2026-08-06T09:00:00+00:00",
        }
        with (
            patch(
                "app.application.extraction.parsing_diagnostics._current_document_source_page_snapshot",
                return_value=({}, _snapshot()),
            ),
            patch(
                "app.application.extraction.parsing_diagnostics._build_current_summary",
                return_value=summary,
            ),
            patch(
                "app.application.extraction.parsing_diagnostics.record_parsing_risk_acknowledgement",
                return_value=persisted,
            ) as persist,
        ):
            result = acknowledge_document_parsing_risks(
                "project-1",
                "document-1",
                _matching_request(summary),
                response,
            )

        persist.assert_called_once_with(
            project_id="project-1",
            document_id="document-1",
            source_sha256=SOURCE_SHA256,
            parse_artifact_id=SNAPSHOT_ID,
            parse_identity_sha256=summary["parse_identity_sha256"],
            severe_warning_count=3,
        )
        self.assertEqual(result["risk_acknowledged_artifact_id"], SNAPSHOT_ID)

    def test_acknowledgement_rejects_artifact_switch_during_persistence(self) -> None:
        response = SimpleNamespace(headers={}, status_code=200)
        summary = _ready_summary(hard_blocker_count=0)
        changed_summary = {
            **summary,
            "active_parse_artifact_id": "d" * 64,
            "parse_identity_sha256": "e" * 64,
        }
        persisted = {
            "parse_artifact_id": SNAPSHOT_ID,
            "severe_warning_count": 3,
            "acknowledged_at": "2026-08-06T09:00:00+00:00",
        }
        with (
            patch(
                "app.application.extraction.parsing_diagnostics._current_document_source_page_snapshot",
                side_effect=[({}, _snapshot()), ({}, _snapshot())],
            ),
            patch(
                "app.application.extraction.parsing_diagnostics._build_current_summary",
                side_effect=[summary, changed_summary],
            ),
            patch(
                "app.application.extraction.parsing_diagnostics.record_parsing_risk_acknowledgement",
                return_value=persisted,
            ) as persist,
        ):
            with self.assertRaises(ApplicationError) as caught:
                acknowledge_document_parsing_risks(
                    "project-1",
                    "document-1",
                    _matching_request(summary),
                    response,
                )

        self.assertEqual(caught.exception.code, "parsing_risk_identity_changed")
        persist.assert_called_once()

    def test_postgres_migration_and_cutover_register_acknowledgements(self) -> None:
        prototype_dir = Path(__file__).resolve().parents[1]
        migration = (
            prototype_dir
            / "migrations"
            / "postgresql"
            / "0012_parsing_risk_acknowledgements.sql"
        ).read_text(encoding="utf-8")
        cutover = (
            prototype_dir.parent
            / "apps/server/src/reqpilotx_server"
            / "application"
            / "cutover"
            / "sqlite_to_postgres.py"
        ).read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS parsing_risk_acknowledgements", migration)
        self.assertIn("parse_identity_sha256", migration)
        self.assertIn("UNIQUE (", migration)
        self.assertGreaterEqual(cutover.count('"parsing_risk_acknowledgements"'), 2)

    def test_repository_is_idempotent_and_exactly_bound_to_one_artifact(self) -> None:
        project = db.create_project("Parsing diagnostics", "Test customer")
        document = db.add_sor_document(
            project["id"],
            "source.pdf",
            "pdf",
            "uploads/source.pdf",
            "",
            source_sha256=SOURCE_SHA256,
            source_size=1,
            declared_extension="pdf",
            detected_format="pdf",
        )
        identity = "f" * 64
        first = record_parsing_risk_acknowledgement(
            project_id=project["id"],
            document_id=document["id"],
            source_sha256=SOURCE_SHA256,
            parse_artifact_id=SNAPSHOT_ID,
            parse_identity_sha256=identity,
            severe_warning_count=3,
        )
        repeated = record_parsing_risk_acknowledgement(
            project_id=project["id"],
            document_id=document["id"],
            source_sha256=SOURCE_SHA256,
            parse_artifact_id=SNAPSHOT_ID,
            parse_identity_sha256=identity,
            severe_warning_count=3,
        )

        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(first["acknowledged_at"], repeated["acknowledged_at"])
        self.assertIsNone(
            get_parsing_risk_acknowledgement(
                project_id=project["id"],
                document_id=document["id"],
                source_sha256=SOURCE_SHA256,
                parse_artifact_id="9" * 64,
                parse_identity_sha256=identity,
            )
        )
        self.assertIsNone(
            get_parsing_risk_acknowledgement(
                project_id=project["id"],
                document_id=document["id"],
                source_sha256=SOURCE_SHA256,
                parse_artifact_id=SNAPSHOT_ID,
                parse_identity_sha256="8" * 64,
            )
        )


def _snapshot() -> dict:
    return {
        "source_sha256": SOURCE_SHA256,
        "snapshot_id": SNAPSHOT_ID,
        "page_count": 6,
        "details": {
            "storage_key": "runtime/source-page-facts/details.json",
            "sha256": DETAILS_SHA256,
            "size": 512,
        },
    }


def _details() -> dict:
    return {
        "schema_version": "extraction-details/v2",
        "run_id": SNAPSHOT_ID,
        "project_id": "project-1",
        "document_id": "document-1",
        "diagnostics": [
            {
                "code": "minor_information",
                "severity": "info",
                "message": "Kept for technical details.",
                "location": {"page_number": 1},
            },
            {
                "code": "visual_gap",
                "severity": "warning",
                "message": "Visual content may be incomplete.",
                "location": {"page_number": 2},
            },
            {
                "code": "source_unreadable",
                "severity": "error",
                "message": "Source content is unreadable.",
                "location": None,
            },
        ],
        "findings": [
            {
                "id": "finding-warning",
                "code": "structure_gap",
                "severity": "WARNING",
                "blocking": False,
                "message_key": "structure_gap",
                "source_refs": [
                    {"location": {"page_number": 3}},
                    {"location": {"page_number": 4}},
                ],
            },
            {
                "id": "finding-blocking",
                "code": "source_identity_gap",
                "severity": "ERROR",
                "blocking": True,
                "message_key": "source_identity_gap",
                "source_refs": [{"location": {"page_number": 5}}],
            },
        ],
    }


def _ready_summary(*, hard_blocker_count: int) -> dict:
    identity = hashlib.sha256(b"parse-identity").hexdigest()
    return {
        "schema_version": "parsing-diagnostics/v2",
        "status": "BLOCKED" if hard_blocker_count else "READY_WITH_RISKS",
        "source_sha256": SOURCE_SHA256,
        "active_parse_artifact_id": SNAPSHOT_ID,
        "parse_identity_sha256": identity,
        "page_count": 6,
        "hard_blocker_count": hard_blocker_count,
        "severe_warning_count": 3,
        "risk_count": 3 + hard_blocker_count,
        "severe_issue_ids": ["risk-1", "risk-2", "risk-3"],
        "blocking_issue_ids": ["blocker-1"] if hard_blocker_count else [],
        "unassigned_risk_count": 0,
        "risk_pages": [],
        "risks": [],
        "risk_acknowledged_at": None,
        "risk_acknowledged_artifact_id": None,
    }


def _matching_request(summary: dict) -> ParsingRiskAcknowledgementRequest:
    return ParsingRiskAcknowledgementRequest(
        expected_source_sha256=summary["source_sha256"],
        expected_parse_artifact_id=summary["active_parse_artifact_id"],
        expected_parse_identity_sha256=summary["parse_identity_sha256"],
    )


if __name__ == "__main__":
    unittest.main()
