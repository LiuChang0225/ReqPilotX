"""Explicit immutable capability epochs for retained native task threads."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from reqpilotx_server.adapters.agents.codex_app_server.native_loop_driver import _digest, _write_json
from reqpilotx_server.application.agent_workflow.source_item_loop_jobs import (
    ACTIVE,
    _existing,
    _locked,
    _read,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.application.agent_workflow.native_structure_bridge import _no_store_headers
from reqpilotx_server.infrastructure.runtime_settings import (
    DeploymentMode,
    NativeCapabilityMode,
    RuntimeSettings,
)


REGISTRY = Path(tempfile.gettempdir()) / "reqpilotx/native-loop-capabilities"


def _manifest_path(value: object, *, category: str) -> PurePosixPath:
    raw = str(value or "")
    path = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != raw
    ):
        raise ValueError(f"Capability bundle has an unsafe {category} path")
    return path


def _ignored_cache(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix == ".pyc"


def _verify_bundle(
    path: Path,
    expected: str | None = None,
    *,
    require_engine_release: bool = False,
):
    path = path.resolve(strict=True)
    receipt = _read(path / "build-receipt.json")
    calculated = _digest(
        {key: value for key, value in receipt.items() if key != "bundle_sha256"}
    )
    if (
        receipt.get("source_repository") != "ReqPilotX"
        or receipt.get("bundle_sha256") != calculated
        or expected
        and calculated != expected
    ):
        raise ValueError(
            "Capability bundle identity does not match its actual frozen build"
        )
    if require_engine_release and (
        receipt.get("release_mode") != "ENGINE_WHEEL"
        or not isinstance(receipt.get("engine_release"), dict)
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(receipt["engine_release"].get("core_identity") or ""),
        )
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(receipt["engine_release"].get("wheel_sha256") or ""),
        )
        or receipt.get("delivery_included") is not True
        or receipt.get("decomposition_included") is not True
        or receipt.get("source_checkout_required_at_runtime") is not False
    ):
        raise ValueError(
            "Registered release tasks require a complete ENGINE_WHEEL plugin bundle"
        )
    declared: set[str] = set()
    for category, prefix in (
        ("plugin_files", path),
        ("runtime_files", path / "runtime/prototype"),
    ):
        entries = receipt.get(category)
        if not isinstance(entries, list):
            raise ValueError(f"Capability bundle receipt is missing {category}")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"Capability bundle has an invalid {category} entry")
            relative = _manifest_path(entry.get("path"), category=category)
            manifest_key = (
                relative.as_posix()
                if category == "plugin_files"
                else (PurePosixPath("runtime/prototype") / relative).as_posix()
            )
            if manifest_key in declared:
                raise ValueError("Capability bundle receipt repeats a file path")
            declared.add(manifest_key)
            target = prefix.joinpath(*relative.parts)
            if (
                target.is_symlink()
                or not target.resolve(strict=True).is_relative_to(prefix.resolve())
                or not target.is_file()
                or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256") or ""))
                or hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]
            ):
                raise ValueError(
                    "Capability bundle file failed immutable build verification"
                )
    actual: set[str] = set()
    for target in path.rglob("*"):
        relative = target.relative_to(path)
        if target.is_symlink():
            raise ValueError("Capability bundle contains a symlink")
        if relative.as_posix() == "build-receipt.json":
            continue
        if _ignored_cache(relative):
            continue
        if target.is_file() and relative.as_posix() != "build-receipt.json":
            actual.add(relative.as_posix())
        elif not target.is_dir():
            raise ValueError("Capability bundle contains a non-regular member")
    if actual != declared:
        missing = sorted(declared - actual)[:3]
        extra = sorted(actual - declared)[:3]
        raise ValueError(
            "Capability bundle manifest is incomplete "
            f"(missing={missing}, undeclared={extra})"
        )
    return receipt


def _registry_root(settings: object | None = None, registry_root: Path | None = None):
    configured = registry_root or getattr(
        settings, "native_capability_registry_root", REGISTRY
    )
    return Path(configured).expanduser().resolve(strict=False)


def _registered_bundle_path(registry: Path, bundle_sha256: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256):
        raise ValueError("Capability bundle identity is not a SHA-256 value")
    controlled_root = registry.resolve(strict=True)
    candidate = registry / bundle_sha256 / "reqpilot"
    if (registry / bundle_sha256).is_symlink() or candidate.is_symlink():
        raise ValueError("Registered capability path contains a symlink")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(controlled_root):
        raise ValueError("Registered capability path leaves its configured registry")
    return resolved


@contextmanager
def _registration_lock(parent: Path):
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    from reqpilotx_server.infrastructure.storage_lock import acquire_file_lease

    with acquire_file_lease(parent / ".registration.lock"):
        yield


def register_native_loop_capability_bundle(
    source: Path,
    *,
    registry_root: Path | None = None,
    expected: str | None = None,
    require_engine_release: bool = False,
) -> dict:
    """Host-only registration; HTTP callers select a hash, never an executable path."""
    receipt = _verify_bundle(
        source, expected, require_engine_release=require_engine_release
    )
    sha = receipt["bundle_sha256"]
    registry = _registry_root(registry_root=registry_root)
    destination = registry / sha / "reqpilot"
    with _registration_lock(destination.parent):
        if not destination.exists():
            staging = destination.parent / (".reqpilot-registering-" + uuid4().hex)
            try:
                shutil.copytree(
                    source,
                    staging,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                _verify_bundle(
                    staging, sha, require_engine_release=require_engine_release
                )
                staging.replace(destination)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        _verify_bundle(
            destination, sha, require_engine_release=require_engine_release
        )
    return {"status": "REGISTERED", "bundle_sha256": sha, "path": str(destination)}


def prepare_native_loop_capability(root: Path, settings: RuntimeSettings | object):
    """Freeze one verified full plugin bundle without trusting request paths."""
    destination = root / "plugin"
    if destination.exists():
        raise ValueError("Task capability destination already exists")
    staging = root / (".plugin-staging-" + uuid4().hex)
    mode = getattr(
        settings,
        "native_capability_mode",
        NativeCapabilityMode.REGISTERED_RELEASE,
    )
    if not isinstance(mode, NativeCapabilityMode):
        mode = NativeCapabilityMode(str(mode))
    try:
        if mode is NativeCapabilityMode.REGISTERED_RELEASE:
            expected = str(
                getattr(settings, "native_capability_bundle_sha256", "") or ""
            ).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError(
                    "Registered release mode requires an exact host-configured "
                    "REQPILOT_NATIVE_CAPABILITY_BUNDLE_SHA256"
                )
            try:
                source = _registered_bundle_path(_registry_root(settings), expected)
            except FileNotFoundError as exc:
                raise ValueError(
                    "Configured native capability bundle is not registered"
                ) from exc
            _verify_bundle(source, expected, require_engine_release=True)
            shutil.copytree(
                source,
                staging,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            copied = _verify_bundle(
                staging, expected, require_engine_release=True
            )
        else:
            if getattr(sys, "frozen", False):
                raise ValueError(
                    "Frozen desktop tasks require a registered release capability bundle"
                )
            deployment = getattr(settings, "deployment_mode", DeploymentMode.LOCAL)
            if str(deployment) == DeploymentMode.CLOUD.value:
                raise ValueError("Cloud tasks cannot build capabilities from source")
            development_root = getattr(settings, "development_root", None)
            if development_root is None:
                raise ValueError(
                    "development-source requires absolute REQPILOT_DEVELOPMENT_ROOT"
                )
            build_script = (
                Path(development_root)
                / "plugins/reqpilot/scripts/build_bundle.py"
            ).resolve(strict=True)
            built = subprocess.run(
                [
                    sys.executable,
                    str(build_script),
                    "--repo",
                    str(Path(development_root).resolve(strict=True)),
                    "--output",
                    str(staging),
                ],
                capture_output=True,
                text=True,
                timeout=90,
            )
            if built.returncode:
                raise ValueError((built.stderr or built.stdout)[-12000:])
            copied = _verify_bundle(staging)
        staging.replace(destination)
        return {
            **copied,
            "capability_source": mode.value,
            "registered_source_verified": mode
            is NativeCapabilityMode.REGISTERED_RELEASE,
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


class NativeLoopCapabilityUpgradeV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=160)
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin: Literal["USER_REQUEST", "AGENT_SELF_REVIEW"] = "USER_REQUEST"
    reason: str = Field(min_length=1, max_length=4000)


def upgrade_native_loop_capability(
    project_id: str,
    document_id: str,
    task_id: str,
    payload: NativeLoopCapabilityUpgradeV1,
    response: ResponseSink,
):
    response.headers.update(_no_store_headers())
    container, root, _ = _existing(project_id, document_id, task_id)
    try:
        source = _registered_bundle_path(
            _registry_root(getattr(container, "settings", None)),
            payload.bundle_sha256,
        )
    except FileNotFoundError:
        raise ApplicationError(status_code=404, detail="这个能力包尚未由宿主登记。")
    _verify_bundle(source, payload.bundle_sha256)
    with _locked(root):
        meta = _read(root / "task.json")
        job = container.jobs.get(meta["latest_job_id"])
        if job.status.value in ACTIVE:
            raise ApplicationError(
                status_code=409,
                detail="请先让当前轮次结束或完成中断核对，再升级同一任务能力。",
            )
        state = _read(root / "runtime/run-state.json")
        terminal = state.get("status") in {"COMPLETED", "INTERRUPTED", "FAILED"} and (
            (state.get("last_receipt") or {}).get("terminal_snapshot_verified") is True
            or (state.get("terminal_observation") or {}).get("verified_terminal")
            is True
        )
        if not terminal or state.get("pid") or state.get("app_server_pid"):
            raise ApplicationError(
                status_code=409, detail="运行状态尚未核对为终态，不能替换当前能力。"
            )
        commands = meta.setdefault("capability_commands", {})
        command_sha = _digest(payload.model_dump(mode="json"))
        if payload.command_id in commands:
            if commands[payload.command_id]["command_sha256"] != command_sha:
                raise ApplicationError(
                    status_code=409, detail="相同能力升级命令不能替换内容。"
                )
            return commands[payload.command_id]
        destination = root / "capabilities" / payload.bundle_sha256 / "reqpilot"
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            staging = destination.parent / (".reqpilot-staging-" + uuid4().hex)
            try:
                shutil.copytree(
                    source,
                    staging,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                _verify_bundle(staging, payload.bundle_sha256)
                staging.replace(destination)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        _verify_bundle(destination, payload.bundle_sha256)
        old_sha = (
            meta.get("active_plugin_bundle_sha256") or meta["plugin_bundle_sha256"]
        )
        reference = (
            "capabilities/"
            + payload.bundle_sha256
            + "/authorization-"
            + command_sha[:24]
            + ".json"
        )
        epoch = {
            "schema_version": "reqpilotx-native-capability-epoch/v1",
            "epoch_id": "cap-" + command_sha[:24],
            "previous_identity_sha256": _digest(state["identity"]),
            "previous_bundle_sha256": old_sha,
            "bundle_sha256": payload.bundle_sha256,
            "origin": payload.origin,
            "reason": payload.reason,
            "authorization_reference": reference,
        }
        authorization = {
            "schema_version": "reqpilotx-native-capability-authorization/v1",
            "task_id": task_id,
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": meta["source_sha256"],
            "thread_id": state["thread_id"],
            "epoch": epoch,
            "recorded_at": datetime.now(UTC).isoformat(),
            "old_package_preserved": True,
            "source_workspace_preserved": True,
            "human_acceptance": "NOT_CLAIMED",
        }
        _write_json(root / reference, authorization)
        meta.update(
            active_plugin_path=str(destination.relative_to(root)),
            active_plugin_bundle_sha256=payload.bundle_sha256,
            capability_epoch=epoch,
        )
        result = {
            "schema_version": "native-loop-capability-upgrade/v1",
            "status": "STAGED",
            "task_id": task_id,
            "thread_id": state["thread_id"],
            "epoch": epoch,
            "command_sha256": command_sha,
            "runtime_activation": "NOT_YET_RUN",
            "new_model_turn_started": False,
            "old_package_preserved": True,
        }
        commands[payload.command_id] = result
        _write_json(root / "task.json", meta)
        return result


def _active_native_loop_plugin(root: Path, meta: dict) -> Path:
    relative = meta.get("active_plugin_path", "plugin")
    if relative != "plugin" and not re.fullmatch(
        r"capabilities/[0-9a-f]{64}/reqpilot", str(relative)
    ):
        raise ValueError("Invalid explicit task capability path")
    resolved = (root / relative).resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Task capability leaves its authorized root")
    expected = (
        meta.get("active_plugin_bundle_sha256") or meta.get("plugin_bundle_sha256")
    )
    if expected:
        _verify_bundle(resolved, expected)
    return resolved


def active_native_loop_plugin(root: Path) -> Path:
    path = root / "task.json"
    meta = _read(path) if path.exists() else {}
    return _active_native_loop_plugin(root, meta)


def native_loop_plugin_for_job(root: Path, meta: dict, payload: dict) -> Path:
    """Verify that a queued job still names the task's exact frozen capability."""
    recorded = _read(root / "task.json")
    for field in (
        "plugin_bundle_sha256",
        "active_plugin_bundle_sha256",
        "active_plugin_path",
        "capability_epoch",
    ):
        if recorded.get(field) != meta.get(field):
            raise ValueError("Native task capability changed while loading its job")
    current = meta.get("active_plugin_bundle_sha256") or meta.get(
        "plugin_bundle_sha256"
    )
    queued = payload.get("plugin_bundle_sha256")
    if queued is not None and queued != current:
        raise ValueError("Queued native job capability differs from its task")
    if "capability_epoch" in payload and payload.get("capability_epoch") != meta.get(
        "capability_epoch"
    ):
        raise ValueError("Queued native job capability epoch differs from its task")
    return _active_native_loop_plugin(root, recorded)
