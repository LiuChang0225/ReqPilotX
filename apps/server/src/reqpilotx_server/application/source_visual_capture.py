"""Neutral, resumable visual enrichment for one immutable source capture."""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reqpilotx_server.adapters.documents.bailian_page_layout_provider import (
    DEFAULT_BAILIAN_PAGE_LAYOUT_MODEL,
    BailianPageLayoutProvider,
)
from reqpilotx_server.adapters.documents.bailian_pdf_page_parsing_provider import (
    BAILIAN_QWEN38_PDF_PAGE_PROVIDER_ID,
    DEFAULT_BAILIAN_QWEN38_PDF_MODEL,
    BailianQwen38PdfPageParsingProvider,
)
from reqpilotx_server.adapters.documents.bailian_visual_text_provider import (
    BAILIAN_VISUAL_TEXT_PROVIDER_ID,
    DEFAULT_BAILIAN_OCR_MODEL,
    BailianVisualTextProvider,
)
from reqpilotx_server.adapters.documents.pdf_source_ir_adapter import render_pdf_visual_page_input
from reqpilotx_server.adapters.models.reading_json_chat import load_reading_ai_config
from reqpilotx_server.adapters.persistence.sql_source_capture import SqlSourceFactsReader
from reqpilotx_server.adapters.persistence.sql_source_visual_capture import (
    SqlSourceVisualCaptureRepository,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.reading_visual_configuration import (
    READING_VISUAL_PROVIDER,
    load_reading_visual_environment,
)
from reqpilotx_server.application.source_capture import (
    capture_current_document_source,
    require_current_source_document,
)
from reqpilotx_server.application.source_runtime import resolve_source_record_path
from reqpilotx_server.domain.extraction.source_ir import (
    NativeDocumentFormat,
    SourceIRSnapshot,
    SourceNodeKind,
)
from reqpilotx_server.domain.source_visual_text import VISUAL_CONTENT_TYPES, VisualTextSnapshot
from reqpilotx_server.domain.source_visual_capture import (
    MAX_VISUAL_CAPTURE_PAGES,
    PDF_PAGE_RENDERER_ID,
    PDF_PAGE_RENDERER_VERSION,
    PDF_PAGE_RENDER_SCALE,
    PDF_VISUAL_RISK_POLICY_VERSION,
    VISUAL_CAPTURE_PROFILE_ID,
    CaptureQualityManifestV1,
    CaptureQualityPageV1,
    CaptureQualityStatus,
    PageCaptureStatus,
    PageReadableMode,
    RequiredVisualCapability,
    VisualCaptureGenerationV1,
    VisualPageContentState,
    VisualPageCapturePublicV1,
    VisualPageCaptureV1,
    stable_effective_bundle_id,
    stable_visual_capture_generation_id,
    visual_observation_text,
    visual_text_run_from_observation,
)
from reqpilotx_server.ports.source_capture_repository import SourceCaptureRecord, SourceFactsReaderPort
from reqpilotx_server.ports.source_visual_capture_repository import (
    SourceVisualCaptureRepositoryConflict,
    SourceVisualCaptureRepositoryPort,
    SourceVisualFactsReaderPort,
    VisualPageFailureRecord,
)
from reqpilotx_server.ports.visual_text_provider import (
    VisualTextProviderPort,
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)


_HARD_VISUAL_RISK_CODES = frozenset(
    {
        "pdf_full_page_image_with_invisible_text_layer",
        "pdf_full_page_image_with_low_native_text_coverage",
        "pdf_full_page_image_without_visible_text",
        "pdf_vector_text_with_low_native_unicode_coverage",
    }
)
_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_VISUAL_LAYOUT_PROVIDER_NAME = "bailian-qwen-page-layout"
_VISUAL_OCR_PROVIDER_NAME = "bailian-qwen-ocr"
_VISUAL_PDF_PROVIDER_NAME = READING_VISUAL_PROVIDER
_READING_PAGE_LAYOUT_PROVIDER_ID = "aliyun-bailian-qwen-page-layout"
_PAGE_CAPTURE_LOCKS_GUARD = threading.Lock()
_PAGE_CAPTURE_LOCKS: dict[tuple[str, str, int], threading.Lock] = {}


class ReadingBailianPageLayoutProvider(BailianPageLayoutProvider):
    """Reading-owned identity for the shared Layout implementation.

    Extraction callers keep the provider class's historical OCR-compatible ID;
    neutral source capture persists the recognizer it actually invoked.
    """

    provider_id = _READING_PAGE_LAYOUT_PROVIDER_ID


class SourceVisualCaptureService:
    """Application boundary that never imports or starts extraction workflows."""

    def __init__(
        self,
        *,
        visual_repository: SourceVisualCaptureRepositoryPort | None = None,
        source_facts_reader: SourceFactsReaderPort | None = None,
        source_capture: Callable[..., SourceCaptureRecord] | None = None,
        current_document: Callable[..., dict[str, Any]] | None = None,
        source_path_resolver: Callable[[dict[str, Any]], Path] | None = None,
        provider_factory: Callable[[], VisualTextProviderPort] | None = None,
        page_capture_builder: Callable[
            [Path, int, VisualTextProviderPort, datetime], VisualTextSnapshot
        ]
        | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._visual_repository = (
            visual_repository or SqlSourceVisualCaptureRepository()
        )
        self._source_facts_reader = source_facts_reader or SqlSourceFactsReader()
        self._source_capture = source_capture or capture_current_document_source
        self._current_document = current_document or require_current_source_document
        self._source_path_resolver = source_path_resolver or resolve_source_record_path
        self._provider_factory = provider_factory or build_reading_visual_provider
        self._page_capture_builder = page_capture_builder or _capture_pdf_page
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def quality(
        self, *, project_id: str, sor_document_id: str
    ) -> CaptureQualityManifestV1:
        capture = self._capture(project_id, sor_document_id, prepare=False)
        return build_capture_quality_manifest(capture, self._visual_repository)

    def get_page(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        page_number: int,
    ) -> VisualPageCapturePublicV1:
        _require_page_number(page_number)
        capture = self._capture(project_id, sor_document_id, prepare=False)
        generation = self._visual_repository.get_active_generation(
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=capture.source_sha256,
        )
        if generation is None:
            raise _page_not_found(page_number)
        page = self._visual_repository.get_page(
            generation_id=generation.generation_id,
            page_number=page_number,
        )
        if page is None:
            raise _page_not_found(page_number)
        return VisualPageCapturePublicV1.from_internal(page)

    def capture_page(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        page_number: int,
    ) -> VisualPageCapturePublicV1:
        """Serialize one page per process and recheck persistence after waiting."""

        _require_page_number(page_number)
        lock = _page_capture_lock(project_id, sor_document_id, page_number)
        with lock:
            return self._capture_page_unlocked(
                project_id=project_id,
                sor_document_id=sor_document_id,
                page_number=page_number,
            )

    def _capture_page_unlocked(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        page_number: int,
    ) -> VisualPageCapturePublicV1:
        _require_page_number(page_number)
        capture = self._capture(project_id, sor_document_id, prepare=True)
        manifest = build_capture_quality_manifest(capture, self._visual_repository)
        if page_number > manifest.page_count:
            raise _page_not_found(page_number)
        quality_page = manifest.pages[page_number - 1]
        if quality_page.required_capability is RequiredVisualCapability.NONE:
            raise ApplicationError(
                409,
                {
                    "reason_code": "visual_page_not_required",
                    "message": "This page already has trusted native selectable facts.",
                    "page_number": page_number,
                },
                code="visual_page_not_required",
            )
        provider = self._provider_factory()
        if not _provider_satisfies_capability(
            provider,
            quality_page.required_capability,
        ):
            raise ApplicationError(
                409,
                {
                    "reason_code": "reading_visual_provider_capability_insufficient",
                    "message": (
                        "The configured reading visual provider cannot satisfy "
                        "this page's required capture capability."
                    ),
                    "page_number": page_number,
                    "required_capability": quality_page.required_capability.value,
                    "provider_id": str(provider.provider_id),
                },
                code="reading_visual_provider_capability_insufficient",
            )
        created_at = _aware_now(self._clock)
        required_pages = tuple(
            page.page_number
            for page in manifest.pages
            if page.required_capability is not RequiredVisualCapability.NONE
        )
        generation = VisualCaptureGenerationV1(
            generation_id=stable_visual_capture_generation_id(
                project_id=project_id,
                sor_document_id=sor_document_id,
                source_sha256=capture.source_sha256,
                base_capture_id=capture.capture_id,
                capture_profile_id=VISUAL_CAPTURE_PROFILE_ID,
                risk_policy_version=PDF_VISUAL_RISK_POLICY_VERSION,
                renderer_id=PDF_PAGE_RENDERER_ID,
                renderer_version=PDF_PAGE_RENDERER_VERSION,
                provider_id=str(provider.provider_id),
                provider_version=str(provider.provider_version),
                required_page_numbers=required_pages,
            ),
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=capture.source_sha256,
            base_capture_id=capture.capture_id,
            base_source_ir_id=capture.source_ir.source_ir_id,
            provider_id=str(provider.provider_id),
            provider_version=str(provider.provider_version),
            required_page_numbers=required_pages,
            created_at_utc=created_at,
        )
        try:
            generation = self._visual_repository.append_generation(generation)
        except SourceVisualCaptureRepositoryConflict as exc:
            if "superseded" in str(exc):
                raise _generation_superseded() from exc
            raise _source_changed() from exc
        existing = self._visual_repository.get_page(
            generation_id=generation.generation_id,
            page_number=page_number,
        )
        if existing is not None:
            return VisualPageCapturePublicV1.from_internal(existing)

        document = self._current_document(
            project_id=project_id,
            sor_document_id=sor_document_id,
            expected_source_sha256=capture.source_sha256,
        )
        try:
            snapshot = self._page_capture_builder(
                self._source_path_resolver(document),
                page_number,
                provider,
                created_at,
            )
        except VisualTextProviderRejected as exc:
            failure_code = f"pdf_visual_text_{exc.code.value}"
            self._record_failure(
                generation=generation,
                page_number=page_number,
                failure_code=failure_code,
            )
            raise _capture_failed(page_number, failure_code) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            self._record_failure(
                generation=generation,
                page_number=page_number,
                failure_code="visual_capture_failed",
            )
            raise _capture_failed(page_number, "visual_capture_failed") from exc

        if snapshot.source_sha256 != capture.source_sha256:
            self._record_failure(
                generation=generation,
                page_number=page_number,
                failure_code="visual_source_hash_mismatch",
            )
            raise _source_changed()
        if snapshot.page_number != page_number:
            failure_code = "pdf_visual_text_page_mismatch"
            self._record_failure(
                generation=generation,
                page_number=page_number,
                failure_code=failure_code,
            )
            raise _capture_failed(page_number, failure_code)
        if (
            snapshot.provider_id != provider.provider_id
            or snapshot.provider_version != provider.provider_version
        ):
            self._record_failure(
                generation=generation,
                page_number=page_number,
                failure_code="visual_provider_identity_mismatch",
            )
            raise _capture_failed(page_number, "visual_provider_identity_mismatch")

        self._current_document(
            project_id=project_id,
            sor_document_id=sor_document_id,
            expected_source_sha256=capture.source_sha256,
        )
        try:
            runs = tuple(
                visual_text_run_from_observation(item)
                for item in snapshot.observations
                if visual_observation_text(item).strip()
            )
        except (TypeError, ValueError) as exc:
            failure_code = "pdf_visual_text_projection_invalid"
            self._record_failure(
                generation=generation,
                page_number=page_number,
                failure_code=failure_code,
            )
            raise _capture_failed(page_number, failure_code) from exc
        if not snapshot.observations:
            if snapshot.evaluated_content_types != VISUAL_CONTENT_TYPES:
                failure_code = "pdf_visual_text_evaluation_incomplete"
                self._record_failure(
                    generation=generation,
                    page_number=page_number,
                    failure_code=failure_code,
                )
                raise _capture_failed(page_number, failure_code)
            content_state = VisualPageContentState.VERIFIED_EMPTY
        elif not runs:
            failure_code = "visual_image_semantics_not_supported"
            self._record_failure(
                generation=generation,
                page_number=page_number,
                failure_code=failure_code,
            )
            raise _capture_failed(page_number, failure_code)
        else:
            content_state = VisualPageContentState.SELECTABLE_TEXT
        try:
            page = VisualPageCaptureV1(
                generation_id=generation.generation_id,
                visual_snapshot_id=snapshot.visual_text_snapshot_id,
                project_id=project_id,
                sor_document_id=sor_document_id,
                source_sha256=capture.source_sha256,
                base_capture_id=capture.capture_id,
                page_number=page_number,
                page_bbox=snapshot.page_bbox,
                page_image_sha256=snapshot.page_image_sha256,
                width_px=snapshot.page_image_width_px,
                height_px=snapshot.page_image_height_px,
                render_scale=PDF_PAGE_RENDER_SCALE,
                renderer_id=PDF_PAGE_RENDERER_ID,
                renderer_version=PDF_PAGE_RENDERER_VERSION,
                provider_id=snapshot.provider_id,
                provider_version=snapshot.provider_version,
                content_state=content_state,
                runs=runs,
                visual_text_snapshot=snapshot,
                created_at_utc=created_at,
            )
        except (TypeError, ValueError) as exc:
            failure_code = "pdf_visual_text_projection_invalid"
            self._record_failure(
                generation=generation,
                page_number=page_number,
                failure_code=failure_code,
            )
            raise _capture_failed(page_number, failure_code) from exc
        try:
            stored = self._visual_repository.append_page(page)
        except SourceVisualCaptureRepositoryConflict as exc:
            raise _source_changed() from exc
        return VisualPageCapturePublicV1.from_internal(stored)

    def _capture(
        self,
        project_id: str,
        sor_document_id: str,
        *,
        prepare: bool,
    ) -> SourceCaptureRecord:
        document = self._current_document(
            project_id=project_id,
            sor_document_id=sor_document_id,
        )
        source_sha256 = str(document["source_sha256"])
        existing = self._source_facts_reader.get_capture(
            project_id=project_id,
            sor_document_id=sor_document_id,
            source_sha256=source_sha256,
        )
        if existing is not None:
            return existing
        if not prepare:
            raise ApplicationError(
                409,
                {
                    "reason_code": "source_capture_not_prepared",
                    "message": "Prepare the document source before reading capture quality.",
                },
                code="source_capture_not_prepared",
            )
        return self._source_capture(
            project_id=project_id,
            sor_document_id=sor_document_id,
            expected_source_sha256=source_sha256,
        )

    def _record_failure(
        self,
        *,
        generation: VisualCaptureGenerationV1,
        page_number: int,
        failure_code: str,
    ) -> None:
        now = _aware_now(self._clock)
        attempt = VisualPageFailureRecord(
            attempt_id=f"visual-attempt-{uuid.uuid4().hex}",
            generation_id=generation.generation_id,
            project_id=generation.project_id,
            sor_document_id=generation.sor_document_id,
            source_sha256=generation.source_sha256,
            page_number=page_number,
            failure_code=_safe_failure_code(failure_code),
            created_at_utc=now,
        )
        try:
            self._visual_repository.append_page_failure(attempt)
        except SourceVisualCaptureRepositoryConflict as exc:
            raise _source_changed() from exc


def get_capture_quality(
    project_id: str, document_id: str
) -> CaptureQualityManifestV1:
    return SourceVisualCaptureService().quality(
        project_id=project_id,
        sor_document_id=document_id,
    )


def get_visual_page(
    project_id: str, document_id: str, page_number: int
) -> VisualPageCapturePublicV1:
    return SourceVisualCaptureService().get_page(
        project_id=project_id,
        sor_document_id=document_id,
        page_number=page_number,
    )


def capture_visual_page(
    project_id: str, document_id: str, page_number: int
) -> VisualPageCapturePublicV1:
    return SourceVisualCaptureService().capture_page(
        project_id=project_id,
        sor_document_id=document_id,
        page_number=page_number,
    )


def build_reading_visual_provider(
    *, environment: Mapping[str, str] | None = None
) -> VisualTextProviderPort:
    """Build only the reading/source-capture Qwen route.

    Model, timeout, and retry policy use dedicated keys.  API credentials and
    the allow-listed DashScope base URL may be shared through reading config.
    Extraction processing profiles and visual route files are never read.
    """

    source = load_reading_visual_environment(environment=environment)
    provider_name = str(
        source.get("REQPILOT_READING_VISUAL_PROVIDER")
        or _VISUAL_PDF_PROVIDER_NAME
    ).strip().lower()
    if provider_name not in {
        _VISUAL_PDF_PROVIDER_NAME,
        _VISUAL_LAYOUT_PROVIDER_NAME,
        _VISUAL_OCR_PROVIDER_NAME,
    }:
        raise ApplicationError(
            409,
            {
                "reason_code": "reading_visual_provider_not_configured",
                "message": "The reading visual provider is not configured.",
            },
            code="reading_visual_provider_not_configured",
        )
    default_model = {
        _VISUAL_PDF_PROVIDER_NAME: DEFAULT_BAILIAN_QWEN38_PDF_MODEL,
        _VISUAL_LAYOUT_PROVIDER_NAME: DEFAULT_BAILIAN_PAGE_LAYOUT_MODEL,
        _VISUAL_OCR_PROVIDER_NAME: DEFAULT_BAILIAN_OCR_MODEL,
    }[provider_name]
    is_pdf_parsing = provider_name == _VISUAL_PDF_PROVIDER_NAME
    visual_model = str(
        source.get("REQPILOT_READING_VISUAL_MODEL") or default_model
    ).strip()
    timeout_seconds = _bounded_number(
        source.get("REQPILOT_READING_VISUAL_TIMEOUT_SECONDS"),
        default=300.0 if is_pdf_parsing else 90.0,
        minimum=1.0,
        maximum=300.0,
        field="REQPILOT_READING_VISUAL_TIMEOUT_SECONDS",
    )
    configured_max_retries = int(
        _bounded_number(
            source.get("REQPILOT_READING_VISUAL_MAX_RETRIES"),
            default=0.0 if is_pdf_parsing else 2.0,
            minimum=0.0,
            maximum=0.0 if is_pdf_parsing else 5.0,
            field="REQPILOT_READING_VISUAL_MAX_RETRIES",
            integer=True,
        )
    )
    # Reading historically accepted up to five retries for OCR/Layout.  The
    # shared page providers now enforce a one-retry execution ceiling, so keep
    # old reading-owned settings loadable while honoring that tighter runtime
    # contract.  PDF page parsing remains fixed at zero retries above.
    max_retries = min(configured_max_retries, 1)
    reading_environment = dict(source)
    reading_environment["REQPILOT_READING_AI_PROVIDER"] = "aliyun"
    reading_environment["REQPILOT_READING_AI_TIMEOUT_SECONDS"] = str(
        timeout_seconds
    )
    reading_environment["REQPILOT_READING_AI_MAX_RETRIES"] = str(max_retries)
    config = load_reading_ai_config(environment=reading_environment)
    if not config.ready or config.provider != "aliyun":
        raise ApplicationError(
            409,
            {
                "reason_code": "reading_visual_provider_not_configured",
                "message": "Reading-owned DashScope credentials are unavailable.",
            },
            code="reading_visual_provider_not_configured",
        )
    try:
        provider_type = {
            _VISUAL_PDF_PROVIDER_NAME: BailianQwen38PdfPageParsingProvider,
            _VISUAL_LAYOUT_PROVIDER_NAME: ReadingBailianPageLayoutProvider,
            _VISUAL_OCR_PROVIDER_NAME: BailianVisualTextProvider,
        }[provider_name]
        return provider_type(
            api_key=config.api_key,
            base_url=config.endpoint,
            model=visual_model,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
    except ValueError as exc:
        raise ApplicationError(
            409,
            {
                "reason_code": "reading_visual_configuration_invalid",
                "message": "The reading visual provider configuration is invalid.",
            },
            code="reading_visual_configuration_invalid",
        ) from exc


def build_capture_quality_manifest(
    capture: SourceCaptureRecord,
    repository: SourceVisualFactsReaderPort,
) -> CaptureQualityManifestV1:
    source_ir = capture.source_ir
    page_count = _page_count(source_ir)
    if page_count > MAX_VISUAL_CAPTURE_PAGES:
        raise ApplicationError(
            422,
            {
                "reason_code": "visual_capture_page_limit_exceeded",
                "message": (
                    "The document exceeds the supported single-document visual "
                    "capture page limit."
                ),
                "page_count": page_count,
                "maximum_page_count": MAX_VISUAL_CAPTURE_PAGES,
            },
            code="visual_capture_page_limit_exceeded",
        )
    page_text = {page: [] for page in range(1, page_count + 1)}
    page_bboxes: dict[int, list[tuple[float, float, float, float]]] = {
        page: [] for page in range(1, page_count + 1)
    }
    layout_signal_pages: set[int] = set()
    risk_codes: dict[int, set[str]] = {
        page: set() for page in range(1, page_count + 1)
    }
    for node in source_ir.nodes:
        page = node.native_locator.page_number
        if page is None or page > page_count:
            continue
        if node.raw_text:
            page_text[page].append(node.raw_text)
        elif node.table_payload is not None:
            page_text[page].extend(
                cell.raw_text for cell in node.table_payload.cells if cell.raw_text
            )
        if node.kind is SourceNodeKind.TABLE or node.table_payload is not None:
            layout_signal_pages.add(page)
        bbox = _positive_bbox(node.native_locator.bbox)
        if bbox is not None:
            page_bboxes[page].append(bbox)
    for diagnostic in source_ir.diagnostics:
        locator = diagnostic.native_locator
        page = locator.page_number if locator is not None else None
        if page is None or page > page_count:
            continue
        bbox = _positive_bbox(locator.bbox)
        if bbox is not None:
            page_bboxes[page].append(bbox)
        if diagnostic.code in _HARD_VISUAL_RISK_CODES:
            risk_codes[page].add(diagnostic.code)

    active = repository.get_active_generation(
        project_id=capture.project_id,
        sor_document_id=capture.sor_document_id,
        source_sha256=capture.source_sha256,
    )
    visual_pages = (
        {page.page_number: page for page in repository.list_pages(active.generation_id)}
        if active is not None
        else {}
    )
    pages: list[CaptureQualityPageV1] = []
    for page_number in range(1, page_count + 1):
        text = "".join(page_text[page_number])
        native_characters = len(text)
        native_cjk_characters = len(_CJK_PATTERN.findall(text))
        native_selectable = native_characters > 0
        page_risks = set(risk_codes[page_number])
        if not native_selectable:
            page_risks.add("pdf_page_without_native_selectable_text")
        risks = tuple(sorted(page_risks))
        requires_visual = bool(risks)
        required_capability = (
            RequiredVisualCapability.LAYOUT
            if page_number in layout_signal_pages
            else RequiredVisualCapability.OCR
        )
        visual = visual_pages.get(page_number) if requires_visual else None
        if not requires_visual:
            pages.append(
                CaptureQualityPageV1(
                    page_number=page_number,
                    page_bbox=_largest_bbox(page_bboxes[page_number]),
                    readable_mode=PageReadableMode.NATIVE,
                    capture_status=PageCaptureStatus.NOT_REQUIRED,
                    native_character_count=native_characters,
                    native_cjk_character_count=native_cjk_characters,
                    native_selectable=native_selectable,
                    risk_codes=(),
                    required_capability=RequiredVisualCapability.NONE,
                )
            )
            continue
        if visual is not None:
            pages.append(
                CaptureQualityPageV1(
                    page_number=page_number,
                    page_bbox=visual.page_bbox,
                    readable_mode=PageReadableMode.VISUAL,
                    capture_status=PageCaptureStatus.READY,
                    native_character_count=native_characters,
                    native_cjk_character_count=native_cjk_characters,
                    native_selectable=native_selectable,
                    risk_codes=risks,
                    required_capability=required_capability,
                    visual_snapshot_id=visual.visual_snapshot_id,
                    page_image_sha256=visual.page_image_sha256,
                    provider_id=visual.provider_id,
                    provider_version=visual.provider_version,
                )
            )
            continue
        failure = (
            repository.latest_page_failure(
                generation_id=active.generation_id,
                page_number=page_number,
            )
            if active is not None
            else None
        )
        pages.append(
            CaptureQualityPageV1(
                page_number=page_number,
                page_bbox=_largest_bbox(page_bboxes[page_number]),
                readable_mode=PageReadableMode.NONE,
                capture_status=(
                    PageCaptureStatus.FAILED
                    if failure is not None
                    else PageCaptureStatus.PENDING
                ),
                native_character_count=native_characters,
                native_cjk_character_count=native_cjk_characters,
                native_selectable=native_selectable,
                risk_codes=risks,
                required_capability=required_capability,
                failure_code=(failure.failure_code if failure is not None else None),
            )
        )
    page_tuple = tuple(pages)
    native_ready = sum(
        item.readable_mode is PageReadableMode.NATIVE for item in page_tuple
    )
    visual_required = sum(
        item.required_capability is not RequiredVisualCapability.NONE
        for item in page_tuple
    )
    visual_ready = sum(
        item.capture_status is PageCaptureStatus.READY for item in page_tuple
    )
    failed = sum(
        item.capture_status is PageCaptureStatus.FAILED for item in page_tuple
    )
    readable = native_ready + visual_ready
    if page_count == 0 or readable == page_count:
        status = CaptureQualityStatus.READY
    elif readable == 0:
        status = CaptureQualityStatus.BLOCKED
    else:
        status = CaptureQualityStatus.PARTIAL
    snapshot_ids = tuple(
        item.visual_snapshot_id
        for item in page_tuple
        if item.visual_snapshot_id is not None
    )
    return CaptureQualityManifestV1(
        project_id=capture.project_id,
        sor_document_id=capture.sor_document_id,
        source_sha256=capture.source_sha256,
        base_capture_id=capture.capture_id,
        base_source_ir_id=source_ir.source_ir_id,
        effective_bundle_id=stable_effective_bundle_id(
            base_capture_id=capture.capture_id,
            visual_generation_id=(active.generation_id if active else None),
            visual_snapshot_ids=snapshot_ids,
        ),
        status=status,
        page_count=page_count,
        native_ready_page_count=native_ready,
        visual_required_page_count=visual_required,
        visual_ready_page_count=visual_ready,
        failed_page_count=failed,
        active_visual_generation_id=(active.generation_id if active else None),
        pages=page_tuple,
    )


def _capture_pdf_page(
    source_path: Path,
    page_number: int,
    provider: VisualTextProviderPort,
    created_at: datetime,
) -> VisualTextSnapshot:
    del created_at
    page_input = render_pdf_visual_page_input(
        source_path,
        page_number=page_number,
        scale=PDF_PAGE_RENDER_SCALE,
    )
    snapshot = provider.recognize_page(page_input)
    if not _snapshot_matches_input(snapshot, page_input, provider):
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "The visual provider response is not bound to this rendered page.",
        )
    return snapshot


def _page_capture_lock(
    project_id: str,
    sor_document_id: str,
    page_number: int,
) -> threading.Lock:
    key = (project_id, sor_document_id, page_number)
    with _PAGE_CAPTURE_LOCKS_GUARD:
        lock = _PAGE_CAPTURE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PAGE_CAPTURE_LOCKS[key] = lock
        return lock


def _page_count(source_ir: SourceIRSnapshot) -> int:
    pages: set[int] = set()
    for node in source_ir.nodes:
        if node.native_locator.format is NativeDocumentFormat.PDF:
            if node.native_locator.page_number is not None:
                pages.add(node.native_locator.page_number)
    for asset in source_ir.assets:
        locator = asset.native_locator
        if locator is not None and locator.format is NativeDocumentFormat.PDF:
            if locator.page_number is not None:
                pages.add(locator.page_number)
    for diagnostic in source_ir.diagnostics:
        locator = diagnostic.native_locator
        if locator is not None and locator.format is NativeDocumentFormat.PDF:
            if locator.page_number is not None:
                pages.add(locator.page_number)
    pages.update(snapshot.page_number for snapshot in source_ir.visual_text_snapshots)
    return max(pages, default=0)


def _snapshot_matches_input(snapshot, page_input, provider) -> bool:
    return (
        isinstance(snapshot, VisualTextSnapshot)
        and snapshot.source_sha256 == page_input.source_sha256
        and snapshot.page_number == page_input.page_number
        and snapshot.page_bbox == page_input.page_bbox
        and snapshot.page_image_sha256 == page_input.page_image_sha256
        and snapshot.page_image_width_px == page_input.page_image_width_px
        and snapshot.page_image_height_px == page_input.page_image_height_px
        and snapshot.provider_id == provider.provider_id
        and snapshot.provider_version == provider.provider_version
    )


def _provider_satisfies_capability(
    provider: VisualTextProviderPort,
    required: RequiredVisualCapability,
) -> bool:
    provider_id = str(getattr(provider, "provider_id", "") or "")
    if required is RequiredVisualCapability.NONE:
        return True
    if required is RequiredVisualCapability.OCR:
        return provider_id in {
            BAILIAN_VISUAL_TEXT_PROVIDER_ID,
            _READING_PAGE_LAYOUT_PROVIDER_ID,
            BAILIAN_QWEN38_PDF_PAGE_PROVIDER_ID,
        }
    return provider_id in {
        _READING_PAGE_LAYOUT_PROVIDER_ID,
        BAILIAN_QWEN38_PDF_PAGE_PROVIDER_ID,
    }


def _largest_bbox(
    values: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    if not values:
        return None
    return max(values, key=lambda value: (value[2] - value[0]) * (value[3] - value[1]))


def _positive_bbox(
    value: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    if value is None or value[2] <= value[0] or value[3] <= value[1]:
        return None
    return tuple(float(item) for item in value)


def _bounded_number(
    raw: object,
    *,
    default: float,
    minimum: float,
    maximum: float,
    field: str,
    integer: bool = False,
) -> float:
    text = str(raw or "").strip()
    try:
        value = default if not text else float(text)
    except ValueError as exc:
        raise _invalid_visual_configuration(field) from exc
    if value < minimum or value > maximum or (integer and not value.is_integer()):
        raise _invalid_visual_configuration(field)
    return value


def _invalid_visual_configuration(field: str) -> ApplicationError:
    return ApplicationError(
        409,
        {
            "reason_code": "reading_visual_configuration_invalid",
            "message": f"{field} is outside the supported reading visual policy.",
        },
        code="reading_visual_configuration_invalid",
    )


def _safe_failure_code(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower())
    return (normalized[:160] or "visual_capture_failed").strip("_")


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("visual capture clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _require_page_number(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_VISUAL_CAPTURE_PAGES
    ):
        raise ApplicationError(
            422,
            {
                "reason_code": "visual_page_number_invalid",
                "message": (
                    "page_number must be a positive integer within the supported "
                    "single-document capture limit."
                ),
                "maximum_page_count": MAX_VISUAL_CAPTURE_PAGES,
            },
            code="visual_page_number_invalid",
        )


def _page_not_found(page_number: int) -> ApplicationError:
    return ApplicationError(
        404,
        {
            "reason_code": "visual_page_not_found",
            "message": "No current visual page capture exists.",
            "page_number": page_number,
        },
        code="visual_page_not_found",
    )


def _capture_failed(page_number: int, failure_code: str) -> ApplicationError:
    return ApplicationError(
        409,
        {
            "reason_code": "visual_page_capture_failed",
            "message": "The page remains unreadable because visual capture failed.",
            "page_number": page_number,
            "failure_code": _safe_failure_code(failure_code),
        },
        code="visual_page_capture_failed",
    )


def _source_changed() -> ApplicationError:
    return ApplicationError(
        409,
        {
            "reason_code": "reading_source_identity_changed",
            "message": "The document source changed while visual capture was running.",
        },
        code="reading_source_identity_changed",
    )


def _generation_superseded() -> ApplicationError:
    return ApplicationError(
        409,
        {
            "reason_code": "visual_capture_generation_superseded",
            "message": (
                "This visual provider revision was already superseded; select a new "
                "provider revision to recapture the page."
            ),
        },
        code="visual_capture_generation_superseded",
    )


__all__ = [
    "ReadingBailianPageLayoutProvider",
    "SourceVisualCaptureService",
    "build_capture_quality_manifest",
    "build_reading_visual_provider",
    "capture_visual_page",
    "get_capture_quality",
    "get_visual_page",
]
