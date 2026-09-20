from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest

from spikes.codex_app_server.pdf_tool_host import (
    prepare_pdf_canary,
    validate_understanding_proposal,
)
from spikes.codex_app_server.read_only_mcp import (
    ReadOnlyMcpError,
    ReadOnlyToolSession,
    STRUCTURE_TOOL_GET_CURRENT,
    STRUCTURE_TOOL_VALIDATE_PROPOSAL,
    write_session_descriptor,
)
from spikes.codex_app_server.shadow_structure import (
    ShadowStructureError,
    prepare_shadow_structure_context,
    shadow_structure_stage_payload,
    validate_shadow_structure_proposal,
    write_immutable_shadow_artifact,
)
from tests.spikes.test_codex_pdf_skill_canary import (
    _valid_structure_proposal,
    _valid_understanding_proposal,
)


_NOW = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)


class CodexShadowStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="reqpilotx-shadow-structure-")
        self.root = Path(self._tmp.name).resolve()
        os.chmod(self.root, 0o700)
        server = self.root / "server"
        model = self.root / "model"
        server.mkdir(mode=0o700)
        model.mkdir(mode=0o700)
        self.pdf_context = prepare_pdf_canary(
            server_private_dir=server,
            model_workspace=model,
            include_prompt_injection=True,
        )
        self.understanding = _valid_understanding_proposal(self.pdf_context)
        self.coverage, understanding_sha = validate_understanding_proposal(
            self.understanding,
            self.pdf_context,
        )
        self.context = prepare_shadow_structure_context(
            pdf_context=self.pdf_context,
            understanding_proposal=self.understanding,
            understanding_proposal_sha256=understanding_sha,
            coverage_receipt=self.coverage,
            clock=lambda: _NOW,
        )
        self.proposal = _valid_structure_proposal(
            self.pdf_context,
            understanding_sha256=understanding_sha,
            coverage=self.coverage,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_validated_envelope_is_bound_and_immutable_with_zero_business_writes(
        self,
    ) -> None:
        envelope, validation = _validate(self.proposal, self.context)

        self.assertEqual(validation["status"], "VALIDATED_SHADOW")
        self.assertFalse(validation["apply_eligible"])
        self.assertEqual(envelope["serving_impact"], "NONE")
        self.assertEqual(envelope["base_structure_identity"]["revision"], 0)
        self.assertEqual(
            envelope["structure_proposal"]["source_sha256"],
            self.pdf_context.fixture.source_sha256,
        )

        destination = self.root / "shadow-structure.json"
        receipt = write_immutable_shadow_artifact(
            envelope,
            destination=destination,
        )
        self.assertTrue(receipt["immutable"])
        self.assertEqual(receipt["write_audit"]["total_writes"], 0)
        self.assertEqual(oct(destination.stat().st_mode & 0o777), "0o600")
        with self.assertRaisesRegex(
            ShadowStructureError,
            "SHADOW_ARTIFACT_ALREADY_EXISTS",
        ):
            write_immutable_shadow_artifact(envelope, destination=destination)

    def test_stale_base_expiry_and_expired_proposals_fail_closed(self) -> None:
        with self.assertRaisesRegex(ShadowStructureError, "BASE_STRUCTURE_STALE"):
            validate_shadow_structure_proposal(
                self.proposal,
                context=self.context,
                expected_base_structure_sha256="0" * 64,
                expected_base_revision=0,
                expected_expires_at_utc=self.context.expires_at_utc,
                now=_NOW,
            )
        with self.assertRaisesRegex(ShadowStructureError, "BASE_STRUCTURE_STALE"):
            validate_shadow_structure_proposal(
                self.proposal,
                context=self.context,
                expected_base_structure_sha256=self.context.base_structure_identity[
                    "structure_sha256"
                ],
                expected_base_revision=1,
                expected_expires_at_utc=self.context.expires_at_utc,
                now=_NOW,
            )
        with self.assertRaisesRegex(ShadowStructureError, "SHADOW_EXPIRY_STALE"):
            validate_shadow_structure_proposal(
                self.proposal,
                context=self.context,
                expected_base_structure_sha256=self.context.base_structure_identity[
                    "structure_sha256"
                ],
                expected_base_revision=0,
                expected_expires_at_utc="2026-08-21T09:00:00Z",
                now=_NOW,
            )
        with self.assertRaisesRegex(ShadowStructureError, "SHADOW_PROPOSAL_EXPIRED"):
            validate_shadow_structure_proposal(
                self.proposal,
                context=self.context,
                expected_base_structure_sha256=self.context.base_structure_identity[
                    "structure_sha256"
                ],
                expected_base_revision=0,
                expected_expires_at_utc=self.context.expires_at_utc,
                now=_NOW + timedelta(minutes=16),
            )

    def test_forged_locator_missing_coverage_and_wrong_order_are_rejected(self) -> None:
        forged_locator = copy.deepcopy(self.proposal)
        forged_locator["canonical_nodes"][0]["locator"] = {"physical_page": 999}
        with self.assertRaisesRegex(ShadowStructureError, "PROPOSAL_REJECTED"):
            _validate(forged_locator, self.context)

        missing = copy.deepcopy(self.proposal)
        missing["canonical_nodes"].pop()
        with self.assertRaisesRegex(ShadowStructureError, "PROPOSAL_REJECTED"):
            _validate(missing, self.context)

        reordered = copy.deepcopy(self.proposal)
        reordered["canonical_nodes"][0]["source_order"] = 999
        with self.assertRaisesRegex(ShadowStructureError, "PROPOSAL_REJECTED"):
            _validate(reordered, self.context)

    def test_failed_validation_creates_no_shadow_artifact(self) -> None:
        destination = self.root / "must-not-exist.json"
        invalid = copy.deepcopy(self.proposal)
        invalid["canonical_nodes"][0]["source_refs"] = ["unknown-source-ref"]
        with self.assertRaisesRegex(ShadowStructureError, "PROPOSAL_REJECTED"):
            envelope, _receipt = _validate(invalid, self.context)
            write_immutable_shadow_artifact(envelope, destination=destination)
        self.assertFalse(destination.exists())

    def test_structure_stage_mcp_uses_two_tools_and_keeps_writes_zero(self) -> None:
        descriptor, _sha256 = write_session_descriptor(
            self.pdf_context,
            destination=self.pdf_context.source_path.parent
            / "structure-mcp-session.json",
            structure_stage=shadow_structure_stage_payload(self.context),
        )
        session = ReadOnlyToolSession(descriptor, clock=lambda: _NOW)
        current = session.call(
            STRUCTURE_TOOL_GET_CURRENT,
            {"run_handle": descriptor.run_handle},
        )
        self.assertEqual(current["base_structure_identity"]["revision"], 0)
        self.assertFalse(current["apply_eligible"])
        validated = session.call(
            STRUCTURE_TOOL_VALIDATE_PROPOSAL,
            {
                "document_handle": descriptor.document_handle,
                "expected_base_structure_sha256": self.context.base_structure_identity[
                    "structure_sha256"
                ],
                "expected_base_revision": 0,
                "expected_expires_at_utc": self.context.expires_at_utc,
                "proposal": self.proposal,
            },
        )
        self.assertEqual(validated["status"], "VALIDATED_SHADOW")
        self.assertEqual(validated["write_audit"]["total_writes"], 0)

        stale_session = ReadOnlyToolSession(descriptor, clock=lambda: _NOW)
        stale_session.call(
            STRUCTURE_TOOL_GET_CURRENT,
            {"run_handle": descriptor.run_handle},
        )
        with self.assertRaisesRegex(ReadOnlyMcpError, "BASE_STRUCTURE_STALE"):
            stale_session.call(
                STRUCTURE_TOOL_VALIDATE_PROPOSAL,
                {
                    "document_handle": descriptor.document_handle,
                    "expected_base_structure_sha256": "0" * 64,
                    "expected_base_revision": 0,
                    "expected_expires_at_utc": self.context.expires_at_utc,
                    "proposal": self.proposal,
                },
            )


def _validate(proposal, context):
    return validate_shadow_structure_proposal(
        proposal,
        context=context,
        expected_base_structure_sha256=context.base_structure_identity[
            "structure_sha256"
        ],
        expected_base_revision=0,
        expected_expires_at_utc=context.expires_at_utc,
        now=_NOW,
    )


if __name__ == "__main__":
    unittest.main()
