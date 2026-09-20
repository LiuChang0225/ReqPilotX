"""Explicit immutable capability epochs for retained native task threads."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from pathlib import Path
import re
import shutil
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.agents.codex_app_server.native_loop_driver import _digest, _write_json
from app.application.agent_workflow.source_item_loop_jobs import (
    ROOT,
    ACTIVE,
    _existing,
    _locked,
    _read,
)
from app.application.errors import ApplicationError
from app.application.transport import ResponseSink
from app.application.agent_workflow.native_structure_bridge import _no_store_headers


REGISTRY = ROOT / "prototype/runtime-data/native-loop-capabilities"


def _verify_bundle(path: Path, expected: str | None = None):
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
    for category, prefix in (
        ("plugin_files", path),
        ("runtime_files", path / "runtime/prototype"),
    ):
        for entry in receipt[category]:
            target = prefix / entry["path"]
            if (
                target.is_symlink()
                or not target.resolve(strict=True).is_relative_to(prefix.resolve())
                or hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]
            ):
                raise ValueError(
                    "Capability bundle file failed immutable build verification"
                )
    return receipt


def register_native_loop_capability_bundle(source: Path) -> dict:
    """Host-only registration; HTTP callers select a hash, never an executable path."""
    receipt = _verify_bundle(source)
    sha = receipt["bundle_sha256"]
    destination = REGISTRY / sha / "reqpilot"
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copytree(
            source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
    _verify_bundle(destination, sha)
    return {"status": "REGISTERED", "bundle_sha256": sha, "path": str(destination)}


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
    source = REGISTRY / payload.bundle_sha256 / "reqpilot"
    if not source.exists():
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
            shutil.copytree(
                source,
                destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
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


def active_native_loop_plugin(root: Path) -> Path:
    path = root / "task.json"
    meta = _read(path) if path.exists() else {}
    relative = meta.get("active_plugin_path", "plugin")
    if relative != "plugin" and not re.fullmatch(
        r"capabilities/[0-9a-f]{64}/reqpilot", str(relative)
    ):
        raise ValueError("Invalid explicit task capability path")
    resolved = (root / relative).resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Task capability leaves its authorized root")
    return resolved
