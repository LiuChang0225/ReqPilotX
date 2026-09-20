#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROTOTYPE_ROOT.parent
if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from app.adapters.agents.codex_app_server.primary_runtime_pdf import (  # noqa: E402
    discover_openai_primary_runtime_pdf_dependencies,
)
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (  # noqa: E402
    SyntheticPdfCapabilityMode,
    build_pdf_capability_matrix,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit  # noqa: E402


CANARY_SCHEMA_VERSION = "reqpilotx-openai-primary-runtime-pdf-dependency-canary/v1"


def run_dependency_canary(
    *,
    repository_root: Path,
    primary_runtime_root: Path | None = None,
) -> dict[str, object]:
    repository = repository_root.resolve(strict=True)
    kit = load_portable_agent_kit(repository / "agent-kit")
    provenance = kit.skill_provenance.get("pdf")
    if provenance is None:
        raise RuntimeError("PDF provenance is missing")
    runtime = discover_openai_primary_runtime_pdf_dependencies(
        provenance=provenance,
        runtime_root=primary_runtime_root,
    )
    with tempfile.TemporaryDirectory(
        prefix="reqpilotx-pdf-dependency-canary-"
    ) as raw:
        fixture_root = Path(raw) / "fixtures"
        fixture_root.mkdir()
        fixtures = build_pdf_capability_matrix(fixture_root.resolve())
        extraction = _extract_native_text(
            runtime.python_executable,
            tuple(item.path for item in fixtures),
        )
        observations: list[dict[str, object]] = []
        for fixture in fixtures:
            native_text = extraction[str(fixture.path)]
            render_prefix = Path(raw) / f"render-{fixture.mode.value.lower()}"
            completed = subprocess.run(
                (
                    str(runtime.pdftoppm_executable),
                    "-f",
                    "1",
                    "-singlefile",
                    "-png",
                    str(fixture.path),
                    str(render_prefix),
                ),
                check=False,
                capture_output=True,
                timeout=30,
                env=runtime.environment({"PATH": "/usr/bin:/bin"}),
            )
            rendered = render_prefix.with_suffix(".png")
            if (
                completed.returncode != 0
                or not rendered.is_file()
                or not rendered.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            ):
                raise RuntimeError(f"render failed for {fixture.mode.value}")
            native_marker_found = (
                fixture.native_marker is not None
                and fixture.native_marker in native_text
            )
            visual_marker_absent_from_native = fixture.visual_marker not in native_text
            if fixture.mode is SyntheticPdfCapabilityMode.SCANNED:
                mode_ok = not native_text.strip() and visual_marker_absent_from_native
            elif fixture.mode is SyntheticPdfCapabilityMode.HYBRID:
                mode_ok = native_marker_found and visual_marker_absent_from_native
            else:
                mode_ok = native_marker_found
            if not mode_ok:
                raise RuntimeError(f"evidence-channel mismatch for {fixture.mode.value}")
            observations.append(
                {
                    "mode": fixture.mode.value,
                    "source_sha256": fixture.source_sha256,
                    "native_text_present": bool(native_text.strip()),
                    "native_marker_found": native_marker_found,
                    "visual_marker_absent_from_native": (
                        visual_marker_absent_from_native
                    ),
                    "rendered_png_sha256": _sha256(rendered),
                    "rendered_png_bytes": rendered.stat().st_size,
                    "status": "PASS",
                }
            )
    attestation = runtime.attestation
    return {
        "schema_version": CANARY_SCHEMA_VERSION,
        "status": "PASS",
        "kit_version": kit.identity.kit_version,
        "kit_bundle_sha256": kit.identity.bundle_sha256,
        "pdf_distribution": {
            "id": attestation.distribution_id,
            "version": attestation.distribution_version,
            "vendored_skill_sha256": attestation.vendored_skill_sha256,
            "runtime_skill_sha256": attestation.runtime_skill_sha256,
        },
        "primary_runtime": {
            "attestation_id": attestation.attestation_id,
            "bundle_version": attestation.runtime_bundle_version,
            "python_version": attestation.python_version,
            "python_packages": {
                item.name: item.version for item in attestation.python_packages
            },
            "render_tools": {
                item.name: item.version for item in attestation.render_tools
            },
        },
        "observations": observations,
    }


def _extract_native_text(
    python_executable: Path,
    paths: tuple[Path, ...],
) -> dict[str, str]:
    program = (
        "import json,sys;from pypdf import PdfReader;"
        "print(json.dumps({p:'\\n'.join((page.extract_text() or '') "
        "for page in PdfReader(p).pages) for p in sys.argv[1:]},sort_keys=True))"
    )
    completed = subprocess.run(
        (str(python_executable), "-I", "-B", "-c", program, *map(str, paths)),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        },
    )
    if completed.returncode != 0:
        raise RuntimeError("bundled Python PDF extraction failed")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict) or set(payload) != {str(path) for path in paths}:
        raise RuntimeError("bundled Python PDF extraction result is invalid")
    return {str(key): str(value) for key, value in payload.items()}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--primary-runtime-root", type=Path)
    args = parser.parse_args()
    try:
        result = run_dependency_canary(
            repository_root=args.repository_root,
            primary_runtime_root=args.primary_runtime_root,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": CANARY_SCHEMA_VERSION,
                    "status": "FAIL",
                    "reason_code": type(exc).__name__.upper(),
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
