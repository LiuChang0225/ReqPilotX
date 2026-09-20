from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from app.adapters.documents import spreadsheet_visual_profile as visual_module
from app.adapters.documents.spreadsheet_visual_profile import (
    LibreOfficeSpreadsheetVisualRenderer,
    SpreadsheetExpectedSheet,
    SpreadsheetExternalVisualRelationship,
    SpreadsheetVisualProfileRejected,
)
from app.domain.extraction.preflight import DocumentFormat
from app.domain.extraction.source_ir import SourceAssetKind


def _sheet(
    index: int,
    name: str,
    *,
    state: str = "visible",
    expects_pages: bool = True,
    render_name: str | None = None,
) -> SpreadsheetExpectedSheet:
    return SpreadsheetExpectedSheet(
        sheet_index=index,
        sheet_name_sha256=hashlib.sha256(name.encode("utf-8")).hexdigest(),
        sheet_state=state,
        expects_rendered_pages=expects_pages,
        render_outline_name_sha256=(
            hashlib.sha256(render_name.encode("utf-8")).hexdigest()
            if render_name is not None
            else None
        ),
    )


def _pdf_bytes(
    *,
    page_count: int,
    outline: tuple[tuple[str, int], ...],
) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    for title, zero_based_page in outline:
        writer.add_outline_item(title, zero_based_page)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class SpreadsheetVisualProfileTests(unittest.TestCase):
    def test_sheet_outline_binds_exact_page_range_and_all_sheet_states(
        self,
    ) -> None:
        expected = (
            _sheet(0, "Requirements"),
            _sheet(1, "Blank", expects_pages=False),
            _sheet(2, "Hidden", state="hidden", expects_pages=False),
        )
        pdf_bytes = _pdf_bytes(
            page_count=2,
            outline=(("Requirements", 0),),
        )

        sheets, page_sheet_indexes = visual_module._sheet_page_bindings(
            pdf_bytes,
            expected_sheets=expected,
        )

        self.assertEqual(page_sheet_indexes, (0, 0))
        self.assertEqual(
            [
                (item.sheet_index, item.page_start, item.page_end)
                for item in sheets
            ],
            [(0, 1, 2), (1, None, None), (2, None, None)],
        )

    def test_missing_or_misordered_sheet_outline_fails_closed(self) -> None:
        expected = (_sheet(0, "First"), _sheet(1, "Second"))
        cases = (
            _pdf_bytes(page_count=1, outline=(("First", 0),)),
            _pdf_bytes(
                page_count=2,
                outline=(("Second", 0), ("First", 1)),
            ),
        )
        for pdf_bytes in cases:
            with self.subTest(pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest()):
                with self.assertRaises(SpreadsheetVisualProfileRejected):
                    visual_module._sheet_page_bindings(
                        pdf_bytes,
                        expected_sheets=expected,
                    )

    def test_trimmed_render_outline_alias_preserves_original_sheet_hash(
        self,
    ) -> None:
        expected = (
            _sheet(
                0,
                "Requirements ",
                render_name="Requirements",
            ),
        )
        sheets, page_sheet_indexes = visual_module._sheet_page_bindings(
            _pdf_bytes(
                page_count=1,
                outline=(("Requirements", 0),),
            ),
            expected_sheets=expected,
        )
        self.assertEqual(page_sheet_indexes, (0,))
        self.assertEqual(
            sheets[0].sheet_name_sha256,
            hashlib.sha256(b"Requirements ").hexdigest(),
        )

    def test_direct_renderer_isolated_profile_and_assets_bind_original_bytes(
        self,
    ) -> None:
        source_bytes = b"verified original XLSB bytes"
        pdf_bytes = _pdf_bytes(
            page_count=1,
            outline=(("Requirements", 0),),
        )
        executable = Path("/bin/echo").resolve()
        executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
        renderer = LibreOfficeSpreadsheetVisualRenderer(
            executable=executable,
            sandbox_executable=executable,
        )
        captured: dict[str, object] = {}
        external_relationship = SpreadsheetExternalVisualRelationship(
            relationship_scope_sha256="a" * 64,
            target_sha256="b" * 64,
            embedded_cache_sha256="c" * 64,
        )

        def fake_run(
            command: tuple[str, ...],
            *,
            cwd: Path,
            environment: dict[str, str],
            timeout_seconds: int,
            output_path: Path,
        ) -> visual_module._ProcessOutcome:
            captured.update(
                {
                    "command": command,
                    "cwd": cwd,
                    "environment": environment,
                    "timeout_seconds": timeout_seconds,
                    "registry": (
                        cwd / "profile" / "user" / "registrymodifications.xcu"
                    ).read_text(encoding="utf-8"),
                    "input_bytes": (
                        cwd / "input" / "source.xlsb"
                    ).read_bytes(),
                }
            )
            output_path.write_bytes(b"safe render log")
            (cwd / "output" / "source.pdf").write_bytes(pdf_bytes)
            return visual_module._ProcessOutcome(
                return_code=0,
                timed_out=False,
                output_sha256=hashlib.sha256(
                    b"safe render log"
                ).hexdigest(),
                output_size=len(b"safe render log"),
            )

        with (
            patch.object(visual_module.platform, "system", return_value="Darwin"),
            patch.object(
                visual_module,
                "_libreoffice_identity",
                return_value=("26.2.4.2", "d" * 40, executable_sha256),
            ),
            patch.object(
                visual_module,
                "_run_bounded_process",
                side_effect=fake_run,
            ),
        ):
            profile = renderer.render(
                source_bytes,
                source_format=DocumentFormat.XLSB,
                expected_sheets=(_sheet(0, "Requirements"),),
                external_visual_relationships=(external_relationship,),
            )

        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        self.assertEqual(profile.source_sha256, source_sha256)
        self.assertEqual(profile.source_size, len(source_bytes))
        self.assertEqual(profile.source_format, DocumentFormat.XLSB)
        self.assertEqual(len(profile.pages), 1)
        self.assertEqual(
            profile.external_visual_relationships,
            (external_relationship,),
        )
        self.assertEqual(
            profile.hash_only_fact()["temporary_artifacts_retained"],
            False,
        )
        self.assertRegex(profile.profile_sha256, r"^[0-9a-f]{64}$")
        assets = profile.page_asset_refs()
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].kind, SourceAssetKind.PAGE_RENDER)
        self.assertIn(source_sha256, assets[0].storage_key)
        self.assertEqual(captured["input_bytes"], source_bytes)
        command = captured["command"]
        assert isinstance(command, tuple)
        self.assertEqual(command[0], str(executable))
        self.assertIn(visual_module._MACOS_NETWORK_SANDBOX_PROFILE, command)
        self.assertIn("--headless", command)
        self.assertIn("--norestore", command)
        self.assertNotIn("--accept", command)
        environment = captured["environment"]
        assert isinstance(environment, dict)
        self.assertEqual(environment["NO_PROXY"], "*")
        self.assertNotIn("HTTP_PROXY", environment)
        registry = captured["registry"]
        assert isinstance(registry, str)
        self.assertIn("DisableMacrosExecution", registry)
        self.assertIn("DisableActiveContent", registry)
        workspace = captured["cwd"]
        assert isinstance(workspace, Path)
        self.assertFalse(workspace.exists())


if __name__ == "__main__":
    unittest.main()
