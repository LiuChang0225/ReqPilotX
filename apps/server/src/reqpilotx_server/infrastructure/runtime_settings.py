from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit


class DeploymentMode(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"
    TEST = "test"


class ProcessRole(StrEnum):
    API = "api"
    WORKER = "worker"


class DatabaseBackend(StrEnum):
    POSTGRESQL = "postgresql"
    SQLITE_TEST = "sqlite-test"


class ObjectStoreBackend(StrEnum):
    LOCAL = "local"
    OSS = "oss"


class AgentRuntimeBackend(StrEnum):
    NONE = "none"
    CODEX_APP_SERVER = "codex-app-server"


class NativeCapabilityMode(StrEnum):
    REGISTERED_RELEASE = "registered-release"
    DEVELOPMENT_SOURCE = "development-source"


class RuntimeConfigurationError(ValueError):
    """Raised when a runtime configuration could weaken an explicit boundary."""


_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_LOCAL_ORIGINS = (
    "http://127.0.0.1",
    "http://localhost",
    "http://[::1]",
)


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    deployment_mode: DeploymentMode
    process_role: ProcessRole
    database_backend: DatabaseBackend
    database_url: str
    object_store_backend: ObjectStoreBackend
    object_root: Path
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    local_worker_count: int = 2
    scratch_root: Path = Path(tempfile.gettempdir()) / "reqpilotx"
    oss_region: str = ""
    oss_bucket: str = ""
    oss_endpoint: str = ""
    oss_prefix: str = "reqpilotx"
    agent_runtime_backend: AgentRuntimeBackend = AgentRuntimeBackend.NONE
    codex_bin: Path | None = None
    codex_home: Path | None = None
    agent_model_id: str = "gpt-5.6-sol"
    agent_customer_data_enabled: bool = False
    native_capability_mode: NativeCapabilityMode = (
        NativeCapabilityMode.REGISTERED_RELEASE
    )
    native_capability_registry_root: Path = Path(tempfile.gettempdir()) / (
        "reqpilotx/native-loop-capabilities"
    )
    native_capability_bundle_sha256: str = ""

    @classmethod
    def from_environment(
        cls,
        *,
        environment: Mapping[str, str] | None = None,
        prototype_dir: Path | None = None,
    ) -> RuntimeSettings:
        source = os.environ if environment is None else environment
        root = (
            Path(__file__).resolve().parents[2]
            if prototype_dir is None
            else Path(prototype_dir)
        )
        test_mode = str(source.get("REQPILOT_TEST_MODE") or "").strip() == "1"
        default_mode = DeploymentMode.TEST.value if test_mode else DeploymentMode.LOCAL.value
        deployment_mode = _enum_value(
            DeploymentMode,
            source.get("REQPILOT_ENV", default_mode),
            "REQPILOT_ENV",
        )
        process_role = _enum_value(
            ProcessRole,
            source.get("REQPILOT_PROCESS_ROLE", ProcessRole.API.value),
            "REQPILOT_PROCESS_ROLE",
        )

        if deployment_mode is DeploymentMode.TEST:
            database_backend = DatabaseBackend.SQLITE_TEST
            database_url = ""
        else:
            database_backend = DatabaseBackend.POSTGRESQL
            database_url = str(
                source.get("REQPILOT_DATABASE_URL")
                or "postgresql://localhost/reqpilotx"
            ).strip()
            _validate_postgresql_url(database_url)

        default_store = (
            ObjectStoreBackend.OSS.value
            if deployment_mode is DeploymentMode.CLOUD
            else ObjectStoreBackend.LOCAL.value
        )
        object_store_backend = _enum_value(
            ObjectStoreBackend,
            source.get("REQPILOT_OBJECT_STORE", default_store),
            "REQPILOT_OBJECT_STORE",
        )
        raw_object_root = str(
            source.get("REQPILOT_OBJECT_ROOT")
            or (
                source.get("REQPILOT_STORAGE_DIR")
                if test_mode
                else None
            )
            or root / "runtime-data"
        ).strip()
        object_root = Path(raw_object_root).expanduser()
        if not object_root.is_absolute():
            object_root = root / object_root
        object_root = object_root.resolve(strict=False) / "objects"
        raw_scratch_root = str(
            source.get("REQPILOT_SCRATCH_ROOT")
            or (
                Path(tempfile.gettempdir()) / "reqpilotx"
                if deployment_mode is DeploymentMode.CLOUD
                else object_root.parent
            )
        ).strip()
        scratch_root = Path(raw_scratch_root).expanduser()
        if not scratch_root.is_absolute():
            scratch_root = root / scratch_root
        scratch_root = scratch_root.resolve(strict=False)

        default_hosts = (*_LOCAL_HOSTS, "testserver") if test_mode else _LOCAL_HOSTS
        allowed_hosts = _csv_values(
            source.get("REQPILOT_ALLOWED_HOSTS"),
            default=default_hosts if deployment_mode is not DeploymentMode.CLOUD else (),
        )
        allowed_origins = _csv_values(
            source.get("REQPILOT_ALLOWED_ORIGINS"),
            default=_LOCAL_ORIGINS if deployment_mode is not DeploymentMode.CLOUD else (),
        )
        _validate_allowed_hosts(allowed_hosts)
        _validate_allowed_origins(allowed_origins)
        local_worker_count = _bounded_integer(
            source.get("REQPILOT_LOCAL_WORKER_COUNT"),
            default=2 if deployment_mode is DeploymentMode.LOCAL else 1,
            minimum=1,
            maximum=2,
            field_name="REQPILOT_LOCAL_WORKER_COUNT",
        )

        oss_region = str(source.get("REQPILOT_OSS_REGION") or "").strip()
        oss_bucket = str(source.get("REQPILOT_OSS_BUCKET") or "").strip()
        oss_endpoint = str(source.get("REQPILOT_OSS_ENDPOINT") or "").strip()
        oss_prefix = _normalized_object_prefix(
            str(source.get("REQPILOT_OSS_PREFIX") or "reqpilotx")
        )
        agent_runtime_backend = _enum_value(
            AgentRuntimeBackend,
            source.get("REQPILOT_AGENT_RUNTIME", AgentRuntimeBackend.NONE.value),
            "REQPILOT_AGENT_RUNTIME",
        )
        raw_codex_bin = str(source.get("REQPILOT_CODEX_BIN") or "").strip()
        raw_codex_home = str(source.get("REQPILOT_CODEX_HOME") or "").strip()
        codex_bin = Path(raw_codex_bin).expanduser() if raw_codex_bin else None
        codex_home = Path(raw_codex_home).expanduser() if raw_codex_home else None
        agent_model_id = str(
            source.get("REQPILOT_AGENT_MODEL_ID") or "gpt-5.6-sol"
        ).strip()
        agent_customer_data_enabled = _boolean_flag(
            source.get("REQPILOT_AGENT_CUSTOMER_DATA_ENABLED"),
            field_name="REQPILOT_AGENT_CUSTOMER_DATA_ENABLED",
        )
        native_capability_mode = _enum_value(
            NativeCapabilityMode,
            source.get(
                "REQPILOT_NATIVE_CAPABILITY_MODE",
                NativeCapabilityMode.REGISTERED_RELEASE.value,
            ),
            "REQPILOT_NATIVE_CAPABILITY_MODE",
        )
        raw_capability_registry = str(
            source.get("REQPILOT_NATIVE_CAPABILITY_REGISTRY")
            or object_root.parent / "native-loop-capabilities"
        ).strip()
        native_capability_registry_root = Path(raw_capability_registry).expanduser()
        if not native_capability_registry_root.is_absolute():
            native_capability_registry_root = root / native_capability_registry_root
        native_capability_registry_root = native_capability_registry_root.resolve(
            strict=False
        )
        native_capability_bundle_sha256 = str(
            source.get("REQPILOT_NATIVE_CAPABILITY_BUNDLE_SHA256") or ""
        ).strip().lower()
        if native_capability_bundle_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", native_capability_bundle_sha256
        ):
            raise RuntimeConfigurationError(
                "REQPILOT_NATIVE_CAPABILITY_BUNDLE_SHA256 must be a lowercase "
                "64-character SHA-256 identity."
            )
        if (
            native_capability_mode is NativeCapabilityMode.DEVELOPMENT_SOURCE
            and deployment_mode is DeploymentMode.CLOUD
        ):
            raise RuntimeConfigurationError(
                "Cloud mode cannot use REQPILOT_NATIVE_CAPABILITY_MODE="
                "development-source."
            )
        if agent_runtime_backend is AgentRuntimeBackend.CODEX_APP_SERVER:
            if deployment_mode is not DeploymentMode.LOCAL:
                raise RuntimeConfigurationError(
                    "Codex App Server Agent Runtime is currently local-only."
                )
            if codex_bin is None or not codex_bin.is_absolute():
                raise RuntimeConfigurationError(
                    "Codex runtime requires absolute REQPILOT_CODEX_BIN."
                )
            if not codex_bin.is_file():
                raise RuntimeConfigurationError(
                    "REQPILOT_CODEX_BIN must identify an existing file."
                )
            if codex_home is None or not codex_home.is_absolute():
                raise RuntimeConfigurationError(
                    "Codex runtime requires absolute REQPILOT_CODEX_HOME."
                )
            if not codex_home.is_dir():
                raise RuntimeConfigurationError(
                    "REQPILOT_CODEX_HOME must identify an existing directory."
                )
            if not agent_model_id:
                raise RuntimeConfigurationError(
                    "REQPILOT_AGENT_MODEL_ID must not be blank."
                )

        if deployment_mode is DeploymentMode.CLOUD:
            if object_store_backend is not ObjectStoreBackend.OSS:
                raise RuntimeConfigurationError(
                    "Cloud mode requires REQPILOT_OBJECT_STORE=oss."
                )
            if not allowed_hosts:
                raise RuntimeConfigurationError(
                    "Cloud mode requires explicit REQPILOT_ALLOWED_HOSTS."
                )
            if not oss_region or not oss_bucket:
                raise RuntimeConfigurationError(
                    "Cloud mode requires REQPILOT_OSS_REGION and REQPILOT_OSS_BUCKET."
                )

        return cls(
            deployment_mode=deployment_mode,
            process_role=process_role,
            database_backend=database_backend,
            database_url=database_url,
            object_store_backend=object_store_backend,
            object_root=object_root,
            scratch_root=scratch_root,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            local_worker_count=local_worker_count,
            oss_region=oss_region,
            oss_bucket=oss_bucket,
            oss_endpoint=oss_endpoint,
            oss_prefix=oss_prefix,
            agent_runtime_backend=agent_runtime_backend,
            codex_bin=codex_bin,
            codex_home=codex_home,
            agent_model_id=agent_model_id,
            agent_customer_data_enabled=agent_customer_data_enabled,
            native_capability_mode=native_capability_mode,
            native_capability_registry_root=native_capability_registry_root,
            native_capability_bundle_sha256=native_capability_bundle_sha256,
        )

    @property
    def is_test(self) -> bool:
        return self.deployment_mode is DeploymentMode.TEST


def _enum_value(enum_type, value: object, field_name: str):
    normalized = str(value or "").strip().lower()
    try:
        return enum_type(normalized)
    except ValueError as exc:
        choices = ", ".join(item.value for item in enum_type)
        raise RuntimeConfigurationError(
            f"{field_name} must be one of: {choices}."
        ) from exc


def _csv_values(value: object, *, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = str(value or "").strip()
    candidates = raw.split(",") if raw else list(default)
    result: list[str] = []
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _bounded_integer(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
    field_name: str,
) -> int:
    raw = str(value or "").strip()
    try:
        normalized = int(raw) if raw else default
    except ValueError as exc:
        raise RuntimeConfigurationError(
            f"{field_name} must be an integer between {minimum} and {maximum}."
        ) from exc
    if not minimum <= normalized <= maximum:
        raise RuntimeConfigurationError(
            f"{field_name} must be between {minimum} and {maximum}."
        )
    return normalized


def _boolean_flag(value: object, *, field_name: str) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "0", "false", "no"}:
        return False
    if normalized in {"1", "true", "yes"}:
        return True
    raise RuntimeConfigurationError(
        f"{field_name} must be one of: 0, 1, false, true, no, yes."
    )


def _validate_postgresql_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.path.strip("/"):
        raise RuntimeConfigurationError(
            "REQPILOT_DATABASE_URL must be a PostgreSQL URL with a database name."
        )


def _validate_allowed_hosts(hosts: tuple[str, ...]) -> None:
    for host in hosts:
        if host == "*":
            raise RuntimeConfigurationError("Wildcard allowed hosts are forbidden.")
        try:
            parsed = urlsplit(f"//{host}")
            _ = parsed.port
        except ValueError as exc:
            raise RuntimeConfigurationError(f"Invalid allowed host: {host!r}.") from exc
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeConfigurationError(f"Invalid allowed host: {host!r}.")


def _validate_allowed_origins(origins: tuple[str, ...]) -> None:
    for origin in origins:
        if origin == "*":
            raise RuntimeConfigurationError("Wildcard allowed origins are forbidden.")
        try:
            parsed = urlsplit(origin)
            _ = parsed.port
        except ValueError as exc:
            raise RuntimeConfigurationError(f"Invalid allowed origin: {origin!r}.") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeConfigurationError(f"Invalid allowed origin: {origin!r}.")


def _normalized_object_prefix(value: str) -> str:
    parts = [part for part in value.strip().strip("/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise RuntimeConfigurationError("REQPILOT_OSS_PREFIX is not a safe object prefix.")
    return "/".join(parts)


__all__ = [
    "AgentRuntimeBackend",
    "DatabaseBackend",
    "DeploymentMode",
    "ObjectStoreBackend",
    "NativeCapabilityMode",
    "ProcessRole",
    "RuntimeConfigurationError",
    "RuntimeSettings",
]
