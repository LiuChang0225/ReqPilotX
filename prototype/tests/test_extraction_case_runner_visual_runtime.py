from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pypdf import PdfWriter

from scripts import extraction_case_runner as runner


class ExtractionCaseRunnerVisualRuntimeTests(unittest.TestCase):
    def test_observation_validates_capture_and_reports_counts_without_text(
        self,
    ) -> None:
        source_hash = "a" * 64
        visual_snapshot = SimpleNamespace(
            observations=(SimpleNamespace(raw_text="customer secret"),),
        )
        snapshot = SimpleNamespace(
            source_sha256=source_hash,
            status=SimpleNamespace(value="PARTIAL"),
            adapter_id="test-pdf",
            adapter_version="1.0",
            nodes=(),
            assets=(),
            diagnostics=(),
            visual_text_snapshots=(visual_snapshot,),
        )
        runtime = SimpleNamespace()
        adapter = object()

        with (
            mock.patch.object(
                runner, "build_source_ir", return_value=snapshot
            ) as build,
            mock.patch.object(
                runner,
                "validate_pdf_visual_text_capture",
            ) as validate_capture,
        ):
            outcome = runner._run_source_ir_observation(
                Path("source.pdf"),
                expected_source_sha256=source_hash,
                adapters=(adapter,),
                pdf_visual_runtime=runtime,
            )

        build.assert_called_once_with(
            Path("source.pdf"),
            preflight_report=None,
            adapters=(adapter,),
        )
        validate_capture.assert_called_once_with(snapshot, runtime)
        self.assertEqual(outcome["source_ir_status"], "PARTIAL")
        self.assertEqual(outcome["source_ir_counts"]["visual_text_snapshots"], 1)
        self.assertEqual(outcome["source_ir_counts"]["visual_text_observations"], 1)
        self.assertNotIn("customer secret", json.dumps(outcome))

    def test_provider_runtime_diagnostic_blocks_before_formal_evaluators(self) -> None:
        source_hash = "b" * 64
        snapshot = SimpleNamespace(
            source_sha256=source_hash,
            status=SimpleNamespace(value="PARTIAL"),
            adapter_id="test-pdf",
            adapter_version="1.0",
            nodes=(),
            assets=(),
            diagnostics=(
                SimpleNamespace(
                    code="pdf_visual_text_provider_failed",
                    native_locator=SimpleNamespace(page_number=17),
                ),
            ),
            visual_text_snapshots=(),
        )

        with (
            mock.patch.object(runner, "build_source_ir", return_value=snapshot),
            mock.patch.object(
                runner,
                "validate_pdf_visual_text_capture",
            ) as validate_capture,
        ):
            outcome = runner._run_source_ir_observation(
                Path("source.pdf"),
                expected_source_sha256=source_hash,
                adapters=(object(),),
                pdf_visual_runtime=SimpleNamespace(),
            )

        validate_capture.assert_not_called()
        self.assertEqual(outcome["source_ir_status"], "BLOCKED_VISUAL_RUNTIME")
        self.assertEqual(outcome["pdf_visual_runtime_failure_count"], 1)
        self.assertEqual(outcome["pdf_visual_runtime_failure_pages"], [17])

    def test_document_observation_passes_only_source_bound_runtime_adapter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corpus_root = Path(directory)
            source = corpus_root / "source.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            with source.open("wb") as stream:
                writer.write(stream)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            document = {
                "document_ref": "external://training-corpus/source.pdf",
                "document_role": "customer_standard",
                "ingress_route": "requirement_source",
                "format": "pdf",
                "required": True,
                "resolution_status": "resolved",
                "source_sha256": source_hash,
            }
            route = SimpleNamespace(source_sha256=source_hash)
            adapter = SimpleNamespace(adapter_version="visual-adapter-v1")
            runtime = SimpleNamespace(
                pdf_adapter=adapter,
                visual_text_provider=SimpleNamespace(
                    provider_id="aliyun-bailian-qwen-ocr"
                ),
                route=SimpleNamespace(page_numbers=frozenset({2, 4})),
            )
            observed: dict[str, object] = {}

            def fake_observation(*args: object, **kwargs: object) -> dict[str, object]:
                observed.update(kwargs)
                return {
                    "source_ir_status": "PARTIAL",
                    "source_ir_counts": {"visual_text_snapshots": 2},
                    "source_ir_adapter": "test@visual-adapter-v1",
                    "source_ir_diagnostic_codes": {},
                }

            with (
                mock.patch.object(
                    runner,
                    "build_pdf_visual_text_runtime_from_silver",
                    return_value=runtime,
                ) as runtime_builder,
                mock.patch.object(
                    runner,
                    "_run_source_ir_observation",
                    side_effect=fake_observation,
                ),
            ):
                result, blockers = runner._evaluate_document(
                    "DOC-TEST",
                    document,
                    corpus_root=corpus_root,
                    legacy_mode="off",
                    source_ir_mode="native",
                    observation_cache={},
                    source_ir_cache={},
                    source_ir_snapshots={},
                    attachment_snapshots={},
                    legacy_timeout_seconds=None,
                    pdf_visual_provider="bailian",
                    pdf_visual_registry=SimpleNamespace(documents={"DOC-ROUTE": route}),
                    bailian_visual_model=None,
                    pdf_visual_table_page_numbers=frozenset({2}),
                )

        runtime_builder.assert_called_once_with(
            source=source.resolve(),
            silver_document=route,
            provider_name="bailian",
            bailian_model=None,
            visual_table_page_numbers=frozenset({2}),
            visual_text_page_numbers=None,
        )
        self.assertEqual(observed["adapters"], (adapter,))
        self.assertIs(observed["pdf_visual_runtime"], runtime)
        self.assertEqual(result["source_ir_status"], "PARTIAL")
        self.assertEqual(result["pdf_visual_route"]["reviewed_page_count"], 2)
        self.assertEqual(blockers, [])


if __name__ == "__main__":
    unittest.main()
