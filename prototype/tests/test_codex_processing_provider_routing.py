from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import db
from app.application.errors import ApplicationError
from app.application.extraction import enrichment_workspace, provider_support
from app.application import processing_profiles
from app.application.runtime import RUNTIME_STATE
from app.domain.extraction.processing_profile import (
    ResolvedProviderBindingsV1,
    build_codex_agent_profile,
    build_resolved_processing_plan,
)


class _SemanticProvider:
    provider_id = "codex-test-semantic"
    provider_version = "1.0.0"
    model_id = "gpt-5.6-sol"

    def preflight_classification_request(self, request):  # type: ignore[no-untyped-def]
        del request

    def preflight_assembly_request(self, request):  # type: ignore[no-untyped-def]
        del request

    def classify_nodes(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected execution: {request!r}")

    def assemble_requirements(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected execution: {request!r}")


class _AtomizationProvider:
    def atomize(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected execution: {request!r}")


def _plan():
    profile = build_codex_agent_profile(project_id="project-1")
    bindings = ResolvedProviderBindingsV1(
        primary_parser_provider="local:pymupdf",
        llm_transport_provider="codex-app-server",
        semantic_deployment="gpt-5.6-sol:semantic@1.0.0",
        atomization_deployment="gpt-5.6-sol:atomization@1.2.0",
    )
    return build_resolved_processing_plan(
        project_id="project-1",
        document_id="document-1",
        source_sha256="1" * 64,
        profile=profile,
        document=profile.document,
        llm_provider="codex",
        provider_bindings=bindings,
        connection_revision=0,
    )


def _container(*, authorized: bool):
    return SimpleNamespace(
        settings=SimpleNamespace(
            agent_runtime_backend=SimpleNamespace(value="codex-app-server"),
            agent_customer_data_enabled=authorized,
            agent_model_id="gpt-5.6-sol",
        ),
        semantic_provider=_SemanticProvider(),
        requirement_atomization_provider=_AtomizationProvider(),
    )


class CodexProcessingProviderRoutingTests(unittest.TestCase):
    def tearDown(self) -> None:
        RUNTIME_STATE.container = None

    def test_default_profile_freezes_codex_without_legacy_connection_revision(
        self,
    ) -> None:
        profile = build_codex_agent_profile(project_id="project-1")
        plan = _plan()

        self.assertEqual(profile.llm_provider, "codex")
        self.assertEqual(plan.llm_provider, "codex")
        self.assertEqual(
            plan.provider_bindings.llm_transport_provider,
            "codex-app-server",
        )
        self.assertEqual(plan.connection_revision, 0)

    def test_runtime_selected_project_default_is_codex_not_local_rules(self) -> None:
        RUNTIME_STATE.container = _container(authorized=True)  # type: ignore[assignment]
        with patch.object(
            db,
            "get_project",
            return_value={
                "id": "project-1",
                "processing_profile_json": "",
                "processing_profile_revision": 0,
                "processing_profile_sha256": "",
            },
        ):
            profile = processing_profiles.get_project_processing_profile("project-1")

        self.assertEqual(profile.llm_provider, "codex")

    def test_capability_and_semantic_provider_use_runtime_container(self) -> None:
        container = _container(authorized=True)
        RUNTIME_STATE.container = container  # type: ignore[assignment]

        capabilities = {
            item.capability_id: item
            for item in processing_profiles.build_runtime_capabilities(probe=False)
        }
        provider = provider_support._build_llm_shadow_semantic_provider(_plan())

        self.assertTrue(capabilities[processing_profiles.CAPABILITY_CODEX_AGENT].ready)
        self.assertIs(provider, container.semantic_provider)

    def test_semantic_provider_rejects_missing_customer_authorization(self) -> None:
        RUNTIME_STATE.container = _container(authorized=False)  # type: ignore[assignment]

        with self.assertRaises(ApplicationError) as caught:
            provider_support._build_llm_shadow_semantic_provider(_plan())

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["reason_code"],
            "codex_customer_data_not_authorized",
        )

    def test_enrichment_uses_codex_atomization_provider(self) -> None:
        container = _container(authorized=True)
        RUNTIME_STATE.container = container  # type: ignore[assignment]

        with patch.object(
            enrichment_workspace,
            "_require_enrichment_processing_plan",
            return_value=_plan(),
        ):
            provider = enrichment_workspace._build_requirement_atomization_provider(
                "project-1",
                "document-1",
            )

        self.assertIs(provider, container.requirement_atomization_provider)


if __name__ == "__main__":
    unittest.main()
