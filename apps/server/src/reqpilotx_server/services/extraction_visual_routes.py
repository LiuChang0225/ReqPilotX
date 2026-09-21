from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from reqpilotx_server.private_eval import private_eval_path

from reqpilotx_server.adapters.documents.pdf_source_ir_adapter import (
    PDF_SOURCE_IR_ADAPTER_VERSION,
)
from reqpilotx_server.adapters.documents.pdf_visual_text_runtime import (
    OCR_PROVIDER_AZURE,
    OCR_PROVIDER_BAILIAN,
    OCR_PROVIDER_BAILIAN_FLASH_LAYOUT,
    OCR_PROVIDER_BAILIAN_LAYOUT,
    PDF_VISUAL_ROUTING_POLICY_ID,
    PdfSilverDocumentRoute,
    PdfVisualTextRuntime,
    build_pdf_visual_text_runtime_from_silver,
    pdf_visual_text_route_from_silver,
    visual_text_provider_id,
)


EXTRACTION_VISUAL_ROUTE_SCHEMA_VERSION = "extraction-visual-route/v1"
EXTRACTION_VISUAL_ROUTE_REGISTRY_SCHEMA_VERSION = (
    "extraction-visual-route-registry/v1"
)
EXTRACTION_VISUAL_STRATEGY_SCHEMA_VERSION = "extraction-visual-strategy/v1"
_MAX_ROUTE_REGISTRY_BYTES = 8 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ROUTE_ID_PATTERN = re.compile(r"^pvr-[0-9a-f]{32}$")
_PROTOTYPE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_VISUAL_ROUTE_REGISTRY_PATH = private_eval_path("extraction_cases/visual_routes.json")


class ExtractionVisualRouteRejected(RuntimeError):
    """Safe failure while resolving an exact, source-bound visual route."""

    def __init__(self, code: str, message: str) -> None:
        token = str(code or "visual_route_rejected").strip()
        if not token:
            raise ValueError("code must not be blank")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must not be blank")
        self.code = token
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _RegistryPageAsset:
    page_number: int
    input_rotation_cw_degrees: int
    source_asset_sha256: str | None
    page_image_sha256: str | None


@dataclass(frozen=True, slots=True)
class _RegistryDocumentRoute:
    source_sha256: str
    allowed_visual_provider_ids: tuple[str, ...]
    page_assets: tuple[_RegistryPageAsset, ...]


@dataclass(frozen=True, slots=True)
class AuthorizedPdfVisualRoute:
    """The runtime-safe subset copied from the reviewed route registry.

    No fidelity, semantic, projection, or Gold expectation is retained here.
    The route hash binds only the source, provider whitelist, page scope,
    rotations, and exact page/render hashes that authorize remote transfer.
    """

    route_id: str
    route_sha256: str
    registry_schema_version: str
    routing_document: PdfSilverDocumentRoute

    @property
    def source_sha256(self) -> str:
        return self.routing_document.source_sha256


@dataclass(frozen=True, slots=True)
class AuthorizedPdfVisualRuntime:
    provider_name: str
    route: AuthorizedPdfVisualRoute
    runtime: PdfVisualTextRuntime

    @property
    def provider_id(self) -> str:
        provider = self.runtime.visual_text_provider
        if provider is None:
            raise RuntimeError("authorized_visual_runtime_provider_missing")
        return provider.provider_id

    @property
    def provider_version(self) -> str:
        provider = self.runtime.visual_text_provider
        if provider is None:
            raise RuntimeError("authorized_visual_runtime_provider_missing")
        return provider.provider_version

    def cache_identity(self) -> dict[str, Any]:
        return {
            "mode": "REMOTE_VISUAL",
            "provider_name": self.provider_name,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "route_id": self.route.route_id,
            "route_sha256": self.route.route_sha256,
            "routing_policy_id": PDF_VISUAL_ROUTING_POLICY_ID,
            "authorized_page_count": len(self.runtime.route.page_numbers),
        }


def extraction_visual_strategy_identity(
    authorized: AuthorizedPdfVisualRuntime | None,
    *,
    source_ir_adapter_version: str | None = None,
) -> dict[str, Any]:
    """Return the credential-free identity bound to one extraction run."""

    if authorized is None:
        adapter_version = (
            PDF_SOURCE_IR_ADAPTER_VERSION
            if source_ir_adapter_version is None
            else source_ir_adapter_version
        )
        return normalize_extraction_visual_strategy_identity(
            {
                "schema_version": EXTRACTION_VISUAL_STRATEGY_SCHEMA_VERSION,
                "mode": "OFF",
                "provider_name": "off",
                "provider_id": None,
                "provider_version": None,
                "route_id": None,
                "route_sha256": None,
                "routing_policy_id": None,
                "authorized_page_count": 0,
                "source_ir_adapter_version": adapter_version,
            },
            expected_source_ir_adapter_version=adapter_version,
        )
    adapter_version = authorized.runtime.pdf_adapter.adapter_version
    return normalize_extraction_visual_strategy_identity(
        {
            "schema_version": EXTRACTION_VISUAL_STRATEGY_SCHEMA_VERSION,
            **authorized.cache_identity(),
            "source_ir_adapter_version": adapter_version,
        },
        expected_source_ir_adapter_version=adapter_version,
    )


def normalize_extraction_visual_strategy_identity(
    value: Mapping[str, Any],
    *,
    expected_source_ir_adapter_version: str | None = None,
) -> dict[str, Any]:
    """Validate the public, source-text-free visual strategy contract."""

    keys = {
        "schema_version",
        "mode",
        "provider_name",
        "provider_id",
        "provider_version",
        "route_id",
        "route_sha256",
        "routing_policy_id",
        "authorized_page_count",
        "source_ir_adapter_version",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("visual_strategy_identity_invalid")
    adapter_version = value.get("source_ir_adapter_version")
    if (
        not isinstance(adapter_version, str)
        or not adapter_version.strip()
        or len(adapter_version) > 200
        or any(ord(character) < 32 for character in adapter_version)
        or (
            expected_source_ir_adapter_version is not None
            and adapter_version != expected_source_ir_adapter_version
        )
    ):
        raise ValueError("visual_strategy_adapter_version_invalid")
    if value.get("schema_version") != EXTRACTION_VISUAL_STRATEGY_SCHEMA_VERSION:
        raise ValueError("visual_strategy_schema_invalid")
    mode = value.get("mode")
    page_count = value.get("authorized_page_count")
    if isinstance(page_count, bool) or not isinstance(page_count, int):
        raise ValueError("visual_strategy_page_count_invalid")
    if mode == "OFF":
        if (
            value.get("provider_name") != "off"
            or any(
                value.get(key) is not None
                for key in (
                    "provider_id",
                    "provider_version",
                    "route_id",
                    "route_sha256",
                    "routing_policy_id",
                )
            )
            or page_count != 0
        ):
            raise ValueError("visual_strategy_off_identity_invalid")
    elif mode == "REMOTE_VISUAL":
        provider_name = value.get("provider_name")
        provider_id = value.get("provider_id")
        provider_version = value.get("provider_version")
        route_id = value.get("route_id")
        route_sha256 = value.get("route_sha256")
        if provider_name not in {
            OCR_PROVIDER_BAILIAN,
            OCR_PROVIDER_BAILIAN_LAYOUT,
            OCR_PROVIDER_BAILIAN_FLASH_LAYOUT,
            OCR_PROVIDER_AZURE,
        }:
            raise ValueError("visual_strategy_provider_invalid")
        if provider_id != visual_text_provider_id(provider_name):
            raise ValueError("visual_strategy_provider_identity_invalid")
        if (
            not isinstance(provider_version, str)
            or not provider_version.strip()
            or len(provider_version) > 500
            or any(ord(character) < 32 for character in provider_version)
            or not isinstance(route_id, str)
            or _ROUTE_ID_PATTERN.fullmatch(route_id) is None
            or not isinstance(route_sha256, str)
            or _SHA256_PATTERN.fullmatch(route_sha256) is None
            or value.get("routing_policy_id") != PDF_VISUAL_ROUTING_POLICY_ID
            or page_count < 1
        ):
            raise ValueError("visual_strategy_remote_identity_invalid")
    else:
        raise ValueError("visual_strategy_mode_invalid")
    return {key: value[key] for key in sorted(keys)}


def load_authorized_pdf_visual_routes(
    registry_path: Path | None = None,
) -> Mapping[str, AuthorizedPdfVisualRoute]:
    """Load only routing fields from the controlled product registry.

    The product path intentionally does not import or deserialize evaluation
    fidelity/semantic expectations.  Entries without a remote page scope are
    ignored because they authorize no bytes to leave the native pipeline.
    """

    path = Path(registry_path or DEFAULT_VISUAL_ROUTE_REGISTRY_PATH)
    try:
        payload_bytes = path.read_bytes()
    except OSError as exc:
        raise ExtractionVisualRouteRejected(
            "visual_route_registry_unavailable",
            "The controlled visual route registry is unavailable.",
        ) from exc
    if not payload_bytes or len(payload_bytes) > _MAX_ROUTE_REGISTRY_BYTES:
        raise ExtractionVisualRouteRejected(
            "visual_route_registry_invalid",
            "The controlled visual route registry has an invalid size.",
        )
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractionVisualRouteRejected(
            "visual_route_registry_invalid",
            "The controlled visual route registry is not valid JSON.",
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "documents",
    }:
        raise ExtractionVisualRouteRejected(
            "visual_route_registry_invalid",
            "The controlled visual route registry has an invalid top-level shape.",
        )
    registry_schema_version = payload.get("schema_version")
    documents = payload.get("documents")
    if registry_schema_version != EXTRACTION_VISUAL_ROUTE_REGISTRY_SCHEMA_VERSION:
        raise ExtractionVisualRouteRejected(
            "visual_route_registry_invalid",
            "The controlled visual route registry has an unsupported schema version.",
        )
    if not isinstance(documents, dict):
        raise ExtractionVisualRouteRejected(
            "visual_route_registry_invalid",
            "The controlled visual route registry has no document map.",
        )

    routes: dict[str, AuthorizedPdfVisualRoute] = {}
    for document_id, raw_document in documents.items():
        if (
            not isinstance(document_id, str)
            or not document_id.strip()
            or len(document_id) > 200
            or any(ord(character) < 32 for character in document_id)
            or not isinstance(raw_document, dict)
            or set(raw_document)
            != {
                "source_sha256",
                "allowed_visual_provider_ids",
                "page_assets",
            }
        ):
            raise ExtractionVisualRouteRejected(
                "visual_route_registry_invalid",
                "A controlled visual route entry has an invalid shape.",
            )
        routing_document = _routing_document(raw_document)
        if not routing_document.page_assets:
            raise ExtractionVisualRouteRejected(
                "visual_route_registry_invalid",
                "A controlled visual route entry has no authorized pages.",
            )
        try:
            route = pdf_visual_text_route_from_silver(routing_document)
        except (TypeError, ValueError) as exc:
            raise ExtractionVisualRouteRejected(
                "visual_route_registry_invalid",
                "A controlled visual route entry failed validation.",
            ) from exc
        route_payload = _route_payload(route)
        canonical = json.dumps(
            route_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        route_sha256 = hashlib.sha256(canonical).hexdigest()
        authorized = AuthorizedPdfVisualRoute(
            route_id=f"pvr-{route_sha256[:32]}",
            route_sha256=route_sha256,
            registry_schema_version=registry_schema_version,
            routing_document=routing_document,
        )
        previous = routes.get(route.source_sha256)
        if previous is not None:
            raise ExtractionVisualRouteRejected(
                "visual_route_registry_ambiguous",
                "More than one controlled visual route targets the same source.",
            )
        routes[route.source_sha256] = authorized
    return MappingProxyType(dict(sorted(routes.items())))


def resolve_authorized_pdf_visual_route(
    source_sha256: str,
    *,
    registry_path: Path | None = None,
) -> AuthorizedPdfVisualRoute | None:
    digest = str(source_sha256 or "").strip().lower()
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise ExtractionVisualRouteRejected(
            "visual_route_source_identity_invalid",
            "The source identity cannot be used for visual-route authorization.",
        )
    return load_authorized_pdf_visual_routes(registry_path).get(digest)


def build_authorized_pdf_visual_runtime(
    *,
    source: Path,
    source_sha256: str,
    provider_name: str,
    registry_path: Path | None = None,
) -> AuthorizedPdfVisualRuntime:
    route = resolve_authorized_pdf_visual_route(
        source_sha256,
        registry_path=registry_path,
    )
    if route is None:
        raise ExtractionVisualRouteRejected(
            "visual_route_not_authorized",
            "This source has no exact page-hash route authorized for remote visual recognition.",
        )
    try:
        provider_id = visual_text_provider_id(provider_name)
    except ValueError as exc:
        raise ExtractionVisualRouteRejected(
            "visual_provider_not_supported",
            "Only the approved remote visual providers can be selected.",
        ) from exc
    parsed_route = pdf_visual_text_route_from_silver(route.routing_document)
    if provider_id not in parsed_route.allowed_visual_provider_ids:
        raise ExtractionVisualRouteRejected(
            "visual_provider_not_authorized",
            "The selected remote provider is not authorized for this source route.",
        )
    if provider_name == OCR_PROVIDER_AZURE and any(
        parsed_route.input_rotations_by_page.values()
    ):
        raise ExtractionVisualRouteRejected(
            "visual_provider_route_unsupported",
            "Azure cannot consume this route's explicit page rotation.",
        )
    try:
        runtime = build_pdf_visual_text_runtime_from_silver(
            source=Path(source),
            silver_document=route.routing_document,
            provider_name=provider_name,
        )
    except RuntimeError as exc:
        cause = str(exc or "visual_runtime_unavailable")
        if "not_configured" in cause:
            code = "visual_provider_not_configured"
            message = "The selected remote visual provider is not configured."
        elif "source_hash_mismatch" in cause:
            code = "visual_route_source_mismatch"
            message = "The source no longer matches its authorized visual route."
        elif "not_allowed" in cause:
            code = "visual_provider_not_authorized"
            message = (
                "The selected remote provider is not authorized for this source route."
            )
        else:
            code = "visual_runtime_unavailable"
            message = "The authorized remote visual runtime could not be prepared."
        raise ExtractionVisualRouteRejected(code, message) from exc
    except (TypeError, ValueError) as exc:
        raise ExtractionVisualRouteRejected(
            "visual_runtime_invalid",
            "The authorized remote visual runtime configuration is invalid.",
        ) from exc
    if runtime.visual_text_provider is None or not runtime.route.page_numbers:
        raise ExtractionVisualRouteRejected(
            "visual_route_not_authorized",
            "This source has no non-empty remote visual page scope.",
        )
    return AuthorizedPdfVisualRuntime(
        provider_name=provider_name,
        route=route,
        runtime=runtime,
    )


def visual_route_capabilities(
    route: AuthorizedPdfVisualRoute | None,
) -> dict[str, Any]:
    if route is None:
        return {
            "schema_version": EXTRACTION_VISUAL_ROUTE_SCHEMA_VERSION,
            "authorization_status": "NOT_REGISTERED",
            "source_sha256": None,
            "route_id": None,
            "route_sha256": None,
            "authorized_page_count": 0,
            "authorized_pages": [],
            "providers": [],
            "routing_policy_id": PDF_VISUAL_ROUTING_POLICY_ID,
            "remote_only": True,
            "local_ocr": False,
            "implicit_fallback": False,
        }
    parsed = pdf_visual_text_route_from_silver(route.routing_document)
    providers: list[dict[str, Any]] = []
    for provider_name in (
        OCR_PROVIDER_BAILIAN_LAYOUT,
        OCR_PROVIDER_BAILIAN_FLASH_LAYOUT,
        OCR_PROVIDER_BAILIAN,
        OCR_PROVIDER_AZURE,
    ):
        provider_id = visual_text_provider_id(provider_name)
        allowed = provider_id in parsed.allowed_visual_provider_ids
        rotation_supported = not (
            provider_name == OCR_PROVIDER_AZURE
            and any(parsed.input_rotations_by_page.values())
        )
        providers.append(
            {
                "name": provider_name,
                "id": provider_id,
                "authorized": allowed,
                "selectable": allowed and rotation_supported,
                "reason_code": (
                    ""
                    if allowed and rotation_supported
                    else "explicit_rotation_not_supported"
                    if allowed
                    else "provider_not_authorized"
                ),
            }
        )
    return {
        "schema_version": EXTRACTION_VISUAL_ROUTE_SCHEMA_VERSION,
        "authorization_status": "AUTHORIZED",
        "source_sha256": route.source_sha256,
        "route_id": route.route_id,
        "route_sha256": route.route_sha256,
        "authorized_page_count": len(parsed.page_numbers),
        "authorized_pages": [
            {
                "page_number": page_number,
                "input_rotation_cw_degrees": binding.input_rotation_cw_degrees,
                "page_binding_kind": (
                    "SOURCE_ASSET_SHA256"
                    if binding.source_asset_sha256 is not None
                    else "PAGE_IMAGE_SHA256"
                ),
                "page_binding_sha256": (
                    binding.source_asset_sha256 or binding.page_image_sha256
                ),
            }
            for page_number, binding in parsed.page_bindings.items()
        ],
        "providers": providers,
        "routing_policy_id": PDF_VISUAL_ROUTING_POLICY_ID,
        "remote_only": True,
        "local_ocr": False,
        "implicit_fallback": False,
    }


def visual_runtime_audit(
    authorized: AuthorizedPdfVisualRuntime,
) -> dict[str, Any]:
    capabilities = visual_route_capabilities(authorized.route)
    return {
        **capabilities,
        "selected_provider": {
            "name": authorized.provider_name,
            "id": authorized.provider_id,
            "version": authorized.provider_version,
        },
    }


def _routing_document(raw_document: dict[str, Any]) -> _RegistryDocumentRoute:
    source_sha256 = raw_document.get("source_sha256")
    providers = raw_document.get("allowed_visual_provider_ids")
    raw_assets = raw_document.get("page_assets")
    if not isinstance(source_sha256, str):
        source_sha256 = ""
    if not isinstance(providers, list) or any(
        not isinstance(item, str) for item in providers
    ):
        providers = []
    if not isinstance(raw_assets, list):
        raw_assets = []
    assets: list[_RegistryPageAsset] = []
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            raise ExtractionVisualRouteRejected(
                "visual_route_registry_invalid",
                "A controlled visual page route is not an object.",
            )
        allowed_keys = {
            "page_number",
            "input_rotation_cw_degrees",
            "source_asset_sha256",
            "page_image_sha256",
        }
        binding_keys = {
            key
            for key in ("source_asset_sha256", "page_image_sha256")
            if key in raw_asset
        }
        if (
            "page_number" not in raw_asset
            or not set(raw_asset).issubset(allowed_keys)
            or len(binding_keys) != 1
        ):
            raise ExtractionVisualRouteRejected(
                "visual_route_registry_invalid",
                "A controlled visual page route has an invalid shape.",
            )
        rotation = raw_asset.get("input_rotation_cw_degrees", 0)
        assets.append(
            _RegistryPageAsset(
                page_number=raw_asset.get("page_number"),
                input_rotation_cw_degrees=rotation,
                source_asset_sha256=raw_asset.get("source_asset_sha256"),
                page_image_sha256=raw_asset.get("page_image_sha256"),
            )
        )
    return _RegistryDocumentRoute(
        source_sha256=source_sha256,
        allowed_visual_provider_ids=tuple(providers),
        page_assets=tuple(assets),
    )


def _route_payload(route: Any) -> dict[str, Any]:
    return {
        "schema_version": EXTRACTION_VISUAL_ROUTE_SCHEMA_VERSION,
        "source_sha256": route.source_sha256,
        "allowed_visual_provider_ids": sorted(route.allowed_visual_provider_ids),
        "pages": [
            {
                "page_number": page_number,
                "input_rotation_cw_degrees": binding.input_rotation_cw_degrees,
                "source_asset_sha256": binding.source_asset_sha256,
                "page_image_sha256": binding.page_image_sha256,
            }
            for page_number, binding in route.page_bindings.items()
        ],
    }


__all__ = [
    "AuthorizedPdfVisualRoute",
    "AuthorizedPdfVisualRuntime",
    "DEFAULT_VISUAL_ROUTE_REGISTRY_PATH",
    "EXTRACTION_VISUAL_ROUTE_REGISTRY_SCHEMA_VERSION",
    "EXTRACTION_VISUAL_ROUTE_SCHEMA_VERSION",
    "ExtractionVisualRouteRejected",
    "build_authorized_pdf_visual_runtime",
    "load_authorized_pdf_visual_routes",
    "resolve_authorized_pdf_visual_route",
    "visual_route_capabilities",
    "visual_runtime_audit",
]
