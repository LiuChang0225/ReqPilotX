from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from app.adapters.documents.preflight import preflight_document
from app.adapters.documents import xlsb_source_ir_adapter as xlsb_module
from app.adapters.documents.spreadsheet_visual_profile import (
    SpreadsheetRenderedPage,
    SpreadsheetSheetPageBinding,
    SpreadsheetVisualProfile,
    SpreadsheetVisualProfileRejected,
    SpreadsheetVisualRenderConfig,
)
from app.adapters.documents.xlsb_source_ir_adapter import (
    LibreOfficeXlsbConverter,
    XlsbConversionArtifact,
    XlsbConversionRejected,
    XlsbConversionRejectionCode,
    XlsbSourceIRAdapter,
)
from app.application.extraction.build_source_ir import (
    SourceIRBuildRejected,
    SourceIRBuildRejectionCode,
    build_source_ir,
)
from app.domain.extraction.preflight import DocumentFormat
from app.domain.extraction.source_ir import (
    SourceAssetKind,
    SourceConversionSecurityConfig,
    SourceIRStatus,
)
from app.ports.source_ir_adapter import (
    SourceIRAdapterRejected,
    SourceIRAdapterRejectionCode,
)


CREATED_AT = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)
_RELATIONSHIPS_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)


class _FakeConverter:
    def __init__(
        self,
        artifact: XlsbConversionArtifact,
        *,
        source_to_mutate: Path | None = None,
        rejection: XlsbConversionRejected | None = None,
    ) -> None:
        self.artifact = artifact
        self.source_to_mutate = source_to_mutate
        self.rejection = rejection
        self.calls = 0

    def convert(self, source_bytes: bytes) -> XlsbConversionArtifact:
        self.calls += 1
        if self.source_to_mutate is not None:
            self.source_to_mutate.write_bytes(
                self.source_to_mutate.read_bytes() + b"changed"
            )
        if self.rejection is not None:
            raise self.rejection
        return self.artifact


class _FakeVisualRenderer:
    def __init__(
        self,
        *,
        rejection: SpreadsheetVisualProfileRejected | None = None,
        invalid_source_identity: bool = False,
    ) -> None:
        self.rejection = rejection
        self.invalid_source_identity = invalid_source_identity
        self.calls = 0
        self.external_visual_relationships = ()

    def render(
        self,
        source_bytes: bytes,
        *,
        source_format: DocumentFormat,
        expected_sheets: tuple,
        external_visual_relationships: tuple = (),
    ) -> SpreadsheetVisualProfile:
        self.calls += 1
        self.external_visual_relationships = external_visual_relationships
        if self.rejection is not None:
            raise self.rejection
        profile_source = (
            source_bytes + b"invalid"
            if self.invalid_source_identity
            else source_bytes
        )
        bindings: list[SpreadsheetSheetPageBinding] = []
        pages: list[SpreadsheetRenderedPage] = []
        next_page = 1
        for sheet in expected_sheets:
            if sheet.expects_rendered_pages:
                page_start = next_page
                page_end = next_page
                png_bytes = f"page-{next_page}".encode("ascii")
                pages.append(
                    SpreadsheetRenderedPage(
                        page_number=next_page,
                        sheet_index=sheet.sheet_index,
                        sheet_name_sha256=sheet.sheet_name_sha256,
                        width_pixels=1_224,
                        height_pixels=1_584,
                        png_sha256=hashlib.sha256(png_bytes).hexdigest(),
                        standalone_pdf_sha256=hashlib.sha256(
                            b"pdf-" + png_bytes
                        ).hexdigest(),
                        png_bytes=png_bytes,
                    )
                )
                next_page += 1
            else:
                page_start = None
                page_end = None
            bindings.append(
                SpreadsheetSheetPageBinding(
                    sheet_index=sheet.sheet_index,
                    sheet_name_sha256=sheet.sheet_name_sha256,
                    sheet_state=sheet.sheet_state,
                    page_start=page_start,
                    page_end=page_end,
                )
            )
        pdf_bytes = b"synthetic direct-render PDF"
        return SpreadsheetVisualProfile(
            source_sha256=hashlib.sha256(profile_source).hexdigest(),
            source_size=len(profile_source),
            source_format=source_format,
            renderer_id="test-direct-renderer",
            renderer_version="1.0",
            libreoffice_version="26.2.4.2",
            libreoffice_build_id="a" * 40,
            libreoffice_executable_sha256="c" * 64,
            pymupdf_version="test",
            config=SpreadsheetVisualRenderConfig(),
            render_output_sha256=hashlib.sha256(b"render log").hexdigest(),
            pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            pdf_bytes=pdf_bytes,
            sheets=tuple(bindings),
            pages=tuple(pages),
            external_visual_relationships=external_visual_relationships,
        )


class XlsbSourceIRAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.derived_bytes = _xlsx_bytes()
        self.artifact = _artifact(self.derived_bytes)
        self.visual_renderer = _FakeVisualRenderer()

    def _adapter(
        self,
        converter: _FakeConverter,
        *,
        visual_renderer: _FakeVisualRenderer | None = None,
    ) -> XlsbSourceIRAdapter:
        return XlsbSourceIRAdapter(
            converter=converter,
            visual_renderer=visual_renderer or self.visual_renderer,
        )

    def test_controlled_conversion_reuses_xlsx_mapper_without_replacing_source_truth(
        self,
    ) -> None:
        source = _write_xlsb(self.root / "source.xlsb", external_cid_image=True)
        original_bytes = source.read_bytes()
        converter = _FakeConverter(self.artifact)
        adapter = self._adapter(converter)

        snapshot = build_source_ir(
            source,
            adapters=(adapter,),
            clock=lambda: CREATED_AT,
        )

        self.assertEqual(converter.calls, 1)
        self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
        self.assertEqual(
            snapshot.source_sha256,
            hashlib.sha256(original_bytes).hexdigest(),
        )
        self.assertEqual(snapshot.adapter_id, "controlled-xlsb-derived-xlsx")
        self.assertIn(self.artifact.derived_source_sha256, snapshot.adapter_version)
        self.assertGreater(len(snapshot.nodes), 0)
        self.assertTrue(
            all(
                node.native_locator.format.value == "xlsx"
                for node in snapshot.nodes
            )
        )
        provenance = snapshot.source_conversion
        assert provenance is not None
        self.assertEqual(provenance.original_source_sha256, snapshot.source_sha256)
        self.assertEqual(
            provenance.derived_source_sha256,
            self.artifact.derived_source_sha256,
        )
        self.assertEqual(provenance.source_truth, "original_bytes")
        self.assertEqual(
            provenance.fidelity_profile,
            "derived_spreadsheet_structure_and_values_only",
        )
        self.assertEqual(
            provenance.converter_config.network_policy,
            "macos_sandbox_deny_non_loopback_ip",
        )
        diagnostic_codes = {item.code for item in snapshot.diagnostics}
        self.assertIn(
            "xlsb_controlled_conversion_provenance_recorded",
            diagnostic_codes,
        )
        self.assertIn(
            "xlsb_derived_xlsx_not_native_fidelity",
            diagnostic_codes,
        )
        self.assertIn(
            "xlsb_direct_visual_profile_recorded",
            diagnostic_codes,
        )
        self.assertIn(
            "xlsb_direct_visual_profile_ai_review_pending",
            diagnostic_codes,
        )
        self.assertIn(
            "xlsb_external_cid_visual_relationship_uses_embedded_cache",
            diagnostic_codes,
        )
        self.assertEqual(self.visual_renderer.calls, 1)
        self.assertEqual(len(self.visual_renderer.external_visual_relationships), 1)
        self.assertEqual(
            self.visual_renderer.external_visual_relationships[
                0
            ].embedded_cache_sha256,
            hashlib.sha256(b"synthetic embedded cache").hexdigest(),
        )
        direct_assets = tuple(
            asset
            for asset in snapshot.assets
            if asset.kind is SourceAssetKind.PAGE_RENDER
        )
        self.assertEqual(len(direct_assets), 1)
        self.assertIn(snapshot.source_sha256, direct_assets[0].storage_key)
        self.assertNotIn(
            self.artifact.derived_source_sha256,
            direct_assets[0].storage_key,
        )
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertEqual(tuple(self.root.iterdir()), (source,))

    def test_xlsb_stays_native_unsupported_but_is_exactly_conversion_eligible(
        self,
    ) -> None:
        source = _write_xlsb(self.root / "source.xlsb")

        report = preflight_document(source)

        self.assertEqual(report.identity.detected_format, DocumentFormat.XLSB)
        self.assertFalse(report.processable)
        self.assertTrue(report.controlled_conversion_eligible)

        source.rename(self.root / "source.xlsx")
        mismatched = preflight_document(self.root / "source.xlsx")
        self.assertFalse(mismatched.controlled_conversion_eligible)

    def test_macro_external_data_and_external_file_links_fail_before_converter(
        self,
    ) -> None:
        cases = (
            {"extra_member": ("xl/vbaProject.bin", b"macro")},
            {"extra_member": ("xl/connections.xml", b"<connections/>")},
            {"external_target": "https://example.invalid/workbook.xlsx"},
            {"external_target": "file:///private/customer/workbook.xlsx"},
            {"isolated_cid_image": True},
        )
        for index, case in enumerate(cases):
            with self.subTest(case=index):
                source = _write_xlsb(
                    self.root / f"source-{index}.xlsb",
                    **case,
                )
                converter = _FakeConverter(self.artifact)
                adapter = self._adapter(converter)

                with self.assertRaises(SourceIRAdapterRejected) as caught:
                    adapter.extract(
                        source,
                        preflight=preflight_document(source),
                        created_at=CREATED_AT,
                    )

                self.assertEqual(
                    caught.exception.code,
                    SourceIRAdapterRejectionCode.CONVERSION_POLICY_REJECTED,
                )
                self.assertEqual(converter.calls, 0)
                self.assertNotIn("example.invalid", str(caught.exception))
                self.assertNotIn("/private/customer", str(caught.exception))

    def test_visual_profile_failure_and_invalid_source_binding_fail_closed(
        self,
    ) -> None:
        source = _write_xlsb(self.root / "visual-profile-failure.xlsb")
        failed_renderer = _FakeVisualRenderer(
            rejection=SpreadsheetVisualProfileRejected(
                "The direct visual profile could not be produced."
            )
        )
        with self.assertRaises(SourceIRAdapterRejected) as failed:
            self._adapter(
                _FakeConverter(self.artifact),
                visual_renderer=failed_renderer,
            ).extract(
                source,
                preflight=preflight_document(source),
                created_at=CREATED_AT,
            )
        self.assertEqual(
            failed.exception.code,
            SourceIRAdapterRejectionCode.VISUAL_PROFILE_FAILED,
        )

        invalid_renderer = _FakeVisualRenderer(invalid_source_identity=True)
        with self.assertRaises(SourceIRAdapterRejected) as invalid:
            self._adapter(
                _FakeConverter(self.artifact),
                visual_renderer=invalid_renderer,
            ).extract(
                source,
                preflight=preflight_document(source),
                created_at=CREATED_AT,
            )
        self.assertEqual(
            invalid.exception.code,
            SourceIRAdapterRejectionCode.VISUAL_PROFILE_FAILED,
        )

    def test_converter_failure_codes_are_preserved_at_safe_adapter_boundary(
        self,
    ) -> None:
        expected = (
            (
                XlsbConversionRejectionCode.CONVERTER_UNAVAILABLE,
                SourceIRAdapterRejectionCode.CONVERTER_UNAVAILABLE,
            ),
            (
                XlsbConversionRejectionCode.VERSION_MISMATCH,
                SourceIRAdapterRejectionCode.CONVERTER_VERSION_MISMATCH,
            ),
            (
                XlsbConversionRejectionCode.TIMEOUT,
                SourceIRAdapterRejectionCode.CONVERSION_TIMEOUT,
            ),
            (
                XlsbConversionRejectionCode.PROCESS_FAILED,
                SourceIRAdapterRejectionCode.CONVERSION_FAILED,
            ),
            (
                XlsbConversionRejectionCode.OUTPUT_INVALID,
                SourceIRAdapterRejectionCode.DERIVED_PREFLIGHT_REJECTED,
            ),
        )
        for index, (conversion_code, adapter_code) in enumerate(expected):
            with self.subTest(code=conversion_code.value):
                source = _write_xlsb(self.root / f"source-{index}.xlsb")
                adapter = self._adapter(
                    _FakeConverter(
                        self.artifact,
                        rejection=XlsbConversionRejected(
                            conversion_code,
                            "safe converter failure",
                        ),
                    ),
                )

                with self.assertRaises(SourceIRAdapterRejected) as caught:
                    adapter.extract(
                        source,
                        preflight=preflight_document(source),
                        created_at=CREATED_AT,
                    )

                self.assertEqual(caught.exception.code, adapter_code)

    def test_invalid_derived_signature_and_source_toctou_fail_closed(self) -> None:
        invalid_artifact = _artifact(b"not an xlsx package")
        invalid_source = _write_xlsb(self.root / "invalid-derived.xlsb")
        with self.assertRaises(SourceIRAdapterRejected) as invalid:
            self._adapter(_FakeConverter(invalid_artifact)).extract(
                invalid_source,
                preflight=preflight_document(invalid_source),
                created_at=CREATED_AT,
            )
        self.assertEqual(
            invalid.exception.code,
            SourceIRAdapterRejectionCode.DERIVED_PREFLIGHT_REJECTED,
        )

        changed_source = _write_xlsb(self.root / "changed.xlsb")
        changed_preflight = preflight_document(changed_source)
        with self.assertRaises(SourceIRAdapterRejected) as changed:
            self._adapter(
                _FakeConverter(
                    self.artifact,
                    source_to_mutate=changed_source,
                )
            ).extract(
                changed_source,
                preflight=changed_preflight,
                created_at=CREATED_AT,
            )
        self.assertEqual(
            changed.exception.code,
            SourceIRAdapterRejectionCode.SOURCE_CHANGED,
        )

    def test_mismatched_extension_and_naive_timestamp_are_rejected(self) -> None:
        source = _write_xlsb(self.root / "source.xlsb")
        disguised = self.root / "source.xlsx"
        disguised.write_bytes(source.read_bytes())

        with self.assertRaises(SourceIRBuildRejected) as mismatched:
            build_source_ir(
                disguised,
                adapters=(self._adapter(_FakeConverter(self.artifact)),),
                clock=lambda: CREATED_AT,
            )
        self.assertEqual(
            mismatched.exception.code,
            SourceIRBuildRejectionCode.PREFLIGHT_REJECTED,
        )
        self.assertIn(
            "extension_signature_mismatch",
            mismatched.exception.preflight_diagnostic_codes,
        )

        with self.assertRaises(SourceIRAdapterRejected) as timestamp:
            self._adapter(_FakeConverter(self.artifact)).extract(
                source,
                preflight=preflight_document(source),
                created_at=datetime(2026, 7, 26, 10, 0),
            )
        self.assertEqual(
            timestamp.exception.code,
            SourceIRAdapterRejectionCode.INVALID_TIMESTAMP,
        )

    def test_libreoffice_profile_command_and_environment_are_isolated(self) -> None:
        sandbox = self.root / "synthetic-sandbox"
        sandbox.write_bytes(b"synthetic executable; subprocess is mocked")
        sandbox.chmod(0o700)
        converter = LibreOfficeXlsbConverter(
            executable="/bin/echo",
            sandbox_executable=sandbox,
        )
        captured: dict[str, object] = {}

        def fake_run(
            command: tuple[str, ...],
            *,
            cwd: Path,
            environment: dict[str, str],
            timeout_seconds: int,
            output_path: Path,
        ) -> xlsb_module._ProcessOutcome:
            captured.update(
                {
                    "command": command,
                    "cwd": cwd,
                    "environment": environment,
                    "timeout_seconds": timeout_seconds,
                    "registry": (
                        cwd / "profile" / "user" / "registrymodifications.xcu"
                    ).read_text(encoding="utf-8"),
                }
            )
            output_path.write_bytes(b"safe converter log")
            outdir = Path(command[command.index("--outdir") + 1])
            (outdir / "source.xlsx").write_bytes(self.derived_bytes)
            return xlsb_module._ProcessOutcome(
                return_code=0,
                timed_out=False,
                output_sha256=hashlib.sha256(b"safe converter log").hexdigest(),
                output_size=len(b"safe converter log"),
            )

        with (
            patch.object(xlsb_module.platform, "system", return_value="Darwin"),
            patch.object(
                converter,
                "_converter_identity",
                return_value=(
                    "26.2.4.2",
                    "a" * 40,
                    hashlib.sha256(Path("/bin/echo").resolve().read_bytes()).hexdigest(),
                ),
            ),
            patch.object(xlsb_module, "_run_bounded_process", side_effect=fake_run),
        ):
            artifact = converter.convert(b"verified source")

        command = captured["command"]
        assert isinstance(command, tuple)
        self.assertEqual(command[0], str(sandbox.resolve()))
        self.assertIn(xlsb_module._MACOS_NETWORK_SANDBOX_PROFILE, command)
        self.assertIn("--headless", command)
        self.assertIn("--norestore", command)
        self.assertNotIn("--accept", command)
        self.assertTrue(
            any(
                item.startswith("-env:UserInstallation=file://")
                for item in command
            )
        )
        registry = captured["registry"]
        assert isinstance(registry, str)
        self.assertIn("DisableMacrosExecution", registry)
        self.assertIn("DisableActiveContent", registry)
        self.assertIn("BlockUntrustedRefererLinks", registry)
        environment = captured["environment"]
        assert isinstance(environment, dict)
        self.assertEqual(environment["NO_PROXY"], "*")
        self.assertNotIn("HTTP_PROXY", environment)
        self.assertEqual(
            artifact.derived_source_sha256,
            hashlib.sha256(self.derived_bytes).hexdigest(),
        )
        workspace = captured["cwd"]
        assert isinstance(workspace, Path)
        self.assertFalse(workspace.exists())

    def test_libreoffice_version_drift_fails_before_document_conversion(self) -> None:
        sandbox = self.root / "synthetic-sandbox"
        sandbox.write_bytes(b"synthetic executable; subprocess is mocked")
        sandbox.chmod(0o700)
        converter = LibreOfficeXlsbConverter(
            executable="/bin/echo",
            sandbox_executable=sandbox,
            expected_version="26.2.4.2",
        )
        completed = subprocess.CompletedProcess(
            args=("/bin/echo", "--version"),
            returncode=0,
            stdout=b"LibreOffice 26.2.5.0 abcdef1234567890\n",
        )

        with (
            patch.object(xlsb_module.platform, "system", return_value="Darwin"),
            patch.object(subprocess, "run", return_value=completed),
        ):
            with self.assertRaises(XlsbConversionRejected) as caught:
                converter.convert(b"verified source")

        self.assertEqual(
            caught.exception.code,
            XlsbConversionRejectionCode.VERSION_MISMATCH,
        )

    def test_bounded_process_timeout_terminates_the_conversion_group(self) -> None:
        output_path = self.root / "timeout.log"

        outcome = xlsb_module._run_bounded_process(
            (
                sys.executable,
                "-c",
                "import time; print('started', flush=True); time.sleep(30)",
            ),
            cwd=self.root,
            environment=xlsb_module._isolated_environment(self.root),
            timeout_seconds=1,
            output_path=output_path,
        )

        self.assertTrue(outcome.timed_out)
        self.assertNotEqual(outcome.return_code, 0)
        self.assertEqual(
            outcome.output_sha256,
            hashlib.sha256(output_path.read_bytes()).hexdigest(),
        )

    def test_mapper_failure_returns_failed_snapshot_without_sensitive_detail(
        self,
    ) -> None:
        source = _write_xlsb(self.root / "mapper-failure.xlsb")
        with patch.object(
            xlsb_module._DerivedXlsxMapper,
            "_extract_bytes",
            side_effect=RuntimeError("sensitive converted cell text"),
        ):
            snapshot = self._adapter(_FakeConverter(self.artifact)).extract(
                source,
                preflight=preflight_document(source),
                created_at=CREATED_AT,
            )

        self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
        self.assertEqual(snapshot.nodes, ())
        self.assertEqual(len(snapshot.assets), 1)
        self.assertEqual(snapshot.assets[0].kind, SourceAssetKind.PAGE_RENDER)
        self.assertEqual(
            snapshot.diagnostics[0].code,
            "xlsb_derived_xlsx_parse_failed",
        )
        self.assertNotIn(
            "sensitive converted cell text",
            snapshot.model_dump_json(),
        )
        self.assertIsNotNone(snapshot.source_conversion)


def _artifact(derived_bytes: bytes) -> XlsbConversionArtifact:
    return XlsbConversionArtifact(
        derived_bytes=derived_bytes,
        derived_source_sha256=hashlib.sha256(derived_bytes).hexdigest(),
        converter_id="test-converter",
        converter_version="26.2.4.2",
        converter_build_id="a" * 40,
        converter_executable_sha256="b" * 64,
        converter_config=SourceConversionSecurityConfig(
            output_filter="xlsx:Calc MS Excel 2007 XML",
            isolated_temporary_directory=True,
            isolated_user_profile=True,
            macro_policy="reject_source_and_disable_runtime",
            external_link_policy=(
                "reject_external_data_and_block_unresolved_cid_visual"
            ),
            network_policy="macos_sandbox_deny_non_loopback_ip",
            timeout_seconds=120,
        ),
        converter_output_sha256=hashlib.sha256(b"test output").hexdigest(),
    )


def _xlsx_bytes() -> bytes:
    output = BytesIO()
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Requirements"
    worksheet.append(("ID", "Requirement"))
    worksheet.append(("R1", "Synthetic requirement"))
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _write_xlsb(
    path: Path,
    *,
    external_cid_image: bool = False,
    isolated_cid_image: bool = False,
    external_target: str | None = None,
    extra_member: tuple[str, bytes] | None = None,
) -> Path:
    workbook_relationships: list[str] = []
    drawing_relationships: list[str] = []
    drawing_xml: str | None = None
    if external_cid_image or isolated_cid_image:
        drawing_relationships.append(
            (
                '<Relationship Id="rId2" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                'Target="cid:synthetic.image" TargetMode="External"/>'
            )
        )
        if external_cid_image:
            drawing_relationships.insert(
                0,
                (
                    '<Relationship Id="rId1" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                    'Target="../media/image1.png"/>'
                ),
            )
            embed_attribute = ' r:embed="rId1"'
        else:
            embed_attribute = ""
        drawing_xml = (
            '<xdr:wsDr '
            'xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<a:blip{embed_attribute} r:link="rId2"/>'
            "</xdr:wsDr>"
        )
    if external_target is not None:
        workbook_relationships.append(
            (
                '<Relationship Id="rId2" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink" '
                f'Target="{external_target}" TargetMode="External"/>'
            )
        )
    workbook_relationship_xml = (
        f'<Relationships xmlns="{_RELATIONSHIPS_NS}">'
        f"{''.join(workbook_relationships)}"
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.bin", b"synthetic workbook")
        archive.writestr(
            "xl/_rels/workbook.bin.rels",
            workbook_relationship_xml,
        )
        if drawing_xml is not None:
            drawing_relationship_xml = (
                f'<Relationships xmlns="{_RELATIONSHIPS_NS}">'
                f"{''.join(drawing_relationships)}"
                "</Relationships>"
            )
            archive.writestr("xl/drawings/drawing1.xml", drawing_xml)
            archive.writestr(
                "xl/drawings/_rels/drawing1.xml.rels",
                drawing_relationship_xml,
            )
        if external_cid_image:
            archive.writestr(
                "xl/media/image1.png",
                b"synthetic embedded cache",
            )
        if extra_member is not None:
            archive.writestr(*extra_member)
    return path


if __name__ == "__main__":
    unittest.main()
