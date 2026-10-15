#!/usr/bin/env bash
# Paste only in the original, already authenticated server session.
python3.11 - <<'REQPILOTX_READONLY_PY'
"""Print bounded, sanitized metadata from the documented pilot paths only.

Run manually in the user's original, already authenticated server session.
No environment/configuration/log/DB contents, network requests or mutations.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


HASH = re.compile(r"[0-9a-f]{64}")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
SNAPSHOT = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]+")
PROPERTIES = ("LoadState", "ActiveState", "SubState", "ExecMainStatus", "NRestarts", "MemoryCurrent")
UNITS = ("reqpilotx-api.service", "reqpilotx-worker.service", "postgresql.service")


def _unavailable(exc: Exception) -> dict:
    # Never echo exception text: it may contain an unapproved path or payload.
    return {"status": "UNKNOWN", "reason": type(exc).__name__}


def _no_symlink_ancestors(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unexpected path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink layout is outside this observation contract")


def release_status(base: Path) -> dict:
    try:
        releases = base / "releases"
        _no_symlink_ancestors(releases)
        pointer = base / "current"
        if not pointer.is_symlink():
            raise ValueError("expected release pointer")
        link = pointer.readlink()
        if ".." in link.parts:
            raise ValueError("ambiguous release pointer")
        target = Path(os.path.normpath(str(link if link.is_absolute() else base / link)))
        if target.parent != releases or not LABEL.fullmatch(target.name) or target.is_symlink():
            raise ValueError("unexpected release path")
        _no_symlink_ancestors(target)
        target = target.resolve(strict=True)
        if target.parent != releases.resolve(strict=True):
            raise ValueError("release escaped documented layout")
        path = target / "release.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("unsafe manifest")
        raw = path.read_bytes()
        manifest = json.loads(raw)
        if (manifest.get("contract") != "reqpilotx-private-pilot-release-v1"
                or manifest.get("target") != "linux-x86_64-python3.11-glibc2.28"
                or not LABEL.fullmatch(str(manifest.get("revision_label", "")))
                or not HASH.fullmatch(str(manifest.get("source_snapshot_sha256", "")))):
            raise ValueError("unexpected manifest identity")
        result = {"status": "READ", "current_target": str(target),
                  "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                  "revision_label": manifest["revision_label"],
                  "source_snapshot_sha256": manifest["source_snapshot_sha256"]}
        for key, pattern in (("source_commit", re.compile(r"[0-9a-f]{40}")),
                             ("engine_core_identity", HASH), ("native_capability_bundle_sha256", HASH)):
            value = manifest.get(key)
            result[key] = value if isinstance(value, str) and pattern.fullmatch(value) else "UNKNOWN"
        # Presence is not full payload verification or proof of running code.
        result["running_application_identity_verified"] = False
        return result
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return _unavailable(exc)


def service_status(runner=subprocess.run) -> dict:
    result = {}
    for unit in UNITS:
        try:
            reply = runner(["systemctl", "show", unit, "--no-pager",
                            "--property=" + ",".join(PROPERTIES)],
                           capture_output=True, text=True, timeout=10, check=False)
            if reply.returncode:
                raise RuntimeError("unreadable unit")
            values = {}
            for line in reply.stdout.splitlines():
                key, separator, value = line.partition("=")
                if key in PROPERTIES and separator:
                    values[key] = value if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) else "UNKNOWN"
            result[unit] = {"status": "READ", **values}
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            result[unit] = _unavailable(exc)
    return result


def backup_status(root: Path) -> dict:
    try:
        _no_symlink_ancestors(root)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("unsafe backup root")
        candidates = sorted(p for p in root.iterdir()
                            if SNAPSHOT.fullmatch(p.name) and not p.is_symlink() and p.is_dir())
        result = {"status": "READ", "snapshot_directories": len(candidates),
                  "backup_integrity_verified": False, "restore_verified": False}
        if candidates:
            newest = candidates[-1]
            result["latest_directory"] = newest.name
            result["files"] = {}
            for name in ("database.dump", "application.tar.gz", "release.json", "SHA256SUMS", "release-path.txt"):
                path = newest / name
                result["files"][name] = ({"regular_file": True, "bytes": path.stat().st_size}
                                         if not path.is_symlink() and path.is_file()
                                         else {"regular_file": False})
        return result
    except OSError as exc:
        return _unavailable(exc)
    except ValueError as exc:
        return _unavailable(exc)


def collect(*, base=Path("/opt/reqpilotx"), state=Path("/var/lib/reqpilotx/application"),
            backups=Path("/var/backups/reqpilotx"), runner=subprocess.run) -> dict:
    result = {"contract": "reqpilotx-sanitized-host-observation-v1",
              "collected_at_utc": datetime.now(timezone.utc).isoformat(),
              "release": release_status(base), "services": service_status(runner),
              "backups": backup_status(backups), "network_requests": 0,
              "application_health": "NOT_CHECKED", "configuration_read": False,
              "configuration_changed": False}
    try:
        _no_symlink_ancestors(state)
        if state.is_symlink() or not state.is_dir():
            raise ValueError("unsafe state root")
        usage = shutil.disk_usage(state)
        result["state_filesystem"] = {"total_bytes": usage.total, "free_bytes": usage.free}
    except (OSError, ValueError) as exc:
        result["state_filesystem"] = _unavailable(exc)
    return result


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=2))

REQPILOTX_READONLY_PY
