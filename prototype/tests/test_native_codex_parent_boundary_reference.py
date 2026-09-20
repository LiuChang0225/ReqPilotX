from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.adapters.agents.codex_app_server.native_parent_boundary_reference import (
    run_native_codex_parent_boundary_reference,
)
from app.application.extraction.native_parent_boundary_reference import (
    NativeParentBoundaryReferenceV1,
    NativeParentBoundaryRequestV1,
    NativeParentBoundarySourceSegmentV1,
    NativeParentBoundaryStatus,
    NativeParentContextDisposition,
    NativeParentContextRangeV1,
    NativeProvisionDisposition,
    NativeProvisionBlockV1,
)
from app.ports.requirement_atomization_provider import RequirementAtomizationRange


TEXT = "1 Scope\n1) Supplier shall provide a validated assembly."
BODY_START = TEXT.index("Supplier")


def _request() -> NativeParentBoundaryRequestV1:
    return NativeParentBoundaryRequestV1(
        source_sha256="1" * 64,
        scope_id="scope-1",
        source_segments=(
            NativeParentBoundarySourceSegmentV1(
                segment_index=0,
                page_number=1,
                evidence_channel="NATIVE_TEXT",
                source_ref="page-1",
                source_text=TEXT,
                source_text_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
            ),
        ),
    )


def _reference() -> NativeParentBoundaryReferenceV1:
    return NativeParentBoundaryReferenceV1(
        source_sha256="1" * 64,
        scope_id="scope-1",
        status=NativeParentBoundaryStatus.OBSERVED,
        provisions=(
            NativeProvisionBlockV1(
                provision_id="native-provision-" + "a" * 32,
                explicit_label="1",
                ancestor_labels=(),
                source_order=0,
                disposition=NativeProvisionDisposition.REQUIREMENT,
                body_ranges=(
                    RequirementAtomizationRange(
                        segment_index=0,
                        start_offset=BODY_START,
                        end_offset=len(TEXT),
                    ),
                ),
                reason_codes=("numbered_normative_provision",),
            ),
        ),
        context_ranges=(
            NativeParentContextRangeV1(
                source_range=RequirementAtomizationRange(
                    segment_index=0,
                    start_offset=0,
                    end_offset=BODY_START,
                ),
                disposition=NativeParentContextDisposition.HEADING,
                reason_codes=("heading_and_marker",),
            ),
        ),
    )


class _FakeRunner:
    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--version":
            return subprocess.CompletedProcess(
                command, 0, stdout="codex-cli test\n", stderr=""
            )
        final_path = Path(command[command.index("--output-last-message") + 1])
        final_path.write_text(_reference().model_dump_json(), encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"type":"turn.completed"}\n',
            stderr="",
        )


class NativeCodexParentBoundaryReferenceTests(unittest.TestCase):
    def test_runner_writes_private_validated_parent_boundary(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            output = Path(raw) / "output"
            result = run_native_codex_parent_boundary_reference(
                _request(),
                output,
                command_runner=_FakeRunner(),
                timeout_seconds=60,
            )

            self.assertEqual(result.run_receipt.provision_count, 1)
            self.assertEqual(result.run_receipt.context_range_count, 1)
            for filename in (
                "native_parent_boundary_reference.json",
                "native_parent_boundary_validation_receipt.json",
                "native_parent_boundary_run_receipt.json",
            ):
                self.assertEqual(
                    stat.S_IMODE((output / filename).stat().st_mode),
                    0o600,
                )


if __name__ == "__main__":
    unittest.main()
