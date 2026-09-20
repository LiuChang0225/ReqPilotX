from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from unittest.mock import patch

from app.application.assessment import (
    _prepare_document_ai_analysis_request,
)
from app.application.errors import ApplicationError


class AssessmentRequirementSetBindingTests(unittest.TestCase):
    def test_route_selected_set_cannot_silently_fall_back_to_active(self) -> None:
        document = {
            "id": "document-1",
            "project_id": "project-1",
            "active_requirement_set_id": "set-active",
            "extraction_locked_at": "2026-07-31T00:00:00+00:00",
        }
        selected = {
            "id": "set-selected",
            "project_id": "project-1",
            "sor_document_id": "document-1",
            "status": "LOCKED",
        }
        with (
            patch(
                "app.application.projects._require_project",
                return_value={"id": "project-1"},
            ),
            patch(
                "app.application.assessment.db.get_sor_document",
                return_value=document,
            ),
            patch(
                "app.application.assessment.db.backfill_sor_document_runtime",
                return_value=document,
            ),
            patch(
                "app.application.assessment.db.get_requirement_set",
                return_value=selected,
            ),
        ):
            with self.assertRaises(ApplicationError) as raised:
                _prepare_document_ai_analysis_request(
                    "project-1",
                    "document-1",
                    requirement_set_id="set-selected",
                    hydrate_knowledge_payloads=False,
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "analysis_requirement_set_not_current",
        )

    def test_active_set_must_be_locked_even_if_document_has_old_lock_time(
        self,
    ) -> None:
        document = {
            "id": "document-1",
            "project_id": "project-1",
            "active_requirement_set_id": "set-active",
            "extraction_locked_at": "2026-07-31T00:00:00+00:00",
        }
        selected = {
            "id": "set-active",
            "project_id": "project-1",
            "sor_document_id": "document-1",
            "status": "DRAFT",
        }
        with (
            patch(
                "app.application.projects._require_project",
                return_value={"id": "project-1"},
            ),
            patch(
                "app.application.assessment.db.get_sor_document",
                return_value=document,
            ),
            patch(
                "app.application.assessment.db.backfill_sor_document_runtime",
                return_value=document,
            ),
            patch(
                "app.application.assessment.db.get_requirement_set",
                return_value=selected,
            ),
        ):
            with self.assertRaises(ApplicationError) as raised:
                _prepare_document_ai_analysis_request(
                    "project-1",
                    "document-1",
                    requirement_set_id="set-active",
                    hydrate_knowledge_payloads=False,
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "analysis_requirement_set_not_locked",
        )


if __name__ == "__main__":
    unittest.main()
