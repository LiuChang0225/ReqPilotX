from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import Lock, RLock
from typing import Any

from app.adapters.documents.azure_document_intelligence_visual_text_provider import (
    AzureDocumentIntelligenceVisualTextProvider,
)
from app.adapters.documents.azure_gpt56_sol_page_layout_provider import (
    AzureGpt56SolPageLayoutProvider,
)
from app.adapters.documents.bailian_page_layout_provider import (
    BailianPageLayoutProvider,
)
from app.adapters.documents.bailian_visual_text_provider import (
    BailianVisualTextProvider,
)
from app.adapters.documents.bailian_visual_table_provider import (
    BailianVisualTableProvider,
)
from app.adapters.documents.pdf_source_ir_adapter import (
    VISUAL_TABLE_MAX_REQUESTS_PER_DOCUMENT,
    VISUAL_TABLE_MAX_REQUESTS_PER_PAGE,
    PdfSourceIRAdapter,
)
from app.domain.extraction.processing_profile import ResolvedProcessingPlanV1
from app.domain.extraction.visual_table import (
    VisualTableCell,
    VisualTableCropInput,
    VisualTableSnapshot,
    stable_visual_table_cell_id,
    stable_visual_table_snapshot_id,
)
from app.domain.extraction.visual_text import (
    VisualTextObservation,
    VisualTextPageInput,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)
from app.ports.visual_text_provider import (
    VisualTextProviderPort,
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)
from app.ports.visual_table_provider import (
    VisualTableProviderPort,
    VisualTableProviderRejected,
    VisualTableProviderRejectionCode,
)
from app.services.ai_copilot import (
    _azure_openai_token_provider,
    get_runtime_ai_config_value,
    load_ai_copilot_config,
)
from app.services.visual_text_snapshot_cache import (
    VisualTextProviderTelemetryCallback,
    VisualTextSnapshotCacheBinding,
)

COMPOSABLE_VISUAL_CHAIN_PROVIDER_ID = "reqpilotx-composable-page-visual-chain"
COMPOSABLE_VISUAL_CHAIN_VERSION = "1.0.0"
PLAN_BOUND_VISUAL_TABLE_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class PageProcessingReceiptV1:
    page_number: int
    page_image_sha256: str
    attempted_provider_ids: tuple[str, ...]
    adopted_provider_id: str
    state: str
    degraded_reason_codes: tuple[str, ...]
    visual_table_mode: str = "OFF"
    visual_table_candidate_count: int = 0
    visual_table_attempt_count: int = 0
    visual_table_ready_count: int = 0
    visual_table_failed_count: int = 0
    visual_table_budget_skipped_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "page-processing-receipt/v1",
            "page_number": self.page_number,
            "page_image_sha256": self.page_image_sha256,
            "attempted_provider_ids": list(self.attempted_provider_ids),
            "adopted_provider_id": self.adopted_provider_id,
            "state": self.state,
            "degraded_reason_codes": list(self.degraded_reason_codes),
            "table_parsing": {
                "mode": self.visual_table_mode,
                "candidate_count": self.visual_table_candidate_count,
                "attempt_count": self.visual_table_attempt_count,
                "ready_count": self.visual_table_ready_count,
                "failed_count": self.visual_table_failed_count,
                "budget_skipped_count": self.visual_table_budget_skipped_count,
                "request_ceiling_per_page": VISUAL_TABLE_MAX_REQUESTS_PER_PAGE,
                "request_ceiling_per_document": (
                    VISUAL_TABLE_MAX_REQUESTS_PER_DOCUMENT
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class _FrozenEnhancementBinding:
    stage: str
    model: str | None


class PlanBoundVisualTableProvider:
    """Expose one immutable table identity for a frozen processing plan."""

    def __init__(
        self,
        delegate: VisualTableProviderPort,
        *,
        provider_id: str,
        processing_plan_sha256: str,
        connection_revision: int,
    ) -> None:
        delegate_id = str(getattr(delegate, "provider_id", "") or "").strip()
        delegate_version = str(getattr(delegate, "provider_version", "") or "").strip()
        if not delegate_id or not delegate_version:
            raise ValueError("visual table delegate identity is incomplete")
        if not provider_id.strip():
            raise ValueError("visual table provider identity is incomplete")
        if connection_revision < 0:
            raise ValueError("connection revision must not be negative")
        identity = {
            "schema_version": "plan-bound-visual-table-provider/v1",
            "processing_plan_sha256": processing_plan_sha256,
            "connection_revision": connection_revision,
            "delegate_provider_id": delegate_id,
            "delegate_provider_version": delegate_version,
        }
        identity_sha256 = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.provider_id = provider_id
        self.provider_version = (
            f"{PLAN_BOUND_VISUAL_TABLE_VERSION};"
            f"plan={processing_plan_sha256[:16]};"
            f"revision={connection_revision};"
            f"identity={identity_sha256}"
        )
        self._delegate = delegate
        self._call_lock = Lock()
        self._terminal_unavailable: VisualTableProviderRejected | None = None

    def parse_table(self, crop_input: VisualTableCropInput) -> VisualTableSnapshot:
        with self._call_lock:
            if self._terminal_unavailable is not None:
                raise self._terminal_unavailable
            try:
                snapshot = self._delegate.parse_table(crop_input)
            except VisualTableProviderRejected as exc:
                if exc.code is VisualTableProviderRejectionCode.UNAVAILABLE:
                    self._terminal_unavailable = exc
                raise
        return _rebind_visual_table_snapshot(
            snapshot,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
        )


class ComposablePageVisualProvider:
    """Escalate one anomalous page and retain the last trusted stage on failure."""

    provider_id = COMPOSABLE_VISUAL_CHAIN_PROVIDER_ID
    supports_concurrent_pages = False
    max_concurrent_pages = 1

    def __init__(
        self,
        *,
        enhancement_providers: tuple[VisualTextProviderPort, ...],
        complex_provider: VisualTextProviderPort | None,
        force_visual_pages: frozenset[int],
        auto_escalation: bool,
        plan_sha256: str,
    ) -> None:
        if not enhancement_providers and complex_provider is None:
            raise ValueError("composable visual chain requires at least one provider")
        self._enhancement_providers = enhancement_providers
        self._complex_provider = complex_provider
        self._force_visual_pages = force_visual_pages
        self._auto_escalation = auto_escalation
        self._receipts: dict[int, PageProcessingReceiptV1] = {}
        self._state_lock = RLock()
        self._terminal_provider_failures: dict[
            tuple[str, str], VisualTextProviderRejected
        ] = {}
        identity = [
            (
                provider.provider_id,
                provider.provider_version,
            )
            for provider in (
                *enhancement_providers,
                *((complex_provider,) if complex_provider is not None else ()),
            )
        ]
        self._provider_call_locks = {
            provider_identity: Lock() for provider_identity in identity
        }
        digest = hashlib.sha256(
            json.dumps(
                {
                    "plan_sha256": plan_sha256,
                    "providers": identity,
                    "force_visual_pages": sorted(force_visual_pages),
                    "auto_escalation": auto_escalation,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        self.provider_version = f"{COMPOSABLE_VISUAL_CHAIN_VERSION};{digest}"

    @property
    def page_receipts(self) -> tuple[PageProcessingReceiptV1, ...]:
        with self._state_lock:
            return tuple(self._receipts[key] for key in sorted(self._receipts))

    def recognize_page(
        self,
        page_input: VisualTextPageInput,
    ) -> VisualTextSnapshot:
        forced = page_input.page_number in self._force_visual_pages
        if forced:
            providers = (
                (self._complex_provider,)
                if self._complex_provider is not None
                else self._enhancement_providers[-1:]
            )
        else:
            providers = self._enhancement_providers
            if self._auto_escalation and self._complex_provider is not None:
                providers = (*providers, self._complex_provider)
            elif not providers and self._complex_provider is not None:
                providers = (self._complex_provider,)

        attempted: list[str] = []
        failures: list[str] = []
        adopted: VisualTextSnapshot | None = None
        adopted_provider_id = ""
        last_error: Exception | None = None
        for index, provider in enumerate(providers):
            attempted.append(provider.provider_id)
            try:
                candidate = self._recognize_with_provider(
                    provider,
                    page_input,
                )
            except Exception as exc:
                last_error = exc
                failures.append(_safe_provider_failure_code(provider, exc))
                continue
            adopted = candidate
            adopted_provider_id = provider.provider_id
            is_last = index == len(providers) - 1
            requires_full_layout = (
                not is_last
                and self._complex_provider is not None
                and providers[index + 1] is self._complex_provider
            )
            if (
                _visual_snapshot_is_adequate(
                    candidate,
                    require_full_layout=requires_full_layout,
                )
                or is_last
            ):
                break
        if adopted is None:
            self._store_receipt(
                PageProcessingReceiptV1(
                    page_number=page_input.page_number,
                    page_image_sha256=page_input.page_image_sha256,
                    attempted_provider_ids=tuple(attempted),
                    adopted_provider_id="upstream:primary-parser",
                    state="DEGRADED",
                    degraded_reason_codes=tuple(failures),
                )
            )
            if last_error is not None:
                raise last_error
            raise RuntimeError("composable_visual_chain_no_result")
        state = "DEGRADED" if failures else "READY"
        self._store_receipt(
            PageProcessingReceiptV1(
                page_number=page_input.page_number,
                page_image_sha256=page_input.page_image_sha256,
                attempted_provider_ids=tuple(attempted),
                adopted_provider_id=adopted_provider_id,
                state=state,
                degraded_reason_codes=tuple(failures),
            )
        )
        return _rebind_snapshot(
            adopted,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
        )

    def _recognize_with_provider(
        self,
        provider: VisualTextProviderPort,
        page_input: VisualTextPageInput,
    ) -> VisualTextSnapshot:
        provider_identity = (
            str(provider.provider_id),
            str(provider.provider_version),
        )
        provider_lock = self._provider_call_locks[provider_identity]
        with provider_lock:
            with self._state_lock:
                cached_failure = self._terminal_provider_failures.get(provider_identity)
            if cached_failure is not None:
                raise cached_failure
            try:
                return provider.recognize_page(page_input)
            except VisualTextProviderRejected as exc:
                if exc.code is VisualTextProviderRejectionCode.UNAVAILABLE:
                    # Authentication/configuration failures are provider-wide.
                    # The provider lock makes this first-call check atomic so a
                    # page fan-out cannot repeat a doomed request.
                    with self._state_lock:
                        self._terminal_provider_failures[provider_identity] = exc
                raise

    def _store_receipt(self, receipt: PageProcessingReceiptV1) -> None:
        with self._state_lock:
            self._receipts[receipt.page_number] = receipt


def build_composable_pdf_source_ir_adapter(
    plan: ResolvedProcessingPlanV1,
    *,
    source_asset_publisher=None,
    page_progress_callback: Callable[[int, int, int], None] | None = None,
    page_staging_callback: Callable[[dict[str, Any]], None] | None = None,
    visual_text_snapshot_cache: VisualTextSnapshotCacheBinding | None = None,
    provider_telemetry_callback: VisualTextProviderTelemetryCallback | None = None,
) -> PdfSourceIRAdapter:
    frozen_enhancements = _resolve_frozen_enhancement_bindings(plan)
    enhancement = tuple(
        _cache_actual_provider(
            _build_enhancement_provider(
                binding.stage,
                frozen_model=binding.model,
                telemetry_callback=provider_telemetry_callback,
            ),
            cache_binding=visual_text_snapshot_cache,
            telemetry_callback=provider_telemetry_callback,
        )
        for binding in frozen_enhancements
    )
    raw_complex_provider = _build_complex_provider(
        plan,
        telemetry_callback=provider_telemetry_callback,
    )
    complex_provider = (
        _cache_actual_provider(
            raw_complex_provider,
            cache_binding=visual_text_snapshot_cache,
            telemetry_callback=provider_telemetry_callback,
        )
        if raw_complex_provider is not None
        else None
    )
    provider = ComposablePageVisualProvider(
        enhancement_providers=enhancement,
        complex_provider=complex_provider,
        force_visual_pages=frozenset(plan.force_visual_pages),
        auto_escalation=plan.document.auto_escalation,
        plan_sha256=plan.canonical_sha256,
    )
    frozen_ocr_binding = next(
        (binding for binding in frozen_enhancements if binding.stage == "bailian_ocr"),
        None,
    )
    visual_table_provider: VisualTableProviderPort | None = None
    if frozen_ocr_binding is not None:
        assert frozen_ocr_binding.model is not None
        visual_table_provider = PlanBoundVisualTableProvider(
            _build_bailian_visual_table_provider(
                frozen_model=frozen_ocr_binding.model,
            ),
            provider_id=provider.provider_id,
            processing_plan_sha256=plan.canonical_sha256,
            connection_revision=plan.connection_revision,
        )
        if visual_text_snapshot_cache is not None:
            visual_table_provider = visual_text_snapshot_cache.wrap_visual_table(
                visual_table_provider,
                processing_plan_sha256=plan.canonical_sha256,
            )
    return PdfSourceIRAdapter(
        visual_text_provider=provider,
        visual_text_page_numbers=None,
        force_visual_text_page_numbers=frozenset(plan.force_visual_pages),
        visual_table_provider=visual_table_provider,
        visual_table_on_visual_text_pages=visual_table_provider is not None,
        source_asset_publisher=source_asset_publisher,
        page_progress_callback=page_progress_callback,
        page_staging_callback=page_staging_callback,
    )


def page_processing_receipts_from_adapter(
    adapter: PdfSourceIRAdapter,
) -> tuple[PageProcessingReceiptV1, ...]:
    provider = getattr(adapter, "_visual_text_provider", None)
    if isinstance(provider, ComposablePageVisualProvider):
        stats_by_page = getattr(
            adapter,
            "_visual_table_processing_stats",
            {},
        )
        blocker_codes_by_page = getattr(
            adapter,
            "_page_processing_blocker_reason_codes",
            {},
        )
        table_enabled = getattr(adapter, "_visual_table_provider", None) is not None
        merged_receipts: list[PageProcessingReceiptV1] = []
        for receipt in provider.page_receipts:
            page_blocker_codes = tuple(
                sorted(blocker_codes_by_page.get(receipt.page_number, ()))
            )
            degraded_reason_codes = tuple(
                dict.fromkeys((*receipt.degraded_reason_codes, *page_blocker_codes))
            )
            merged_receipts.append(
                replace(
                    receipt,
                    state=(
                        "DEGRADED"
                        if receipt.state == "DEGRADED" or page_blocker_codes
                        else "READY"
                    ),
                    degraded_reason_codes=degraded_reason_codes,
                    visual_table_mode=(
                        "NATIVE_TABLE_CROPS" if table_enabled else "OFF"
                    ),
                    visual_table_candidate_count=int(
                        stats_by_page.get(receipt.page_number, {}).get(
                            "candidate_count",
                            0,
                        )
                    ),
                    visual_table_attempt_count=int(
                        stats_by_page.get(receipt.page_number, {}).get(
                            "attempt_count",
                            0,
                        )
                    ),
                    visual_table_ready_count=int(
                        stats_by_page.get(receipt.page_number, {}).get(
                            "ready_count",
                            0,
                        )
                    ),
                    visual_table_failed_count=int(
                        stats_by_page.get(receipt.page_number, {}).get(
                            "failed_count",
                            0,
                        )
                    ),
                    visual_table_budget_skipped_count=int(
                        stats_by_page.get(receipt.page_number, {}).get(
                            "budget_skipped_count",
                            0,
                        )
                    ),
                )
            )
        return tuple(merged_receipts)
    return ()


def _build_enhancement_provider(
    stage: str,
    *,
    frozen_model: str | None = None,
    telemetry_callback: VisualTextProviderTelemetryCallback | None = None,
) -> VisualTextProviderPort:
    if stage == "azure_document_intelligence":
        return AzureDocumentIntelligenceVisualTextProvider(
            endpoint=get_runtime_ai_config_value(
                "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
            ),
            api_key=get_runtime_ai_config_value("AZURE_DOCUMENT_INTELLIGENCE_API_KEY"),
        )
    if stage == "bailian_layout":
        if not frozen_model:
            raise ValueError("Bailian layout model is not frozen in the plan")
        config = load_ai_copilot_config("aliyun")
        return BailianPageLayoutProvider(
            api_key=config.dashscope_api_key,
            base_url=config.dashscope_base_url,
            model=frozen_model,
            timeout_seconds=_bounded_page_provider_timeout(config),
            max_retries=_bounded_page_provider_retries(config),
            telemetry_callback=telemetry_callback,
        )
    if stage == "bailian_ocr":
        if not frozen_model:
            raise ValueError("Bailian OCR model is not frozen in the plan")
        config = load_ai_copilot_config("aliyun")
        return BailianVisualTextProvider(
            api_key=config.dashscope_api_key,
            base_url=config.dashscope_base_url,
            model=frozen_model,
            timeout_seconds=_bounded_page_provider_timeout(config),
            max_retries=0,
            telemetry_callback=telemetry_callback,
        )
    raise ValueError("unsupported document enhancement provider")


def _build_bailian_visual_table_provider(
    *,
    frozen_model: str,
) -> VisualTableProviderPort:
    if not frozen_model:
        raise ValueError("Bailian OCR table model is not frozen in the plan")
    config = load_ai_copilot_config("aliyun")
    return BailianVisualTableProvider(
        api_key=config.dashscope_api_key,
        base_url=config.dashscope_base_url,
        model=frozen_model,
        timeout_seconds=_bounded_page_provider_timeout(config),
        max_retries=0,
    )


def _resolve_frozen_enhancement_bindings(
    plan: ResolvedProcessingPlanV1,
) -> tuple[_FrozenEnhancementBinding, ...]:
    stages = tuple(plan.document.enhancement_chain)
    provider_bindings = tuple(plan.provider_bindings.enhancement_providers)
    if len(stages) != len(provider_bindings):
        raise ValueError("Frozen enhancement stages and providers do not match")
    resolved: list[_FrozenEnhancementBinding] = []
    for stage, provider_binding in zip(stages, provider_bindings, strict=True):
        if stage == "azure_document_intelligence":
            if provider_binding != "azure:document-intelligence:prebuilt-layout":
                raise ValueError("Azure enhancement provider binding is not frozen")
            resolved.append(_FrozenEnhancementBinding(stage=stage, model=None))
            continue
        prefix = {
            "bailian_layout": "aliyun:bailian-layout:",
            "bailian_ocr": "aliyun:bailian-ocr:",
        }.get(stage)
        if prefix is None or not provider_binding.startswith(prefix):
            raise ValueError("Enhancement provider binding does not match its stage")
        model = provider_binding.removeprefix(prefix).strip()
        if not model or len(model) > 120 or ":" in model:
            raise ValueError("Enhancement model binding is invalid")
        resolved.append(_FrozenEnhancementBinding(stage=stage, model=model))
    return tuple(resolved)


def _build_complex_provider(
    plan: ResolvedProcessingPlanV1,
    *,
    telemetry_callback: VisualTextProviderTelemetryCallback | None = None,
) -> VisualTextProviderPort | None:
    if plan.document.complex_vision == "azure_gpt56_sol":
        config = load_ai_copilot_config("azure")
        deployment = plan.provider_bindings.complex_vision_deployment
        if not deployment:
            raise ValueError("Azure Sol visual deployment is not frozen")
        return AzureGpt56SolPageLayoutProvider(
            endpoint=config.azure_openai_endpoint,
            deployment=deployment,
            api_version=config.azure_openai_api_version,
            token_provider=_azure_openai_token_provider(
                config.azure_allow_interactive_login
            ),
        )
    if plan.document.complex_vision == "bailian_vision":
        config = load_ai_copilot_config("aliyun")
        return BailianPageLayoutProvider(
            api_key=config.dashscope_api_key,
            base_url=config.dashscope_base_url,
            model=(
                plan.provider_bindings.complex_vision_deployment or config.chat_model
            ),
            timeout_seconds=_bounded_page_provider_timeout(config),
            max_retries=_bounded_page_provider_retries(config),
            telemetry_callback=telemetry_callback,
        )
    return None


def _cache_actual_provider(
    provider: VisualTextProviderPort,
    *,
    cache_binding: VisualTextSnapshotCacheBinding | None,
    telemetry_callback: VisualTextProviderTelemetryCallback | None,
) -> VisualTextProviderPort:
    if cache_binding is None:
        return provider
    return cache_binding.wrap(
        provider,
        telemetry_callback=telemetry_callback,
    )


def _bounded_page_provider_timeout(config: object) -> float:
    try:
        configured = float(getattr(config, "llm_timeout_seconds", 45.0))
    except (TypeError, ValueError):
        configured = 45.0
    if configured <= 0:
        configured = 45.0
    return min(configured, 45.0)


def _bounded_page_provider_retries(config: object) -> int:
    configured = getattr(config, "openai_max_retries", 0)
    if isinstance(configured, bool):
        return 0
    try:
        return min(max(int(configured), 0), 1)
    except (TypeError, ValueError):
        return 0


def _visual_snapshot_is_adequate(
    snapshot: VisualTextSnapshot,
    *,
    require_full_layout: bool,
) -> bool:
    if not snapshot.observations:
        return False
    confidences = tuple(
        item.confidence for item in snapshot.observations if item.confidence is not None
    )
    if confidences and sum(confidences) / len(confidences) < 0.65:
        return False
    if require_full_layout and len(snapshot.evaluated_content_types) < 6:
        return False
    for observation in snapshot.observations:
        if observation.content_type == "TABLE" and observation.layout_table is None:
            return False
    return True


def _safe_provider_failure_code(
    provider: VisualTextProviderPort,
    exc: Exception,
) -> str:
    if isinstance(exc, VisualTextProviderRejected):
        return f"{provider.provider_id}:{exc.code.value}"
    return f"{provider.provider_id}:provider_failed"


def _rebind_snapshot(
    snapshot: VisualTextSnapshot,
    *,
    provider_id: str,
    provider_version: str,
) -> VisualTextSnapshot:
    observations = tuple(
        VisualTextObservation(
            **{
                **item.model_dump(mode="python"),
                "observation_id": stable_visual_text_observation_id(
                    source_sha256=item.source_sha256,
                    page_image_sha256=item.page_image_sha256,
                    provider_id=provider_id,
                    provider_version=provider_version,
                    page_number=item.page_number,
                    observation_index=item.observation_index,
                    bbox=item.bbox,
                ),
                "provider_id": provider_id,
                "provider_version": provider_version,
            }
        )
        for item in snapshot.observations
    )
    snapshot_id = stable_visual_text_snapshot_id(
        source_sha256=snapshot.source_sha256,
        page_image_sha256=snapshot.page_image_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=snapshot.page_number,
        page_bbox=snapshot.page_bbox,
        page_image_width_px=snapshot.page_image_width_px,
        page_image_height_px=snapshot.page_image_height_px,
    )
    return VisualTextSnapshot(
        **{
            **snapshot.model_dump(mode="python"),
            "visual_text_snapshot_id": snapshot_id,
            "provider_id": provider_id,
            "provider_version": provider_version,
            "observations": observations,
        }
    )


def _rebind_visual_table_snapshot(
    snapshot: VisualTableSnapshot,
    *,
    provider_id: str,
    provider_version: str,
) -> VisualTableSnapshot:
    snapshot_id = stable_visual_table_snapshot_id(
        source_sha256=snapshot.source_sha256,
        crop_image_sha256=snapshot.crop_image_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=snapshot.page_number,
        table_source_node_id=snapshot.table_source_node_id,
        table_id=snapshot.table_id,
        table_index=snapshot.table_index,
        table_bbox=snapshot.table_bbox,
        crop_image_width_px=snapshot.crop_image_width_px,
        crop_image_height_px=snapshot.crop_image_height_px,
    )
    cells = tuple(
        VisualTableCell(
            **{
                **cell.model_dump(mode="python"),
                "visual_table_cell_id": stable_visual_table_cell_id(
                    visual_table_snapshot_id=snapshot_id,
                    row=cell.row,
                    column=cell.column,
                    row_span=cell.row_span,
                    column_span=cell.column_span,
                ),
            }
        )
        for cell in snapshot.cells
    )
    return VisualTableSnapshot(
        **{
            **snapshot.model_dump(mode="python"),
            "visual_table_snapshot_id": snapshot_id,
            "provider_id": provider_id,
            "provider_version": provider_version,
            "cells": cells,
        }
    )


__all__ = [
    "COMPOSABLE_VISUAL_CHAIN_PROVIDER_ID",
    "PLAN_BOUND_VISUAL_TABLE_VERSION",
    "ComposablePageVisualProvider",
    "PageProcessingReceiptV1",
    "PlanBoundVisualTableProvider",
    "build_composable_pdf_source_ir_adapter",
    "page_processing_receipts_from_adapter",
]
