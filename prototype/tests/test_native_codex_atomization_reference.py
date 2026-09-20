from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.adapters.agents.codex_app_server.native_atomization_reference import (
    CODEX_CONSENSUS_ATOMIZATION_PROVIDER_ID,
    CodexConsensusRequirementAtomizationProvider,
    _normalize_reference_codes,
    run_native_codex_atomization_reference,
)
from app.application.extraction.native_atomization_reference import (
    NativeAtomizationContextDisposition,
    NativeAtomizationContextRangeV1,
    NativeAtomizationReferenceStatus,
    NativeAtomizationReferenceV1,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationObligation,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationRange,
    RequirementAtomizationSourceSegment,
)
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus


TEXT = "The unit shall operate at 24 V; test method per Standard X."
METHOD_START = TEXT.index("test method")


def _request() -> RequirementAtomizationProviderRequest:
    return RequirementAtomizationProviderRequest(
        lineage_sha256="1" * 64,
        review_node_id="review-node-1",
        requirement_subtype="TECHNICAL_REQUIREMENT",
        source_segments=(
            RequirementAtomizationSourceSegment(
                segment_index=0,
                semantic_unit_id="semantic-unit-" + "a" * 32,
                owner_structure_node_id="structure-node-" + "b" * 32,
                source_start_offset=0,
                source_end_offset=len(TEXT),
                source_text=TEXT,
            ),
        ),
    )


def _reference() -> NativeAtomizationReferenceV1:
    return NativeAtomizationReferenceV1(
        lineage_sha256="1" * 64,
        review_node_id="review-node-1",
        status=NativeAtomizationReferenceStatus.OBSERVED,
        obligations=(
            RequirementAtomizationObligation(
                source_ranges=(
                    RequirementAtomizationRange(
                        segment_index=0,
                        start_offset=0,
                        end_offset=METHOD_START,
                    ),
                ),
                atomic_statement="The unit shall operate at 24 V.",
                split_basis="independent_obligation",
                reason_codes=("independent_obligation",),
            ),
        ),
        context_ranges=(
            NativeAtomizationContextRangeV1(
                source_range=RequirementAtomizationRange(
                    segment_index=0,
                    start_offset=METHOD_START,
                    end_offset=len(TEXT),
                ),
                disposition=NativeAtomizationContextDisposition.METHOD,
                reason_codes=("verification_method",),
            ),
        ),
    )


class _FakeRunner:
    def __init__(self, reference: NativeAtomizationReferenceV1) -> None:
        self.reference = reference
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
        final_path.write_text(self.reference.model_dump_json(), encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=('{"type":"thread.started"}\n{"type":"turn.completed"}\n'),
            stderr="",
        )


class NativeCodexAtomizationReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_runner_validates_exact_cover_and_writes_private_artifacts(self) -> None:
        fake = _FakeRunner(_reference())
        output = self.root / "output"

        result = run_native_codex_atomization_reference(
            _request(),
            output,
            command_runner=fake,
            timeout_seconds=60,
        )

        self.assertEqual(result.run_receipt.obligation_count, 1)
        self.assertEqual(result.run_receipt.context_range_count, 1)
        self.assertEqual(len(fake.commands), 2)
        self.assertIn("--ephemeral", fake.commands[1])
        for filename in (
            "native_atomization_reference.json",
            "native_atomization_validation_receipt.json",
            "native_atomization_run_receipt.json",
        ):
            path = output / filename
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn(
            TEXT,
            (output / "native_atomization_run_receipt.json").read_text(),
        )

    def test_runner_rejects_model_output_that_does_not_cover_source(self) -> None:
        invalid = _reference().model_copy(
            update={"context_ranges": ()},
        )

        with self.assertRaisesRegex(ValueError, "does not exact-cover"):
            run_native_codex_atomization_reference(
                _request(),
                self.root / "output",
                command_runner=_FakeRunner(invalid),
                timeout_seconds=60,
            )

    def test_non_semantic_code_order_is_normalized_without_changing_ranges(
        self,
    ) -> None:
        payload = _reference().model_dump(mode="json")
        payload["obligations"][0]["reason_codes"] = ["z_reason", "a_reason"]
        original_ranges = payload["obligations"][0]["source_ranges"]

        normalized = _normalize_reference_codes(payload)

        self.assertEqual(
            normalized["obligations"][0]["reason_codes"],
            ["a_reason", "z_reason"],
        )
        self.assertEqual(
            normalized["obligations"][0]["source_ranges"],
            original_ranges,
        )

    def test_consensus_provider_implements_existing_atomization_port(self) -> None:
        provider = CodexConsensusRequirementAtomizationProvider(
            repetition_count=2,
        )
        reference = _reference()

        with patch(
            "app.adapters.agents.codex_app_server.native_atomization_reference.run_native_codex_atomization_reference",
            side_effect=(
                SimpleNamespace(reference=reference),
                SimpleNamespace(reference=reference),
            ),
        ):
            result = provider.atomize(_request())

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(
            result.run.provider_id,
            CODEX_CONSENSUS_ATOMIZATION_PROVIDER_ID,
        )
        self.assertEqual(len(result.obligations), 1)
        self.assertEqual(len(result.context_ranges), 1)


if __name__ == "__main__":
    unittest.main()
