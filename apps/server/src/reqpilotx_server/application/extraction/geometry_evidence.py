from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from threading import RLock
from typing import Any, Mapping, Sequence

from reqpilotx_server import db
from reqpilotx_server.adapters.documents.pdf_source_crop_renderer import (
    PdfVisiblePageGeometry,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.domain.extraction.page_geometry import (
    PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
    PDF_PAGE_GEOMETRY_SCHEMA_VERSION,
    PDF_VISIBLE_COORDINATE_SPACE,
    PdfPageGeometryIndexV1,
    PdfPageGeometryLineV1,
    PdfPageGeometryObjectV1,
    PdfPageGeometrySourceIdentityV1,
    create_pdf_page_geometry_index,
)
from reqpilotx_server.domain.extraction.requirement_tree import (
    RequirementTreeNodeV1,
    RequirementTreeSnapshotV1,
)
from reqpilotx_server.domain.extraction.table_records import (
    TableRecordKind,
    TableRecordV1,
)
from reqpilotx_server.domain.extraction.source_ir import (
    NativeDocumentFormat,
    SourceIRSnapshot,
    SourceNodeKind,
)
from reqpilotx_server.ports.geometry_evidence import (
    GeometryEvidencePort,
    GeometryFocusRegion,
)


PDF_PAGE_GEOMETRY_PREVIEW_KEY = "_pdf_page_geometry_indexes"
GEOMETRY_FOCUS_SCHEMA_VERSION = "geometry-focus-regions/v1"
GEOMETRY_FOCUS_ALGORITHM_VERSION = "geometry-focus/v4"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_FOCUS_CACHE_ENTRIES = 1_024
_FOCUS_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_FOCUS_CACHE_LOCK = RLock()
_MAX_PAGE_CACHE_ENTRIES = 24
_MAX_GEOMETRY_MANIFEST_BYTES = 64 * 1024 * 1024
_PAGE_CACHE: OrderedDict[
    tuple[str, int],
    PdfPageGeometryIndexV1,
] = OrderedDict()
_PAGE_CACHE_LOCK = RLock()


def build_pdf_page_geometry_preview(
    source_ir: SourceIRSnapshot,
    *,
    page_geometries: Sequence[PdfVisiblePageGeometry],
) -> dict[str, Any]:
    """Build page indexes only from the already-validated SourceIR geometry."""

    source_identity = PdfPageGeometrySourceIdentityV1(
        source_sha256=source_ir.source_sha256,
        source_ir_id=source_ir.source_ir_id,
        source_ir_schema_version=source_ir.schema_version,
        adapter_id=source_ir.adapter_id,
        adapter_version=source_ir.adapter_version,
    )
    page_by_number = {page.page_number: page for page in page_geometries}
    raw_lines: dict[
        int,
        list[
            tuple[
                int,
                int,
                str,
                int,
                int,
                int,
                tuple[float, float, float, float],
            ]
        ],
    ] = {page_number: [] for page_number in page_by_number}
    raw_tables: dict[
        int,
        list[tuple[int, str, tuple[float, float, float, float], str]],
    ] = {page_number: [] for page_number in page_by_number}
    raw_figures: dict[
        int,
        list[tuple[int, str, tuple[float, float, float, float], str]],
    ] = {page_number: [] for page_number in page_by_number}

    for node in source_ir.nodes:
        locator = node.native_locator
        page_number = locator.page_number
        if (
            locator.format is not NativeDocumentFormat.PDF
            or page_number not in page_by_number
        ):
            continue
        for line_index, line in enumerate(node.pdf_text_lines):
            raw_lines[page_number].append(
                (
                    node.source_order,
                    line_index,
                    node.source_node_id,
                    len(node.raw_text),
                    line.start_offset,
                    line.end_offset,
                    line.bbox,
                )
            )
        bbox = locator.bbox
        if bbox is None or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        if node.kind is SourceNodeKind.TABLE:
            raw_tables[page_number].append(
                (
                    node.source_order,
                    node.source_node_id,
                    bbox,
                    (
                        node.table_payload.table_id
                        if node.table_payload is not None
                        else ""
                    ),
                )
            )
        elif node.kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}:
            raw_figures[page_number].append(
                (
                    node.source_order,
                    node.source_node_id,
                    bbox,
                    str(node.asset_ref or ""),
                )
            )

    pages: dict[str, dict[str, Any]] = {}
    for page_number, page in sorted(page_by_number.items()):
        lines = tuple(
            PdfPageGeometryLineV1(
                source_node_id=source_node_id,
                source_text_length=source_text_length,
                start_offset=start_offset,
                end_offset=end_offset,
                bbox=bbox,
                source_order=node_order * 1_000_000 + line_index,
            )
            for (
                node_order,
                line_index,
                source_node_id,
                source_text_length,
                start_offset,
                end_offset,
                bbox,
            ) in sorted(raw_lines[page_number])
        )
        tables = tuple(
            PdfPageGeometryObjectV1(
                source_node_id=source_node_id,
                kind="TABLE",
                bbox=bbox,
                source_order=node_order * 1_000_000,
                table_id=table_id,
            )
            for (
                node_order,
                source_node_id,
                bbox,
                table_id,
            ) in sorted(raw_tables[page_number])
        )
        figures = tuple(
            PdfPageGeometryObjectV1(
                source_node_id=source_node_id,
                kind="FIGURE",
                bbox=bbox,
                source_order=node_order * 1_000_000,
                asset_ref=asset_ref,
            )
            for (
                node_order,
                source_node_id,
                bbox,
                asset_ref,
            ) in sorted(raw_figures[page_number])
        )
        x0, y0, x1, y1 = page.bounds
        index = create_pdf_page_geometry_index(
            source_identity=source_identity,
            page_number=page_number,
            page_width=x1 - x0,
            page_height=y1 - y0,
            rotation_cw_degrees=page.rotation_cw_degrees,
            text_lines=lines,
            tables=tables,
            figures=figures,
        )
        pages[str(page_number)] = index.model_dump(mode="json")
    return {
        "schema_version": PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
        "source_identity": source_identity.model_dump(mode="json"),
        "coordinate_space": PDF_VISIBLE_COORDINATE_SPACE,
        "page_count": len(pages),
        "pages": pages,
    }


def persist_pdf_page_geometry_preview(
    preview: Any,
    *,
    project_id: str,
    document_id: str,
) -> dict[str, Any] | None:
    """Persist immutable page indexes and return their private descriptor."""

    raw = _record(preview)
    if not raw:
        return None
    if str(raw.get("schema_version") or "") != (
        PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION
    ) or str(raw.get("coordinate_space") or "") != PDF_VISIBLE_COORDINATE_SPACE:
        raise ValueError("PDF page geometry preview has an unsupported schema")
    source_identity = PdfPageGeometrySourceIdentityV1.model_validate(
        _record(raw.get("source_identity"))
    )
    raw_pages = _record(raw.get("pages"))
    pages = {
        str(page_number): PdfPageGeometryIndexV1.model_validate(payload)
        for page_number, payload in raw_pages.items()
        if isinstance(page_number, str) and isinstance(payload, dict)
    }
    if (
        not pages
        or len(pages) != int(raw.get("page_count") or 0)
        or tuple(sorted(int(page_number) for page_number in pages))
        != tuple(range(1, len(pages) + 1))
    ):
        raise ValueError("PDF page geometry preview has an invalid page set")
    for page_number, page in pages.items():
        if (
            page.page_number != int(page_number)
            or page.source_identity != source_identity
        ):
            raise ValueError("PDF page geometry identity is inconsistent")

    aggregate_identity = _geometry_aggregate_identity(
        source_identity=source_identity.model_dump(mode="json"),
        pages={
            page_number: {
                "page_number": page.page_number,
                "index_sha256": page.index_sha256,
            }
            for page_number, page in pages.items()
        },
    )
    geometry_index_sha256 = _canonical_sha256(aggregate_identity)
    geometry_index_id = _canonical_sha256(
        {
            "kind": PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
            "geometry_index_sha256": geometry_index_sha256,
        }
    )
    from reqpilotx_server.application.ingestion import _put_runtime_json

    prefix = (
        f"runtime/pdf-page-geometry/{source_identity.source_sha256}/"
        f"{geometry_index_id}"
    )
    page_descriptors: dict[str, dict[str, Any]] = {}
    for page_number, page in sorted(pages.items(), key=lambda item: int(item[0])):
        artifact = _put_runtime_json(
            storage_key=f"{prefix}/pages/{page_number}.json",
            payload=page.model_dump(mode="json"),
        )
        page_descriptors[page_number] = {
            **artifact,
            "page_number": page.page_number,
            "schema_version": PDF_PAGE_GEOMETRY_SCHEMA_VERSION,
            "index_sha256": page.index_sha256,
        }
    manifest_payload = {
        "schema_version": PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
        "state": "READY",
        "geometry_index_id": geometry_index_id,
        "geometry_index_sha256": geometry_index_sha256,
        "project_id": project_id,
        "document_id": document_id,
        "source_identity": source_identity.model_dump(mode="json"),
        "coordinate_space": PDF_VISIBLE_COORDINATE_SPACE,
        "page_count": len(page_descriptors),
        "pages": page_descriptors,
    }
    manifest_artifact = _put_runtime_json(
        storage_key=(
            f"{prefix}/bindings/{project_id}/{document_id}/manifest.json"
        ),
        payload=manifest_payload,
    )
    return {
        **manifest_payload,
        "manifest": manifest_artifact,
    }


def public_pdf_page_geometry_descriptor(
    value: Any,
    *,
    project_id: str,
    document_id: str,
    expected_source_sha256: str,
) -> dict[str, Any]:
    descriptor = validated_pdf_page_geometry_descriptor(
        value,
        project_id=project_id,
        document_id=document_id,
        expected_source_sha256=expected_source_sha256,
    )
    if descriptor is None:
        return {
            "schema_version": PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
            "state": "UNAVAILABLE",
            "page_count": 0,
        }
    return {
        "schema_version": PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
        "state": "READY",
        "geometry_index_id": str(descriptor["geometry_index_id"]),
        "geometry_index_sha256": str(descriptor["geometry_index_sha256"]),
        "coordinate_space": PDF_VISIBLE_COORDINATE_SPACE,
        "page_count": int(descriptor["page_count"]),
    }


def validated_pdf_page_geometry_descriptor(
    value: Any,
    *,
    project_id: str,
    document_id: str,
    expected_source_sha256: str,
) -> dict[str, Any] | None:
    descriptor = _record(value)
    source_identity = _record(descriptor.get("source_identity"))
    pages = _record(descriptor.get("pages"))
    try:
        validated_source_identity = (
            PdfPageGeometrySourceIdentityV1.model_validate(source_identity)
        )
    except ValueError:
        return None
    if (
        str(descriptor.get("schema_version") or "")
        != PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION
        or str(descriptor.get("state") or "").upper() != "READY"
        or str(descriptor.get("project_id") or "") != project_id
        or str(descriptor.get("document_id") or "") != document_id
        or str(descriptor.get("coordinate_space") or "")
        != PDF_VISIBLE_COORDINATE_SPACE
        or validated_source_identity.source_sha256
        != expected_source_sha256
        or not _SHA256_PATTERN.fullmatch(
            str(descriptor.get("geometry_index_id") or "")
        )
        or not _SHA256_PATTERN.fullmatch(
            str(descriptor.get("geometry_index_sha256") or "")
        )
        or int(descriptor.get("page_count") or 0) < 1
        or len(pages) != int(descriptor.get("page_count") or 0)
    ):
        return None
    try:
        ordered_page_numbers = tuple(sorted(int(page_number) for page_number in pages))
    except (TypeError, ValueError):
        return None
    if ordered_page_numbers != tuple(range(1, len(pages) + 1)):
        return None
    for page_number, page_value in pages.items():
        page = _record(page_value)
        if (
            str(page.get("schema_version") or "")
            != PDF_PAGE_GEOMETRY_SCHEMA_VERSION
            or _positive_int(page.get("page_number")) != int(page_number)
            or not _SHA256_PATTERN.fullmatch(
                str(page.get("index_sha256") or "")
            )
            or not _SHA256_PATTERN.fullmatch(str(page.get("sha256") or ""))
            or _positive_int(page.get("size")) is None
            or not str(page.get("storage_key") or "").strip()
        ):
            return None
    aggregate_identity = _geometry_aggregate_identity(
        source_identity=source_identity,
        pages={
            page_number: {
                "page_number": _record(page).get("page_number"),
                "index_sha256": _record(page).get("index_sha256"),
            }
            for page_number, page in pages.items()
        },
    )
    if _canonical_sha256(aggregate_identity) != str(
        descriptor.get("geometry_index_sha256") or ""
    ):
        return None
    expected_id = _canonical_sha256(
        {
            "kind": PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
            "geometry_index_sha256": descriptor["geometry_index_sha256"],
        }
    )
    if expected_id != descriptor.get("geometry_index_id"):
        return None
    return descriptor


def load_pdf_page_geometry_descriptor_by_identity(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    geometry_index_id: str,
    geometry_index_sha256: str,
) -> dict[str, Any] | None:
    """Load a historical manifest by its content-bound tree identity.

    Requirement Set review must not silently switch to a later extraction's
    geometry. Manifests have deterministic source/index/project/document keys;
    page and aggregate hashes then verify the entire immutable chain.
    """

    if (
        not project_id
        or not document_id
        or not _SHA256_PATTERN.fullmatch(source_sha256)
        or not _SHA256_PATTERN.fullmatch(geometry_index_id)
        or not _SHA256_PATTERN.fullmatch(geometry_index_sha256)
    ):
        return None
    from reqpilotx_server.application.runtime import RUNTIME_STATE

    container = RUNTIME_STATE.container
    if container is None:
        return None
    storage_key = (
        f"runtime/pdf-page-geometry/{source_sha256}/{geometry_index_id}/"
        f"bindings/{project_id}/{document_id}/manifest.json"
    )
    stored = container.object_store.stat(storage_key)
    if (
        stored is None
        or stored.size < 1
        or stored.size > _MAX_GEOMETRY_MANIFEST_BYTES
        or not _SHA256_PATTERN.fullmatch(stored.sha256)
    ):
        return None
    with container.object_store.open(storage_key) as source:
        payload = source.read(stored.size + 1)
    if (
        len(payload) != stored.size
        or hashlib.sha256(payload).hexdigest() != stored.sha256
    ):
        return None
    try:
        raw_descriptor = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    descriptor = validated_pdf_page_geometry_descriptor(
        raw_descriptor,
        project_id=project_id,
        document_id=document_id,
        expected_source_sha256=source_sha256,
    )
    if (
        descriptor is None
        or str(descriptor.get("geometry_index_id") or "")
        != geometry_index_id
        or str(descriptor.get("geometry_index_sha256") or "")
        != geometry_index_sha256
    ):
        return None
    return descriptor


class InMemoryGeometryEvidence(GeometryEvidencePort):
    def __init__(
        self,
        pages: Sequence[PdfPageGeometryIndexV1],
        *,
        geometry_index_id: str = "",
        geometry_index_sha256: str = "",
    ) -> None:
        self._pages = {page.page_number: page for page in pages}
        if not self._pages:
            raise ValueError("geometry evidence requires at least one page")
        source_identity = next(iter(self._pages.values())).source_identity
        if any(
            page.source_identity != source_identity for page in self._pages.values()
        ):
            raise ValueError("geometry evidence pages have mixed source identities")
        aggregate = _geometry_aggregate_identity(
            source_identity=source_identity.model_dump(mode="json"),
            pages={
                str(page_number): {
                    "page_number": page_number,
                    "index_sha256": page.index_sha256,
                }
                for page_number, page in self._pages.items()
            },
        )
        self._geometry_index_sha256 = (
            geometry_index_sha256 or _canonical_sha256(aggregate)
        )
        self._geometry_index_id = geometry_index_id or _canonical_sha256(
            {
                "kind": PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
                "geometry_index_sha256": self._geometry_index_sha256,
            }
        )

    @property
    def geometry_index_id(self) -> str:
        return self._geometry_index_id

    @property
    def geometry_index_sha256(self) -> str:
        return self._geometry_index_sha256

    def load_page(self, page_number: int) -> PdfPageGeometryIndexV1:
        try:
            return self._pages[page_number]
        except KeyError as exc:
            raise KeyError(f"PDF geometry page {page_number} is unavailable") from exc

    def load_pages(
        self,
        page_numbers: Sequence[int],
    ) -> tuple[PdfPageGeometryIndexV1, ...]:
        return tuple(self.load_page(page_number) for page_number in page_numbers)

    def lines_intersecting(
        self,
        *,
        page_number: int,
        source_node_id: str,
        start_offset: int,
        end_offset: int,
    ) -> tuple[PdfPageGeometryLineV1, ...]:
        return tuple(
            line
            for line in self.load_page(page_number).text_lines
            if line.source_node_id == source_node_id
            and line.end_offset > start_offset
            and line.start_offset < end_offset
        )

    def regions_for_content(
        self,
        *,
        content_blocks: Sequence[Mapping[str, Any]],
        source_refs: Sequence[Mapping[str, Any]],
    ) -> tuple[GeometryFocusRegion, ...]:
        return focus_regions_for_content(
            evidence=self,
            content_blocks=content_blocks,
            source_refs=source_refs,
        )

    def reading_evidence_between(
        self,
        *,
        page_number: int,
        start_source_order: int,
        end_source_order: int,
    ) -> tuple[dict[str, Any], ...]:
        page = self.load_page(page_number)
        evidence = [
            {
                "kind": "TEXT_LINE",
                **line.model_dump(mode="json"),
            }
            for line in page.text_lines
            if start_source_order <= line.source_order <= end_source_order
        ]
        evidence.extend(
            {
                "kind": item.kind,
                **item.model_dump(mode="json"),
            }
            for item in (*page.tables, *page.figures)
            if start_source_order <= item.source_order <= end_source_order
        )
        evidence.sort(
            key=lambda item: (
                int(item.get("source_order") or 0),
                str(item.get("source_node_id") or ""),
            )
        )
        return tuple(evidence)


class PersistedGeometryEvidence(InMemoryGeometryEvidence):
    """Lazy verified adapter over page-addressable object storage."""

    def __init__(self, descriptor: dict[str, Any]) -> None:
        self._descriptor = descriptor
        self._page_cache: dict[int, PdfPageGeometryIndexV1] = {}
        self._pages = {}
        self._geometry_index_id = str(descriptor["geometry_index_id"])
        self._geometry_index_sha256 = str(descriptor["geometry_index_sha256"])

    def load_page(self, page_number: int) -> PdfPageGeometryIndexV1:
        cached = self._page_cache.get(page_number)
        if cached is not None:
            return cached
        shared_cache_key = (self._geometry_index_sha256, page_number)
        with _PAGE_CACHE_LOCK:
            shared = _PAGE_CACHE.get(shared_cache_key)
            if shared is not None:
                _PAGE_CACHE.move_to_end(shared_cache_key)
                self._page_cache[page_number] = shared
                return shared
        raw_descriptor = _record(
            _record(self._descriptor.get("pages")).get(str(page_number))
        )
        if not raw_descriptor:
            raise KeyError(f"PDF geometry page {page_number} is unavailable")
        from reqpilotx_server.application.extraction.source_page_facts import (
            _read_verified_json_artifact,
        )

        payload = _read_verified_json_artifact(
            raw_descriptor,
            not_found_message="The requested PDF geometry page is missing.",
        )
        try:
            page = PdfPageGeometryIndexV1.model_validate(
                json.loads(payload.decode("utf-8"))
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("PDF geometry page failed contract validation") from exc
        source_identity = _record(self._descriptor.get("source_identity"))
        if (
            page.page_number != page_number
            or page.index_sha256 != raw_descriptor.get("index_sha256")
            or page.source_identity.model_dump(mode="json") != source_identity
        ):
            raise ValueError("PDF geometry page lineage does not match its manifest")
        self._page_cache[page_number] = page
        with _PAGE_CACHE_LOCK:
            _PAGE_CACHE[shared_cache_key] = page
            _PAGE_CACHE.move_to_end(shared_cache_key)
            while len(_PAGE_CACHE) > _MAX_PAGE_CACHE_ENTRIES:
                _PAGE_CACHE.popitem(last=False)
        return page


def focus_regions_for_content(
    *,
    evidence: GeometryEvidencePort,
    content_blocks: Sequence[Mapping[str, Any]],
    source_refs: Sequence[Mapping[str, Any]],
) -> tuple[GeometryFocusRegion, ...]:
    """Project every independently bound source through immutable geometry.

    A table or figure match must not suppress unresolved prose.  Text blocks
    without offsets/bboxes are still exact when their source node and page can
    be resolved to the frozen line index.
    """

    page_by_source: dict[str, int] = {}
    for ref in source_refs:
        source_id = str(ref.get("source_id") or "")
        if not source_id:
            continue
        page_number = _positive_int(
            ref.get("page_number")
            or _record(ref.get("location")).get("page_number")
            or ref.get("page_start")
        )
        if page_number is not None:
            page_by_source[source_id] = page_number

    # Exact source spans outrank whole-node compatibility refs.  Retaining an
    # unbounded ref for the same immutable SourceNode would expand a selected
    # clause back to adjacent siblings that happen to share one PDF text
    # block.  Page lineage from the broad ref remains available through
    # ``page_by_source`` when the precise span itself omits it.
    bounded_source_ids = {
        str(ref.get("source_id") or "")
        for ref in source_refs
        if str(ref.get("source_id") or "")
        and (
            (start := _optional_nonnegative_int(ref.get("start_offset")))
            is not None
        )
        and (
            (end := _optional_nonnegative_int(ref.get("end_offset")))
            is not None
        )
        and end > start
    }
    effective_source_refs = tuple(
        ref
        for ref in source_refs
        if str(ref.get("source_id") or "") not in bounded_source_ids
        or (
            (start := _optional_nonnegative_int(ref.get("start_offset")))
            is not None
            and (
                (end := _optional_nonnegative_int(ref.get("end_offset")))
                is not None
            )
            and end > start
        )
    )

    blocks = list(content_blocks)
    candidates: list[tuple[GeometryFocusRegion, int | None, int]] = []
    resolved_source_pages: set[tuple[str, int]] = set()
    page_cache: dict[int, PdfPageGeometryIndexV1 | None] = {}
    sequence = 0

    def load_page(page_number: int) -> PdfPageGeometryIndexV1 | None:
        if page_number not in page_cache:
            try:
                page_cache[page_number] = evidence.load_page(page_number)
            except (KeyError, ValueError):
                page_cache[page_number] = None
        return page_cache[page_number]

    def append_region(
        region: GeometryFocusRegion,
        *,
        source_order: int | None,
    ) -> None:
        nonlocal sequence
        candidates.append((region, source_order, sequence))
        sequence += 1

    def matching_lines(
        *,
        page_number: int,
        source_id: str,
        start_offset: int | None,
        end_offset: int | None,
    ) -> tuple[PdfPageGeometryLineV1, ...]:
        if (
            start_offset is not None
            and end_offset is not None
            and end_offset > start_offset
        ):
            return evidence.lines_intersecting(
                page_number=page_number,
                source_node_id=source_id,
                start_offset=start_offset,
                end_offset=end_offset,
            )
        page = load_page(page_number)
        if page is None:
            return ()
        return tuple(
            line for line in page.text_lines if line.source_node_id == source_id
        )

    def matching_object(
        *,
        page_number: int,
        source_id: str,
        expected_kind: str = "",
    ) -> PdfPageGeometryObjectV1 | None:
        page = load_page(page_number)
        if page is None:
            return None
        objects = (*page.tables, *page.figures)
        return next(
            (
                item
                for item in objects
                if item.source_node_id == source_id
                and (not expected_kind or item.kind == expected_kind)
            ),
            None,
        )

    def append_lines(
        *,
        lines: Sequence[PdfPageGeometryLineV1],
        page_number: int,
        source_id: str,
        start_offset: int | None,
        end_offset: int | None,
        role: str,
        id_scope: str,
    ) -> bool:
        if not lines:
            return False
        bounded = (
            start_offset is not None
            and end_offset is not None
            and end_offset > start_offset
        )
        for line_index, line in enumerate(lines):
            projected_start = (
                max(start_offset, line.start_offset)
                if bounded and start_offset is not None
                else line.start_offset
            )
            projected_end = (
                min(end_offset, line.end_offset)
                if bounded and end_offset is not None
                else line.end_offset
            )
            append_region(
                GeometryFocusRegion(
                    id=f"focus:{source_id}:{id_scope}:line:{line_index}",
                    page_number=page_number,
                    bbox=line.bbox,
                    source_id=source_id,
                    start_offset=projected_start,
                    end_offset=projected_end,
                    role=role,
                    precision=(
                        "LINE_APPROXIMATE"
                        if bounded
                        and (
                            (
                                start_offset is not None
                                and start_offset > line.start_offset
                            )
                            or (
                                end_offset is not None
                                and end_offset < line.end_offset
                            )
                        )
                        else "EXACT_LINE"
                    ),
                ),
                source_order=line.source_order,
            )
        return True

    for index, block in enumerate(blocks):
        kind = str(block.get("kind") or block.get("type") or "text").upper()
        location = _record(block.get("location"))
        source_id = str(
            block.get("source_id") or block.get("source_node_id") or ""
        )
        page_number = _positive_int(
            block.get("page_start")
            or block.get("page")
            or location.get("page_number")
            or page_by_source.get(source_id)
        )
        start_offset = _optional_nonnegative_int(block.get("start_offset"))
        end_offset = _optional_nonnegative_int(block.get("end_offset"))
        next_kind = (
            str(
                blocks[index + 1].get("kind")
                or blocks[index + 1].get("type")
                or ""
            ).upper()
            if index + 1 < len(blocks)
            else ""
        )
        role = (
            "TABLE"
            if kind == "TABLE"
            else "FIGURE"
            if kind in {"FIGURE", "IMAGE"}
            else "TABLE_CAPTION"
            if next_kind == "TABLE"
            else "CLAUSE_TEXT"
        )
        if (
            kind in {"TEXT", "PARAGRAPH", "CONTENT", "DIRECT_CONTENT"}
            and page_number is not None
            and source_id
        ):
            if (
                source_id in bounded_source_ids
                and not (
                    start_offset is not None
                    and end_offset is not None
                    and end_offset > start_offset
                )
            ):
                continue
            selected_lines = matching_lines(
                page_number=page_number,
                source_id=source_id,
                start_offset=start_offset,
                end_offset=end_offset,
            )
            if append_lines(
                lines=selected_lines,
                page_number=page_number,
                source_id=source_id,
                start_offset=start_offset,
                end_offset=end_offset,
                role=role,
                id_scope=f"block:{index}",
            ):
                resolved_source_pages.add((source_id, page_number))
                continue

        expected_object_kind = (
            "TABLE" if kind == "TABLE" else "FIGURE" if role == "FIGURE" else ""
        )
        if page_number is not None and source_id and expected_object_kind:
            matched_object = matching_object(
                page_number=page_number,
                source_id=source_id,
                expected_kind=expected_object_kind,
            )
            if matched_object is not None:
                append_region(
                    GeometryFocusRegion(
                        id=f"focus:{source_id}:{page_number}:{index}",
                        page_number=page_number,
                        bbox=matched_object.bbox,
                        source_id=source_id,
                        start_offset=None,
                        end_offset=None,
                        role=matched_object.kind,
                        precision="EXACT_BLOCK",
                    ),
                    source_order=matched_object.source_order,
                )
                resolved_source_pages.add((source_id, page_number))
                continue

        bbox = _valid_bbox(location.get("bbox") or block.get("bbox"))
        if page_number is not None and bbox is None and source_id:
            matched_object = matching_object(
                page_number=page_number,
                source_id=source_id,
            )
            if matched_object is not None:
                append_region(
                    GeometryFocusRegion(
                        id=f"focus:{source_id}:{page_number}:{index}",
                        page_number=page_number,
                        bbox=matched_object.bbox,
                        source_id=source_id,
                        start_offset=start_offset,
                        end_offset=end_offset,
                        role=matched_object.kind,
                        precision="EXACT_BLOCK",
                    ),
                    source_order=matched_object.source_order,
                )
                resolved_source_pages.add((source_id, page_number))
                continue
        if page_number is None or bbox is None:
            continue
        append_region(
            GeometryFocusRegion(
                id=f"focus:{source_id or role}:{page_number}:{index}",
                page_number=page_number,
                bbox=bbox,
                source_id=source_id,
                start_offset=start_offset,
                end_offset=end_offset,
                role=role,
                precision=(
                    "EXACT_BLOCK"
                    if role in {"TABLE", "FIGURE", "TABLE_CAPTION"}
                    else "BLOCK_APPROXIMATE"
                ),
            ),
            source_order=None,
        )
        if source_id:
            resolved_source_pages.add((source_id, page_number))

    # Source refs supplement unresolved blocks one source/page at a time.  A
    # successful table lookup therefore cannot disable a prose lookup.
    for index, ref in enumerate(effective_source_refs):
        location = _record(ref.get("location"))
        source_id = str(ref.get("source_id") or "")
        page_number = _positive_int(
            ref.get("page_number")
            or location.get("page_number")
            or ref.get("page_start")
            or page_by_source.get(source_id)
        )
        if page_number is None:
            continue
        if source_id and (source_id, page_number) in resolved_source_pages:
            continue
        start_offset = _optional_nonnegative_int(ref.get("start_offset"))
        end_offset = _optional_nonnegative_int(ref.get("end_offset"))
        if source_id:
            lines = matching_lines(
                page_number=page_number,
                source_id=source_id,
                start_offset=start_offset,
                end_offset=end_offset,
            )
            if append_lines(
                lines=lines,
                page_number=page_number,
                source_id=source_id,
                start_offset=start_offset,
                end_offset=end_offset,
                role="CLAUSE_TEXT",
                id_scope=f"source-ref:{index}",
            ):
                continue
            matched_object = matching_object(
                page_number=page_number,
                source_id=source_id,
            )
            if matched_object is not None:
                append_region(
                    GeometryFocusRegion(
                        id=(
                            f"focus:{source_id}:{page_number}:"
                            f"{matched_object.kind.lower()}"
                        ),
                        page_number=page_number,
                        bbox=matched_object.bbox,
                        source_id=source_id,
                        start_offset=None,
                        end_offset=None,
                        role=matched_object.kind,
                        precision="EXACT_BLOCK",
                    ),
                    source_order=matched_object.source_order,
                )
                continue
        bbox = _valid_bbox(ref.get("bbox") or location.get("bbox"))
        if bbox is not None:
            append_region(
                GeometryFocusRegion(
                    id=f"focus:{source_id or index}:{page_number}:fallback",
                    page_number=page_number,
                    bbox=bbox,
                    source_id=source_id,
                    start_offset=None,
                    end_offset=None,
                    role="CLAUSE_TEXT",
                    precision="BLOCK_APPROXIMATE",
                ),
                source_order=None,
            )

    deduped: list[tuple[GeometryFocusRegion, int | None, int]] = []
    seen: set[tuple[Any, ...]] = set()
    for region, source_order, insertion_order in candidates:
        key = (
            region.page_number,
            region.bbox,
            region.role,
            region.source_id,
            region.start_offset,
            region.end_offset,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append((region, source_order, insertion_order))

    if deduped and all(source_order is not None for _, source_order, _ in deduped):
        deduped.sort(
            key=lambda item: (
                item[0].page_number,
                int(item[1] or 0),
                item[2],
            )
        )
    else:
        deduped.sort(key=lambda item: (item[0].page_number, item[2]))
    return tuple(region for region, _, _ in deduped)


def focus_regions_for_table_record(
    *,
    evidence: GeometryEvidencePort,
    record: TableRecordV1,
    table_blocks: Sequence[Mapping[str, Any]],
) -> tuple[GeometryFocusRegion, ...]:
    """Resolve a proven table record to its bound PDF cells.

    The complete table remains the immutable presentation fact, but it must
    not become the selected record's focus region.  Exact record geometry is
    returned only when every bound cell can be verified inside the matching
    table object from the frozen page geometry.  Callers can then fall back to
    whole-table focus without misrepresenting that fallback as record-exact.
    """

    bound_cell_ids_by_table, precision = _table_record_focus_cell_ids(record)
    if not bound_cell_ids_by_table or not set(
        bound_cell_ids_by_table
    ).issubset(record.table_ids):
        return ()
    focus_table_ids = tuple(
        table_id
        for table_id in record.table_ids
        if table_id in bound_cell_ids_by_table
    )

    table_by_id = {
        str(block.get("table_id") or block.get("id") or ""): block
        for block in table_blocks
        if str(block.get("table_id") or block.get("id") or "")
    }
    if not set(focus_table_ids).issubset(table_by_id):
        return ()

    regions: list[GeometryFocusRegion] = []
    for table_id in focus_table_ids:
        table = table_by_id[table_id]
        table_location = _record(table.get("location"))
        table_page_number = _positive_int(
            table.get("page_start")
            or table.get("page_number")
            or table_location.get("page_number")
        )
        table_source_id = str(
            table.get("source_id") or table.get("source_node_id") or ""
        )
        if table_page_number is None:
            return ()
        try:
            page = evidence.load_page(table_page_number)
        except (KeyError, ValueError):
            return ()
        geometry_table = next(
            (
                item
                for item in page.tables
                if (
                    (item.table_id and item.table_id == table_id)
                    or (
                        table_source_id
                        and item.source_node_id == table_source_id
                    )
                )
            ),
            None,
        )
        if geometry_table is None:
            return ()

        cells = table.get("cells")
        if not isinstance(cells, (list, tuple)):
            return ()
        cells_by_id = {
            str(cell.get("cell_id") or cell.get("id") or ""): cell
            for cell in cells
            if isinstance(cell, Mapping)
            and str(cell.get("cell_id") or cell.get("id") or "")
        }
        expected_cell_ids = bound_cell_ids_by_table[table_id]
        if not expected_cell_ids or any(
            cell_id not in cells_by_id for cell_id in expected_cell_ids
        ):
            return ()

        cell_bboxes: list[tuple[float, float, float, float]] = []
        for cell_id in expected_cell_ids:
            cell = cells_by_id[cell_id]
            location = _record(cell.get("location"))
            page_number = _positive_int(
                cell.get("page_number") or location.get("page_number")
            )
            bbox = _valid_bbox(cell.get("bbox") or location.get("bbox"))
            if (
                page_number != table_page_number
                or bbox is None
                or not _bbox_contains(geometry_table.bbox, bbox)
            ):
                return ()
            cell_bboxes.append(bbox)

        regions.append(
            GeometryFocusRegion(
                id=(
                    f"focus:{record.record_id}:{table_id}:"
                    f"{table_page_number}"
                ),
                page_number=table_page_number,
                bbox=_bbox_union(cell_bboxes),
                source_id=geometry_table.source_node_id,
                start_offset=None,
                end_offset=None,
                role="TABLE_RECORD",
                precision=precision,
            )
        )

    return tuple(regions)


def _table_record_focus_cell_ids(
    record: TableRecordV1,
) -> tuple[dict[str, tuple[str, ...]], str]:
    """Return only cells that constitute the selected record itself.

    ``record.table_ids`` also carries header, shared-merge, note and visual
    context across continuation pages.  Those tables prove interpretation but
    must not be framed as if they were cells of the selected row or matrix
    intersection.
    """

    if record.kind is TableRecordKind.ROW:
        return (
            {
                binding.table_id: binding.cell_ids
                for binding in record.row_bindings
                if binding.cell_ids
            },
            "EXACT_TABLE_ROW",
        )
    column_cells_by_table = {
        binding.table_id: set(binding.cell_ids)
        for binding in record.column_bindings
    }
    intersections: dict[str, tuple[str, ...]] = {}
    for binding in record.row_bindings:
        intersection = tuple(
            cell_id
            for cell_id in binding.cell_ids
            if cell_id in column_cells_by_table.get(binding.table_id, set())
        )
        if intersection:
            intersections[binding.table_id] = intersection
    return intersections, "EXACT_TABLE_CELL"


def get_document_geometry_focus(
    project_id: str,
    document_id: str,
    node_id: str,
    response: ResponseSink,
    requirement_set_id: str = "",
) -> dict[str, Any]:
    """Resolve one selected tree node against its immutable page geometry."""

    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.extraction.document_jobs import (
        _document_extraction_result_artifact_identity,
    )
    from reqpilotx_server.application.extraction.runtime_read_models import (
        cache_document_extraction_runtime_read_model,
        get_document_extraction_runtime_read_model,
    )
    from reqpilotx_server.application.extraction.structure_workspace import (
        _serialize_structure_workbench_for_api,
        _structure_agent_job_payload,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    document = _require_project_document(project_id, document_id)
    if requirement_set_id:
        from reqpilotx_server.application.extraction.requirement_tree_snapshots import (
            RequirementTreeSnapshotError,
            load_requirement_set_tree_snapshot,
        )

        requirement_set = db.get_requirement_set(requirement_set_id)
        if (
            requirement_set is None
            or str(requirement_set.get("project_id") or "") != project_id
            or str(requirement_set.get("sor_document_id") or "") != document_id
        ):
            raise _geometry_application_error(
                "requirement_set_not_found",
                "The requested Requirement Set does not belong to this document.",
                status_code=404,
            )
        try:
            tree = load_requirement_set_tree_snapshot(requirement_set)
        except RequirementTreeSnapshotError as exc:
            raise _geometry_application_error(
                "requirement_tree_snapshot_invalid",
                "The Requirement Set tree snapshot could not be verified.",
                status_code=409,
            ) from exc
        descriptor = load_pdf_page_geometry_descriptor_by_identity(
            project_id=project_id,
            document_id=document_id,
            source_sha256=tree.source_identity.source_sha256,
            geometry_index_id=tree.source_identity.geometry_index_id,
            geometry_index_sha256=(
                tree.source_identity.geometry_index_sha256
            ),
        )
        if descriptor is None:
            raise _geometry_application_error(
                "geometry_index_unavailable",
                "The Requirement Set has no verified PDF geometry snapshot.",
                status_code=409,
            )
    else:
        source_sha256 = str(document.get("source_sha256") or "")
        artifact_identity = _document_extraction_result_artifact_identity(
            project_id,
            document_id,
            source_sha256=source_sha256,
        )
        cached = get_document_extraction_runtime_read_model(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_identity=artifact_identity,
        )
        if cached is None or cached.requirement_tree is None:
            job, payload, agent = _structure_agent_job_payload(
                project_id=project_id,
                document_id=document_id,
                document=document,
                require_final=False,
            )
            workbench = _serialize_structure_workbench_for_api(
                project_id=project_id,
                document_id=document_id,
                document=document,
                job=job,
                payload=payload,
                agent=agent,
            )
            cached = cache_document_extraction_runtime_read_model(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                artifact_identity=artifact_identity,
                payload=payload,
                workbench=workbench,
            )
            source_page_snapshot = _record(
                payload.get("source_page_snapshot")
            )
            tree = RequirementTreeSnapshotV1.model_validate(
                _record(workbench.get("requirement_tree"))
            )
        else:
            source_page_snapshot = cached.source_page_snapshot
            tree = cached.requirement_tree
        descriptor = validated_pdf_page_geometry_descriptor(
            source_page_snapshot.get("geometry_index"),
            project_id=project_id,
            document_id=document_id,
            expected_source_sha256=source_sha256,
        )
        if descriptor is None:
            raise _geometry_application_error(
                "geometry_index_unavailable",
                "The current extraction has no verified PDF page geometry index.",
                status_code=409,
            )
        assert tree is not None

    geometry_index_id = str(descriptor.get("geometry_index_id") or "")
    geometry_index_sha256 = str(
        descriptor.get("geometry_index_sha256") or ""
    )
    if (
        tree.source_identity.geometry_index_id != geometry_index_id
        or tree.source_identity.geometry_index_sha256
        != geometry_index_sha256
    ):
        raise _geometry_application_error(
            "geometry_lineage_mismatch",
            "The selected tree is not bound to the current PDF geometry index.",
            status_code=409,
        )
    node_by_id = {node.id: node for node in tree.nodes}
    selected = node_by_id.get(node_id)
    if selected is None:
        raise _geometry_application_error(
            "requirement_tree_node_not_found",
            "The selected requirement tree node does not exist.",
            status_code=404,
        )

    focus_nodes, focus_scope = _geometry_focus_scope_nodes(
        selected=selected,
        node_by_id=node_by_id,
    )
    table_record = _table_record_for_node(tree=tree, node=selected)
    table_blocks = (
        _table_blocks_for_record(tree=tree, record=table_record)
        if table_record is not None
        else ()
    )

    cache_key = _focus_cache_key(
        tree=tree,
        selected=selected,
        focus_nodes=focus_nodes,
        focus_scope=focus_scope,
        geometry_index_sha256=geometry_index_sha256,
        table_record=table_record,
        table_blocks=table_blocks,
    )
    cached = _focus_cache_get(cache_key)
    if cached is not None:
        result = {**cached, "cache_state": "HIT"}
    else:
        evidence = PersistedGeometryEvidence(descriptor)
        focus_node = selected
        regions = (
            focus_regions_for_table_record(
                evidence=evidence,
                record=table_record,
                table_blocks=table_blocks,
            )
            if table_record is not None
            else ()
        )
        if not regions:
            regions = _focus_regions_for_nodes(
                evidence=evidence,
                nodes=focus_nodes,
            )
        if (
            selected.kind == "REQUIREMENT"
            and not any(
                region.precision
                in {"EXACT_LINE", "LINE_APPROXIMATE", "EXACT_BLOCK"}
                for region in regions
            )
        ):
            parent = _nearest_non_requirement_parent(selected, node_by_id)
            if parent is not None:
                focus_node = parent
                regions = evidence.regions_for_content(
                    content_blocks=parent.content_blocks,
                    source_refs=parent.source_refs,
                )
                focus_scope = "OWNER_FALLBACK"
        result = {
            "schema_version": GEOMETRY_FOCUS_SCHEMA_VERSION,
            "algorithm_version": GEOMETRY_FOCUS_ALGORITHM_VERSION,
            "geometry_identity": {
                "geometry_index_id": evidence.geometry_index_id,
                "geometry_index_sha256": evidence.geometry_index_sha256,
            },
            "node_id": selected.id,
            "resolved_node_id": focus_node.id,
            "focus_scope": focus_scope,
            "page_numbers": sorted({region.page_number for region in regions}),
            "regions": [region.as_dict() for region in regions],
            "cache_state": "MISS",
        }
        _focus_cache_put(cache_key, result)
    if hasattr(response, "headers"):
        response.headers.update(_ASSET_RESPONSE_HEADERS)
    return result


_STRUCTURE_FOCUS_KINDS = {
    "DOCUMENT",
    "TOC",
    "SECTION",
    "CLAUSE",
    "GROUP",
    "LIST_GROUP",
    "APPENDIX",
}
_DERIVED_FOCUS_LEAF_KINDS = {"REQUIREMENT", "TABLE_RECORD"}


def _geometry_focus_scope_nodes(
    *,
    selected: RequirementTreeNodeV1,
    node_by_id: Mapping[str, RequirementTreeNodeV1],
) -> tuple[tuple[RequirementTreeNodeV1, ...], str]:
    """Return one source-faithful selection scope for PDF focus.

    Structural containers resolve to their visible source subtree.  Derived
    semantic/table-record leaves are not replayed inside that subtree because
    their evidence is already represented by the source structure/table.
    """

    if selected.kind.upper() not in _STRUCTURE_FOCUS_KINDS or not selected.child_ids:
        return (selected,), "OWN"

    ordered: list[RequirementTreeNodeV1] = []
    visited: set[str] = set()

    def visit(node: RequirementTreeNodeV1) -> None:
        if node.id in visited:
            return
        visited.add(node.id)
        ordered.append(node)
        if node.kind.upper() == "TABLE":
            return
        children = [
            child
            for child_id in node.child_ids
            if (child := node_by_id.get(child_id)) is not None
            and child.kind.upper() not in _DERIVED_FOCUS_LEAF_KINDS
        ]
        children.sort(key=lambda child: (child.source_order, child.depth, child.id))
        for child in children:
            visit(child)

    visit(selected)
    return tuple(ordered), "SUBTREE"


def _focus_regions_for_nodes(
    *,
    evidence: GeometryEvidencePort,
    nodes: Sequence[RequirementTreeNodeV1],
) -> tuple[GeometryFocusRegion, ...]:
    regions: list[GeometryFocusRegion] = []
    region_index_by_source_box: dict[tuple[Any, ...], int] = {}
    for node in nodes:
        blocks: list[Mapping[str, Any]] = []
        block_hashes: set[str] = set()
        for block in (*node.content_blocks, *node.table_refs, *node.figure_refs):
            block_hash = _canonical_sha256(block)
            if block_hash in block_hashes:
                continue
            block_hashes.add(block_hash)
            blocks.append(block)
        table_pages = {
            page_number
            for block in blocks
            if str(block.get("kind") or block.get("type") or "").upper()
            == "TABLE"
            for page_number in _content_block_page_numbers(block)
        }
        if table_pages:
            blocks = [
                block
                for block in blocks
                if not (
                    _is_table_cell_content_block(block)
                    and table_pages.intersection(
                        _content_block_page_numbers(block)
                    )
                )
            ]
        for region in evidence.regions_for_content(
            content_blocks=blocks,
            source_refs=node.source_refs,
        ):
            key = (
                region.page_number,
                region.bbox,
                region.source_id,
            )
            existing_index = region_index_by_source_box.get(key)
            if existing_index is not None:
                if (
                    regions[existing_index].role == "TABLE_CAPTION"
                    and region.role == "CLAUSE_TEXT"
                ):
                    regions[existing_index] = region
                continue
            region_index_by_source_box[key] = len(regions)
            regions.append(region)
    ordered = sorted(
        enumerate(regions),
        key=lambda item: _focus_region_source_order(
            evidence=evidence,
            region=item[1],
            insertion_order=item[0],
        ),
    )
    return tuple(region for _, region in ordered)


def _focus_region_source_order(
    *,
    evidence: GeometryEvidencePort,
    region: GeometryFocusRegion,
    insertion_order: int,
) -> tuple[Any, ...]:
    try:
        page = evidence.load_page(region.page_number)
    except (KeyError, ValueError):
        page = None
    source_order: int | None = None
    if page is not None:
        matching_line = next(
            (
                line
                for line in page.text_lines
                if line.source_node_id == region.source_id
                and line.bbox == region.bbox
            ),
            None,
        )
        if matching_line is not None:
            source_order = matching_line.source_order
        else:
            matching_object = next(
                (
                    item
                    for item in (*page.tables, *page.figures)
                    if item.source_node_id == region.source_id
                    and item.bbox == region.bbox
                ),
                None,
            )
            if matching_object is not None:
                source_order = matching_object.source_order
    return (
        region.page_number,
        source_order is None,
        source_order if source_order is not None else 0,
        region.bbox[1],
        region.bbox[0],
        insertion_order,
    )


def _is_table_cell_content_block(block: Mapping[str, Any]) -> bool:
    refs = block.get("source_refs")
    if not isinstance(refs, (list, tuple)):
        return False
    return any(
        isinstance(ref, Mapping)
        and str(ref.get("kind") or "").upper() == "TABLE_CELL"
        for ref in refs
    )


def _content_block_page_numbers(block: Mapping[str, Any]) -> tuple[int, ...]:
    location = _record(block.get("location"))
    pages = {
        page
        for value in (
            block.get("page_number"),
            block.get("page_start"),
            block.get("page_end"),
            location.get("page_number"),
        )
        if (page := _positive_int(value)) is not None
    }
    refs = block.get("source_refs")
    if isinstance(refs, (list, tuple)):
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            ref_location = _record(ref.get("location"))
            for value in (
                ref.get("page_number"),
                ref.get("page_start"),
                ref.get("page_end"),
                ref_location.get("page_number"),
            ):
                if (page := _positive_int(value)) is not None:
                    pages.add(page)
    return tuple(sorted(pages))


def _nearest_non_requirement_parent(
    node: RequirementTreeNodeV1,
    node_by_id: dict[str, RequirementTreeNodeV1],
) -> RequirementTreeNodeV1 | None:
    current_id = node.parent_id
    visited: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        current = node_by_id.get(current_id)
        if current is None:
            return None
        if current.kind != "REQUIREMENT":
            return current
        current_id = current.parent_id
    return None


def _table_record_for_node(
    *,
    tree: RequirementTreeSnapshotV1,
    node: RequirementTreeNodeV1,
) -> TableRecordV1 | None:
    if node.kind != "TABLE_RECORD" or not node.table_record_id:
        return None
    matches = tuple(
        record
        for projection in tree.table_record_projections
        for record in projection.records
        if record.record_id == node.table_record_id
    )
    return matches[0] if len(matches) == 1 else None


def _table_blocks_for_record(
    *,
    tree: RequirementTreeSnapshotV1,
    record: TableRecordV1,
) -> tuple[Mapping[str, Any], ...]:
    """Resolve a record's exact physical tables from its immutable IDs.

    ``TABLE_RECORD.table_refs`` intentionally stores projection lineage, not a
    second copy of every cell.  The complete table facts live on the table
    container nodes.  Resolve those facts by the record's proven ``table_ids``
    instead of assuming that the selected leaf owns full table blocks.
    """

    bound_cell_ids_by_table, _precision = _table_record_focus_cell_ids(record)
    expected_ids = tuple(
        table_id
        for table_id in record.table_ids
        if table_id in bound_cell_ids_by_table
    )
    expected_id_set = set(expected_ids)
    if not expected_ids or expected_id_set != set(bound_cell_ids_by_table):
        return ()
    blocks_by_id: dict[str, Mapping[str, Any]] = {}
    hashes_by_id: dict[str, str] = {}
    ambiguous_ids: set[str] = set()
    for node in tree.nodes:
        for block in (*node.table_refs, *node.content_blocks):
            explicit_table_id = str(block.get("table_id") or "")
            block_id = str(block.get("id") or "")
            if explicit_table_id and block_id and explicit_table_id != block_id:
                ambiguous_ids.add(explicit_table_id)
                continue
            table_id = explicit_table_id or block_id
            if table_id not in expected_id_set:
                continue
            block_hash = _physical_table_block_sha256(block, table_id=table_id)
            existing_hash = hashes_by_id.get(table_id)
            if existing_hash is not None and existing_hash != block_hash:
                ambiguous_ids.add(table_id)
                continue
            if existing_hash is not None:
                if len(block) < len(blocks_by_id[table_id]):
                    blocks_by_id[table_id] = block
                continue
            hashes_by_id[table_id] = block_hash
            blocks_by_id[table_id] = block
    if ambiguous_ids or set(blocks_by_id) != expected_id_set:
        return ()
    return tuple(blocks_by_id[table_id] for table_id in expected_ids)


def _physical_table_block_sha256(
    block: Mapping[str, Any],
    *,
    table_id: str,
) -> str:
    """Hash physical table facts without RequirementTree adornment fields."""

    normalized = {
        "id": table_id,
        "source_node_id": str(
            block.get("source_node_id") or block.get("source_id") or ""
        ),
        "page_start": block.get("page_start"),
        "page_end": block.get("page_end"),
        "location": block.get("location"),
        "cells": block.get("cells"),
        "geometry": block.get("geometry"),
        "row_count": block.get("row_count"),
        "column_count": block.get("column_count"),
        "merged_regions": block.get("merged_regions"),
        "header_regions": block.get("header_regions"),
        "continuation": block.get("continuation"),
        "reconstruction": block.get("reconstruction"),
    }
    return _canonical_sha256(normalized)


def _focus_cache_key(
    *,
    tree: RequirementTreeSnapshotV1,
    selected: RequirementTreeNodeV1,
    focus_nodes: Sequence[RequirementTreeNodeV1],
    focus_scope: str,
    geometry_index_sha256: str,
    table_record: TableRecordV1 | None = None,
    table_blocks: Sequence[Mapping[str, Any]] = (),
) -> str:
    node_span_sha256 = _canonical_sha256(
        {
            "node_id": selected.id,
            "focus_scope": focus_scope,
            "nodes": [
                {
                    "node_id": node.id,
                    "parent_id": node.parent_id,
                    "child_ids": node.child_ids,
                    "source_refs": node.source_refs,
                    "content_blocks": node.content_blocks,
                    "table_refs": node.table_refs,
                    "figure_refs": node.figure_refs,
                    "page_start": node.page_start,
                    "page_end": node.page_end,
                }
                for node in focus_nodes
            ],
            "table_record": (
                table_record.model_dump(mode="json")
                if table_record is not None
                else None
            ),
            "table_blocks": list(table_blocks),
        }
    )
    return _canonical_sha256(
        {
            "source_sha256": tree.source_identity.source_sha256,
            "geometry_index_sha256": geometry_index_sha256,
            "structure_tree_sha256": (
                tree.structure_identity.effective_tree_sha256
            ),
            "requirement_set_id": tree.source_identity.requirement_set_id,
            "node_span_sha256": node_span_sha256,
            "algorithm_version": GEOMETRY_FOCUS_ALGORITHM_VERSION,
        }
    )


def _focus_cache_get(key: str) -> dict[str, Any] | None:
    with _FOCUS_CACHE_LOCK:
        cached = _FOCUS_CACHE.get(key)
        if cached is None:
            return None
        _FOCUS_CACHE.move_to_end(key)
        return json.loads(json.dumps(cached))


def _focus_cache_put(key: str, value: dict[str, Any]) -> None:
    stored = json.loads(json.dumps(value))
    stored.pop("cache_state", None)
    with _FOCUS_CACHE_LOCK:
        _FOCUS_CACHE[key] = stored
        _FOCUS_CACHE.move_to_end(key)
        while len(_FOCUS_CACHE) > _MAX_FOCUS_CACHE_ENTRIES:
            _FOCUS_CACHE.popitem(last=False)


def _geometry_aggregate_identity(
    *,
    source_identity: dict[str, Any],
    pages: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    normalized_pages = {
        str(page_number): {
            "page_number": int(_record(page).get("page_number") or 0),
            "index_sha256": str(_record(page).get("index_sha256") or ""),
        }
        for page_number, page in sorted(
            pages.items(),
            key=lambda item: int(item[0]),
        )
    }
    return {
        "schema_version": PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
        "source_identity": source_identity,
        "coordinate_space": PDF_VISIBLE_COORDINATE_SPACE,
        "page_count": len(normalized_pages),
        "pages": normalized_pages,
    }


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _geometry_application_error(
    code: str,
    message: str,
    *,
    status_code: int,
) -> ApplicationError:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    return ApplicationError(
        status_code=status_code,
        code=code,
        detail={"code": code, "message": message},
        headers=_ASSET_RESPONSE_HEADERS,
    )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _valid_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or not all(
            isinstance(point, (int, float)) and not isinstance(point, bool)
            for point in value
        )
    ):
        return None
    bbox = tuple(float(point) for point in value)
    return bbox if bbox[2] > bbox[0] and bbox[3] > bbox[1] else None


def _bbox_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    *,
    tolerance: float = 0.01,
) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _bbox_union(
    values: Sequence[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    if not values:
        raise ValueError("at least one bbox is required")
    return (
        min(value[0] for value in values),
        min(value[1] for value in values),
        max(value[2] for value in values),
        max(value[3] for value in values),
    )


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


__all__ = [
    "GEOMETRY_FOCUS_ALGORITHM_VERSION",
    "GEOMETRY_FOCUS_SCHEMA_VERSION",
    "PDF_PAGE_GEOMETRY_PREVIEW_KEY",
    "InMemoryGeometryEvidence",
    "PersistedGeometryEvidence",
    "build_pdf_page_geometry_preview",
    "focus_regions_for_content",
    "focus_regions_for_table_record",
    "get_document_geometry_focus",
    "load_pdf_page_geometry_descriptor_by_identity",
    "persist_pdf_page_geometry_preview",
    "public_pdf_page_geometry_descriptor",
    "validated_pdf_page_geometry_descriptor",
]
