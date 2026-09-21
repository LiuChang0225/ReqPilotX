from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit

from reqpilotx_server.infrastructure.local_runtime import LOOPBACK_HOST


OWNER_ENV_KEY = "REQPILOT_LOCAL_QDRANT_OWNER"
_OWNER_SCHEMA_VERSION = 1
_LOCAL_HOSTS = frozenset({LOOPBACK_HOST, "localhost"})
_CONFIG_URL_KEYS = (
    "REQPILOT_ALIYUN_QDRANT_URL",
    "REQPILOT_QDRANT_URL",
    "QDRANT_URL",
)
_SAFE_ENV_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "WINDIR",
    }
)


class LocalQdrantConfigurationError(ValueError):
    """A configured URL cannot be used by the loopback-only launcher."""


class LocalQdrantError(RuntimeError):
    """The optional local Qdrant process could not be started safely."""


@dataclass(frozen=True, slots=True)
class LocalQdrantEndpoint:
    url: str
    http_port: int
    grpc_port: int


@dataclass(frozen=True, slots=True)
class LocalQdrantResult:
    state: str
    url: str = ""
    ready: bool = False
    owned: bool = False
    pid: int | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _OwnerRecord:
    schema_version: int
    pid: int
    owner_token: str
    process_start: str
    binary: str
    url: str


def parse_local_qdrant_endpoint(url: str) -> LocalQdrantEndpoint:
    raw = str(url or "").strip()
    if not raw:
        raise LocalQdrantConfigurationError("Qdrant URL is empty.")
    try:
        parsed = urlsplit(raw)
        port = parsed.port or 6333
    except ValueError as exc:
        raise LocalQdrantConfigurationError("Qdrant URL is invalid.") from exc
    if (
        parsed.scheme.lower() != "http"
        or str(parsed.hostname or "").lower() not in _LOCAL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise LocalQdrantConfigurationError(
            "Local Qdrant must use a plain loopback HTTP root URL."
        )
    if not 1 <= port < 65535:
        raise LocalQdrantConfigurationError(
            "Local Qdrant HTTP port must be between 1 and 65534."
        )
    return LocalQdrantEndpoint(
        url=f"http://{LOOPBACK_HOST}:{port}",
        http_port=port,
        grpc_port=port + 1,
    )


def configured_qdrant_url(
    storage_dir: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    source = os.environ if environment is None else environment
    for key in _CONFIG_URL_KEYS:
        value = str(source.get(key) or "").strip()
        if value:
            return value
    config_path = Path(storage_dir) / "config.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in _CONFIG_URL_KEYS:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def owner_record_path(storage_dir: Path) -> Path:
    return (
        Path(storage_dir).expanduser().resolve(strict=False)
        / "qdrant"
        / "local-process.json"
    )


def build_local_qdrant_environment(
    endpoint: LocalQdrantEndpoint,
    *,
    storage_path: Path,
    source: Mapping[str, str] | None = None,
    owner_token: str,
) -> dict[str, str]:
    inherited = os.environ if source is None else source
    environment = {
        key: str(inherited[key])
        for key in _SAFE_ENV_KEYS
        if key in inherited and str(inherited[key])
    }
    environment.update(
        {
            "QDRANT__SERVICE__HOST": LOOPBACK_HOST,
            "QDRANT__SERVICE__HTTP_PORT": str(endpoint.http_port),
            "QDRANT__SERVICE__GRPC_PORT": str(endpoint.grpc_port),
            "QDRANT__STORAGE__STORAGE_PATH": str(Path(storage_path)),
            "QDRANT__TELEMETRY_DISABLED": "true",
            OWNER_ENV_KEY: owner_token,
        }
    )
    return environment


def resolve_local_qdrant_binary(
    *,
    environment: Mapping[str, str] | None = None,
    prototype_dir: Path | None = None,
) -> Path | None:
    source = os.environ if environment is None else environment
    root = (
        Path(__file__).resolve().parents[2]
        if prototype_dir is None
        else Path(prototype_dir).expanduser().resolve(strict=False)
    )
    executable_name = "qdrant.exe" if sys.platform == "win32" else "qdrant"
    platform_bundle = "win" if sys.platform == "win32" else "mac"
    explicit = str(source.get("REQPILOT_QDRANT_BINARY") or "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(root / "bin" / platform_bundle / executable_name)
    discovered = shutil.which(executable_name, path=source.get("PATH"))
    if discovered:
        candidates.append(Path(discovered))
    candidates.append(Path.home() / ".local" / "bin" / executable_name)
    if sys.platform == "darwin":
        candidates.extend(
            (
                Path("/opt/homebrew/bin/qdrant"),
                Path("/usr/local/bin/qdrant"),
            )
        )
        brew = shutil.which("brew", path=source.get("PATH"))
        if brew:
            try:
                result = subprocess.run(
                    [brew, "--prefix", "qdrant"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={
                        key: str(source[key])
                        for key in _SAFE_ENV_KEYS
                        if key in source and str(source[key])
                    },
                )
            except (OSError, subprocess.TimeoutExpired):
                result = None
            if result is not None and result.returncode == 0:
                prefix = Path(result.stdout.strip())
                candidates.append(prefix / "bin" / executable_name)

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_file() and (
            sys.platform == "win32" or os.access(resolved, os.X_OK)
        ):
            return resolved
    if explicit:
        raise LocalQdrantError(
            "REQPILOT_QDRANT_BINARY does not identify an executable Qdrant binary."
        )
    return None


def probe_qdrant(
    endpoint: LocalQdrantEndpoint, *, timeout_seconds: float = 1.0
) -> bool:
    request = urllib_request.Request(
        f"{endpoint.url}/collections",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib_request.urlopen(
            request, timeout=max(0.1, float(timeout_seconds))
        ) as response:
            status = int(getattr(response, "status", 0) or 0)
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
    except (OSError, ValueError, urllib_error.URLError, json.JSONDecodeError):
        return False
    return (
        200 <= status < 300
        and isinstance(payload, dict)
        and isinstance(payload.get("result"), dict)
        and isinstance(payload["result"].get("collections"), list)
    )


def listener_is_loopback_only(port: int, *, pid: int | None = None) -> bool:
    if sys.platform == "win32":
        return _windows_listener_is_loopback_only(port, pid=pid)
    lsof = shutil.which("lsof")
    if not lsof:
        return False
    command = [lsof, "-nP"]
    if pid is not None:
        command.extend(("-a", "-p", str(pid)))
    command.extend((f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-Fn"))
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env=_safe_tool_environment(),
    )
    if result.returncode != 0:
        return False
    addresses = [
        line[1:] for line in result.stdout.splitlines() if line.startswith("n")
    ]
    if not addresses:
        return False
    for address in addresses:
        host = address.rsplit(":", 1)[0].strip("[]").lower()
        if host not in _LOCAL_HOSTS and host != "::1":
            return False
    return True


def _windows_listener_is_loopback_only(port: int, *, pid: int | None = None) -> bool:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return False
    script = (
        f"$rows = @(Get-NetTCPConnection -State Listen -LocalPort {int(port)} "
        "-ErrorAction SilentlyContinue); "
        "if ($rows.Count -eq 0) { exit 3 }; "
    )
    if pid is not None:
        script += f"if (@($rows | Where-Object {{ $_.OwningProcess -ne {int(pid)} }}).Count -gt 0) {{ exit 4 }}; "
    script += (
        "$safe = @('127.0.0.1', '::1'); "
        "if (@($rows | Where-Object { $safe -notcontains $_.LocalAddress }).Count "
        "-gt 0) { exit 5 }; exit 0"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=_safe_tool_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def ensure_local_qdrant_running(
    storage_dir: Path,
    url: str | None,
    *,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.1,
) -> LocalQdrantResult:
    storage_root = _prepare_storage_root(storage_dir)
    configured_url = str(url or "").strip()
    if not configured_url:
        return LocalQdrantResult(state="not_configured")
    endpoint, remote_url = _local_or_remote_endpoint(configured_url)
    if endpoint is None:
        return LocalQdrantResult(
            state="remote_configured",
            url=remote_url,
            detail="Remote Qdrant is managed outside the local runtime.",
        )
    with _lifecycle_lock(storage_root):
        record = _read_owner_record(storage_root)
        if probe_qdrant(endpoint):
            if not _endpoint_listeners_loopback_only(endpoint):
                raise LocalQdrantError(
                    "Qdrant is reachable but its HTTP/gRPC listeners are not proven "
                    "loopback-only."
                )
            if record is not None and _owned_process_matches(record):
                return LocalQdrantResult(
                    state="ready_owned",
                    url=endpoint.url,
                    ready=True,
                    owned=True,
                    pid=record.pid,
                )
            if record is not None:
                _remove_owner_record(storage_root)
            return LocalQdrantResult(
                state="ready_external",
                url=endpoint.url,
                ready=True,
                owned=False,
                detail="A healthy loopback Qdrant was already running.",
            )

        if record is not None:
            if _owned_process_matches(record):
                _terminate_owned_process(
                    record,
                    timeout_seconds=min(max(timeout_seconds, 0.1), 3.0),
                    poll_interval_seconds=poll_interval_seconds,
                )
            _remove_owner_record(storage_root)
        occupied_ports = [
            port
            for port in (endpoint.http_port, endpoint.grpc_port)
            if _loopback_port_in_use(port)
        ]
        if occupied_ports:
            ports = ", ".join(str(port) for port in occupied_ports)
            raise LocalQdrantError(
                f"Required Qdrant loopback port(s) {ports} are occupied."
            )

        if sys.platform == "win32":
            raise LocalQdrantError(
                "Automatic local Qdrant spawn is not supported on Windows; "
                "use start.ps1 -WithQdrant or configure a managed remote Qdrant URL."
            )

        binary = resolve_local_qdrant_binary()
        if binary is None:
            raise LocalQdrantError(
                "No local Qdrant binary was found. Install Qdrant or set "
                "REQPILOT_QDRANT_BINARY."
            )
        runtime_dir = storage_root / "qdrant"
        database_dir = runtime_dir / "data"
        runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        database_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _tighten_directory(runtime_dir)
        _tighten_directory(database_dir)
        owner_token = secrets.token_urlsafe(24)
        environment = build_local_qdrant_environment(
            endpoint,
            storage_path=database_dir,
            owner_token=owner_token,
        )
        log_path = runtime_dir / "local-qdrant.log"
        process = _spawn_qdrant(binary, runtime_dir, log_path, environment)
        try:
            process_start = _process_start_fingerprint(process.pid)
            if not process_start:
                raise LocalQdrantError(
                    "Qdrant process identity could not be proven after startup."
                )
            record = _OwnerRecord(
                schema_version=_OWNER_SCHEMA_VERSION,
                pid=process.pid,
                owner_token=owner_token,
                process_start=process_start,
                binary=str(binary),
                url=endpoint.url,
            )
            deadline = time.monotonic() + max(0.0, float(timeout_seconds))
            while True:
                if probe_qdrant(endpoint):
                    if not _endpoint_listeners_loopback_only(endpoint, pid=process.pid):
                        raise LocalQdrantError(
                            "Started Qdrant did not bind HTTP/gRPC exclusively to "
                            "loopback."
                        )
                    _write_owner_record(storage_root, record)
                    return LocalQdrantResult(
                        state="started",
                        url=endpoint.url,
                        ready=True,
                        owned=True,
                        pid=process.pid,
                    )
                if process.poll() is not None:
                    raise LocalQdrantError(
                        "Qdrant exited before becoming ready; inspect the local "
                        "Qdrant log."
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LocalQdrantError(
                        "Qdrant did not become ready before the startup timeout."
                    )
                time.sleep(min(max(0.01, float(poll_interval_seconds)), remaining))
        except BaseException:
            _remove_owner_record(storage_root)
            _best_effort_terminate_spawned_process(process)
            raise


def get_local_qdrant_status(
    storage_dir: Path,
    url: str | None = None,
) -> LocalQdrantResult:
    storage_root = Path(storage_dir).expanduser().resolve(strict=False)
    configured_url = str(url or configured_qdrant_url(storage_root)).strip()
    if not configured_url:
        return LocalQdrantResult(state="not_configured")
    endpoint, remote_url = _local_or_remote_endpoint(configured_url)
    if endpoint is None:
        return LocalQdrantResult(state="remote_configured", url=remote_url)
    record = _read_owner_record(storage_root)
    ready = probe_qdrant(endpoint)
    loopback_only = ready and _endpoint_listeners_loopback_only(endpoint)
    if record is not None and _owned_process_matches(record):
        return LocalQdrantResult(
            state="ready_owned" if ready and loopback_only else "owned_not_ready",
            url=endpoint.url,
            ready=bool(ready and loopback_only),
            owned=True,
            pid=record.pid,
        )
    if ready and loopback_only:
        return LocalQdrantResult(
            state="ready_external",
            url=endpoint.url,
            ready=True,
        )
    if _loopback_port_in_use(endpoint.http_port):
        return LocalQdrantResult(
            state="port_conflict",
            url=endpoint.url,
            detail="The configured loopback port is occupied but Qdrant is not ready.",
        )
    return LocalQdrantResult(state="stopped", url=endpoint.url)


def stop_owned_local_qdrant(
    storage_dir: Path,
    *,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.1,
) -> LocalQdrantResult:
    storage_root = Path(storage_dir).expanduser().resolve(strict=False)
    with _lifecycle_lock(storage_root):
        record = _read_owner_record(storage_root)
        if record is None:
            return LocalQdrantResult(state="not_owned")
        if not _owned_process_matches(record):
            _remove_owner_record(storage_root)
            return LocalQdrantResult(
                state="stale_owner_record",
                url=record.url,
                detail="The owner record did not match a live ReqPilotX Qdrant process.",
            )
        _terminate_owned_process(
            record,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        _remove_owner_record(storage_root)
        return LocalQdrantResult(
            state="stopped",
            url=record.url,
            owned=True,
            pid=record.pid,
        )


def _local_or_remote_endpoint(
    configured_url: str,
) -> tuple[LocalQdrantEndpoint | None, str]:
    try:
        parsed = urlsplit(configured_url)
        port = parsed.port
    except ValueError as exc:
        raise LocalQdrantConfigurationError("Qdrant URL is invalid.") from exc
    if not parsed.scheme or not parsed.hostname:
        raise LocalQdrantConfigurationError("Qdrant URL is invalid.")
    hostname = parsed.hostname.lower()
    if hostname in _LOCAL_HOSTS:
        endpoint = parse_local_qdrant_endpoint(configured_url)
        return endpoint, ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LocalQdrantConfigurationError("Remote Qdrant URL is invalid.")
    display_url = f"{parsed.scheme.lower()}://{hostname}"
    if port is not None:
        display_url = f"{display_url}:{port}"
    return None, display_url


def _endpoint_listeners_loopback_only(
    endpoint: LocalQdrantEndpoint,
    *,
    pid: int | None = None,
) -> bool:
    return listener_is_loopback_only(
        endpoint.http_port, pid=pid
    ) and listener_is_loopback_only(endpoint.grpc_port, pid=pid)


def _prepare_storage_root(storage_dir: Path) -> Path:
    root = Path(storage_dir).expanduser()
    if root.is_symlink():
        raise LocalQdrantError("Qdrant storage root must not be a symbolic link.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = root.resolve(strict=True)
    _tighten_directory(root)
    return root


def _tighten_directory(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)


@contextmanager
def _lifecycle_lock(storage_dir: Path) -> Iterator[None]:
    runtime_dir = storage_dir / "qdrant"
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _tighten_directory(runtime_dir)
    lock_path = runtime_dir / ".lifecycle.lock"
    with lock_path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _spawn_qdrant(
    binary: Path,
    runtime_dir: Path,
    log_path: Path,
    environment: Mapping[str, str],
) -> subprocess.Popen[bytes]:
    popen_kwargs: dict[str, Any] = {
        "cwd": str(runtime_dir),
        "env": dict(environment),
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = 0x08000000
    else:
        popen_kwargs["start_new_session"] = True
    with log_path.open("ab") as log_stream:
        return subprocess.Popen(
            [str(binary)],
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            **popen_kwargs,
        )


def _terminate_spawned_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _best_effort_terminate_spawned_process(
    process: subprocess.Popen[bytes],
) -> None:
    try:
        _terminate_spawned_process(process)
    except Exception:
        # Preserve the original startup failure while still making a final
        # best-effort attempt to avoid an untracked child process.
        try:
            process.kill()
        except Exception:
            pass


def _loopback_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.2)
        return client.connect_ex((LOOPBACK_HOST, int(port))) == 0


def _process_start_fingerprint(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=_safe_tool_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _process_has_owner_token(pid: int, owner_token: str) -> bool:
    proc_environ = Path("/proc") / str(pid) / "environ"
    try:
        if proc_environ.is_file():
            entries = proc_environ.read_bytes().split(b"\0")
            marker = f"{OWNER_ENV_KEY}={owner_token}".encode("utf-8")
            return marker in entries
    except OSError:
        return False
    try:
        result = subprocess.run(
            ["ps", "eww", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=_safe_tool_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    marker = f"{OWNER_ENV_KEY}={owner_token}"
    return result.returncode == 0 and marker in result.stdout.split()


def _owned_process_matches(record: _OwnerRecord) -> bool:
    if record.pid <= 0 or not record.owner_token or not record.process_start:
        return False
    try:
        os.kill(record.pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    if _process_start_fingerprint(record.pid) != record.process_start:
        return False
    if not _process_has_owner_token(record.pid, record.owner_token):
        return False
    try:
        command = subprocess.run(
            ["ps", "-ww", "-p", str(record.pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=_safe_tool_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return command.returncode == 0 and str(Path(record.binary)) in command.stdout


def _terminate_owned_process(
    record: _OwnerRecord,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> None:
    if not _owned_process_matches(record):
        return
    os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while _owned_process_matches(record) and time.monotonic() < deadline:
        time.sleep(max(0.01, float(poll_interval_seconds)))
    if _owned_process_matches(record):
        os.kill(record.pid, signal.SIGKILL)


def _read_owner_record(storage_dir: Path) -> _OwnerRecord | None:
    try:
        payload = json.loads(owner_record_path(storage_dir).read_text(encoding="utf-8"))
        record = _OwnerRecord(
            schema_version=int(payload["schema_version"]),
            pid=int(payload["pid"]),
            owner_token=str(payload["owner_token"]),
            process_start=str(payload["process_start"]),
            binary=str(payload["binary"]),
            url=str(payload["url"]),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if record.schema_version != _OWNER_SCHEMA_VERSION:
        return None
    return record


def _write_owner_record(storage_dir: Path, record: _OwnerRecord) -> None:
    path = owner_record_path(storage_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(asdict(record), ensure_ascii=True, sort_keys=True),
        encoding="utf-8",
    )
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, path)


def _remove_owner_record(storage_dir: Path) -> None:
    try:
        owner_record_path(storage_dir).unlink()
    except FileNotFoundError:
        pass


def _safe_tool_environment() -> dict[str, str]:
    return {
        key: str(os.environ[key])
        for key in _SAFE_ENV_KEYS
        if key in os.environ and str(os.environ[key])
    }
