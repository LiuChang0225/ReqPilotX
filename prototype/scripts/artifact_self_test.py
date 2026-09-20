from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib import error as urllib_error
from urllib import request as urllib_request

from app.infrastructure.local_runtime import LOOPBACK_HOST
from app.infrastructure.storage_lock import StorageLockError, acquire_storage_lease

from scripts.release_preflight import (
    PreflightError,
    QdrantArtifact,
    _CREDENTIAL_URL_RE,
    _PRIVATE_KEY_RE,
    _REQUIRED_FRONTEND_FILES,
    _SECRET_ASSIGNMENT_RE,
    _is_humanized_identifier_label,
    _looks_like_secret,
    _probe_qdrant_version,
)


ARTIFACT_CONTRACT = "reqpilotx-artifact-v1"
ARTIFACT_MANIFEST_NAME = "reqpilotx-artifact-manifest.json"
_TEXT_SUFFIXES = {".cfg", ".css", ".env", ".html", ".ini", ".js", ".json", ".mjs", ".svg", ".toml", ".txt", ".yaml", ".yml"}
_FORBIDDEN_RUNTIME_NAMES = {".env", "app.db", "app_desktop.log", "config.json"}
_SECRET_ENV_KEYS = {
    "ALIYUN_ACCESS_TOKEN",
    "AZURE_AI_SEARCH_API_KEY",
    "AZURE_DOCUMENT_INTELLIGENCE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "DASHSCOPE_API_KEY",
    "DOCUMENTINTELLIGENCE_API_KEY",
    "DATABASE_PASSWORD",
    "OPENAI_API_KEY",
    "QDRANT_API_KEY",
    "REQPILOT_ALIYUN_API_KEY",
    "REQPILOT_ALIYUN_QDRANT_API_KEY",
    "REQPILOT_DASHSCOPE_API_KEY",
    "REQPILOT_QDRANT_API_KEY",
}
_BINARY_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(
        rb"(?<![A-Za-z0-9_./+=-])"
        rb"(?:sk-|gh[pousr]_)[A-Za-z0-9_./+=-]{16,512}"
        rb"(?![A-Za-z0-9_./+=-])"
    ),
    re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(rb"https?://[^/@\s:]+:[^/@\s]+@", re.IGNORECASE),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalize_platform(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"darwin", "mac", "macos"}:
        return "darwin"
    if normalized in {"win32", "windows"}:
        return "win32"
    raise PreflightError(f"unsupported desktop artifact platform: {value!r}")


def resolve_artifact_layout(dist_dir: Path, platform_name: str) -> dict[str, Path | str]:
    dist_dir = Path(dist_dir).resolve()
    target = _normalize_platform(platform_name)
    if target == "darwin":
        artifact_root = dist_dir / "ReqPilotX.app"
        entrypoint = artifact_root / "Contents" / "MacOS" / "ReqPilotX"
        runtime_root = artifact_root / "Contents" / "Frameworks"
        stale_sibling = dist_dir / "ReqPilotX"
        kind = "macos-app"
    else:
        artifact_root = dist_dir / "ReqPilotX"
        entrypoint = artifact_root / "ReqPilotX.exe"
        runtime_root = artifact_root / "_internal"
        stale_sibling = dist_dir / "ReqPilotX.app"
        kind = "windows-onedir"

    if stale_sibling.exists():
        raise PreflightError(f"ambiguous stale desktop artifact output: {stale_sibling}")
    if artifact_root.is_symlink():
        raise PreflightError(f"canonical desktop artifact root must not be a symlink: {artifact_root}")
    if not artifact_root.is_dir():
        raise PreflightError(f"canonical desktop artifact root is missing: {artifact_root}")
    if not entrypoint.is_file() or entrypoint.is_symlink():
        raise PreflightError(f"canonical desktop entrypoint is missing: {entrypoint}")
    if target != "win32" and not (entrypoint.stat().st_mode & 0o111):
        raise PreflightError(f"desktop entrypoint is not executable: {entrypoint}")
    if not runtime_root.is_dir():
        raise PreflightError(f"desktop runtime root is missing: {runtime_root}")

    for relative in _REQUIRED_FRONTEND_FILES:
        asset = runtime_root / "frontend" / "dist" / relative
        if not asset.is_file():
            raise PreflightError(f"required packaged frontend file is missing: {asset}")
        if not _is_within(asset.resolve(), artifact_root):
            raise PreflightError(f"packaged frontend file escapes artifact root: {asset}")
    return {
        "platform": target,
        "kind": kind,
        "dist_dir": dist_dir,
        "artifact_root": artifact_root,
        "entrypoint": entrypoint,
        "runtime_root": runtime_root,
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _artifact_entries(artifact_root: Path) -> list[dict[str, Any]]:
    root = artifact_root.resolve()
    entries: list[dict[str, Any]] = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(dirnames):
            path = current_path / name
            if path.is_symlink():
                entries.append(_symlink_entry(path, root))
                dirnames.remove(name)
        for name in filenames:
            path = current_path / name
            if path.is_symlink():
                entries.append(_symlink_entry(path, root))
                continue
            mode = path.stat(follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise PreflightError(f"artifact contains a non-regular file: {path}")
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "type": "file",
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                    "executable": bool(mode & 0o111),
                }
            )
    return sorted(entries, key=lambda item: str(item["path"]))


def _symlink_entry(path: Path, artifact_root: Path) -> dict[str, Any]:
    target = os.readlink(path)
    if Path(target).is_absolute() or not _is_within(path.resolve(), artifact_root):
        raise PreflightError(f"artifact symlink escapes canonical root: {path}")
    if not path.resolve().exists():
        raise PreflightError(f"artifact contains a broken symlink: {path}")
    return {
        "path": path.relative_to(artifact_root).as_posix(),
        "type": "symlink",
        "target": target,
    }


def _git_source(project_dir: Path) -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=project_dir,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise PreflightError("Git source identity cannot be recorded for the artifact") from exc
    commit_sha = commit.stdout.strip()
    if commit.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise PreflightError("artifact source must resolve to a known 40-character Git commit")
    if status.returncode != 0:
        raise PreflightError("artifact source Git status could not be verified")
    if status.stdout.strip():
        raise PreflightError("artifact source Git worktree must be clean")
    return {"commit": commit_sha, "clean": True}


def write_artifact_manifest(
    dist_dir: Path,
    *,
    project_dir: Path,
    platform_name: str,
    arch: str,
    qdrant: QdrantArtifact | None = None,
) -> Path:
    layout = resolve_artifact_layout(dist_dir, platform_name)
    root = Path(layout["artifact_root"])
    runtime_root = Path(layout["runtime_root"])
    entrypoint = Path(layout["entrypoint"])
    qdrant_path = runtime_root / "bin" / ("mac" if layout["platform"] == "darwin" else "win") / (
        "qdrant" if layout["platform"] == "darwin" else "qdrant.exe"
    )
    qdrant_candidates = _qdrant_physical_targets(root)
    if qdrant is None:
        if qdrant_candidates:
            raise PreflightError("default artifact contains an unexpected Qdrant executable")
        qdrant_payload: dict[str, Any] = {"embedded": False}
    else:
        if not qdrant_path.is_file() or qdrant_path.resolve() not in qdrant_candidates:
            raise PreflightError(f"bundled Qdrant is missing at runtime path: {qdrant_path}")
        if len(qdrant_candidates) != 1:
            raise PreflightError("artifact contains more than one physical Qdrant executable")
        qdrant_payload = {
            "embedded": True,
            "path": qdrant_path.relative_to(root).as_posix(),
            "version": qdrant.version,
            "size_bytes": qdrant_path.stat().st_size,
            "sha256": _sha256(qdrant_path),
        }

    payload = {
        "schema_version": 1,
        "contract": ARTIFACT_CONTRACT,
        "source": _git_source(project_dir),
        "build": {
            "python": platform.python_version(),
            "pyinstaller": _package_version("pyinstaller"),
            "signature_verified": False,
        },
        "target": {"platform": layout["platform"], "arch": str(arch)},
        "layout": {
            "kind": layout["kind"],
            "root": root.relative_to(Path(dist_dir).resolve()).as_posix(),
            "entrypoint": entrypoint.relative_to(root).as_posix(),
            "runtime_root": runtime_root.relative_to(root).as_posix(),
        },
        "qdrant": qdrant_payload,
        "required_frontend_files": list(_REQUIRED_FRONTEND_FILES),
        "files": _artifact_entries(root),
    }
    manifest_path = Path(dist_dir).resolve() / ARTIFACT_MANIFEST_NAME
    _atomic_json(manifest_path, payload)
    return manifest_path


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _qdrant_physical_targets(artifact_root: Path) -> set[Path]:
    targets: set[Path] = set()
    for path in artifact_root.rglob("*"):
        if path.name not in {"qdrant", "qdrant.exe"} or not path.is_file():
            continue
        resolved = path.resolve()
        if not _is_within(resolved, artifact_root):
            raise PreflightError(f"Qdrant alias escapes artifact root: {path}")
        targets.add(resolved)
    return targets


def validate_artifact_manifest(
    dist_dir: Path,
    manifest_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Path | str]]:
    dist_dir = Path(dist_dir).resolve()
    manifest_path = Path(manifest_path or dist_dir / ARTIFACT_MANIFEST_NAME).resolve()
    if manifest_path.parent != dist_dir or not manifest_path.is_file():
        raise PreflightError("artifact sidecar manifest is missing from the dist root")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError("artifact sidecar manifest is unreadable or invalid") from exc
    if payload.get("schema_version") != 1 or payload.get("contract") != ARTIFACT_CONTRACT:
        raise PreflightError("artifact manifest contract is unsupported")
    target = payload.get("target") or {}
    layout = resolve_artifact_layout(dist_dir, str(target.get("platform") or ""))
    root = Path(layout["artifact_root"])
    expected_layout = payload.get("layout") or {}
    if expected_layout.get("root") != root.relative_to(dist_dir).as_posix():
        raise PreflightError("artifact manifest root does not match canonical layout")
    if expected_layout.get("entrypoint") != Path(layout["entrypoint"]).relative_to(root).as_posix():
        raise PreflightError("artifact manifest entrypoint does not match canonical layout")
    actual_entries = _artifact_entries(root)
    if payload.get("files") != actual_entries:
        raise PreflightError("artifact files differ from the sidecar manifest")
    _validate_qdrant_manifest(
        root,
        Path(layout["runtime_root"]),
        str(target.get("platform") or ""),
        payload.get("qdrant") or {},
    )
    _scan_artifact_for_sensitive_material(root, actual_entries)
    return payload, layout


def _validate_qdrant_manifest(
    root: Path,
    runtime_root: Path,
    platform_name: str,
    payload: dict[str, Any],
) -> None:
    targets = _qdrant_physical_targets(root)
    if not payload.get("embedded"):
        if targets:
            raise PreflightError("default artifact contains an unexpected Qdrant executable")
        return
    target = _normalize_platform(platform_name)
    expected = runtime_root / "bin" / ("mac" if target == "darwin" else "win") / (
        "qdrant" if target == "darwin" else "qdrant.exe"
    )
    raw_path = str(payload.get("path") or "")
    candidate = root / raw_path
    if candidate != expected or not candidate.is_file() or len(targets) != 1:
        raise PreflightError("artifact Qdrant layout is invalid")
    if candidate.stat().st_size != payload.get("size_bytes") or _sha256(candidate) != payload.get("sha256"):
        raise PreflightError("artifact Qdrant hash does not match its final manifest")


def _scan_artifact_for_sensitive_material(root: Path, entries: list[dict[str, Any]]) -> None:
    inherited_secrets = {
        key: value.encode("utf-8")
        for key in _SECRET_ENV_KEYS
        if len(value := str(os.getenv(key) or "")) >= 8
    }
    for entry in entries:
        if entry.get("type") != "file":
            continue
        path = root / str(entry["path"])
        lowered_name = path.name.lower()
        if lowered_name in _FORBIDDEN_RUNTIME_NAMES or lowered_name.endswith(("-wal", "-shm")):
            raise PreflightError(f"artifact contains forbidden runtime data: {entry['path']}")
        if "/frontend/dist/" in f"/{entry['path']}" and path.suffix.lower() in _TEXT_SUFFIXES:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise PreflightError(f"packaged text is not valid UTF-8: {entry['path']}") from exc
            if _PRIVATE_KEY_RE.search(text) or _CREDENTIAL_URL_RE.search(text):
                raise PreflightError(f"artifact contains sensitive text material: {entry['path']}")
            for match in _SECRET_ASSIGNMENT_RE.finditer(text):
                value = match.group("quoted_value") or match.group("bare_value")
                if not _is_humanized_identifier_label(match.group("name"), value) and _looks_like_secret(value):
                    raise PreflightError(f"artifact contains a probable credential: {entry['path']}")
        _scan_binary_file(path, inherited_secrets)


def _scan_binary_file(path: Path, inherited_secrets: dict[str, bytes]) -> None:
    overlap = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sample = overlap + chunk
            for pattern in _BINARY_SECRET_PATTERNS:
                if pattern.search(sample):
                    raise PreflightError(f"artifact contains probable secret material: {path.name}")
            for key, value in inherited_secrets.items():
                if value in sample:
                    raise PreflightError(f"artifact contains inherited secret material for {key}")
            overlap = sample[-512:]


def run_artifact_self_test(
    dist_dir: Path,
    *,
    manifest_path: Path | None = None,
    launch: bool = True,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    payload, layout = validate_artifact_manifest(dist_dir, manifest_path)
    report: dict[str, Any] = {
        "ok": True,
        "contract": ARTIFACT_CONTRACT,
        "manifest_sha256": _sha256(Path(manifest_path or Path(dist_dir) / ARTIFACT_MANIFEST_NAME)),
        "structure_verified": True,
        "runtime_verified": False,
        "signature_verified": bool((payload.get("build") or {}).get("signature_verified")),
    }
    if not launch:
        return report
    target_platform = str((payload.get("target") or {}).get("platform") or "")
    if _normalize_platform(sys.platform) != target_platform:
        raise PreflightError("artifact runtime self-test must execute on its target platform")
    qdrant = payload.get("qdrant") or {}
    if qdrant.get("embedded"):
        qdrant_path = Path(layout["artifact_root"]) / str(qdrant.get("path") or "")
        actual_version = _probe_qdrant_version(qdrant_path)
        if actual_version != str(qdrant.get("version") or ""):
            raise PreflightError("artifact Qdrant version does not match its final manifest")
    report.update(
        _run_headless_runtime(
            Path(layout["entrypoint"]),
            Path(layout["runtime_root"]),
            timeout_seconds,
        )
    )
    report["runtime_verified"] = True
    return report


def _run_headless_runtime(
    entrypoint: Path,
    runtime_root: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    qdrant_listener_before = _port_accepts_connection(LOOPBACK_HOST, 6333)
    with tempfile.TemporaryDirectory(prefix="reqpilotx-artifact-smoke-") as temporary:
        storage_root = Path(temporary) / "storage"
        handshake_path = Path(temporary) / "handshake.json"
        nonce = secrets.token_hex(32)
        environment = _smoke_environment(storage_root)
        try:
            process = subprocess.Popen(
                [
                    str(entrypoint),
                    "--artifact-smoke-server",
                    "--artifact-smoke-handshake",
                    str(handshake_path),
                    "--artifact-smoke-nonce",
                    nonce,
                ],
                cwd=entrypoint.parent,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            raise PreflightError("artifact entrypoint could not be executed") from exc
        graceful_shutdown = False
        host = ""
        port = 0
        try:
            handshake = _wait_for_handshake(
                handshake_path,
                process,
                nonce=nonce,
                timeout_seconds=timeout_seconds,
            )
            host = str(handshake["host"])
            port = int(handshake["port"])
            identity = _wait_for_identity(
                host,
                port,
                process,
                nonce=nonce,
                timeout_seconds=timeout_seconds,
            )
            if identity != handshake:
                raise PreflightError("artifact smoke identity does not match its handshake")
            wrong_nonce_status, _ = _read_json(
                host,
                port,
                "/api/artifact-smoke/identity",
                headers={"X-ReqPilotX-Smoke-Nonce": "0" * 64},
            )
            if wrong_nonce_status != 404:
                raise PreflightError("artifact smoke identity accepted an invalid nonce")
            _assert_storage_lease_held(storage_root)
            runtime = _read_json(host, port, "/api/runtime-readiness")
            readiness = _read_json(host, port, "/api/readiness")
            home_status, home_body = _read_bytes(host, port, "/")
            frontend_assets = runtime_root / "frontend" / "dist" / "assets"
            entry_asset = next(
                iter(sorted(frontend_assets.glob("index-*.js"))),
                None,
            )
            if entry_asset is None:
                raise PreflightError("artifact Vite entry asset is missing")
            asset_status, _asset_body = _read_bytes(
                host,
                port,
                f"/assets/{entry_asset.name}",
            )
            bad_host_status, _ = _read_json(
                host,
                port,
                "/api/readiness",
                headers={"Host": "evil.example"},
            )
            bad_origin_status, _ = _read_json(
                host,
                port,
                "/api/readiness",
                headers={"Origin": "https://evil.example"},
            )
            if runtime[0] != 200 or runtime[1].get("runtime_ready") is not True:
                raise PreflightError("artifact runtime readiness did not become true")
            for name in ("startup", "storage", "sqlite"):
                if (runtime[1].get("checks") or {}).get(name, {}).get("status") != "ok":
                    raise PreflightError(f"artifact runtime check failed: {name}")
            if readiness[0] != 200 or readiness[1].get("ready") is not True:
                raise PreflightError("artifact full readiness did not become true")
            serialized = json.dumps([runtime[1], readiness[1]], ensure_ascii=False)
            if str(storage_root) in serialized or runtime[1].get("storage_root") != "redacted" or runtime[1].get("db_path") != "redacted":
                raise PreflightError("artifact readiness leaked a runtime storage path")
            if home_status != 200 or b"ReqPilotX" not in home_body or asset_status != 200:
                raise PreflightError("artifact entry page or static asset is unavailable")
            if bad_host_status != 400 or bad_origin_status != 403:
                raise PreflightError("artifact local Host/Origin boundary is not enforced")
            if not qdrant_listener_before and _port_accepts_connection(LOOPBACK_HOST, 6333):
                raise PreflightError("default artifact smoke opened an unexpected Qdrant listener")
            shutdown_status, shutdown_payload = _read_json(
                host,
                port,
                "/api/artifact-smoke/shutdown",
                headers={"X-ReqPilotX-Smoke-Nonce": nonce},
                method="POST",
            )
            if shutdown_status != 200 or shutdown_payload.get("pid") != process.pid:
                raise PreflightError("artifact graceful shutdown endpoint failed")
            return_code = _wait_for_process_exit(process, timeout_seconds=5.0)
            if return_code != 0:
                raise PreflightError("artifact process did not exit cleanly after shutdown")
            graceful_shutdown = True
        finally:
            if not graceful_shutdown:
                _force_stop_process(process)
            elif process.stdout is not None:
                process.stdout.close()
        _wait_for_port_closed(host, port, timeout_seconds=5.0)
        _assert_storage_lease_released(storage_root, timeout_seconds=5.0)
        return {
            "listener_host": host,
            "runtime_readiness": True,
            "full_readiness": True,
            "static_assets": True,
            "host_origin_boundary": True,
            "qdrant_listener_unchanged": True,
            "qdrant_preexisting": qdrant_listener_before,
        }


def _smoke_environment(storage_root: Path) -> dict[str, str]:
    allowed = ("HOME", "LANG", "LC_ALL", "PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update(
        {
            "NO_PROXY": "127.0.0.1,localhost",
            "REQPILOT_TEST_MODE": "1",
            "REQPILOT_ENV": "test",
            "REQPILOT_STORAGE_DIR": str(storage_root.resolve()),
            "REQPILOT_OBJECT_STORE": "local",
            "REQPILOT_OBJECT_ROOT": str(storage_root.resolve()),
            "REQPILOT_SCRATCH_ROOT": str((storage_root / "scratch").resolve()),
            "REQPILOT_AGENT_RUNTIME": "none",
            "REQPILOT_AI_DEFAULT_PROFILE": "azure",
            "REQPILOT_AI_ASSESSMENT_MODE": "heuristic",
            "REQPILOT_RETRIEVAL_BACKEND": "local",
            "REQPILOT_AZURE_AUTH_MODE": "auto",
            "REQPILOT_ALIYUN_ASSESSMENT_MODE": "heuristic",
            "REQPILOT_ALIYUN_RETRIEVAL_BACKEND": "disabled",
        }
    )
    return environment


def _local_opener():
    return urllib_request.build_opener(urllib_request.ProxyHandler({}))


def _read_bytes(
    host: str,
    port: int,
    path: str,
    headers: dict[str, str] | None = None,
    *,
    method: str = "GET",
) -> tuple[int, bytes]:
    request = urllib_request.Request(
        f"http://{host}:{port}{path}",
        headers=headers or {},
        method=method,
    )
    try:
        with _local_opener().open(request, timeout=1.0) as response:
            return int(response.status), response.read(2 * 1024 * 1024)
    except urllib_error.HTTPError as exc:
        return int(exc.code), exc.read(2 * 1024 * 1024)
    except (urllib_error.URLError, OSError):
        return 0, b""


def _read_json(
    host: str,
    port: int,
    path: str,
    headers: dict[str, str] | None = None,
    *,
    method: str = "GET",
) -> tuple[int, dict[str, Any]]:
    status_code, body = _read_bytes(host, port, path, headers, method=method)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    return status_code, payload if isinstance(payload, dict) else {}


def _validate_handshake_payload(
    payload: object,
    *,
    process_pid: int,
    nonce: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"pid", "port", "nonce", "host"}:
        raise PreflightError("artifact smoke handshake fields are invalid")
    pid = payload.get("pid")
    port = payload.get("port")
    host = payload.get("host")
    received_nonce = payload.get("nonce")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid != process_pid:
        raise PreflightError("artifact smoke handshake PID does not match the child process")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise PreflightError("artifact smoke handshake port is invalid")
    if host != LOOPBACK_HOST:
        raise PreflightError("artifact smoke handshake is not loopback-bound")
    if (
        not isinstance(received_nonce, str)
        or not re.fullmatch(r"[0-9a-f]{64}", received_nonce)
        or not secrets.compare_digest(received_nonce, nonce)
    ):
        raise PreflightError("artifact smoke handshake nonce is invalid")
    return {"pid": pid, "port": port, "nonce": received_nonce, "host": host}


def _wait_for_handshake(
    path: Path,
    process: subprocess.Popen[str],
    *,
    nonce: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise PreflightError("artifact executable exited before writing its handshake")
        if os.path.lexists(path):
            if path.is_symlink() or not path.is_file():
                raise PreflightError("artifact smoke handshake is not a regular file")
            if path.stat().st_size == 0 or path.stat().st_size > 4096:
                raise PreflightError("artifact smoke handshake size is invalid")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PreflightError("artifact smoke handshake is invalid JSON") from exc
            return _validate_handshake_payload(
                payload,
                process_pid=int(process.pid),
                nonce=nonce,
            )
        time.sleep(0.05)
    raise PreflightError("artifact smoke handshake timed out")


def _wait_for_identity(
    host: str,
    port: int,
    process: subprocess.Popen[str],
    *,
    nonce: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise PreflightError("artifact executable exited before readiness")
        status, payload = _read_json(
            host,
            port,
            "/api/artifact-smoke/identity",
            headers={"X-ReqPilotX-Smoke-Nonce": nonce},
        )
        if status == 200 and payload:
            return _validate_handshake_payload(
                payload,
                process_pid=int(process.pid),
                nonce=nonce,
            )
        time.sleep(0.1)
    raise PreflightError("artifact smoke identity timed out")


def _wait_for_process_exit(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float,
) -> int:
    try:
        return int(process.wait(timeout=max(0.1, timeout_seconds)))
    except subprocess.TimeoutExpired as exc:
        raise PreflightError("artifact graceful shutdown timed out") from exc


def _force_stop_process(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
    finally:
        if process.stdout is not None:
            process.stdout.close()


def _port_accepts_connection(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.settimeout(0.2)
        return handle.connect_ex((host, port)) == 0


def _wait_for_port_closed(host: str, port: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        if not _port_accepts_connection(host, port):
            return
        time.sleep(0.05)
    raise PreflightError("artifact listener remained open after graceful shutdown")


def _assert_storage_lease_held(storage_root: Path) -> None:
    try:
        lease = acquire_storage_lease(storage_root, blocking=False)
    except StorageLockError:
        return
    lease.release()
    raise PreflightError("artifact process did not hold the runtime storage lease")


def _assert_storage_lease_released(
    storage_root: Path,
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            lease = acquire_storage_lease(storage_root, blocking=False)
        except StorageLockError:
            time.sleep(0.05)
            continue
        lease.release()
        return
    raise PreflightError("artifact storage lease remained held after graceful shutdown")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a final ReqPilotX desktop dist offline.")
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--structure-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        report = run_artifact_self_test(
            args.dist,
            manifest_path=args.manifest,
            launch=not args.structure_only,
            timeout_seconds=args.timeout,
        )
    except PreflightError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    if args.report:
        _atomic_json(args.report.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
