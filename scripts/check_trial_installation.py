"""Verify installed candidate bytes without importing the application runtime."""

from __future__ import annotations

import hashlib
from importlib import metadata, util
import json
from pathlib import Path, PurePosixPath
import sys


def verify_payload(root: Path, expected: dict[str, str]) -> str:
    if root.is_symlink() or not root.is_dir() or not expected:
        raise ValueError("Expected a regular installed package")
    actual = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Installed package contains a symlink")
        if path.is_file() and "__pycache__" not in path.parts:
            key = path.relative_to(root).as_posix()
            actual[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    for key in expected:
        relative = PurePosixPath(key)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != key
        ):
            raise ValueError("Invalid candidate member")
    if actual != expected:
        raise ValueError("Installed package differs from committed candidate bytes")
    return hashlib.sha256(
        json.dumps(actual, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify(expected):
    prefix = Path(sys.prefix).resolve()
    roots = {}
    for package in ("reqpilotx", "reqpilotx_engine", "reqpilotx_server"):
        spec = util.find_spec(package)
        if spec is None or not spec.origin:
            raise ValueError("Installed package is missing")
        path = Path(spec.origin).resolve().parent
        if not path.is_relative_to(prefix):
            raise ValueError(
                "Editable or source-checkout application import is forbidden"
            )
        roots[package] = path
    for distribution in ("reqpilotx-engine", "reqpilotx-server"):
        installed = metadata.distribution(distribution)
        direct = installed.read_text("direct_url.json")
        if direct and json.loads(direct).get("dir_info", {}).get("editable"):
            raise ValueError("Editable installation is forbidden")
    from reqpilotx_engine import core_manifest

    engine = core_manifest()
    if (
        engine["source_commit"] != expected["source_commit"]
        or engine["source_tree"] != expected["source_tree"]
        or engine["core_identity"] != expected["engine_core_identity"]
    ):
        raise ValueError("Installed engine identity differs from the selected bundle")
    engine_files = {
        row["wheel_path"].removeprefix("reqpilotx/"): row["sha256"]
        for row in engine["runtime_files"]
    }
    engine_payload = verify_payload(roots["reqpilotx"], engine_files)
    server_payload = verify_payload(roots["reqpilotx_server"], expected["server_files"])
    return {
        "status": "VERIFIED",
        "source_commit": expected["source_commit"],
        "engine_core_identity": engine["core_identity"],
        "engine_payload_sha256": engine_payload,
        "server_payload_sha256": server_payload,
        "python": sys.executable,
        "server_file_count": len(expected["server_files"]),
    }


if __name__ == "__main__":
    try:
        print(json.dumps(verify(json.loads(Path(sys.argv[1]).read_text()))))
    except Exception as error:
        print(json.dumps({"status": "REJECTED", "error_type": type(error).__name__}))
        raise SystemExit(2)
