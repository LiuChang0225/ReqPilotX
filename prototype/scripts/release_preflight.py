from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import platform as platform_module
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


class PreflightError(RuntimeError):
    """A release input failed a deterministic safety check."""


@dataclass(frozen=True)
class QdrantArtifact:
    path: Path
    platform: str
    arch: str
    version: str
    size_bytes: int
    sha256: str
    bundle_destination: str


_PLATFORM_ALIASES = {
    "darwin": "darwin",
    "mac": "darwin",
    "macos": "darwin",
    "win32": "win32",
    "windows": "win32",
}
_ARCH_ALIASES = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "x86_64": "x86_64",
}
_QDRANT_LAYOUT = {
    "darwin": ("qdrant", "bin/mac"),
    "win32": ("qdrant.exe", "bin/win"),
}
_MACH_CPU_TYPES = {
    "x86_64": 0x01000007,
    "arm64": 0x0100000C,
}
_PE_MACHINE_TYPES = {
    "x86_64": 0x8664,
    "arm64": 0xAA64,
}
_MIN_QDRANT_BINARY_BYTES = 1_000_000
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_DEFAULT_QDRANT_MANIFEST = Path("release/qdrant-artifacts.json")
_FORMAL_FRONTEND_ROUTES = (
    "index.html",
    "/",
    "/home",
    "/workspace",
    "/extraction-review",
    "/pdf-viewer",
)
_REQUIRED_FRONTEND_FILES = (
    "index.html",
    ".vite/manifest.json",
)
_ASSET_REFERENCE_RE = re.compile(r"/assets/([A-Za-z0-9_./-]+)")
_LEGACY_FRONTEND_MARKERS = (
    "app/static",
    "content-preparation.js",
    "workspace-shell.js",
    "extraction-review.js",
    "pdf-viewer.html",
)
_DEV_ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/[A-Za-z0-9._-]+(?:/[^\s\"'<>`]*)?"),
    re.compile(r"(?i)\b[A-Z]:\\{1,2}Users\\{1,2}[^\\\s\"'<>`]+(?:\\{1,2}[^\s\"'<>`]*)?"),
)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_CREDENTIAL_URL_RE = re.compile(r"https?://[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9_])
    (?P<name_quote>["'])?
    (?P<name>[A-Za-z0-9_.-]*(?:
        secret[_-]?access[_-]?key
        | secret[_-]?key
        | api[_-]?key
        | access[_-]?token
        | auth[_-]?token
        | bearer[_-]?token
        | client[_-]?secret
        | password
        | secret
    ))
    (?(name_quote)(?P=name_quote))
    (?![A-Za-z0-9_.-])
    \s*[:=]\s*
    (?:
        (?P<value_quote>["'`])
        (?P<quoted_value>[^"'`\r\n]+)
        (?P=value_quote)
        |
        (?P<bare_value>[A-Za-z0-9_./+:@=-]+)
    )
    (?=$|[\s,;#}\]])
    """
)
_TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".mjs",
    ".py",
    ".svg",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_STATIC_BINARY_SUFFIXES = {".png"}
_CANONICAL_RUNTIME_SOURCE_ROOTS = (
    Path("packages/engine/src/reqpilotx"),
    Path("apps/server/src/reqpilotx_server"),
)


def normalize_platform(value: str) -> str:
    normalized = _PLATFORM_ALIASES.get(str(value or "").strip().lower())
    if not normalized:
        raise PreflightError(f"unsupported Qdrant platform: {value!r}")
    return normalized


def normalize_arch(value: str) -> str:
    normalized = _ARCH_ALIASES.get(str(value or "").strip().lower())
    if not normalized:
        raise PreflightError(f"unsupported Qdrant architecture: {value!r}")
    return normalized


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_mach_o(path: Path, arch: str) -> None:
    with path.open("rb") as stream:
        data = stream.read(4096)
    if len(data) < 8:
        raise PreflightError(f"Qdrant binary is too small to be Mach-O: {path}")

    magic = data[:4]
    expected_cpu = _MACH_CPU_TYPES[arch]
    if magic == b"\xcf\xfa\xed\xfe":
        actual_cpu = int.from_bytes(data[4:8], "little")
        if actual_cpu != expected_cpu:
            raise PreflightError(
                f"Qdrant Mach-O architecture mismatch: expected {arch}, "
                f"CPU type 0x{actual_cpu:08x} found"
            )
        return
    if magic == b"\xfe\xed\xfa\xcf":
        actual_cpu = int.from_bytes(data[4:8], "big")
        if actual_cpu != expected_cpu:
            raise PreflightError(
                f"Qdrant Mach-O architecture mismatch: expected {arch}, "
                f"CPU type 0x{actual_cpu:08x} found"
            )
        return

    fat_formats = {
        b"\xca\xfe\xba\xbe": ("big", 20),
        b"\xbe\xba\xfe\xca": ("little", 20),
        b"\xca\xfe\xba\xbf": ("big", 32),
        b"\xbf\xba\xfe\xca": ("little", 32),
    }
    fat_format = fat_formats.get(magic)
    if fat_format:
        byteorder, entry_size = fat_format
        count = int.from_bytes(data[4:8], byteorder)
        if count < 1 or count > 32 or len(data) < 8 + (count * entry_size):
            raise PreflightError(f"invalid Mach-O universal header: {path}")
        cpu_types = {
            int.from_bytes(data[8 + (index * entry_size) : 12 + (index * entry_size)], byteorder)
            for index in range(count)
        }
        if expected_cpu not in cpu_types:
            raise PreflightError(
                f"Qdrant universal binary does not contain required {arch} slice"
            )
        return

    raise PreflightError(f"Qdrant binary is not a recognized Mach-O executable: {path}")


def _validate_pe(path: Path, arch: str) -> None:
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise PreflightError(f"Qdrant binary is not a PE executable: {path}")
        pe_offset = int.from_bytes(header[0x3C:0x40], "little")
        if pe_offset < 64 or pe_offset > 16 * 1024 * 1024:
            raise PreflightError(f"Qdrant PE header offset is invalid: {path}")
        stream.seek(pe_offset)
        pe_header = stream.read(6)
    if len(pe_header) < 6 or pe_header[:4] != b"PE\x00\x00":
        raise PreflightError(f"Qdrant binary has an invalid PE signature: {path}")
    actual_machine = int.from_bytes(pe_header[4:6], "little")
    expected_machine = _PE_MACHINE_TYPES[arch]
    if actual_machine != expected_machine:
        raise PreflightError(
            f"Qdrant PE architecture mismatch: expected {arch}, "
            f"machine 0x{actual_machine:04x} found"
        )


def _load_manifest(path: Path, *, content: bytes | None = None) -> dict[str, Any]:
    try:
        raw = path.read_bytes() if content is None else content
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"Qdrant manifest is unreadable or invalid JSON: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PreflightError("Qdrant manifest must use schema_version 1")
    if not isinstance(payload.get("artifacts"), list):
        raise PreflightError("Qdrant manifest artifacts must be a list")
    return payload


def _validate_manifest_trust_anchor(project_dir: Path, manifest_path: Path) -> bytes:
    """Return committed manifest bytes after proving the worktree copy is unchanged."""
    if not manifest_path.is_file():
        raise PreflightError(f"Qdrant manifest is missing: {manifest_path}")
    try:
        root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError(
            "Qdrant manifest cannot be trusted outside a Git worktree with a committed HEAD"
        ) from exc

    repo_root = Path(root_result.stdout.strip()).resolve()
    if not _path_within(manifest_path, repo_root):
        raise PreflightError("Qdrant manifest must be stored inside the Git worktree")
    relative_manifest = manifest_path.relative_to(repo_root).as_posix()

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative_manifest],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if tracked.returncode != 0:
        raise PreflightError("Qdrant manifest must be tracked in Git HEAD")

    committed = subprocess.run(
        ["git", "show", f"HEAD:{relative_manifest}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if committed.returncode != 0:
        raise PreflightError("Qdrant manifest must be committed in Git HEAD")

    unchanged = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", relative_manifest],
        cwd=repo_root,
        check=False,
    )
    if unchanged.returncode == 1:
        raise PreflightError("Qdrant manifest differs from the committed Git HEAD trust anchor")
    if unchanged.returncode != 0:
        raise PreflightError("unable to compare Qdrant manifest with Git HEAD")
    return committed.stdout


def _sanitized_probe_environment() -> dict[str, str]:
    allowed = (
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    )
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _probe_qdrant_version(path: Path) -> str:
    try:
        result = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=_sanitized_probe_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"Qdrant executable probe failed: {path}") from exc
    if result.returncode != 0:
        raise PreflightError(
            f"Qdrant executable probe returned exit code {result.returncode}: {path}"
        )
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    match = re.search(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)(?![0-9])", output)
    if not match:
        raise PreflightError("Qdrant --version output did not contain a semantic version")
    return match.group(1)


def validate_qdrant_artifact(
    project_dir: Path,
    manifest_path: Path,
    *,
    target_platform: str,
    target_arch: str,
    version_probe: Callable[[Path], str] | None = None,
) -> QdrantArtifact:
    """Select and validate exactly one pinned Qdrant binary for the build target."""
    project_dir = Path(project_dir).resolve()
    manifest_path = Path(manifest_path).resolve()
    target_platform = normalize_platform(target_platform)
    target_arch = normalize_arch(target_arch)
    trusted_manifest = _validate_manifest_trust_anchor(project_dir, manifest_path)
    payload = _load_manifest(manifest_path, content=trusted_manifest)

    matches: list[dict[str, Any]] = []
    for index, raw in enumerate(payload["artifacts"]):
        if not isinstance(raw, dict):
            raise PreflightError(f"Qdrant manifest artifact #{index} must be an object")
        try:
            record_platform = normalize_platform(raw.get("platform", ""))
            record_arch = normalize_arch(raw.get("arch", ""))
        except PreflightError as exc:
            raise PreflightError(f"Qdrant manifest artifact #{index}: {exc}") from exc
        if record_platform == target_platform and record_arch == target_arch:
            matches.append(raw)

    if len(matches) != 1:
        raise PreflightError(
            "Qdrant manifest must contain exactly one artifact for "
            f"{target_platform}/{target_arch}; found {len(matches)}"
        )

    record = matches[0]
    raw_path = record.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise PreflightError("Qdrant artifact path must be a non-empty relative path")
    relative_path = Path(raw_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise PreflightError("Qdrant artifact path must stay within the project directory")
    artifact_path = (project_dir / relative_path).resolve()
    if not _path_within(artifact_path, project_dir):
        raise PreflightError("Qdrant artifact resolves outside the project directory")
    if not artifact_path.is_file():
        raise PreflightError(f"Qdrant artifact is missing or not a regular file: {artifact_path}")

    expected_size = record.get("size_bytes")
    if not isinstance(expected_size, int) or isinstance(expected_size, bool):
        raise PreflightError("Qdrant artifact size_bytes must be an integer")
    if expected_size < _MIN_QDRANT_BINARY_BYTES:
        raise PreflightError(
            f"Qdrant artifact is implausibly small: {expected_size} bytes"
        )
    actual_size = artifact_path.stat().st_size
    if actual_size != expected_size:
        raise PreflightError(
            f"Qdrant size mismatch for {artifact_path}: expected {expected_size}, got {actual_size}"
        )

    expected_name, bundle_destination = _QDRANT_LAYOUT[target_platform]
    if artifact_path.name != expected_name:
        raise PreflightError(
            f"Qdrant artifact filename must be {expected_name!r} for {target_platform}"
        )
    if target_platform != "win32" and not (artifact_path.stat().st_mode & 0o111):
        raise PreflightError(f"Qdrant artifact is not executable: {artifact_path}")

    expected_digest = record.get("sha256")
    if not isinstance(expected_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_digest
    ):
        raise PreflightError("Qdrant artifact sha256 must be 64 lowercase hex characters")
    actual_digest = _sha256(artifact_path)
    if actual_digest != expected_digest:
        raise PreflightError(
            f"Qdrant SHA-256 mismatch for {artifact_path}: "
            f"expected {expected_digest}, got {actual_digest}"
        )

    if target_platform == "darwin":
        _validate_mach_o(artifact_path, target_arch)
    else:
        _validate_pe(artifact_path, target_arch)

    expected_version = record.get("version")
    if not isinstance(expected_version, str) or not _VERSION_RE.fullmatch(expected_version):
        raise PreflightError("Qdrant artifact version must be a pinned semantic version")
    if version_probe is None:
        host_platform = normalize_platform(sys.platform)
        host_arch = normalize_arch(platform_module.machine())
        if target_platform != host_platform or target_arch != host_arch:
            raise PreflightError(
                "Qdrant release validation must run natively on the target platform/architecture"
            )
        version_probe = _probe_qdrant_version
    actual_version = str(version_probe(artifact_path) or "").strip()
    if actual_version != expected_version:
        raise PreflightError(
            f"Qdrant version mismatch: expected {expected_version}, got {actual_version or 'unknown'}"
        )

    return QdrantArtifact(
        path=artifact_path,
        platform=target_platform,
        arch=target_arch,
        version=actual_version,
        size_bytes=actual_size,
        sha256=actual_digest,
        bundle_destination=bundle_destination,
    )


def validate_bundled_qdrant(
    dist_dir: Path,
    artifact: QdrantArtifact,
    *,
    version_probe: Callable[[Path], str] | None = None,
) -> Path:
    """Prove the distribution contains one byte-identical runnable Qdrant."""
    dist_dir = Path(dist_dir).resolve()
    if artifact.platform == "darwin":
        artifact_root = dist_dir / "ReqPilotX.app"
        bundled = artifact_root / "Contents" / "Frameworks" / "bin" / "mac" / "qdrant"
    else:
        artifact_root = dist_dir / "ReqPilotX"
        bundled = artifact_root / "_internal" / "bin" / "win" / "qdrant.exe"
    if not bundled.is_file():
        raise PreflightError(f"bundled Qdrant is missing at canonical runtime path: {bundled}")

    physical_targets: set[Path] = set()
    for candidate in dist_dir.rglob("*") if dist_dir.is_dir() else ():
        if candidate.name not in {"qdrant", "qdrant.exe"} or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if not _path_within(resolved, artifact_root):
            raise PreflightError(f"Qdrant executable exists outside canonical artifact: {candidate}")
        physical_targets.add(resolved)
    if bundled.resolve() not in physical_targets or len(physical_targets) != 1:
        raise PreflightError(
            "distribution must contain exactly one physical Qdrant executable at its runtime path"
        )

    actual_size = bundled.stat().st_size
    if actual_size != artifact.size_bytes:
        raise PreflightError(
            f"bundled Qdrant size mismatch: expected {artifact.size_bytes}, got {actual_size}"
        )
    actual_digest = _sha256(bundled)
    if actual_digest != artifact.sha256:
        raise PreflightError(
            "bundled Qdrant SHA-256 mismatch: "
            f"expected {artifact.sha256}, got {actual_digest}"
        )
    if artifact.platform != "win32" and not (bundled.stat().st_mode & 0o111):
        raise PreflightError(f"bundled Qdrant is not executable: {bundled}")

    probe = version_probe or _probe_qdrant_version
    actual_version = str(probe(bundled) or "").strip()
    if actual_version != artifact.version:
        raise PreflightError(
            "bundled Qdrant version mismatch: "
            f"expected {artifact.version}, got {actual_version or 'unknown'}"
        )
    return bundled.resolve()


def validate_static_asset_closure(project_dir: Path) -> int:
    """Validate the immutable Vite build and reject every legacy entry marker."""

    frontend_root = (Path(project_dir) / "frontend" / "dist").resolve()
    if not frontend_root.is_dir():
        raise PreflightError(f"frontend build directory is missing: {frontend_root}")

    for relative in _REQUIRED_FRONTEND_FILES:
        required = frontend_root / relative
        if not required.is_file():
            raise PreflightError(f"required frontend build file is missing: {required}")

    try:
        manifest = json.loads(
            (frontend_root / ".vite" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError("Vite manifest is unreadable or invalid") from exc
    entry = manifest.get("index.html") if isinstance(manifest, dict) else None
    if not isinstance(entry, dict) or entry.get("isEntry") is not True:
        raise PreflightError("Vite manifest does not declare index.html as its entry")

    checked = 0
    source_files = sorted(
        path
        for path in frontend_root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".css", ".html", ".js", ".json", ".mjs"}
    )
    for source in source_files:
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PreflightError(f"static source is not valid UTF-8: {source}") from exc
        if source.suffix.lower() != ".json":
            for match in _ASSET_REFERENCE_RE.finditer(text):
                relative = Path(match.group(1))
                if ".." in relative.parts:
                    raise PreflightError(
                        f"static asset reference escapes /assets in {source}: {match.group(0)}"
                    )
                target = (frontend_root / "assets" / relative).resolve()
                if not _path_within(target, frontend_root) or not target.is_file():
                    raise PreflightError(
                        f"missing static asset referenced by {source}: /assets/{relative.as_posix()}"
                    )
                checked += 1
        if source.suffix.lower() in {".html", ".js"}:
            for marker in _LEGACY_FRONTEND_MARKERS:
                if marker in text:
                    raise PreflightError(
                        f"legacy frontend marker {marker!r} remains in {source}"
                    )
    if checked < 2:
        raise PreflightError("Vite entrypoint does not close over JavaScript and CSS assets")
    return checked


def _entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_like_secret(value: str) -> bool:
    value = value.strip()
    lowered = value.lower()
    if len(value) < 16:
        return False
    if re.match(r"^(?:sk-|gh[pousr]_|AKIA|ASIA|eyJ)", value):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{32,}", value):
        return True
    if value.startswith(("${", "{{", "<", "env:", "os.getenv", "process.env")):
        return False
    placeholder_words = {
        "api",
        "changeme",
        "dummy",
        "example",
        "here",
        "key",
        "me",
        "only",
        "password",
        "placeholder",
        "replace",
        "secret",
        "test",
        "token",
        "your",
    }
    placeholder_markers = {"changeme", "dummy", "example", "placeholder", "replace", "your"}
    words = re.findall(r"[a-z0-9]+", lowered)
    if words and placeholder_markers.intersection(words) and set(words) <= placeholder_words:
        return False
    character_classes = sum(
        bool(pattern.search(value))
        for pattern in (
            re.compile(r"[a-z]"),
            re.compile(r"[A-Z]"),
            re.compile(r"[0-9]"),
            re.compile(r"[^A-Za-z0-9]"),
        )
    )
    return len(value) >= 20 and character_classes >= 3 and _entropy(value) >= 3.5


def _is_humanized_identifier_label(name: str, value: str) -> bool:
    """Return true when a value is only the display form of its field name."""
    normalized_name = re.sub(r"[^a-z0-9]+", "", name.casefold())
    normalized_value = re.sub(r"[^a-z0-9]+", "", value.casefold())
    return bool(normalized_value) and normalized_name == normalized_value


def _canonical_runtime_source_roots(project_dir: Path) -> tuple[Path, ...]:
    project_dir = Path(project_dir).resolve()
    repo_dir = project_dir.parent
    roots = []
    for relative in _CANONICAL_RUNTIME_SOURCE_ROOTS:
        declared = repo_dir / relative
        if declared.is_symlink():
            raise PreflightError(
                f"canonical runtime source root must not be a symlink: {declared}"
            )
        root = declared.resolve()
        if not _path_within(root, repo_dir) or not root.is_dir():
            raise PreflightError(f"canonical runtime source root is missing: {declared}")
        roots.append(root)
    return tuple(roots)


def validate_desktop_entrypoint_namespace(project_dir: Path) -> str:
    entrypoint = Path(project_dir).resolve() / "app_desktop.py"
    try:
        source = entrypoint.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(entrypoint))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise PreflightError(
            f"desktop entrypoint is not valid UTF-8 Python: {entrypoint}"
        ) from exc

    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    if "app" in imported_roots:
        raise PreflightError(
            "desktop entrypoint still imports the prototype/app compatibility namespace"
        )
    if "reqpilotx_server" not in imported_roots:
        raise PreflightError(
            "desktop entrypoint must compose the canonical reqpilotx_server package"
        )
    return "reqpilotx_server"


def _iter_packaged_text_files(project_dir: Path) -> Iterable[Path]:
    project_dir = Path(project_dir).resolve()
    for source_root in _canonical_runtime_source_roots(project_dir):
        for path in sorted(source_root.rglob("*")):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            is_text = suffix in _TEXT_SUFFIXES or path.name.lower() == ".env"
            if is_text:
                yield path
    frontend_dir = project_dir / "frontend" / "dist"
    if frontend_dir.is_dir():
        for path in sorted(frontend_dir.rglob("*")):
            if path.is_file() and (
                path.suffix.lower() in _TEXT_SUFFIXES
                or path.name.lower() == ".env"
            ):
                yield path
    desktop_entrypoint = project_dir / "app_desktop.py"
    if desktop_entrypoint.is_file():
        yield desktop_entrypoint


def validate_packaged_sources(project_dir: Path) -> int:
    scanned = 0
    for source in _iter_packaged_text_files(project_dir):
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PreflightError(f"packaged source is not valid UTF-8: {source}") from exc
        scanned += 1

        third_party_module = (
            "/frontend/dist/assets/" in f"/{source.as_posix()}"
            and source.suffix.lower() == ".mjs"
        )
        if not third_party_module:
            for pattern in _DEV_ABSOLUTE_PATH_PATTERNS:
                match = pattern.search(text)
                if match:
                    raise PreflightError(
                        f"development absolute path embedded in {source}: {match.group(0)}"
                    )
        if _PRIVATE_KEY_RE.search(text):
            raise PreflightError(f"private key material embedded in packaged source: {source}")
        if _CREDENTIAL_URL_RE.search(text):
            raise PreflightError(f"credential-bearing URL embedded in packaged source: {source}")
        for match in _SECRET_ASSIGNMENT_RE.finditer(text):
            value = match.group("quoted_value") or match.group("bare_value")
            if _is_humanized_identifier_label(match.group("name"), value):
                continue
            if _looks_like_secret(value):
                raise PreflightError(
                    f"probable credential literal embedded in packaged source: {source}"
                )
    return scanned


def validate_python_version(required: str | None) -> str:
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    if required is None:
        return current
    if not re.fullmatch(r"[0-9]+\.[0-9]+", required):
        raise PreflightError("required Python version must use MAJOR.MINOR format")
    if current != required:
        raise PreflightError(f"Python {required} required; current interpreter is {current}")
    return current


def validate_clean_git_tree(project_dir: Path) -> None:
    repo_dir = Path(project_dir).resolve().parent
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError(f"unable to inspect Git worktree at {repo_dir}") from exc
    if result.stdout.strip():
        first_change = result.stdout.splitlines()[0]
        raise PreflightError(f"Git worktree is not clean; first change: {first_change}")


def run_preflight(
    project_dir: Path,
    *,
    with_qdrant: bool = False,
    qdrant_manifest: Path | None = None,
    target_platform: str | None = None,
    target_arch: str | None = None,
    require_python: str | None = None,
    require_clean_tree: bool = False,
    qdrant_version_probe: Callable[[Path], str] | None = None,
) -> dict[str, Any]:
    project_dir = Path(project_dir).resolve()
    if not (project_dir / "app_desktop.py").is_file():
        raise PreflightError(f"desktop entrypoint is missing: {project_dir / 'app_desktop.py'}")

    python_version = validate_python_version(require_python)
    desktop_namespace = validate_desktop_entrypoint_namespace(project_dir)
    static_references = validate_static_asset_closure(project_dir)
    scanned_files = validate_packaged_sources(project_dir)
    if require_clean_tree:
        validate_clean_git_tree(project_dir)

    qdrant: QdrantArtifact | None = None
    if with_qdrant:
        if qdrant_manifest is None:
            qdrant_manifest = project_dir / _DEFAULT_QDRANT_MANIFEST
        qdrant = validate_qdrant_artifact(
            project_dir,
            qdrant_manifest,
            target_platform=target_platform or sys.platform,
            target_arch=target_arch or platform_module.machine(),
            version_probe=qdrant_version_probe,
        )

    return {
        "ok": True,
        "project_dir": str(project_dir),
        "python": python_version,
        "desktop_runtime_namespace": desktop_namespace,
        "static_references_checked": static_references,
        "packaged_text_files_scanned": scanned_files,
        "clean_tree_required": require_clean_tree,
        "qdrant_embedded": qdrant is not None,
        "qdrant_artifact": str(qdrant.path) if qdrant else None,
        "qdrant_platform": qdrant.platform if qdrant else None,
        "qdrant_arch": qdrant.arch if qdrant else None,
        "qdrant_version": qdrant.version if qdrant else None,
        "qdrant_size_bytes": qdrant.size_bytes if qdrant else None,
        "qdrant_sha256": qdrant.sha256 if qdrant else None,
        "qdrant_bundle_destination": qdrant.bundle_destination if qdrant else None,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate ReqPilotX release source inputs.")
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--with-qdrant", action="store_true")
    parser.add_argument("--qdrant-manifest", type=Path)
    parser.add_argument("--platform", dest="target_platform")
    parser.add_argument("--arch", dest="target_arch")
    parser.add_argument("--require-python", metavar="MAJOR.MINOR")
    parser.add_argument("--require-clean-tree", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = run_preflight(
            args.project_dir,
            with_qdrant=args.with_qdrant,
            qdrant_manifest=args.qdrant_manifest,
            target_platform=args.target_platform,
            target_arch=args.target_arch,
            require_python=args.require_python,
            require_clean_tree=args.require_clean_tree,
        )
    except PreflightError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"release preflight failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        qdrant_status = "embedded" if report["qdrant_embedded"] else "not embedded"
        print(
            "release preflight passed: "
            f"{report['static_references_checked']} asset references, "
            f"{report['packaged_text_files_scanned']} packaged text files, "
            f"Qdrant {qdrant_status}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
