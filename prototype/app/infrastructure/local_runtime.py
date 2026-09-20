from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


LOOPBACK_HOST = "127.0.0.1"
QDRANT_HTTP_PORT = 6333
QDRANT_SERVICE_HOST_ENV = "QDRANT__SERVICE__HOST"
_PROVIDER_SECRET_ENV_KEYS = frozenset(
    {
        "AZURE_AI_SEARCH_API_KEY",
        "AZURE_DOCUMENT_INTELLIGENCE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "DOCUMENTINTELLIGENCE_API_KEY",
        "DASHSCOPE_API_KEY",
        "QDRANT_API_KEY",
        "REQPILOT_ALIYUN_API_KEY",
        "REQPILOT_ALIYUN_QDRANT_API_KEY",
        "REQPILOT_DASHSCOPE_API_KEY",
        "REQPILOT_QDRANT_API_KEY",
    }
)


def prepare_artifact_smoke_storage(
    path: Path,
    *,
    frozen: bool,
) -> Path:
    if not frozen:
        raise ValueError("Artifact smoke mode is available only in a frozen executable.")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Artifact smoke storage must be an absolute path.")
    if candidate.is_symlink():
        raise ValueError("Artifact smoke storage must not be a symbolic link.")
    if candidate.exists():
        if not candidate.is_dir():
            raise ValueError("Artifact smoke storage must be a directory.")
        if any(candidate.iterdir()):
            raise ValueError("Artifact smoke storage must not contain existing data.")
    else:
        candidate.mkdir(parents=True, mode=0o700)
    return candidate.resolve()


def shutdown_runtime_in_order(
    *,
    stop_qdrant: Callable[[], None],
    stop_backend: Callable[[], None],
    release_storage: Callable[[], None],
) -> None:
    """Release storage only after owned services have stopped successfully."""
    stop_qdrant()
    stop_backend()
    release_storage()


def require_loopback_host(host: str | None = None) -> str:
    """Return the canonical local bind host or reject a remote bind."""

    normalized = str(host or LOOPBACK_HOST).strip().lower()
    if normalized in {LOOPBACK_HOST, "localhost"}:
        return LOOPBACK_HOST
    raise ValueError("ReqPilotX local services must bind to 127.0.0.1.")


def loopback_url(port: int, path: str = "") -> str:
    normalized_port = int(port)
    if not 1 <= normalized_port <= 65535:
        raise ValueError(f"Invalid local service port: {normalized_port}")
    normalized_path = str(path or "").strip()
    if normalized_path and not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    return f"http://{LOOPBACK_HOST}:{normalized_port}{normalized_path}"


def uvicorn_loopback_kwargs(port: int, *, log_level: str = "warning") -> dict[str, Any]:
    return {
        "host": LOOPBACK_HOST,
        "port": int(port),
        "log_level": str(log_level or "warning"),
    }


def qdrant_loopback_environment(
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if base_environment is None else base_environment)
    for key in _PROVIDER_SECRET_ENV_KEYS:
        environment.pop(key, None)
    environment[QDRANT_SERVICE_HOST_ENV] = LOOPBACK_HOST
    return environment


def probe_local_readiness(
    port: int,
    *,
    timeout_seconds: float = 1.0,
    opener: Callable[..., Any] = urllib_request.urlopen,
) -> bool:
    """Require complete runtime and default-provider readiness."""

    result = _read_local_readiness(
        port,
        path="/api/readiness",
        timeout_seconds=timeout_seconds,
        opener=opener,
    )
    if result is None:
        return False
    status_code, payload = result
    return (
        status_code == 200
        and payload.get("ready") is True
        and payload.get("status") == "ok"
    )


def probe_local_runtime_readiness(
    port: int,
    *,
    timeout_seconds: float = 1.0,
    opener: Callable[..., Any] = urllib_request.urlopen,
) -> bool:
    """Allow the recovery UI once startup, storage and SQLite are ready."""

    result = _read_local_readiness(
        port,
        path="/api/runtime-readiness",
        timeout_seconds=timeout_seconds,
        opener=opener,
    )
    if result is None:
        return False
    status_code, payload = result
    return status_code == 200 and payload.get("runtime_ready") is True


def _read_local_readiness(
    port: int,
    *,
    path: str,
    timeout_seconds: float,
    opener: Callable[..., Any],
) -> tuple[int, dict[str, Any]] | None:
    """Read a bounded readiness response, including intentional HTTP 503s."""

    request = urllib_request.Request(
        loopback_url(port, path),
        headers={"Accept": "application/json"},
    )
    try:
        with opener(request, timeout=max(0.1, float(timeout_seconds))) as response:
            status_code = int(getattr(response, "status", 0) or 0)
            payload = json.loads(response.read(64 * 1024).decode("utf-8"))
    except urllib_error.HTTPError as exc:
        try:
            status_code = int(exc.code)
            payload = json.loads(exc.read(64 * 1024).decode("utf-8"))
        except Exception:
            return None
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return status_code, payload


def wait_for_local_readiness(
    port: int,
    *,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.1,
    probe: Callable[[int], bool] | None = None,
) -> bool:
    readiness_probe = probe or probe_local_readiness
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        if readiness_probe(int(port)):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(max(0.01, float(poll_interval_seconds)), remaining))
