"""Provider adapters for the existing source-bound table/text window machinery.

The business compilers, retries, cumulative checkpoints and result schemas are
shared with the Codex path. Only profile/lifecycle and stage transport differ.
The legacy checkpoint/receipt ``codex_version`` field is an opaque transport
identity here, explicitly prefixed ``provider-structured-stage:<provider>``;
it does not assert a Codex installation or capability.
"""
from __future__ import annotations
from reqpilotx_server.saas.context import owned_task_identity

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_driver import canonical_sha256
from reqpilotx_server.adapters.agents.codex_app_server.source_item_text_executor import (
    LocalCanonicalSourceItemTextExecutor,
)
from reqpilotx_server.adapters.agents.codex_app_server.source_requirement_window_executor import (
    LocalCanonicalSourceRequirementWindowExecutor,
)
from reqpilotx_server.adapters.agents.codex_app_server.native_source_item_text_windowed import (
    boundary_output_schema, classification_output_schema,
)
from reqpilotx_server.adapters.agents.codex_app_server.native_source_requirement_windowed_reference import (
    _boundary_output_schema, _classification_output_schema,
)
from reqpilotx_server.adapters.agents.portable_kit import load_portable_agent_kit
from reqpilotx_server.adapters.agents.provider_structured_stage import (
    PROVIDER_STRUCTURED_STAGE_TRANSPORT_KIND, PROVIDER_STRUCTURED_STAGE_VERSION,
    ProviderStructuredStageError, ProviderStructuredStageInvocation,
    ProviderStructuredStageTransport,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentExtractionStage
from reqpilotx_server.infrastructure.runtime_settings import RuntimeSettings


class _ProviderWindowedStageMixin:
    adapter_version = PROVIDER_STRUCTURED_STAGE_VERSION
    transport_kind = PROVIDER_STRUCTURED_STAGE_TRANSPORT_KIND
    # Neither provider has a universally portable reasoning-effort parameter.
    # Do not report the Codex profile's high/medium effort as a provider setting.
    reasoning_effort = "provider-default"

    def __init__(self, *, provider: str, model_id: str, client_factory: Callable[[], Any],
                 kit_root: Path, customer_data_enabled: bool = False,
                 configuration_check: Callable[[], Any] | None = None) -> None:
        if provider not in {"azure", "aliyun"} or not model_id:
            raise ValueError("Windowed provider requires an explicit provider and model")
        self.provider = provider
        self.model_id = model_id
        self._client_factory = client_factory
        self._kit_root = kit_root.expanduser().resolve(strict=False)
        self._customer_data_enabled = customer_data_enabled
        self._configuration_check = configuration_check

    def readiness_reason(self) -> str | None:
        if not self._customer_data_enabled:
            return "PROVIDER_STAGE_CUSTOMER_DATA_DISABLED"
        try:
            if self._configuration_check:
                provider, model, _factory = self._configuration_check()
                if (provider, model) != (self.provider, self.model_id):
                    return "PROVIDER_STAGE_CONFIGURATION_DRIFTED"
            load_portable_agent_kit(self._kit_root)
        except Exception:
            return "PROVIDER_STAGE_CONFIGURATION_OR_KIT_UNAVAILABLE"
        return None

    def _load_stage_kit(self):  # type: ignore[no-untyped-def]
        if not self._customer_data_enabled:
            raise ProviderStructuredStageError("PROVIDER_STAGE_CUSTOMER_DATA_DISABLED")
        if self._configuration_check:
            # The job was pinned to this adapter's model before execute. A new
            # profile must not change that identity even on a first attempt.
            provider, model, factory = self._configuration_check()
            if (provider, model) != (self.provider, self.model_id):
                raise ProviderStructuredStageError("PROVIDER_STAGE_CONFIGURATION_DRIFTED")
            self._client_factory = factory
        return load_portable_agent_kit(self._kit_root)

    @contextmanager
    def _stage_runtime(self, *, task):  # type: ignore[no-untyped-def]
        context_sha256 = canonical_sha256({
            "project_id": task.project_id, "document_id": task.document_id,
            "semantic_context": task.semantic_context,
            "execution_context": getattr(task, "execution_context", None),
            "max_concurrency": task.max_concurrency,
        })
        yield None, f"provider-structured-stage:{self.provider}:{self.adapter_version}:{context_sha256}"

    def _invoke_phase(self, *, task, source, execution_mode, files, task_directive,
                      cancellation_requested, **_context):  # type: ignore[no-untyped-def]
        kit = load_portable_agent_kit(self._kit_root)
        stage = next(item for item in kit.manifest.stages
                     if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION)
        variant = next((item for item in stage.runner_output_schema_variants
                        if item.execution_mode == execution_mode), None)
        if variant is None:
            raise ProviderStructuredStageError("PROVIDER_STAGE_EXECUTION_MODE_UNAVAILABLE")
        schema = json.loads((kit.root / variant.schema_path).read_text(encoding="utf-8"))
        expected = {
            "SOURCE_REQUIREMENT_BOUNDARY_PLAN": _boundary_output_schema,
            "SOURCE_REQUIREMENT_CLASSIFICATION_WINDOW": _classification_output_schema,
            "SOURCE_ITEM_TEXT_BOUNDARY_PLAN": boundary_output_schema,
            "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW": classification_output_schema,
        }[execution_mode]()
        if canonical_sha256(schema) != canonical_sha256(expected):
            raise ProviderStructuredStageError("PROVIDER_STAGE_OUTPUT_SCHEMA_DRIFTED")
        skill = next(item for item in kit.manifest.skills if item.skill_id == stage.skill_id)
        instructions = (kit.root / skill.path).read_text(encoding="utf-8")
        # References remain read-only resources and are available without
        # requiring filesystem or developer-shell access from the model.
        references = tuple((path, {"text": (kit.root / path).read_text(encoding="utf-8")})
                           for path in stage.reference_paths)
        invocation = ProviderStructuredStageInvocation(
            source_path=source, source_sha256=str(task.document_map["source_sha256"]),
            execution_mode=execution_mode, files=(*files, *references),
            instructions=instructions + "\n\nHost stage directive:\n" + task_directive,
            output_schema=schema,
            binding=owned_task_identity({"executor_id": self.adapter_id, "executor_version": self.adapter_version,
                     "project_id": task.project_id, "document_id": task.document_id,
                     "kit_bundle_sha256": kit.identity.bundle_sha256}),
        )
        return ProviderStructuredStageTransport(
            provider=self.provider, model_id=self.model_id, client_factory=self._client_factory,
            timeout_seconds=task.timeout_seconds, cancellation_requested=cancellation_requested,
        ).invoke(invocation)

    def _execute_with_runtime(self, **kwargs):  # type: ignore[no-untyped-def]
        execution = super()._execute_with_runtime(**kwargs)
        if "evidence_delivery" in execution.run_receipt:
            receipt = dict(execution.run_receipt)
            receipt["evidence_delivery"] = "INLINE_AND_FILES"
            return replace(execution, run_receipt=receipt)
        return execution


class ProviderSourceRequirementWindowExecutor(
    _ProviderWindowedStageMixin, LocalCanonicalSourceRequirementWindowExecutor,
):
    adapter_id = "provider-source-requirement-window"


class ProviderSourceItemTextExecutor(
    _ProviderWindowedStageMixin, LocalCanonicalSourceItemTextExecutor,
):
    adapter_id = "provider-source-item-text"


def build_provider_window_executor(settings: RuntimeSettings, *, text: bool = False):
    from reqpilotx_server.adapters.agents.provider_tool_loop import _configuration
    from reqpilotx_server.resource_paths import package_resource_path

    root = settings.structured_agent_kit_root
    if root is None:
        root = (settings.development_root / "agent-kit" if settings.development_root
                else package_resource_path("agent-kit"))
    # Application startup remains possible before the employee configures the
    # provider. Readiness/execute resolve the live profile without a live call.
    provider = settings.agent_provider.value if settings.agent_provider else ""
    try:
        _, model, factory = _configuration(settings, fresh_client=True)
    except ValueError:
        model = settings.agent_model_id or "unconfigured"
        factory = lambda: _configuration(settings, fresh_client=True)[2]()
    adapter = ProviderSourceItemTextExecutor if text else ProviderSourceRequirementWindowExecutor
    return adapter(provider=provider, model_id=model, client_factory=factory, kit_root=root,
                   customer_data_enabled=settings.agent_customer_data_enabled,
                   configuration_check=lambda: _configuration(settings, fresh_client=True))
