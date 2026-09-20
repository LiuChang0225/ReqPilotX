#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.application.agent_workflow.source_requirement_baseline import (
    SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineValidationReceiptV1,
    SourceRequirementClassificationCoordinationDecisionV1,
    compile_source_requirement_classification_coordination,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply one bounded Codex App professional A/B/F coordination to an "
            "immutable SourceItem baseline without changing source evidence."
        )
    )
    parser.add_argument("--source-reference-dir", type=Path, required=True)
    parser.add_argument("--decisions-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    args = parser.parse_args()

    source = args.source_reference_dir.expanduser().resolve(strict=True)
    decisions_path = args.decisions_json.expanduser().resolve(strict=True)
    output = args.output_dir.expanduser().resolve()
    if not source.is_dir():
        raise ValueError("source requirement reference must be a directory")
    if output.exists():
        raise ValueError("coordination output directory must not already exist")
    if source == output or source in output.parents:
        raise ValueError("coordination output must be separate from its source")

    previous_artifact = SourceRequirementBaselineArtifactV1.model_validate_json(
        (source / "source-requirement-baseline.json").read_text(encoding="utf-8")
    )
    previous_validation = (
        SourceRequirementBaselineValidationReceiptV1.model_validate_json(
            (source / "validation-receipt.json").read_text(encoding="utf-8")
        )
    )
    raw_decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    if not isinstance(raw_decisions, list):
        raise TypeError("classification coordination decisions must be an array")
    decisions = tuple(
        SourceRequirementClassificationCoordinationDecisionV1.model_validate(item)
        for item in raw_decisions
    )
    artifact, validation, coordination, coordination_receipt = (
        compile_source_requirement_classification_coordination(
            previous_artifact=previous_artifact,
            previous_validation_receipt=previous_validation,
            decisions=decisions,
            model_id=args.model,
        )
    )

    shutil.copytree(source, output)
    os.chmod(output, 0o700)
    _replace_private_json(
        output / "source-requirement-baseline.json",
        artifact.model_dump(mode="json"),
    )
    _replace_private_json(
        output / "validation-receipt.json",
        validation.model_dump(mode="json"),
    )
    _replace_private_json(
        output / "classification-coordination.json",
        coordination.model_dump(mode="json"),
    )
    _replace_private_json(
        output / "classification-coordination-receipt.json",
        coordination_receipt.model_dump(mode="json"),
    )
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise TypeError("source requirement reference manifest must be an object")
    manifest.update(
        {
            "baseline_artifact_id": artifact.artifact_id,
            "baseline_artifact_sha256": artifact.artifact_sha256,
            "baseline_revision": artifact.revision,
            "predecessor_artifact_id": artifact.predecessor_artifact_id,
            "predecessor_artifact_sha256": artifact.predecessor_artifact_sha256,
            "classification_coordination_sha256": (coordination.coordination_sha256),
        }
    )
    manifest["files"] = _manifest_files(output)
    _replace_private_json(manifest_path, manifest)
    print(
        json.dumps(
            {
                "status": artifact.status,
                "baseline_artifact_id": artifact.artifact_id,
                "baseline_revision": artifact.revision,
                "predecessor_artifact_id": artifact.predecessor_artifact_id,
                "record_count": len(artifact.records),
                "coordination_decision_count": len(decisions),
                "coordination_sha256": coordination.coordination_sha256,
                "output_directory": str(output),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _manifest_files(root: Path) -> list[dict[str, object]]:
    files = []
    for path in sorted(
        item for item in root.rglob("*.json") if item.name != "manifest.json"
    ):
        payload = path.read_bytes()
        files.append(
            {
                "path": str(path.relative_to(root)),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
    return files


def _replace_private_json(path: Path, payload: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


if __name__ == "__main__":
    raise SystemExit(main())
