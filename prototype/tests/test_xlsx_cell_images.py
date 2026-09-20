from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import base64
import hashlib
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from lxml import etree
from openpyxl import Workbook

from app.adapters.documents.preflight import preflight_document
from app.adapters.documents.xlsx_source_ir_adapter import XlsxSourceIRAdapter
from app.adapters.documents.xlsx_cell_images import (
    XlsxCellImageAuditRejected,
    audit_xlsx_cell_images,
    materialize_xlsx_cell_image_render_overlay,
)
from app.domain.extraction.source_ir import SourceIRStatus, SourceNodeKind
from datetime import datetime, timezone


_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)
_SPREADSHEET_NS = (
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
)
_PACKAGE_REL_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)


class XlsxCellImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_embedded_and_unresolved_dispimg_references_are_distinct(self) -> None:
        path = _write_dispimg_workbook(
            self.root / "cell-images.xlsx",
            include_embedded_definition=True,
        )
        source_bytes = path.read_bytes()

        first = audit_xlsx_cell_images(source_bytes)
        second = audit_xlsx_cell_images(source_bytes)

        self.assertEqual(first, second)
        self.assertEqual(first.audit_sha256, second.audit_sha256)
        self.assertEqual(len(first.references), 2)
        self.assertEqual(len(first.resolved_references), 1)
        self.assertEqual(len(first.unresolved_references), 1)
        resolved = first.resolved_references[0]
        self.assertEqual(resolved.cell_range, "A1")
        self.assertEqual(
            resolved.embedded_content_sha256,
            hashlib.sha256(_ONE_PIXEL_PNG).hexdigest(),
        )
        self.assertEqual(resolved.embedded_storage_key, "ooxml/xl/media/cell.png")
        unresolved = first.unresolved_references[0]
        self.assertEqual(unresolved.cell_range, "B1")
        self.assertIsNone(unresolved.embedded_bytes)
        serialized = first.hash_only_fact()
        self.assertNotIn("ID_EMBEDDED", str(serialized))
        self.assertNotIn("ID_ORPHAN", str(serialized))

    def test_render_overlay_uses_only_bound_media_and_blanks_resolved_formula(
        self,
    ) -> None:
        path = _write_dispimg_workbook(
            self.root / "overlay.xlsx",
            include_embedded_definition=True,
        )
        source_bytes = path.read_bytes()
        audit = audit_xlsx_cell_images(source_bytes)

        overlay = materialize_xlsx_cell_image_render_overlay(
            source_bytes,
            audit=audit,
        )

        self.assertNotEqual(overlay, source_bytes)
        with zipfile.ZipFile(BytesIO(overlay)) as archive:
            worksheet = etree.fromstring(
                archive.read("xl/worksheets/sheet1.xml")
            )
            embedded_cell = worksheet.find(
                f".//{{{_SPREADSHEET_NS}}}c[@r='A1']"
            )
            orphan_cell = worksheet.find(
                f".//{{{_SPREADSHEET_NS}}}c[@r='B1']"
            )
            assert embedded_cell is not None
            assert orphan_cell is not None
            self.assertIsNone(
                embedded_cell.find(f"{{{_SPREADSHEET_NS}}}f")
            )
            self.assertIsNotNone(
                orphan_cell.find(f"{{{_SPREADSHEET_NS}}}f")
            )
            drawing_parts = sorted(
                name
                for name in archive.namelist()
                if name.startswith("xl/drawings/drawing")
                and name.endswith(".xml")
            )
            self.assertEqual(len(drawing_parts), 1)
            drawing = archive.read(drawing_parts[0]).decode("utf-8")
            self.assertIn(
                audit.resolved_references[0].reference_sha256[:16],
                drawing,
            )
            relationship_part = (
                "xl/drawings/_rels/"
                f"{Path(drawing_parts[0]).name}.rels"
            )
            relationships = archive.read(relationship_part).decode("utf-8")
            self.assertIn("../media/cell.png", relationships)
            self.assertEqual(
                archive.read("xl/media/cell.png"),
                _ONE_PIXEL_PNG,
            )

    def test_overlay_rejects_audit_from_other_source(self) -> None:
        first = _write_dispimg_workbook(
            self.root / "first.xlsx",
            include_embedded_definition=True,
        ).read_bytes()
        second = _write_dispimg_workbook(
            self.root / "second.xlsx",
            include_embedded_definition=False,
        ).read_bytes()

        with self.assertRaises(XlsxCellImageAuditRejected):
            materialize_xlsx_cell_image_render_overlay(
                second,
                audit=audit_xlsx_cell_images(first),
            )

    def test_source_ir_binds_embedded_media_and_flags_orphan_reference(
        self,
    ) -> None:
        path = _write_dispimg_workbook(
            self.root / "source-ir.xlsx",
            include_embedded_definition=True,
        )

        snapshot = XlsxSourceIRAdapter().extract(
            path,
            preflight=preflight_document(path),
            created_at=datetime(2026, 7, 26, 11, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
        images = [
            item
            for item in snapshot.nodes
            if item.kind is SourceNodeKind.IMAGE
        ]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].native_locator.sheet, "Images")
        self.assertEqual(images[0].native_locator.cell_range, "A1")
        self.assertEqual(len(snapshot.assets), 1)
        self.assertEqual(
            snapshot.assets[0].content_sha256,
            hashlib.sha256(_ONE_PIXEL_PNG).hexdigest(),
        )
        codes = [item.code for item in snapshot.diagnostics]
        self.assertIn(
            "xlsx_dispimg_embedded_cell_image_preserved",
            codes,
        )
        self.assertIn("xlsx_dispimg_reference_unresolved", codes)

    def test_excel_rich_value_local_image_is_bound_and_overlayed(self) -> None:
        path = _write_rich_value_image_workbook(
            self.root / "rich-value-image.xlsx"
        )
        source_bytes = path.read_bytes()

        audit = audit_xlsx_cell_images(source_bytes)

        self.assertEqual(len(audit.references), 1)
        reference = audit.references[0]
        self.assertEqual(reference.binding_kind, "rich_value_local_image")
        self.assertEqual(reference.cell_range, "A1")
        self.assertEqual(
            reference.embedded_content_sha256,
            hashlib.sha256(_ONE_PIXEL_PNG).hexdigest(),
        )
        overlay = materialize_xlsx_cell_image_render_overlay(
            source_bytes,
            audit=audit,
        )
        with zipfile.ZipFile(BytesIO(overlay)) as archive:
            worksheet = etree.fromstring(
                archive.read("xl/worksheets/sheet1.xml")
            )
            cell = worksheet.find(
                f".//{{{_SPREADSHEET_NS}}}c[@r='A1']"
            )
            assert cell is not None
            self.assertNotIn("vm", cell.attrib)
            self.assertIsNone(cell.find(f"{{{_SPREADSHEET_NS}}}v"))

        snapshot = XlsxSourceIRAdapter().extract(
            path,
            preflight=preflight_document(path),
            created_at=datetime(2026, 7, 26, 11, 0, tzinfo=timezone.utc),
        )
        images = [
            item
            for item in snapshot.nodes
            if item.kind is SourceNodeKind.IMAGE
        ]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].native_locator.cell_range, "A1")
        self.assertIn(
            "xlsx_rich_value_embedded_cell_image_preserved",
            {item.code for item in snapshot.diagnostics},
        )


def _write_dispimg_workbook(
    path: Path,
    *,
    include_embedded_definition: bool,
) -> Path:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Images"
    worksheet["A1"] = '=_xlfn.DISPIMG("ID_EMBEDDED",1)'
    worksheet["B1"] = '=_xlfn.DISPIMG("ID_ORPHAN",1)'
    workbook.save(path)
    workbook.close()
    if not include_embedded_definition:
        return path

    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        payloads = {
            item.filename: archive.read(item.filename)
            for item in infos
        }
    content_types = etree.fromstring(payloads["[Content_Types].xml"])
    if not any(
        item.get("Extension") == "png"
        for item in content_types
        if etree.QName(item).localname == "Default"
    ):
        etree.SubElement(
            content_types,
            "{http://schemas.openxmlformats.org/package/2006/content-types}Default",
            Extension="png",
            ContentType="image/png",
        )
    etree.SubElement(
        content_types,
        "{http://schemas.openxmlformats.org/package/2006/content-types}Override",
        PartName="/xl/cellimages.xml",
        ContentType="application/vnd.wps-officedocument.cellimage+xml",
    )
    payloads["[Content_Types].xml"] = etree.tostring(
        content_types,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )
    workbook_relationships = etree.fromstring(
        payloads["xl/_rels/workbook.xml.rels"]
    )
    etree.SubElement(
        workbook_relationships,
        f"{{{_PACKAGE_REL_NS}}}Relationship",
        Id="rIdCellImage",
        Type="http://www.wps.cn/officeDocument/2020/cellImage",
        Target="cellimages.xml",
    )
    payloads["xl/_rels/workbook.xml.rels"] = etree.tostring(
        workbook_relationships,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )
    payloads["xl/cellimages.xml"] = b"""<?xml version="1.0" encoding="UTF-8"?>
<etc:cellImages
 xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData"
 xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <etc:cellImage><xdr:pic><xdr:nvPicPr>
  <xdr:cNvPr id="1" name="ID_EMBEDDED"/><xdr:cNvPicPr/>
 </xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rId1"/>
 <a:stretch><a:fillRect/></a:stretch></xdr:blipFill>
 <xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>
 </xdr:pic></etc:cellImage>
</etc:cellImages>"""
    payloads["xl/_rels/cellimages.xml.rels"] = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
  Target="media/cell.png"/>
</Relationships>"""
    payloads["xl/media/cell.png"] = _ONE_PIXEL_PNG

    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        existing = set()
        for info in infos:
            archive.writestr(info, payloads[info.filename])
            existing.add(info.filename)
        for name in sorted(set(payloads) - existing):
            archive.writestr(name, payloads[name])
    path.write_bytes(output.getvalue())
    return path


def _write_rich_value_image_workbook(path: Path) -> Path:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Rich Image"
    worksheet["A1"] = "#VALUE!"
    workbook.save(path)
    workbook.close()

    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        payloads = {
            item.filename: archive.read(item.filename)
            for item in infos
        }
    worksheet_root = etree.fromstring(
        payloads["xl/worksheets/sheet1.xml"]
    )
    cell = worksheet_root.find(
        f".//{{{_SPREADSHEET_NS}}}c[@r='A1']"
    )
    assert cell is not None
    cell.set("vm", "1")
    payloads["xl/worksheets/sheet1.xml"] = etree.tostring(
        worksheet_root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )
    content_types = etree.fromstring(payloads["[Content_Types].xml"])
    content_type_ns = (
        "http://schemas.openxmlformats.org/package/2006/content-types"
    )
    if not any(
        item.get("Extension") == "png"
        for item in content_types
        if etree.QName(item).localname == "Default"
    ):
        etree.SubElement(
            content_types,
            f"{{{content_type_ns}}}Default",
            Extension="png",
            ContentType="image/png",
        )
    for part_name, content_type in (
        (
            "/xl/metadata.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheetMetadata+xml",
        ),
        (
            "/xl/richData/rdrichvalue.xml",
            "application/vnd.ms-excel.rdrichvalue+xml",
        ),
        (
            "/xl/richData/rdrichvaluestructure.xml",
            "application/vnd.ms-excel.rdrichvaluestructure+xml",
        ),
        (
            "/xl/richData/richValueRel.xml",
            "application/vnd.ms-excel.richvaluerel+xml",
        ),
    ):
        etree.SubElement(
            content_types,
            f"{{{content_type_ns}}}Override",
            PartName=part_name,
            ContentType=content_type,
        )
    payloads["[Content_Types].xml"] = etree.tostring(
        content_types,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )
    payloads["xl/metadata.xml"] = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:xlrd="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata">
 <metadataTypes count="1"><metadataType name="XLRICHVALUE"/></metadataTypes>
 <futureMetadata name="XLRICHVALUE" count="1"><bk><extLst><ext uri="{test}">
  <xlrd:rvb i="0"/>
 </ext></extLst></bk></futureMetadata>
 <valueMetadata count="1"><bk><rc t="1" v="0"/></bk></valueMetadata>
</metadata>"""
    payloads["xl/richData/rdrichvaluestructure.xml"] = b"""<?xml version="1.0" encoding="UTF-8"?>
<rvStructures xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata"
 count="1"><s t="_localImage">
 <k n="_rvRel:LocalImageIdentifier" t="i"/><k n="CalcOrigin" t="i"/>
</s></rvStructures>"""
    payloads["xl/richData/rdrichvalue.xml"] = b"""<?xml version="1.0" encoding="UTF-8"?>
<rvData xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata"
 count="1"><rv s="0"><v>0</v><v>5</v></rv></rvData>"""
    payloads["xl/richData/richValueRel.xml"] = b"""<?xml version="1.0" encoding="UTF-8"?>
<richValueRels
 xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/richvaluerel"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <rel r:id="rId1"/>
</richValueRels>"""
    payloads[
        "xl/richData/_rels/richValueRel.xml.rels"
    ] = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
  Target="../media/rich.png"/>
</Relationships>"""
    payloads["xl/media/rich.png"] = _ONE_PIXEL_PNG

    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        existing = set()
        for info in infos:
            archive.writestr(info, payloads[info.filename])
            existing.add(info.filename)
        for name in sorted(set(payloads) - existing):
            archive.writestr(name, payloads[name])
    path.write_bytes(output.getvalue())
    return path


if __name__ == "__main__":
    unittest.main()
