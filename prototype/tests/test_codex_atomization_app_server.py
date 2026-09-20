from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from app.adapters.agents.codex_app_server.atomization_app_server import (
    CODEX_APP_SERVER_ATOMIZATION_PROVIDER_ID,
    AtomizationDriverResult,
    CodexAppServerConsensusRequirementAtomizationProvider,
)
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderRequest,
)
from tests.test_native_codex_atomization_reference import _reference, _request


class _FakeDriver:
    model_id = "gpt-5.6-sol"

    def atomize_once(
        self,
        request: RequirementAtomizationProviderRequest,
    ) -> AtomizationDriverResult:
        reference = _reference().model_copy(
            update={
                "lineage_sha256": request.lineage_sha256,
                "review_node_id": request.review_node_id,
            }
        )
        from app.application.extraction.native_atomization_reference import (
            validate_native_atomization_reference,
        )

        return AtomizationDriverResult(
            reference=reference,
            validation_receipt=validate_native_atomization_reference(
                request, reference
            ),
            candidate_sha256="2" * 64,
            runtime_thread_id="thread-test",
            request_sha256="3" * 64,
            tool_receipt_sha256="4" * 64,
            output_schema_sha256="5" * 64,
        )


class CodexAtomizationAppServerTests(unittest.TestCase):
    def test_consensus_provider_implements_exact_cover_atomization_port(self) -> None:
        provider = CodexAppServerConsensusRequirementAtomizationProvider(
            _FakeDriver(),
            repetition_count=2,
        )

        result = provider.atomize(_request())

        self.assertEqual(provider.provider_id, CODEX_APP_SERVER_ATOMIZATION_PROVIDER_ID)
        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.run.request_count, 2)
        self.assertEqual(len(result.obligations), 1)
        self.assertEqual(len(result.context_ranges), 1)


if __name__ == "__main__":
    unittest.main()
