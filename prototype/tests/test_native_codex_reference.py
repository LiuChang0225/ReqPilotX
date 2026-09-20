from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter

from app.adapters.agents.codex_app_server.native_reference import (
    run_native_codex_reference,
)
from app.application.extraction.codex_extraction_parity import (
    CodexExtractionMetricV1,
    CodexExtractionStage,
    CodexExtractionStageObservationV1,
    CodexExtractionStageStatus,
    NativeCodexReferencePayloadV1,
)


def _payload(source_sha256: str) -> NativeCodexReferencePayloadV1:
    metrics = {
        CodexExtractionStage.PAGE_UNDERSTANDING: (
            ("page_count", 1),
            ("pages_observed", 1),
            ("pages_unresolved", 0),
        ),
        CodexExtractionStage.STRUCTURE: (
            ("native_reference_figures", 0),
            ("native_reference_logical_tables", 0),
            ("native_reference_numbered_sections", 0),
        ),
        CodexExtractionStage.REQUIREMENT_RECOGNITION: (
            ("native_reference_requirement_parents", 0),
            ("native_reference_semantic_unresolved", 0),
        ),
        CodexExtractionStage.REQUIREMENT_DECOMPOSITION: (
            ("native_reference_requirement_children", 0),
            ("native_reference_requirement_parents", 0),
            ("native_reference_unresolved_spans", 0),
        ),
    }
    capabilities = {
        CodexExtractionStage.PAGE_UNDERSTANDING: ("page_render", "pdf_native_text"),
        CodexExtractionStage.STRUCTURE: ("structure_reconstruction",),
        CodexExtractionStage.REQUIREMENT_RECOGNITION: ("semantic_reasoning",),
        CodexExtractionStage.REQUIREMENT_DECOMPOSITION: ("parent_child_decomposition",),
    }
    return NativeCodexReferencePayloadV1(
        source_sha256=source_sha256,
        page_count=1,
        model_id="gpt-5.6-sol",
        skill_ids=("pdf",),
        stages=tuple(
            CodexExtractionStageObservationV1(
                stage=stage,
                status=(
                    CodexExtractionStageStatus.NOT_RUN
                    if stage
                    in {
                        CodexExtractionStage.REQUIREMENT_RECOGNITION,
                        CodexExtractionStage.REQUIREMENT_DECOMPOSITION,
                    }
                    else CodexExtractionStageStatus.OBSERVED
                ),
                metrics=tuple(
                    CodexExtractionMetricV1(
                        name=name,
                        value=(
                            0
                            if stage
                            in {
                                CodexExtractionStage.REQUIREMENT_RECOGNITION,
                                CodexExtractionStage.REQUIREMENT_DECOMPOSITION,
                            }
                            else value
                        ),
                    )
                    for name, value in metrics[stage]
                ),
                capabilities_used=(
                    ()
                    if stage
                    in {
                        CodexExtractionStage.REQUIREMENT_RECOGNITION,
                        CodexExtractionStage.REQUIREMENT_DECOMPOSITION,
                    }
                    else capabilities[stage]
                ),
                diagnostic_codes=(
                    ("source_bound_stage_deferred",)
                    if stage
                    in {
                        CodexExtractionStage.REQUIREMENT_RECOGNITION,
                        CodexExtractionStage.REQUIREMENT_DECOMPOSITION,
                    }
                    else ()
                ),
            )
            for stage in CodexExtractionStage
        ),
    )


class _FakeCodexRunner:
    def __init__(self, source_sha256: str) -> None:
        self.source_sha256 = source_sha256
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="codex-cli test-version\n",
                stderr="",
            )
        final_path = Path(command[command.index("--output-last-message") + 1])
        final_path.write_text(
            _payload(self.source_sha256).model_dump_json(),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"type":"thread.started"}\n'
                '{"type":"item.completed","item":{"name":"shell"}}\n'
                '{"type":"item.completed","item":{"type":"command_execution"}}\n'
                '{"type":"turn.completed"}\n'
            ),
            stderr="",
        )


class NativeCodexReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.source_directory = self.root / "source"
        self.source_directory.mkdir()
        self.pdf_path = self.source_directory / "document.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with self.pdf_path.open("wb") as stream:
            writer.write(stream)

    def test_runner_writes_private_content_free_reference_artifacts(self) -> None:
        import hashlib

        source_sha256 = hashlib.sha256(self.pdf_path.read_bytes()).hexdigest()
        fake = _FakeCodexRunner(source_sha256)
        output = self.root / "output"

        result = run_native_codex_reference(
            self.pdf_path,
            output,
            command_runner=fake,
            timeout_seconds=60,
        )

        self.assertEqual(result.receipt.source_sha256, source_sha256)
        self.assertEqual(result.receipt.page_count, 1)
        self.assertEqual(
            result.receipt.tool_names,
            ("command_execution", "shell"),
        )
        self.assertIn("item.completed:2", result.receipt.event_counts)
        self.assertEqual(len(fake.commands), 2)
        exec_command = fake.commands[1]
        self.assertIn("--ephemeral", exec_command)
        self.assertIn("workspace-write", exec_command)
        self.assertNotIn(str(self.pdf_path), exec_command)
        for filename in (
            "native_reference_payload.json",
            "native_reference_observation.json",
            "native_reference_receipt.json",
        ):
            path = output / filename
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            payload = path.read_text(encoding="utf-8")
            self.assertNotIn(str(self.pdf_path), payload)
        receipt = json.loads((output / "native_reference_receipt.json").read_text())
        self.assertFalse(receipt["is_human_gold"])
        self.assertFalse(receipt["is_serving_activation_gate"])

    def test_runner_rejects_output_beside_the_customer_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "output cannot be inside"):
            run_native_codex_reference(
                self.pdf_path,
                self.source_directory / "output",
                command_runner=_FakeCodexRunner("0" * 64),
                timeout_seconds=60,
            )


if __name__ == "__main__":
    unittest.main()
