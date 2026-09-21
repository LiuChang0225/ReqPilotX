from __future__ import annotations

from reqpilotx_server.resource_paths import development_path

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, TypeAlias

from reqpilotx_server.adapters.documents.azure_document_intelligence_visual_text_provider import (
    AZURE_DOCUMENT_INTELLIGENCE_VISUAL_TEXT_PROVIDER_ID,
    AzureDocumentIntelligenceVisualTextProvider,
)
from reqpilotx_server.adapters.documents.bailian_page_layout_provider import (
    DEFAULT_BAILIAN_FLASH_PAGE_LAYOUT_MODEL,
    DEFAULT_BAILIAN_PAGE_LAYOUT_MODEL,
    BailianPageLayoutProvider,
)
from reqpilotx_server.adapters.documents.bailian_visual_text_provider import (
    BAILIAN_VISUAL_TEXT_PROVIDER_ID,
    DEFAULT_BAILIAN_OCR_MODEL,
    BailianVisualTextProvider,
)
from reqpilotx_server.adapters.documents.bailian_visual_table_provider import (
    BailianVisualTableProvider,
)
from reqpilotx_server.adapters.documents.pdf_source_ir_adapter import PdfSourceIRAdapter
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot, SourceNodeKind
from reqpilotx_server.domain.source_visual_text import VisualTextPageInput, VisualTextSnapshot
from reqpilotx_server.ports.visual_text_provider import VisualTextProviderPort
from reqpilotx_server.ports.visual_table_provider import VisualTableProviderPort
from reqpilotx_server.runtime_paths import resolve_storage_dir


OCR_PROVIDER_BAILIAN = "bailian"
OCR_PROVIDER_BAILIAN_LAYOUT = "bailian-qwen3.7-plus-layout"
OCR_PROVIDER_BAILIAN_FLASH_LAYOUT = "bailian-qwen3.6-flash-layout"
OCR_PROVIDER_AZURE = "azure-document-intelligence"
SUPPORTED_PDF_VISUAL_PROVIDER_NAMES = frozenset(
    {
        OCR_PROVIDER_BAILIAN,
        OCR_PROVIDER_BAILIAN_LAYOUT,
        OCR_PROVIDER_BAILIAN_FLASH_LAYOUT,
        OCR_PROVIDER_AZURE,
    }
)
SUPPORTED_PDF_VISUAL_PROVIDER_IDS = frozenset(
    {
        BAILIAN_VISUAL_TEXT_PROVIDER_ID,
        AZURE_DOCUMENT_INTELLIGENCE_VISUAL_TEXT_PROVIDER_ID,
    }
)

_PROTOTYPE_DIR = development_path('prototype')
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_INPUT_ROTATIONS = frozenset({0, 90, 180, 270})
PDF_VISUAL_ROUTING_POLICY_ID = "native-first-simple/v1"
_VISUAL_ROUTE_REASON_BY_DIAGNOSTIC = MappingProxyType(
    {
        "pdf_full_page_image_with_invisible_text_layer": (
            "FULL_PAGE_IMAGE_UNTRUSTED_TEXT"
        ),
        "pdf_full_page_image_without_visible_text": ("FULL_PAGE_IMAGE_UNTRUSTED_TEXT"),
        "pdf_vector_text_with_low_native_unicode_coverage": (
            "DENSE_VECTOR_LOW_UNICODE"
        ),
    }
)


class PdfSilverPageAssetRoute(Protocol):
    """Routing-only view of one reviewed Silver page asset."""

    page_number: int
    input_rotation_cw_degrees: int
    source_asset_sha256: str | None
    page_image_sha256: str | None


class PdfSilverDocumentRoute(Protocol):
    """The only Silver fields consumed by the controlled visual runtime."""

    source_sha256: str
    allowed_visual_provider_ids: Sequence[str]
    page_assets: Sequence[PdfSilverPageAssetRoute]


RemotePdfVisualTextProvider: TypeAlias = (
    BailianVisualTextProvider
    | BailianPageLayoutProvider
    | AzureDocumentIntelligenceVisualTextProvider
)


class ExplicitPageVisualTextProviderRouter:
    """Dispatch only explicitly declared pages without provider fallback.

    All delegates must share one authorized provider ID. Their individual
    provider versions remain on returned snapshots; ``provider_version`` binds
    the complete routing map for runtime/checkpoint identity.
    """

    def __init__(
        self,
        *,
        default_provider: VisualTextProviderPort,
        providers_by_page: Mapping[int, VisualTextProviderPort],
    ) -> None:
        provider_id = str(getattr(default_provider, "provider_id", "") or "")
        default_version = str(
            getattr(default_provider, "provider_version", "") or ""
        )
        if not provider_id or not default_version:
            raise ValueError("default visual provider identity is incomplete")
        normalized: dict[int, VisualTextProviderPort] = {}
        route_versions: list[dict[str, object]] = []
        for page_number, provider in sorted(providers_by_page.items()):
            if (
                isinstance(page_number, bool)
                or not isinstance(page_number, int)
                or page_number < 1
            ):
                raise ValueError("page provider route has an invalid page number")
            delegate_id = str(getattr(provider, "provider_id", "") or "")
            delegate_version = str(
                getattr(provider, "provider_version", "") or ""
            )
            if delegate_id != provider_id or not delegate_version:
                raise ValueError(
                    "page provider route must preserve one authorized provider ID"
                )
            normalized[page_number] = provider
            route_versions.append(
                {
                    "page_number": page_number,
                    "provider_version": delegate_version,
                }
            )
        payload = {
            "schema": "explicit-page-visual-provider-router/v1",
            "provider_id": provider_id,
            "default_provider_version": default_version,
            "page_routes": route_versions,
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.provider_id = provider_id
        self.provider_version = f"explicit-page-router/v1:{digest}"
        self._default_provider = default_provider
        self._providers_by_page = MappingProxyType(normalized)

    def provider_version_for_page(self, page_number: int) -> str:
        provider = self._providers_by_page.get(
            page_number,
            self._default_provider,
        )
        return str(provider.provider_version)

    def recognize_page(
        self,
        page_input: VisualTextPageInput,
    ) -> VisualTextSnapshot:
        page_number = page_input.page_number
        provider = self._providers_by_page.get(
            page_number,
            self._default_provider,
        )
        return provider.recognize_page(page_input)


@dataclass(frozen=True, slots=True)
class PdfVisualTextPageBinding:
    page_number: int
    input_rotation_cw_degrees: int
    source_asset_sha256: str | None
    page_image_sha256: str | None


@dataclass(frozen=True, slots=True)
class PdfVisualTextRoute:
    """Source-bound routing facts, without any Silver semantic expectations."""

    source_sha256: str
    page_numbers: frozenset[int]
    input_rotations_by_page: Mapping[int, int]
    page_bindings: Mapping[int, PdfVisualTextPageBinding]
    allowed_visual_provider_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class PdfVisualTextRuntime:
    """A native-only or exact-page remote visual PDF adapter runtime."""

    pdf_adapter: PdfSourceIRAdapter
    visual_text_provider: VisualTextProviderPort | None
    visual_text_page_numbers: frozenset[int]
    visual_table_provider: VisualTableProviderPort | None
    visual_table_page_numbers: frozenset[int]
    route: PdfVisualTextRoute


def build_pdf_visual_text_runtime_from_silver(
    *,
    source: Path,
    silver_document: PdfSilverDocumentRoute,
    provider_name: str = OCR_PROVIDER_BAILIAN,
    bailian_model: str | None = None,
    visual_text_page_numbers: frozenset[int] | set[int] | tuple[int, ...] | None = None,
    page_provider_names_by_page: Mapping[int, str] | None = None,
    visual_table_page_numbers: frozenset[int]
    | set[int]
    | tuple[int, ...] = frozenset(),
) -> PdfVisualTextRuntime:
    """Build an exact-invocation-scope runtime from a full Silver route.

    Silver is used only as a source-bound input-routing registry here.  This
    function intentionally does not inspect fidelity, structure, semantic, or
    projection expectations and therefore cannot promote Silver to formal
    Gold evidence. Provider identity always binds the complete authorized
    rotation map; ``visual_text_page_numbers`` narrows adapter invocation only.
    """

    route = pdf_visual_text_route_from_silver(silver_document)
    selected_visual_text_pages = (
        route.page_numbers
        if visual_text_page_numbers is None
        else normalize_visual_table_page_numbers(
            visual_text_page_numbers,
            visual_route_page_numbers=route.page_numbers,
        )
    )
    selected_visual_table_pages = normalize_visual_table_page_numbers(
        visual_table_page_numbers,
        visual_route_page_numbers=selected_visual_text_pages,
    )
    normalized_page_provider_names = _normalize_page_provider_names_by_page(
        page_provider_names_by_page,
        selected_visual_text_pages=selected_visual_text_pages,
    )
    actual_source_sha256 = source_sha256(Path(source))
    if actual_source_sha256 != route.source_sha256:
        raise RuntimeError("pdf_visual_text_source_hash_mismatch")

    # Validate the selected runtime even for a native-only route. This keeps a
    # programmatic caller from smuggling a local OCR name through an empty
    # page scope while still guaranteeing that no credentials are touched.
    selected_provider_id = visual_text_provider_id(provider_name)
    if selected_provider_id not in route.allowed_visual_provider_ids:
        raise RuntimeError("visual_text_provider_not_allowed")
    if not selected_visual_text_pages:
        return PdfVisualTextRuntime(
            pdf_adapter=PdfSourceIRAdapter(
                visual_text_page_numbers=selected_visual_text_pages,
            ),
            visual_text_provider=None,
            visual_text_page_numbers=selected_visual_text_pages,
            visual_table_provider=None,
            visual_table_page_numbers=selected_visual_table_pages,
            route=route,
        )

    if provider_name == OCR_PROVIDER_AZURE and any(
        route.input_rotations_by_page.values()
    ):
        raise RuntimeError("azure_input_rotation_not_supported")

    default_provider = build_remote_visual_text_provider(
        provider_name=provider_name,
        bailian_model=bailian_model,
        # Provider identity remains bound to the complete reviewed route.
        # The adapter below owns the narrower invocation scope. In particular,
        # rebuilding a Bailian provider from a subset rotation map would create
        # a different provider_version and could misrepresent that subset as
        # the registered full-route profile.
        input_rotations_by_page=route.input_rotations_by_page,
    )
    if default_provider.provider_id != selected_provider_id:
        raise RuntimeError("visual_text_provider_identity_mismatch")
    routed_providers: dict[int, VisualTextProviderPort] = {}
    providers_by_name: dict[str, VisualTextProviderPort] = {
        provider_name: default_provider
    }
    for page_number, routed_provider_name in normalized_page_provider_names.items():
        routed_provider = providers_by_name.get(routed_provider_name)
        if routed_provider is None:
            routed_provider = build_remote_visual_text_provider(
                provider_name=routed_provider_name,
                bailian_model=None,
                input_rotations_by_page=route.input_rotations_by_page,
            )
            providers_by_name[routed_provider_name] = routed_provider
        if routed_provider.provider_id != selected_provider_id:
            raise RuntimeError("visual_text_provider_identity_mismatch")
        routed_providers[page_number] = routed_provider
    provider: VisualTextProviderPort = (
        ExplicitPageVisualTextProviderRouter(
            default_provider=default_provider,
            providers_by_page=routed_providers,
        )
        if routed_providers
        else default_provider
    )

    # The page OCR port remains provider-neutral.  Today only Bailian OCR has the
    # independently verified single-table parsing adapter; Azure stays explicit
    # and Qwen page layout already carries its own table grids.
    visual_table_provider: VisualTableProviderPort | None = None
    if selected_visual_table_pages.intersection(routed_providers):
        raise RuntimeError("visual_table_page_provider_override_not_supported")
    if selected_visual_table_pages and provider_name == OCR_PROVIDER_BAILIAN:
        visual_table_provider = build_remote_visual_table_provider(
            bailian_model=bailian_model,
        )

    return PdfVisualTextRuntime(
        pdf_adapter=PdfSourceIRAdapter(
            visual_text_provider=provider,
            visual_text_page_numbers=selected_visual_text_pages,
            visual_table_provider=visual_table_provider,
            visual_table_page_numbers=selected_visual_table_pages,
        ),
        visual_text_provider=provider,
        visual_text_page_numbers=selected_visual_text_pages,
        visual_table_provider=visual_table_provider,
        visual_table_page_numbers=selected_visual_table_pages,
        route=route,
    )


def _normalize_page_provider_names_by_page(
    values: Mapping[int, str] | None,
    *,
    selected_visual_text_pages: frozenset[int],
) -> Mapping[int, str]:
    if values is None:
        return MappingProxyType({})
    if not isinstance(values, Mapping):
        raise TypeError("page_provider_names_by_page must be a mapping")
    normalized: dict[int, str] = {}
    for page_number, provider_name in values.items():
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
        ):
            raise ValueError("page provider route has an invalid page number")
        normalized_provider_name = str(provider_name or "").strip()
        if normalized_provider_name not in SUPPORTED_PDF_VISUAL_PROVIDER_NAMES:
            raise ValueError("page provider route has an unsupported provider")
        if page_number not in selected_visual_text_pages:
            raise ValueError("page provider route is outside the selected page scope")
        normalized[page_number] = normalized_provider_name
    return MappingProxyType(dict(sorted(normalized.items())))


def pdf_visual_text_route_from_silver(
    silver_document: PdfSilverDocumentRoute,
) -> PdfVisualTextRoute:
    """Copy and validate only the input-routing fields of a Silver entry."""

    try:
        expected_source_sha256 = silver_document.source_sha256
        allowed_provider_values = silver_document.allowed_visual_provider_ids
        page_asset_values = silver_document.page_assets
    except AttributeError as exc:
        raise TypeError("silver document lacks PDF visual routing fields") from exc

    if (
        not isinstance(expected_source_sha256, str)
        or _SHA256_PATTERN.fullmatch(expected_source_sha256) is None
    ):
        raise ValueError("silver visual route has an invalid source sha256")
    if isinstance(allowed_provider_values, (str, bytes)):
        raise TypeError("allowed visual provider ids must be a sequence")
    allowed_provider_ids = tuple(allowed_provider_values)
    if (
        not allowed_provider_ids
        or any(not isinstance(item, str) for item in allowed_provider_ids)
        or len(allowed_provider_ids) != len(set(allowed_provider_ids))
        or not set(allowed_provider_ids).issubset(SUPPORTED_PDF_VISUAL_PROVIDER_IDS)
    ):
        raise ValueError("silver visual route has an invalid provider whitelist")
    if isinstance(page_asset_values, (str, bytes)):
        raise TypeError("silver page assets must be a sequence")

    rotations_by_page: dict[int, int] = {}
    bindings_by_page: dict[int, PdfVisualTextPageBinding] = {}
    for page_asset in page_asset_values:
        try:
            page_number = page_asset.page_number
            rotation = page_asset.input_rotation_cw_degrees
            source_asset_hash = page_asset.source_asset_sha256
            page_image_hash = page_asset.page_image_sha256
        except AttributeError as exc:
            raise TypeError("silver page asset lacks routing fields") from exc
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
        ):
            raise ValueError("silver visual route has an invalid page number")
        if page_number in rotations_by_page:
            raise ValueError("silver visual route has a duplicate page number")
        if (
            isinstance(rotation, bool)
            or not isinstance(rotation, int)
            or rotation not in _ALLOWED_INPUT_ROTATIONS
        ):
            raise ValueError("silver visual route has an invalid input rotation")
        if (source_asset_hash is None) == (page_image_hash is None):
            raise ValueError(
                "silver visual route requires exactly one page hash binding"
            )
        selected_hash = source_asset_hash or page_image_hash
        if (
            not isinstance(selected_hash, str)
            or _SHA256_PATTERN.fullmatch(selected_hash) is None
        ):
            raise ValueError("silver visual route has an invalid page hash")
        rotations_by_page[page_number] = rotation
        bindings_by_page[page_number] = PdfVisualTextPageBinding(
            page_number=page_number,
            input_rotation_cw_degrees=rotation,
            source_asset_sha256=source_asset_hash,
            page_image_sha256=page_image_hash,
        )

    ordered_rotations = {
        page_number: rotations_by_page[page_number]
        for page_number in sorted(rotations_by_page)
    }
    return PdfVisualTextRoute(
        source_sha256=expected_source_sha256,
        page_numbers=frozenset(ordered_rotations),
        input_rotations_by_page=MappingProxyType(ordered_rotations),
        page_bindings=MappingProxyType(
            {
                page_number: bindings_by_page[page_number]
                for page_number in sorted(bindings_by_page)
            }
        ),
        allowed_visual_provider_ids=frozenset(allowed_provider_ids),
    )


def validate_pdf_visual_text_capture(
    source_ir: SourceIRSnapshot,
    runtime: PdfVisualTextRuntime,
) -> None:
    """Fail closed unless the simple router's capture is exactly authorized.

    The reviewed route is an authorization ceiling, not an instruction to send
    every registered page. The native-first router selects only pages with one
    of two hard risks: an untrusted full-page image/text layer, or dense vector
    glyphs with low native Unicode coverage. This validates only provider,
    selected-page, and hash routing; it deliberately makes no claim that the
    recognized content or downstream semantics are correct.
    """

    route = runtime.route
    if source_ir.source_sha256 != route.source_sha256:
        raise RuntimeError("pdf_visual_text_capture_source_mismatch")
    routing_reasons = pdf_visual_text_routing_reasons(source_ir)
    required_pages = frozenset(routing_reasons)
    selected_pages = frozenset(
        getattr(runtime, "visual_text_page_numbers", route.page_numbers)
    )
    if not selected_pages.issubset(route.page_numbers):
        raise RuntimeError("pdf_visual_text_capture_page_not_authorized")
    captured_required_pages = required_pages & selected_pages
    out_of_scope_pages = required_pages - selected_pages
    declared_out_of_scope_pages = {
        diagnostic.native_locator.page_number
        for diagnostic in source_ir.diagnostics
        if diagnostic.code == "pdf_visual_text_page_out_of_scope"
        and diagnostic.native_locator is not None
        and diagnostic.native_locator.page_number is not None
    }
    unauthorized_required_pages = required_pages - route.page_numbers
    if not unauthorized_required_pages.issubset(declared_out_of_scope_pages):
        raise RuntimeError("pdf_visual_text_required_page_not_authorized")
    if declared_out_of_scope_pages != out_of_scope_pages:
        raise RuntimeError("pdf_visual_text_out_of_scope_evidence_mismatch")
    snapshots_by_page: dict[int, list[object]] = {}
    for snapshot in source_ir.visual_text_snapshots:
        snapshots_by_page.setdefault(snapshot.page_number, []).append(snapshot)
    if set(snapshots_by_page) != set(captured_required_pages):
        raise RuntimeError("pdf_visual_text_capture_page_scope_mismatch")
    expected_provider_id = (
        runtime.visual_text_provider.provider_id
        if runtime.visual_text_provider is not None
        else None
    )
    large_asset_hashes = _large_image_asset_hashes_by_page(source_ir)
    for page_number in sorted(captured_required_pages):
        binding = route.page_bindings[page_number]
        page_snapshots = snapshots_by_page.get(page_number, [])
        if len(page_snapshots) != 1:
            raise RuntimeError("pdf_visual_text_capture_page_ambiguous")
        snapshot = page_snapshots[0]
        if (
            snapshot.source_sha256 != route.source_sha256
            or snapshot.provider_id != expected_provider_id
            or snapshot.provider_id not in route.allowed_visual_provider_ids
        ):
            raise RuntimeError("pdf_visual_text_capture_provider_mismatch")
        if binding.page_image_sha256 is not None:
            if snapshot.page_image_sha256 != binding.page_image_sha256:
                raise RuntimeError("pdf_visual_text_capture_page_hash_mismatch")
        else:
            expected_asset_hash = binding.source_asset_sha256
            if expected_asset_hash is None:
                raise AssertionError("validated visual route lost its page hash")
            if large_asset_hashes.get(page_number, ()).count(expected_asset_hash) != 1:
                raise RuntimeError("pdf_visual_text_capture_asset_hash_mismatch")

    visual_table_snapshots = tuple(getattr(source_ir, "visual_table_snapshots", ()))
    if tuple(getattr(source_ir, "visual_table_page_numbers", ())) != tuple(
        sorted(getattr(runtime, "visual_table_page_numbers", ()))
    ):
        raise RuntimeError("pdf_visual_table_capture_scope_mismatch")
    expected_table_provider_id = (
        getattr(runtime.visual_table_provider, "provider_id", None)
        if getattr(runtime, "visual_table_provider", None) is not None
        else None
    )
    if expected_table_provider_id is None and visual_table_snapshots:
        raise RuntimeError("pdf_visual_table_capture_unexpected")
    for table_snapshot in visual_table_snapshots:
        if (
            table_snapshot.source_sha256 != route.source_sha256
            or table_snapshot.page_number not in runtime.visual_table_page_numbers
            or table_snapshot.provider_id != expected_table_provider_id
            or table_snapshot.provider_id not in route.allowed_visual_provider_ids
        ):
            raise RuntimeError("pdf_visual_table_capture_binding_mismatch")


def pdf_visual_text_routing_reasons(
    source_ir: SourceIRSnapshot,
) -> dict[int, tuple[str, ...]]:
    """Return the two-rule native-first decision for each VLM-required page."""

    reasons_by_page: dict[int, set[str]] = {}
    for diagnostic in source_ir.diagnostics:
        reason = _VISUAL_ROUTE_REASON_BY_DIAGNOSTIC.get(diagnostic.code)
        locator = diagnostic.native_locator
        page_number = locator.page_number if locator is not None else None
        if reason is None or page_number is None:
            continue
        reasons_by_page.setdefault(page_number, set()).add(reason)
    return {
        page_number: tuple(sorted(reasons))
        for page_number, reasons in sorted(reasons_by_page.items())
    }


def _large_image_asset_hashes_by_page(
    source_ir: SourceIRSnapshot,
) -> dict[int, tuple[str, ...]]:
    assets_by_id = {asset.asset_id: asset for asset in source_ir.assets}
    candidates: dict[int, list[tuple[float, str]]] = {}
    for node in source_ir.nodes:
        locator = node.native_locator
        bbox = locator.bbox
        if (
            node.kind not in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
            or locator.page_number is None
            or bbox is None
            or node.asset_ref is None
        ):
            continue
        asset = assets_by_id.get(node.asset_ref)
        if asset is None:
            continue
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if width <= 0.0 or height <= 0.0:
            continue
        candidates.setdefault(locator.page_number, []).append(
            (width * height, asset.content_sha256)
        )
    return {
        page_number: tuple(
            sorted(
                digest
                for area, digest in records
                if area >= max(item[0] for item in records) * 0.95
            )
        )
        for page_number, records in candidates.items()
    }


def build_remote_visual_text_provider(
    *,
    provider_name: str,
    bailian_model: str | None,
    input_rotations_by_page: Mapping[int, int] | None = None,
    bailian_config_loader: Callable[[str | None], object] | None = None,
    runtime_setting_loader: Callable[..., str] | None = None,
    bailian_provider_type: Callable[..., RemotePdfVisualTextProvider] | None = None,
    bailian_layout_provider_type: (
        Callable[..., RemotePdfVisualTextProvider] | None
    ) = None,
    azure_provider_type: Callable[..., RemotePdfVisualTextProvider] | None = None,
) -> RemotePdfVisualTextProvider:
    """Construct one approved remote visual provider, with no fallback."""

    normalized_rotations = normalize_input_rotations_by_page(
        visual_text_page_numbers=frozenset(input_rotations_by_page or ()),
        input_rotations_by_page=input_rotations_by_page,
    )
    load_bailian_config = bailian_config_loader or _load_ai_copilot_config
    load_setting = runtime_setting_loader or runtime_setting
    make_bailian_provider = bailian_provider_type or BailianVisualTextProvider
    make_bailian_layout_provider = (
        bailian_layout_provider_type or BailianPageLayoutProvider
    )
    make_azure_provider = (
        azure_provider_type or AzureDocumentIntelligenceVisualTextProvider
    )

    if provider_name == OCR_PROVIDER_BAILIAN:
        config = load_bailian_config("aliyun")
        api_key = getattr(config, "dashscope_api_key", "")
        base_url = getattr(config, "dashscope_base_url", "")
        if not isinstance(api_key, str) or not api_key.strip():
            raise RuntimeError("bailian_visual_text_not_configured")
        model = (
            str(bailian_model or "").strip()
            or load_setting("REQPILOT_ALIYUN_VISUAL_TEXT_MODEL")
            or DEFAULT_BAILIAN_OCR_MODEL
        )
        provider_arguments: dict[str, object] = {
            "api_key": api_key,
            "base_url": str(base_url or ""),
            "model": model,
            "enable_rotate": not any(normalized_rotations.values()),
        }
        if input_rotations_by_page is not None:
            provider_arguments["input_rotations_by_page"] = normalized_rotations
        return make_bailian_provider(**provider_arguments)

    if provider_name in {
        OCR_PROVIDER_BAILIAN_LAYOUT,
        OCR_PROVIDER_BAILIAN_FLASH_LAYOUT,
    }:
        config = load_bailian_config("aliyun")
        api_key = getattr(config, "dashscope_api_key", "")
        base_url = getattr(config, "dashscope_base_url", "")
        if not isinstance(api_key, str) or not api_key.strip():
            raise RuntimeError("bailian_page_layout_not_configured")
        model_setting, default_model = (
            (
                "REQPILOT_ALIYUN_FLASH_PAGE_LAYOUT_MODEL",
                DEFAULT_BAILIAN_FLASH_PAGE_LAYOUT_MODEL,
            )
            if provider_name == OCR_PROVIDER_BAILIAN_FLASH_LAYOUT
            else (
                "REQPILOT_ALIYUN_PAGE_LAYOUT_MODEL",
                DEFAULT_BAILIAN_PAGE_LAYOUT_MODEL,
            )
        )
        model = (
            str(bailian_model or "").strip()
            or load_setting(model_setting)
            or default_model
        )
        provider_arguments = {
            "api_key": api_key,
            "base_url": str(base_url or ""),
            "model": model,
        }
        if input_rotations_by_page is not None:
            provider_arguments["input_rotations_by_page"] = normalized_rotations
        return make_bailian_layout_provider(**provider_arguments)

    if provider_name == OCR_PROVIDER_AZURE:
        if any(normalized_rotations.values()):
            raise RuntimeError("azure_input_rotation_not_supported")
        endpoint = load_setting(
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
            "DOCUMENTINTELLIGENCE_ENDPOINT",
        )
        api_key = load_setting(
            "AZURE_DOCUMENT_INTELLIGENCE_API_KEY",
            "DOCUMENTINTELLIGENCE_API_KEY",
        )
        if not endpoint or not api_key:
            raise RuntimeError("azure_document_intelligence_not_configured")
        return make_azure_provider(
            endpoint=endpoint,
            api_key=api_key,
        )

    raise ValueError("unsupported_visual_text_provider")


def build_remote_visual_table_provider(
    *,
    bailian_model: str | None,
    bailian_config_loader: Callable[[str | None], object] | None = None,
    runtime_setting_loader: Callable[..., str] | None = None,
    bailian_provider_type: Callable[..., VisualTableProviderPort] | None = None,
) -> VisualTableProviderPort:
    """Build the verified Bailian table adapter; never select another provider."""

    load_bailian_config = bailian_config_loader or _load_ai_copilot_config
    load_setting = runtime_setting_loader or runtime_setting
    make_provider = bailian_provider_type or BailianVisualTableProvider
    config = load_bailian_config("aliyun")
    api_key = getattr(config, "dashscope_api_key", "")
    base_url = getattr(config, "dashscope_base_url", "")
    if not isinstance(api_key, str) or not api_key.strip():
        raise RuntimeError("bailian_visual_table_not_configured")
    model = (
        str(bailian_model or "").strip()
        or load_setting("REQPILOT_ALIYUN_VISUAL_TEXT_MODEL")
        or DEFAULT_BAILIAN_OCR_MODEL
    )
    return make_provider(
        api_key=api_key,
        base_url=str(base_url or ""),
        model=model,
    )


def visual_text_provider_id(provider_name: str) -> str:
    if provider_name in {
        OCR_PROVIDER_BAILIAN,
        OCR_PROVIDER_BAILIAN_LAYOUT,
        OCR_PROVIDER_BAILIAN_FLASH_LAYOUT,
    }:
        return BAILIAN_VISUAL_TEXT_PROVIDER_ID
    if provider_name == OCR_PROVIDER_AZURE:
        return AZURE_DOCUMENT_INTELLIGENCE_VISUAL_TEXT_PROVIDER_ID
    raise ValueError("unsupported_visual_text_provider")


def normalize_input_rotations_by_page(
    *,
    visual_text_page_numbers: frozenset[int],
    input_rotations_by_page: Mapping[int, int] | None,
) -> dict[int, int]:
    """Validate and deterministically order an exact remote-page scope."""

    if input_rotations_by_page is None:
        return {page_number: 0 for page_number in sorted(visual_text_page_numbers)}
    if not isinstance(input_rotations_by_page, Mapping):
        raise TypeError("input_rotations_by_page must be a page-to-angle mapping")

    normalized: dict[int, int] = {}
    for page_number, rotation in input_rotations_by_page.items():
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
        ):
            raise ValueError("input rotation page numbers must be positive integers")
        if (
            isinstance(rotation, bool)
            or not isinstance(rotation, int)
            or rotation not in _ALLOWED_INPUT_ROTATIONS
        ):
            raise ValueError("input rotations must be one of 0, 90, 180, or 270")
        normalized[page_number] = rotation

    if frozenset(normalized) != visual_text_page_numbers:
        raise RuntimeError("visual_text_rotation_scope_mismatch")
    return {page_number: normalized[page_number] for page_number in sorted(normalized)}


def normalize_visual_table_page_numbers(
    value: frozenset[int] | set[int] | tuple[int, ...],
    *,
    visual_route_page_numbers: frozenset[int],
) -> frozenset[int]:
    """Validate an explicit, cost-bounded table-parsing subset."""

    if not isinstance(value, (frozenset, set, tuple)):
        raise TypeError("visual_table_page_numbers must be a set or tuple")
    values = tuple(value)
    if any(
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
        for page_number in values
    ):
        raise ValueError("visual table page numbers must be positive integers")
    if len(values) != len(set(values)):
        raise ValueError("visual table page numbers must not contain duplicates")
    normalized = frozenset(values)
    if not normalized.issubset(visual_route_page_numbers):
        raise RuntimeError("visual_table_page_scope_mismatch")
    return normalized


def runtime_setting(*names: str) -> str:
    """Read persisted runtime configuration before environment overrides."""

    storage_config: dict[str, object] = {}
    config_path = resolve_storage_dir(_PROTOTYPE_DIR) / "config.json"
    try:
        candidate = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(candidate, dict):
            storage_config = candidate
    except (OSError, json.JSONDecodeError):
        pass
    for name in names:
        persisted = storage_config.get(name)
        if persisted is not None and str(persisted).strip():
            return str(persisted).strip()
        environment = os.environ.get(name)
        if environment is not None and environment.strip():
            return environment.strip()
    return ""


def _load_ai_copilot_config(profile_id: str | None) -> object:
    # Keep credential-bearing application configuration lazy so importing the
    # runtime for a native-only Silver route does not read AI configuration.
    from reqpilotx_server.services.ai_copilot import load_ai_copilot_config

    return load_ai_copilot_config(profile_id)


def source_sha256(source: Path) -> str:
    digest = hashlib.sha256()
    with Path(source).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "ExplicitPageVisualTextProviderRouter",
    "OCR_PROVIDER_AZURE",
    "OCR_PROVIDER_BAILIAN",
    "OCR_PROVIDER_BAILIAN_FLASH_LAYOUT",
    "OCR_PROVIDER_BAILIAN_LAYOUT",
    "PDF_VISUAL_ROUTING_POLICY_ID",
    "PdfSilverDocumentRoute",
    "PdfSilverPageAssetRoute",
    "PdfVisualTextRoute",
    "PdfVisualTextRuntime",
    "PdfVisualTextPageBinding",
    "SUPPORTED_PDF_VISUAL_PROVIDER_IDS",
    "SUPPORTED_PDF_VISUAL_PROVIDER_NAMES",
    "build_pdf_visual_text_runtime_from_silver",
    "build_remote_visual_table_provider",
    "validate_pdf_visual_text_capture",
    "build_remote_visual_text_provider",
    "normalize_input_rotations_by_page",
    "normalize_visual_table_page_numbers",
    "pdf_visual_text_route_from_silver",
    "pdf_visual_text_routing_reasons",
    "runtime_setting",
    "source_sha256",
    "visual_text_provider_id",
]
