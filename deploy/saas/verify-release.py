"""Verify immutable release payload without evaluating arbitrary release code."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path, PurePosixPath


def verify(root: Path) -> dict:
    root = root.resolve()
    manifest_path = root / "release.json"
    if manifest_path.is_symlink():
        raise ValueError("Manifest must not be a symlink")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("contract") != "reqpilotx-private-pilot-release-v1":
        raise ValueError("Unexpected release contract")
    if manifest.get("target") != "linux-x86_64-python3.11-glibc2.28":
        raise ValueError("Unexpected runtime target")
    files = manifest["files"]
    payload_paths = [path for path in root.rglob("*") if path.relative_to(root).parts[0] != "venv"]
    if any(path.is_symlink() for path in payload_paths):
        raise ValueError("Symlinks are prohibited in release payload")
    actual = {str(path.relative_to(root)) for path in payload_paths if path.is_file() and path != manifest_path}
    if actual != set(files):
        raise ValueError("Unexpected or missing release files")
    for name, record in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("Unsafe release path")
        path = root.joinpath(*relative.parts)
        if any(component.is_symlink() for component in [path, *path.parents] if component != root.parent):
            raise ValueError("Symlinks are prohibited in a release payload")
        if path.stat().st_size != record["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("Artifact hash or size mismatch")
    return {"ok": True, "revision_label": manifest["revision_label"], "artifact_count": len(files), "source_snapshot_sha256": manifest["source_snapshot_sha256"]}


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1]))))
