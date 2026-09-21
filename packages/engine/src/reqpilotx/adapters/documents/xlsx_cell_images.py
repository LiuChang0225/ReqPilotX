from __future__ import annotations

import hashlib
import json
import mimetypes
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

from lxml import etree
from openpyxl.utils.cell import range_boundaries


XLSX_CELL_IMAGE_AUDIT_SCHEMA_VERSION = "xlsx-cell-image-audit/v1"
_WORKBOOK_PART = "xl/workbook.xml"
_CELL_IMAGES_PART = "xl/cellimages.xml"
_METADATA_PART = "xl/metadata.xml"
_RICH_VALUE_DATA_PART = "xl/richData/rdrichvalue.xml"
_RICH_VALUE_STRUCTURE_PART = "xl/richData/rdrichvaluestructure.xml"
_RICH_VALUE_REL_PART = "xl/richData/richValueRel.xml"
_CONTENT_TYPES_PART = "[Content_Types].xml"
_SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOCUMENT_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPE_NS = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
_DRAWING_NS = (
    "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
)
_DRAWING_MAIN_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_DRAWING_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"
)
_IMAGE_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
)
_MAX_XML_PART_BYTES = 32 * 1024 * 1024
_MAX_CELL_IMAGE_REFERENCES = 50_000
_MAX_EMBEDDED_MEDIA_BYTES = 256 * 1024 * 1024
_MAX_OVERLAY_PACKAGE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DISPIMG_PATTERN = re.compile(
    r'^\s*=?\s*(?:_xlfn\.)?DISPIMG\(\s*"([^"]+)"'
    r"\s*(?:,\s*[01]\s*)?\)\s*$",
    flags=re.IGNORECASE,
)


class XlsxCellImageAuditRejected(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class XlsxCellImageReference:
    sheet_index: int
    sheet_name_sha256: str
    worksheet_part_sha256: str
    cell_range: str
    binding_kind: str
    source_binding_sha256: str
    reference_id: str = field(repr=False, compare=False)
    reference_sha256: str
    disposition: str
    embedded_content_sha256: str | None = None
    embedded_media_type: str | None = None
    embedded_storage_key: str | None = None
    embedded_bytes: bytes | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.sheet_index < 0:
            raise ValueError("sheet_index must not be negative")
        if not self.cell_range:
            raise ValueError("cell_range must not be blank")
        for name in (
            "sheet_name_sha256",
            "worksheet_part_sha256",
            "source_binding_sha256",
            "reference_sha256",
        ):
            _validate_sha256(getattr(self, name), name)
        if self.binding_kind not in {
            "dispimg_formula",
            "rich_value_local_image",
        }:
            raise ValueError("cell image binding kind is invalid")
        if self.disposition not in {
            "embedded_source_bound",
            "unresolved_source_reference",
        }:
            raise ValueError("cell image disposition is invalid")
        embedded = self.disposition == "embedded_source_bound"
        values = (
            self.embedded_content_sha256,
            self.embedded_media_type,
            self.embedded_storage_key,
            self.embedded_bytes,
        )
        if embedded != all(item is not None for item in values):
            raise ValueError("embedded cell image fields are incomplete")
        if not embedded and any(item is not None for item in values):
            raise ValueError("unresolved cell image must not carry media")
        if embedded:
            assert self.embedded_content_sha256 is not None
            assert self.embedded_bytes is not None
            _validate_sha256(
                self.embedded_content_sha256,
                "embedded_content_sha256",
            )
            if (
                hashlib.sha256(self.embedded_bytes).hexdigest()
                != self.embedded_content_sha256
            ):
                raise ValueError("embedded_content_sha256 must bind media bytes")

    def hash_only_fact(self) -> dict[str, object]:
        return {
            "sheet_index": self.sheet_index,
            "sheet_name_sha256": self.sheet_name_sha256,
            "worksheet_part_sha256": self.worksheet_part_sha256,
            "cell_range": self.cell_range,
            "binding_kind": self.binding_kind,
            "source_binding_sha256": self.source_binding_sha256,
            "reference_sha256": self.reference_sha256,
            "disposition": self.disposition,
            "embedded_content_sha256": self.embedded_content_sha256,
            "embedded_media_type": self.embedded_media_type,
            "embedded_storage_key": self.embedded_storage_key,
        }


@dataclass(frozen=True, slots=True)
class XlsxCellImageAudit:
    source_sha256: str
    source_size: int
    references: tuple[XlsxCellImageReference, ...]
    definition_count: int
    unused_definition_count: int
    schema_version: str = XLSX_CELL_IMAGE_AUDIT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_sha256(self.source_sha256, "source_sha256")
        if self.source_size < 1:
            raise ValueError("source_size must be positive")
        if self.definition_count < 0 or self.unused_definition_count < 0:
            raise ValueError("definition counts must not be negative")
        if self.unused_definition_count > self.definition_count:
            raise ValueError("unused definition count exceeds definition count")
        if len(self.references) > _MAX_CELL_IMAGE_REFERENCES:
            raise ValueError("cell image reference count exceeds safe boundary")

    @property
    def resolved_references(self) -> tuple[XlsxCellImageReference, ...]:
        return tuple(
            item
            for item in self.references
            if item.disposition == "embedded_source_bound"
        )

    @property
    def unresolved_references(self) -> tuple[XlsxCellImageReference, ...]:
        return tuple(
            item
            for item in self.references
            if item.disposition == "unresolved_source_reference"
        )

    @property
    def audit_sha256(self) -> str:
        return _canonical_sha256(self.hash_only_fact(include_audit_sha256=False))

    def hash_only_fact(
        self,
        *,
        include_audit_sha256: bool = True,
    ) -> dict[str, object]:
        fact: dict[str, object] = {
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "source_size": self.source_size,
            "reference_count": len(self.references),
            "resolved_reference_count": len(self.resolved_references),
            "unresolved_reference_count": len(self.unresolved_references),
            "definition_count": self.definition_count,
            "unused_definition_count": self.unused_definition_count,
            "references": [item.hash_only_fact() for item in self.references],
        }
        if include_audit_sha256:
            fact["audit_sha256"] = self.audit_sha256
        return fact


@dataclass(frozen=True, slots=True)
class _Relationship:
    relationship_id: str
    relationship_type: str
    target: str
    external: bool


@dataclass(frozen=True, slots=True)
class _CellImageDefinition:
    reference_id: str
    reference_sha256: str
    relationship_id: str
    embedded_content_sha256: str | None
    embedded_media_type: str | None
    embedded_storage_key: str | None
    embedded_bytes: bytes | None = field(repr=False, compare=False)


def audit_xlsx_cell_images(source_bytes: bytes) -> XlsxCellImageAudit:
    if not isinstance(source_bytes, bytes) or not source_bytes:
        raise XlsxCellImageAuditRejected("XLSX bytes are unavailable.")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    try:
        with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
            return _audit_archive(
                archive,
                source_sha256=source_sha256,
                source_size=len(source_bytes),
            )
    except (zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        raise XlsxCellImageAuditRejected(
            "XLSX cell-image parts could not be audited safely."
        ) from exc


def materialize_xlsx_cell_image_render_overlay(
    source_bytes: bytes,
    *,
    audit: XlsxCellImageAudit,
) -> bytes:
    """Build an in-memory render-only OOXML overlay from embedded source media."""

    if hashlib.sha256(source_bytes).hexdigest() != audit.source_sha256:
        raise XlsxCellImageAuditRejected(
            "Cell-image audit does not bind the provided XLSX bytes."
        )
    resolved = audit.resolved_references
    if not resolved:
        return source_bytes

    with zipfile.ZipFile(BytesIO(source_bytes)) as source:
        infos = source.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise XlsxCellImageAuditRejected(
                "Duplicate OOXML member names are not accepted."
            )
        if (
            sum(item.file_size for item in infos)
            > _MAX_OVERLAY_PACKAGE_UNCOMPRESSED_BYTES
        ):
            raise XlsxCellImageAuditRejected(
                "XLSX render overlay exceeds safe package boundary."
            )
        payloads = {item.filename: source.read(item.filename) for item in infos}

    member_names = set(payloads)
    sheet_parts = _workbook_sheet_parts_from_payloads(payloads)
    references_by_sheet: dict[int, list[XlsxCellImageReference]] = {}
    for reference in resolved:
        references_by_sheet.setdefault(reference.sheet_index, []).append(reference)

    next_drawing_number = _next_drawing_number(member_names)
    content_types_root = _parse_payload_xml(
        payloads.get(_CONTENT_TYPES_PART, b""),
        _CONTENT_TYPES_PART,
    )

    for sheet_index, references in sorted(references_by_sheet.items()):
        try:
            _, sheet_part = sheet_parts[sheet_index]
        except IndexError as exc:
            raise XlsxCellImageAuditRejected(
                "Cell-image reference sheet index is unavailable."
            ) from exc
        worksheet_root = _parse_payload_xml(payloads[sheet_part], sheet_part)
        if (
            hashlib.sha256(payloads[sheet_part]).hexdigest()
            != references[0].worksheet_part_sha256
        ):
            raise XlsxCellImageAuditRejected(
                "Worksheet bytes changed after the cell-image audit."
            )
        sheet_relationship_part = _relationship_part_name(sheet_part)
        relationship_root = _relationship_root(
            payloads.get(sheet_relationship_part)
        )
        relationships = _relationships_from_root(relationship_root)
        drawing_element = worksheet_root.find(
            f"{{{_SPREADSHEET_NS}}}drawing"
        )
        drawing_part: str | None = None
        if drawing_element is not None:
            relationship_id = drawing_element.get(
                f"{{{_DOCUMENT_REL_NS}}}id",
                "",
            )
            relationship = relationships.get(relationship_id)
            if relationship is not None and not relationship.external:
                drawing_part = _resolve_relationship_target(
                    sheet_part,
                    relationship.target,
                )
        if drawing_part is None or drawing_part not in payloads:
            drawing_part = f"xl/drawings/drawing{next_drawing_number}.xml"
            next_drawing_number += 1
            drawing_relationship_id = _next_relationship_id(relationships)
            etree.SubElement(
                relationship_root,
                f"{{{_PACKAGE_REL_NS}}}Relationship",
                Id=drawing_relationship_id,
                Type=_DRAWING_REL_TYPE,
                Target=posixpath.relpath(
                    drawing_part,
                    posixpath.dirname(sheet_part),
                ),
            )
            if drawing_element is None:
                drawing_element = etree.Element(
                    f"{{{_SPREADSHEET_NS}}}drawing"
                )
                extension_list = worksheet_root.find(
                    f"{{{_SPREADSHEET_NS}}}extLst"
                )
                if extension_list is None:
                    worksheet_root.append(drawing_element)
                else:
                    extension_list.addprevious(drawing_element)
            drawing_element.set(
                f"{{{_DOCUMENT_REL_NS}}}id",
                drawing_relationship_id,
            )
            drawing_root = etree.Element(
                f"{{{_DRAWING_NS}}}wsDr",
                nsmap={"xdr": _DRAWING_NS, "a": _DRAWING_MAIN_NS},
            )
            payloads[drawing_part] = _serialize_xml(drawing_root)
            _ensure_drawing_content_type(content_types_root, drawing_part)
            member_names.add(drawing_part)
        else:
            drawing_root = _parse_payload_xml(
                payloads[drawing_part],
                drawing_part,
            )

        drawing_relationship_part = _relationship_part_name(drawing_part)
        drawing_relationship_root = _relationship_root(
            payloads.get(drawing_relationship_part)
        )
        drawing_relationships = _relationships_from_root(
            drawing_relationship_root
        )
        next_picture_id = _next_picture_id(drawing_root)

        for reference in references:
            cell = worksheet_root.find(
                f".//{{{_SPREADSHEET_NS}}}c[@r='{reference.cell_range}']"
            )
            if cell is None:
                raise XlsxCellImageAuditRejected(
                    "Cell-image formula cell is unavailable."
                )
            if _cell_source_binding_sha256(cell) != (
                reference.source_binding_sha256
            ):
                raise XlsxCellImageAuditRejected(
                    "Cell-image source binding changed after the audit."
                )
            for child_name in ("f", "v"):
                child = cell.find(f"{{{_SPREADSHEET_NS}}}{child_name}")
                if child is not None:
                    cell.remove(child)
            cell.attrib.pop("t", None)
            cell.attrib.pop("vm", None)

            assert reference.embedded_storage_key is not None
            relationship_id = _next_relationship_id(drawing_relationships)
            target = posixpath.relpath(
                reference.embedded_storage_key.removeprefix("ooxml/"),
                posixpath.dirname(drawing_part),
            )
            etree.SubElement(
                drawing_relationship_root,
                f"{{{_PACKAGE_REL_NS}}}Relationship",
                Id=relationship_id,
                Type=_IMAGE_REL_TYPE,
                Target=target,
            )
            drawing_relationships[relationship_id] = _Relationship(
                relationship_id=relationship_id,
                relationship_type=_IMAGE_REL_TYPE,
                target=target,
                external=False,
            )
            min_column, min_row, max_column, max_row = _cell_anchor_bounds(
                worksheet_root,
                reference.cell_range,
            )
            x_emu, y_emu, width_emu, height_emu = (
                _cell_anchor_geometry(
                    worksheet_root,
                    min_column=min_column,
                    min_row=min_row,
                    max_column=max_column,
                    max_row=max_row,
                )
            )
            _append_cell_image_anchor(
                drawing_root,
                relationship_id=relationship_id,
                picture_id=next_picture_id,
                reference_sha256=reference.reference_sha256,
                min_column=min_column,
                min_row=min_row,
                max_column=max_column,
                max_row=max_row,
                x_emu=x_emu,
                y_emu=y_emu,
                width_emu=width_emu,
                height_emu=height_emu,
            )
            next_picture_id += 1

        payloads[sheet_part] = _serialize_xml(worksheet_root)
        payloads[sheet_relationship_part] = _serialize_xml(
            relationship_root
        )
        payloads[drawing_part] = _serialize_xml(drawing_root)
        payloads[drawing_relationship_part] = _serialize_xml(
            drawing_relationship_root
        )
        member_names.update(
            {
                sheet_relationship_part,
                drawing_relationship_part,
            }
        )

    payloads[_CONTENT_TYPES_PART] = _serialize_xml(content_types_root)
    output = BytesIO()
    existing = set()
    with zipfile.ZipFile(output, "w") as destination:
        for info in infos:
            destination.writestr(info, payloads[info.filename])
            existing.add(info.filename)
        for name in sorted(set(payloads) - existing):
            destination.writestr(name, payloads[name])
    return output.getvalue()


def _audit_archive(
    archive: zipfile.ZipFile,
    *,
    source_sha256: str,
    source_size: int,
) -> XlsxCellImageAudit:
    infos = archive.infolist()
    names = [item.filename for item in infos]
    if len(names) != len(set(names)):
        raise XlsxCellImageAuditRejected(
            "Duplicate OOXML member names are not accepted."
        )
    member_names = frozenset(names)
    content_types = _read_content_types(archive, member_names)
    sheet_parts = _workbook_sheet_parts(archive, member_names)
    wps_definitions = _cell_image_definitions(
        archive,
        member_names=member_names,
        content_types=content_types,
    )
    rich_definitions, rich_reference_by_vm = (
        _rich_value_image_definitions(
            archive,
            member_names=member_names,
            content_types=content_types,
        )
    )
    definitions = dict(wps_definitions)
    for reference_id, definition in rich_definitions.items():
        if reference_id in definitions:
            raise XlsxCellImageAuditRejected(
                "Cell-image definition identifiers must be unique."
            )
        definitions[reference_id] = definition
    references: list[XlsxCellImageReference] = []
    used_definition_ids: set[str] = set()
    embedded_media_bytes = 0
    counted_media_hashes: set[str] = set()

    for sheet_index, (sheet_name, worksheet_part) in enumerate(sheet_parts):
        worksheet_payload = _bounded_read_xml(archive, worksheet_part)
        worksheet_sha256 = hashlib.sha256(worksheet_payload).hexdigest()
        root = _parse_xml(worksheet_payload)
        for cell in root.findall(f".//{{{_SPREADSHEET_NS}}}c"):
            formula = cell.find(f"{{{_SPREADSHEET_NS}}}f")
            formula_text = (
                "" if formula is None else "".join(formula.itertext())
            )
            match = _DISPIMG_PATTERN.fullmatch(formula_text)
            if match is not None:
                binding_kind = "dispimg_formula"
                reference_id = match.group(1)
            else:
                vm_index = _positive_integer(cell.get("vm"))
                reference_id = rich_reference_by_vm.get(vm_index)
                if reference_id is None:
                    continue
                binding_kind = "rich_value_local_image"
            if len(references) >= _MAX_CELL_IMAGE_REFERENCES:
                raise XlsxCellImageAuditRejected(
                    "Cell-image reference count exceeds safe boundary."
                )
            reference_sha256 = hashlib.sha256(
                reference_id.encode("utf-8")
            ).hexdigest()
            definition = definitions.get(reference_id)
            cell_range = cell.get("r", "")
            _single_cell_bounds(cell_range)
            if (
                definition is not None
                and definition.embedded_content_sha256 is not None
                and definition.embedded_bytes is not None
            ):
                used_definition_ids.add(reference_id)
                if definition.embedded_content_sha256 not in counted_media_hashes:
                    counted_media_hashes.add(definition.embedded_content_sha256)
                    embedded_media_bytes += len(definition.embedded_bytes)
                    if embedded_media_bytes > _MAX_EMBEDDED_MEDIA_BYTES:
                        raise XlsxCellImageAuditRejected(
                            "Embedded cell-image media exceeds safe boundary."
                        )
                disposition = "embedded_source_bound"
                embedded_content_sha256 = (
                    definition.embedded_content_sha256
                )
                embedded_media_type = definition.embedded_media_type
                embedded_storage_key = definition.embedded_storage_key
                embedded_bytes = definition.embedded_bytes
            else:
                disposition = "unresolved_source_reference"
                embedded_content_sha256 = None
                embedded_media_type = None
                embedded_storage_key = None
                embedded_bytes = None
            references.append(
                XlsxCellImageReference(
                    sheet_index=sheet_index,
                    sheet_name_sha256=hashlib.sha256(
                        sheet_name.encode("utf-8")
                    ).hexdigest(),
                    worksheet_part_sha256=worksheet_sha256,
                    cell_range=cell_range,
                    binding_kind=binding_kind,
                    source_binding_sha256=_cell_source_binding_sha256(cell),
                    reference_id=reference_id,
                    reference_sha256=reference_sha256,
                    disposition=disposition,
                    embedded_content_sha256=embedded_content_sha256,
                    embedded_media_type=embedded_media_type,
                    embedded_storage_key=embedded_storage_key,
                    embedded_bytes=embedded_bytes,
                )
            )

    references.sort(
        key=lambda item: (
            item.sheet_index,
            _single_cell_bounds(item.cell_range)[1],
            _single_cell_bounds(item.cell_range)[0],
            item.reference_sha256,
        )
    )
    return XlsxCellImageAudit(
        source_sha256=source_sha256,
        source_size=source_size,
        references=tuple(references),
        definition_count=len(definitions),
        unused_definition_count=len(set(definitions) - used_definition_ids),
    )


def _cell_image_definitions(
    archive: zipfile.ZipFile,
    *,
    member_names: frozenset[str],
    content_types: dict[str, str],
) -> dict[str, _CellImageDefinition]:
    if _CELL_IMAGES_PART not in member_names:
        return {}
    root = _parse_xml(_bounded_read_xml(archive, _CELL_IMAGES_PART))
    relationships = _read_relationships(
        archive,
        member_names,
        _CELL_IMAGES_PART,
    )
    result: dict[str, _CellImageDefinition] = {}
    for cell_image in root.iter():
        if etree.QName(cell_image).localname != "cellImage":
            continue
        non_visual = next(
            (
                item
                for item in cell_image.iter()
                if etree.QName(item).localname == "cNvPr"
            ),
            None,
        )
        blip = next(
            (
                item
                for item in cell_image.iter()
                if etree.QName(item).localname == "blip"
            ),
            None,
        )
        reference_id = "" if non_visual is None else non_visual.get("name", "")
        relationship_id = (
            ""
            if blip is None
            else blip.get(f"{{{_DOCUMENT_REL_NS}}}embed", "")
        )
        if not reference_id or reference_id in result:
            raise XlsxCellImageAuditRejected(
                "Cell-image definitions must have unique non-blank IDs."
            )
        relationship = relationships.get(relationship_id)
        media_part: str | None = None
        media_bytes: bytes | None = None
        media_sha256: str | None = None
        media_type: str | None = None
        storage_key: str | None = None
        if (
            relationship is not None
            and not relationship.external
            and relationship.relationship_type.endswith("/image")
        ):
            media_part = _resolve_relationship_target(
                _CELL_IMAGES_PART,
                relationship.target,
            )
            if media_part in member_names:
                assert media_part is not None
                media_info = archive.getinfo(media_part)
                if media_info.file_size > _MAX_EMBEDDED_MEDIA_BYTES:
                    raise XlsxCellImageAuditRejected(
                        "Embedded cell-image media exceeds safe boundary."
                    )
                media_bytes = archive.read(media_part)
                if len(media_bytes) > _MAX_EMBEDDED_MEDIA_BYTES:
                    raise XlsxCellImageAuditRejected(
                        "Embedded cell-image media exceeds safe boundary."
                    )
                media_sha256 = hashlib.sha256(media_bytes).hexdigest()
                media_type = _media_type(media_part, content_types)
                storage_key = f"ooxml/{media_part}"
        result[reference_id] = _CellImageDefinition(
            reference_id=reference_id,
            reference_sha256=hashlib.sha256(
                reference_id.encode("utf-8")
            ).hexdigest(),
            relationship_id=relationship_id,
            embedded_content_sha256=media_sha256,
            embedded_media_type=media_type,
            embedded_storage_key=storage_key,
            embedded_bytes=media_bytes,
        )
    return result


def _rich_value_image_definitions(
    archive: zipfile.ZipFile,
    *,
    member_names: frozenset[str],
    content_types: dict[str, str],
) -> tuple[dict[str, _CellImageDefinition], dict[int, str]]:
    rich_parts = {
        _METADATA_PART,
        _RICH_VALUE_DATA_PART,
        _RICH_VALUE_STRUCTURE_PART,
        _RICH_VALUE_REL_PART,
    }
    present = rich_parts & member_names
    if not present:
        return {}, {}
    if not {
        _METADATA_PART,
        _RICH_VALUE_DATA_PART,
        _RICH_VALUE_STRUCTURE_PART,
    }.issubset(member_names):
        raise XlsxCellImageAuditRejected(
            "Rich-value cell-image metadata is incomplete."
        )

    structure_root = _parse_xml(
        _bounded_read_xml(archive, _RICH_VALUE_STRUCTURE_PART)
    )
    local_image_keys: dict[int, int] = {}
    structures = [
        item
        for item in structure_root
        if etree.QName(item).localname == "s"
    ]
    for structure_index, structure in enumerate(structures):
        if structure.get("t") != "_localImage":
            continue
        keys = [
            item
            for item in structure
            if etree.QName(item).localname == "k"
        ]
        key_index = next(
            (
                index
                for index, key in enumerate(keys)
                if key.get("n") == "_rvRel:LocalImageIdentifier"
            ),
            None,
        )
        if key_index is None:
            raise XlsxCellImageAuditRejected(
                "Local-image rich-value structure has no relationship key."
            )
        local_image_keys[structure_index] = key_index
    if not local_image_keys:
        return {}, {}

    rel_nodes: list[etree._Element] = []
    relationships: dict[str, _Relationship] = {}
    if _RICH_VALUE_REL_PART in member_names:
        relation_root = _parse_xml(
            _bounded_read_xml(archive, _RICH_VALUE_REL_PART)
        )
        rel_nodes = [
            item
            for item in relation_root
            if etree.QName(item).localname == "rel"
        ]
        relationships = _read_relationships(
            archive,
            member_names,
            _RICH_VALUE_REL_PART,
        )

    data_root = _parse_xml(
        _bounded_read_xml(archive, _RICH_VALUE_DATA_PART)
    )
    rich_values = [
        item for item in data_root if etree.QName(item).localname == "rv"
    ]
    definitions: dict[str, _CellImageDefinition] = {}
    reference_id_by_rich_value: dict[int, str] = {}
    media_cache: dict[
        str,
        tuple[str, str, str, bytes],
    ] = {}
    for rich_value_index, rich_value in enumerate(rich_values):
        structure_index = _nonnegative_integer(rich_value.get("s"))
        if structure_index not in local_image_keys:
            continue
        values = [
            item
            for item in rich_value
            if etree.QName(item).localname == "v"
        ]
        key_index = local_image_keys[structure_index]
        relationship_index = (
            _nonnegative_integer(values[key_index].text)
            if key_index < len(values)
            else None
        )
        relationship_id = ""
        if (
            relationship_index is not None
            and relationship_index < len(rel_nodes)
        ):
            relationship_id = rel_nodes[relationship_index].get(
                f"{{{_DOCUMENT_REL_NS}}}id",
                "",
            )
        reference_id = (
            f"rich-value-local-image:{rich_value_index}:"
            f"{relationship_index if relationship_index is not None else 'missing'}"
        )
        relationship = relationships.get(relationship_id)
        media_sha256: str | None = None
        media_type: str | None = None
        storage_key: str | None = None
        media_bytes: bytes | None = None
        if (
            relationship is not None
            and not relationship.external
            and relationship.relationship_type.endswith("/image")
        ):
            media_part = _resolve_relationship_target(
                _RICH_VALUE_REL_PART,
                relationship.target,
            )
            if media_part in member_names:
                assert media_part is not None
                cached = media_cache.get(media_part)
                if cached is None:
                    media_info = archive.getinfo(media_part)
                    if media_info.file_size > _MAX_EMBEDDED_MEDIA_BYTES:
                        raise XlsxCellImageAuditRejected(
                            "Embedded rich-value image exceeds safe boundary."
                        )
                    payload = archive.read(media_part)
                    if len(payload) > _MAX_EMBEDDED_MEDIA_BYTES:
                        raise XlsxCellImageAuditRejected(
                            "Embedded rich-value image exceeds safe boundary."
                        )
                    cached = (
                        hashlib.sha256(payload).hexdigest(),
                        _media_type(media_part, content_types),
                        f"ooxml/{media_part}",
                        payload,
                    )
                    media_cache[media_part] = cached
                (
                    media_sha256,
                    media_type,
                    storage_key,
                    media_bytes,
                ) = cached
        definitions[reference_id] = _CellImageDefinition(
            reference_id=reference_id,
            reference_sha256=hashlib.sha256(
                reference_id.encode("utf-8")
            ).hexdigest(),
            relationship_id=relationship_id,
            embedded_content_sha256=media_sha256,
            embedded_media_type=media_type,
            embedded_storage_key=storage_key,
            embedded_bytes=media_bytes,
        )
        reference_id_by_rich_value[rich_value_index] = reference_id

    metadata_root = _parse_xml(_bounded_read_xml(archive, _METADATA_PART))
    future_metadata = next(
        (
            item
            for item in metadata_root
            if etree.QName(item).localname == "futureMetadata"
            and item.get("name") == "XLRICHVALUE"
        ),
        None,
    )
    value_metadata = next(
        (
            item
            for item in metadata_root
            if etree.QName(item).localname == "valueMetadata"
        ),
        None,
    )
    if future_metadata is None or value_metadata is None:
        raise XlsxCellImageAuditRejected(
            "Rich-value cell-image metadata binding is unavailable."
        )
    future_rich_value_indexes: list[int | None] = []
    for block in future_metadata:
        if etree.QName(block).localname != "bk":
            continue
        rich_value_binding = next(
            (
                item
                for item in block.iter()
                if etree.QName(item).localname == "rvb"
            ),
            None,
        )
        future_rich_value_indexes.append(
            _nonnegative_integer(
                None
                if rich_value_binding is None
                else rich_value_binding.get("i")
            )
        )

    reference_by_vm: dict[int, str] = {}
    vm_index = 0
    for block in value_metadata:
        if etree.QName(block).localname != "bk":
            continue
        vm_index += 1
        record = next(
            (
                item
                for item in block.iter()
                if etree.QName(item).localname == "rc"
            ),
            None,
        )
        future_index = _nonnegative_integer(
            None if record is None else record.get("v")
        )
        if (
            future_index is None
            or future_index >= len(future_rich_value_indexes)
        ):
            continue
        rich_value_index = future_rich_value_indexes[future_index]
        if rich_value_index is None:
            continue
        reference_id = reference_id_by_rich_value.get(rich_value_index)
        if reference_id is not None:
            reference_by_vm[vm_index] = reference_id
    return definitions, reference_by_vm


def _workbook_sheet_parts(
    archive: zipfile.ZipFile,
    member_names: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    if _WORKBOOK_PART not in member_names:
        return ()
    workbook_root = _parse_xml(_bounded_read_xml(archive, _WORKBOOK_PART))
    relationships = _read_relationships(
        archive,
        member_names,
        _WORKBOOK_PART,
    )
    result: list[tuple[str, str]] = []
    for sheet in workbook_root.findall(
        f".//{{{_SPREADSHEET_NS}}}sheet"
    ):
        name = sheet.get("name", "")
        relationship_id = sheet.get(f"{{{_DOCUMENT_REL_NS}}}id", "")
        relationship = relationships.get(relationship_id)
        if not name or relationship is None or relationship.external:
            continue
        target = _resolve_relationship_target(
            _WORKBOOK_PART,
            relationship.target,
        )
        if target is not None and target in member_names:
            result.append((name, target))
    return tuple(result)


def _workbook_sheet_parts_from_payloads(
    payloads: dict[str, bytes],
) -> tuple[tuple[str, str], ...]:
    workbook_root = _parse_payload_xml(
        payloads.get(_WORKBOOK_PART, b""),
        _WORKBOOK_PART,
    )
    relationship_root = _relationship_root(
        payloads.get(_relationship_part_name(_WORKBOOK_PART))
    )
    relationships = _relationships_from_root(relationship_root)
    result: list[tuple[str, str]] = []
    for sheet in workbook_root.findall(
        f".//{{{_SPREADSHEET_NS}}}sheet"
    ):
        name = sheet.get("name", "")
        relationship_id = sheet.get(f"{{{_DOCUMENT_REL_NS}}}id", "")
        relationship = relationships.get(relationship_id)
        if not name or relationship is None or relationship.external:
            continue
        target = _resolve_relationship_target(
            _WORKBOOK_PART,
            relationship.target,
        )
        if target is not None and target in payloads:
            result.append((name, target))
    return tuple(result)


def _read_relationships(
    archive: zipfile.ZipFile,
    member_names: frozenset[str],
    source_part: str,
) -> dict[str, _Relationship]:
    relationship_part = _relationship_part_name(source_part)
    if relationship_part not in member_names:
        return {}
    return _relationships_from_root(
        _parse_xml(_bounded_read_xml(archive, relationship_part))
    )


def _relationships_from_root(
    root: etree._Element,
) -> dict[str, _Relationship]:
    result: dict[str, _Relationship] = {}
    for element in root.findall(f"{{{_PACKAGE_REL_NS}}}Relationship"):
        relationship_id = element.get("Id", "")
        if not relationship_id:
            continue
        result[relationship_id] = _Relationship(
            relationship_id=relationship_id,
            relationship_type=element.get("Type", ""),
            target=element.get("Target", ""),
            external=element.get("TargetMode", "").lower() == "external",
        )
    return result


def _relationship_root(payload: bytes | None) -> etree._Element:
    if payload is None:
        return etree.Element(
            f"{{{_PACKAGE_REL_NS}}}Relationships",
            nsmap={None: _PACKAGE_REL_NS},
        )
    return _parse_xml(payload)


def _read_content_types(
    archive: zipfile.ZipFile,
    member_names: frozenset[str],
) -> dict[str, str]:
    if _CONTENT_TYPES_PART not in member_names:
        return {}
    root = _parse_xml(_bounded_read_xml(archive, _CONTENT_TYPES_PART))
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for element in root:
        local_name = etree.QName(element).localname
        if local_name == "Default":
            defaults[element.get("Extension", "").lower()] = element.get(
                "ContentType",
                "",
            )
        elif local_name == "Override":
            overrides[element.get("PartName", "").lstrip("/")] = element.get(
                "ContentType",
                "",
            )
    result = {
        f"*.{extension}": value
        for extension, value in defaults.items()
        if value
    }
    result.update({name: value for name, value in overrides.items() if value})
    return result


def _media_type(part_name: str, content_types: dict[str, str]) -> str:
    exact = content_types.get(part_name)
    if exact:
        return exact
    extension = posixpath.splitext(part_name)[1].lstrip(".").lower()
    default = content_types.get(f"*.{extension}")
    if default:
        return default
    return mimetypes.guess_type(part_name)[0] or "application/octet-stream"


def _bounded_read_xml(archive: zipfile.ZipFile, part_name: str) -> bytes:
    try:
        info = archive.getinfo(part_name)
    except KeyError as exc:
        raise XlsxCellImageAuditRejected(
            "Required OOXML part is unavailable."
        ) from exc
    if info.file_size > _MAX_XML_PART_BYTES:
        raise XlsxCellImageAuditRejected(
            "OOXML XML part exceeds safe boundary."
        )
    payload = archive.read(part_name)
    if len(payload) > _MAX_XML_PART_BYTES:
        raise XlsxCellImageAuditRejected(
            "OOXML XML part exceeds safe boundary."
        )
    return payload


def _parse_payload_xml(payload: bytes, part_name: str) -> etree._Element:
    if not payload or len(payload) > _MAX_XML_PART_BYTES:
        raise XlsxCellImageAuditRejected(
            f"Required OOXML part {part_name!r} is unavailable."
        )
    return _parse_xml(payload)


def _parse_xml(payload: bytes) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        huge_tree=False,
    )
    return etree.fromstring(payload, parser=parser)


def _serialize_xml(root: etree._Element) -> bytes:
    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )


def _relationship_part_name(source_part: str) -> str:
    directory, name = posixpath.split(source_part)
    return posixpath.join(directory, "_rels", f"{name}.rels")


def _resolve_relationship_target(
    source_part: str,
    target: str,
) -> str | None:
    if not target or "\\" in target:
        return None
    if target.startswith("/"):
        normalized = posixpath.normpath(target.lstrip("/"))
    else:
        normalized = posixpath.normpath(
            posixpath.join(posixpath.dirname(source_part), target)
        )
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return None
    return normalized


def _single_cell_bounds(cell_range: str) -> tuple[int, int]:
    try:
        min_column, min_row, max_column, max_row = range_boundaries(cell_range)
    except ValueError as exc:
        raise XlsxCellImageAuditRejected(
            "DISPIMG formula has no valid cell coordinate."
        ) from exc
    if min_column != max_column or min_row != max_row:
        raise XlsxCellImageAuditRejected(
            "DISPIMG formula locator is not a single cell."
        )
    return min_column, min_row


def _cell_source_binding_sha256(cell: etree._Element) -> str:
    formula = cell.find(f"{{{_SPREADSHEET_NS}}}f")
    value = cell.find(f"{{{_SPREADSHEET_NS}}}v")
    return _canonical_sha256(
        {
            "cell_range": cell.get("r"),
            "type": cell.get("t"),
            "value_metadata_index": cell.get("vm"),
            "formula": (
                None if formula is None else "".join(formula.itertext())
            ),
            "value": None if value is None else "".join(value.itertext()),
        }
    )


def _positive_integer(value: object) -> int | None:
    parsed = _nonnegative_integer(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_integer(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _cell_anchor_bounds(
    worksheet_root: etree._Element,
    cell_range: str,
) -> tuple[int, int, int, int]:
    column, row = _single_cell_bounds(cell_range)
    for merged in worksheet_root.findall(
        f".//{{{_SPREADSHEET_NS}}}mergeCell"
    ):
        merged_range = merged.get("ref", "")
        try:
            min_column, min_row, max_column, max_row = range_boundaries(
                merged_range
            )
        except ValueError:
            continue
        if (
            min_column <= column <= max_column
            and min_row <= row <= max_row
        ):
            return min_column, min_row, max_column, max_row
    return column, row, column, row


def _cell_anchor_geometry(
    worksheet_root: etree._Element,
    *,
    min_column: int,
    min_row: int,
    max_column: int,
    max_row: int,
) -> tuple[int, int, int, int]:
    sheet_format = worksheet_root.find(
        f"{{{_SPREADSHEET_NS}}}sheetFormatPr"
    )
    default_column_width = 8.43
    default_row_height = 15.0
    if sheet_format is not None:
        try:
            default_column_width = float(
                sheet_format.get("defaultColWidth", default_column_width)
            )
        except ValueError:
            pass
        try:
            default_row_height = float(
                sheet_format.get("defaultRowHeight", default_row_height)
            )
        except ValueError:
            pass

    column_widths: dict[int, float] = {}
    columns = worksheet_root.find(f"{{{_SPREADSHEET_NS}}}cols")
    if columns is not None:
        for item in columns:
            if etree.QName(item).localname != "col":
                continue
            start = _positive_integer(item.get("min"))
            end = _positive_integer(item.get("max"))
            if start is None or end is None or end < start:
                continue
            hidden = item.get("hidden") in {"1", "true", "TRUE"}
            try:
                width = float(item.get("width", default_column_width))
            except ValueError:
                width = default_column_width
            for column in range(start, end + 1):
                column_widths[column] = 0.0 if hidden else width

    row_heights: dict[int, float] = {}
    for item in worksheet_root.findall(
        f".//{{{_SPREADSHEET_NS}}}sheetData/"
        f"{{{_SPREADSHEET_NS}}}row"
    ):
        row = _positive_integer(item.get("r"))
        if row is None:
            continue
        hidden = item.get("hidden") in {"1", "true", "TRUE"}
        try:
            height = float(item.get("ht", default_row_height))
        except ValueError:
            height = default_row_height
        row_heights[row] = 0.0 if hidden else height

    def column_emu(column: int) -> int:
        width = column_widths.get(column, default_column_width)
        if width <= 0:
            return 0
        pixels = int(width * 7.0 + 5.0) if width >= 1 else int(width * 12.0 + 0.5)
        return max(1, pixels) * 9_525

    def row_emu(row: int) -> int:
        points = row_heights.get(row, default_row_height)
        return max(0, int(round(points * 12_700)))

    x_emu = sum(column_emu(column) for column in range(1, min_column))
    y_emu = sum(row_emu(row) for row in range(1, min_row))
    width_emu = sum(
        column_emu(column)
        for column in range(min_column, max_column + 1)
    )
    height_emu = sum(
        row_emu(row)
        for row in range(min_row, max_row + 1)
    )
    return (
        x_emu,
        y_emu,
        max(1, width_emu),
        max(1, height_emu),
    )


def _append_cell_image_anchor(
    drawing_root: etree._Element,
    *,
    relationship_id: str,
    picture_id: int,
    reference_sha256: str,
    min_column: int,
    min_row: int,
    max_column: int,
    max_row: int,
    x_emu: int,
    y_emu: int,
    width_emu: int,
    height_emu: int,
) -> None:
    anchor = etree.SubElement(
        drawing_root,
        f"{{{_DRAWING_NS}}}twoCellAnchor",
        editAs="twoCell",
    )
    start = etree.SubElement(anchor, f"{{{_DRAWING_NS}}}from")
    _append_marker(
        start,
        column=min_column - 1,
        row=min_row - 1,
    )
    end = etree.SubElement(anchor, f"{{{_DRAWING_NS}}}to")
    _append_marker(end, column=max_column, row=max_row)
    picture = etree.SubElement(anchor, f"{{{_DRAWING_NS}}}pic")
    non_visual = etree.SubElement(
        picture,
        f"{{{_DRAWING_NS}}}nvPicPr",
    )
    etree.SubElement(
        non_visual,
        f"{{{_DRAWING_NS}}}cNvPr",
        id=str(picture_id),
        name=f"ReqPilotX-cell-image-{reference_sha256[:16]}",
    )
    etree.SubElement(non_visual, f"{{{_DRAWING_NS}}}cNvPicPr")
    blip_fill = etree.SubElement(
        picture,
        f"{{{_DRAWING_NS}}}blipFill",
    )
    blip = etree.SubElement(
        blip_fill,
        f"{{{_DRAWING_MAIN_NS}}}blip",
    )
    blip.set(f"{{{_DOCUMENT_REL_NS}}}embed", relationship_id)
    stretch = etree.SubElement(
        blip_fill,
        f"{{{_DRAWING_MAIN_NS}}}stretch",
    )
    etree.SubElement(stretch, f"{{{_DRAWING_MAIN_NS}}}fillRect")
    shape_properties = etree.SubElement(
        picture,
        f"{{{_DRAWING_NS}}}spPr",
    )
    transform = etree.SubElement(
        shape_properties,
        f"{{{_DRAWING_MAIN_NS}}}xfrm",
    )
    etree.SubElement(
        transform,
        f"{{{_DRAWING_MAIN_NS}}}off",
        x=str(x_emu),
        y=str(y_emu),
    )
    etree.SubElement(
        transform,
        f"{{{_DRAWING_MAIN_NS}}}ext",
        cx=str(width_emu),
        cy=str(height_emu),
    )
    preset = etree.SubElement(
        shape_properties,
        f"{{{_DRAWING_MAIN_NS}}}prstGeom",
        prst="rect",
    )
    etree.SubElement(preset, f"{{{_DRAWING_MAIN_NS}}}avLst")
    etree.SubElement(anchor, f"{{{_DRAWING_NS}}}clientData")


def _append_marker(
    parent: etree._Element,
    *,
    column: int,
    row: int,
) -> None:
    for local_name, value in (
        ("col", column),
        ("colOff", 0),
        ("row", row),
        ("rowOff", 0),
    ):
        child = etree.SubElement(
            parent,
            f"{{{_DRAWING_NS}}}{local_name}",
        )
        child.text = str(value)


def _next_relationship_id(
    relationships: dict[str, _Relationship],
) -> str:
    used = set(relationships)
    index = 1
    while f"rId{index}" in used:
        index += 1
    return f"rId{index}"


def _next_picture_id(drawing_root: etree._Element) -> int:
    used: set[int] = set()
    for element in drawing_root.iter():
        if etree.QName(element).localname != "cNvPr":
            continue
        try:
            used.add(int(element.get("id", "0")))
        except ValueError:
            continue
    candidate = max(used, default=0) + 1
    while candidate in used:
        candidate += 1
    return candidate


def _next_drawing_number(member_names: set[str]) -> int:
    pattern = re.compile(r"^xl/drawings/drawing([0-9]+)\.xml$")
    used = {
        int(match.group(1))
        for name in member_names
        if (match := pattern.fullmatch(name)) is not None
    }
    candidate = max(used, default=0) + 1
    while candidate in used:
        candidate += 1
    return candidate


def _ensure_drawing_content_type(
    root: etree._Element,
    drawing_part: str,
) -> None:
    part_name = f"/{drawing_part}"
    for item in root:
        if (
            etree.QName(item).localname == "Override"
            and item.get("PartName") == part_name
        ):
            return
    etree.SubElement(
        root,
        f"{{{_CONTENT_TYPE_NS}}}Override",
        PartName=part_name,
        ContentType="application/vnd.openxmlformats-officedocument.drawing+xml",
    )


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_sha256(value: str, field_name: str) -> None:
    if not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256")


__all__ = [
    "XLSX_CELL_IMAGE_AUDIT_SCHEMA_VERSION",
    "XlsxCellImageAudit",
    "XlsxCellImageAuditRejected",
    "XlsxCellImageReference",
    "audit_xlsx_cell_images",
    "materialize_xlsx_cell_image_render_overlay",
]
