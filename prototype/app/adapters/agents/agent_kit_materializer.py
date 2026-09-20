from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil

from app.adapters.agents.portable_kit import (
    AgentKitValidationError,
    AgentRunnerProfileV1,
    PortableAgentKit,
    load_portable_agent_kit,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1


@dataclass(frozen=True, slots=True)
class MaterializedAgentKit:
    root: Path
    identity: AgentKitIdentityV1
    runner_profile: AgentRunnerProfileV1
    runner_profile_sha256: str


def materialize_agent_kit(
    kit: PortableAgentKit,
    destination: Path,
    *,
    runner_profile_id: str,
) -> MaterializedAgentKit:
    """Copy one exact canonical bundle into a new private runner workspace."""

    kit.runner_profile(runner_profile_id)
    destination = destination.expanduser()
    if destination.exists() or destination.is_symlink():
        raise AgentKitValidationError(
            "Agent Kit materialization destination must not already exist"
        )
    _reject_symlink_ancestor(destination)
    destination.mkdir(parents=True, mode=0o700)
    try:
        for locked in kit.lock.files:
            source = kit.root / locked.path
            target = destination / locked.path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, target)
            os.chmod(target, 0o600)
        lock_target = destination / "agent-kit.lock.json"
        shutil.copyfile(kit.root / "agent-kit.lock.json", lock_target)
        os.chmod(lock_target, 0o600)
        for directory in sorted(
            (item for item in destination.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            os.chmod(directory, 0o700)
        loaded = load_portable_agent_kit(destination)
        if loaded.identity != kit.identity:
            raise AgentKitValidationError("materialized Agent Kit identity drifted")
        loaded_profile = loaded.runner_profile(runner_profile_id)
        return MaterializedAgentKit(
            root=destination.resolve(),
            identity=loaded.identity,
            runner_profile=loaded_profile,
            runner_profile_sha256=loaded.runner_profile_sha256[runner_profile_id],
        )
    except Exception:
        shutil.rmtree(destination)
        raise


def _reject_symlink_ancestor(destination: Path) -> None:
    current = destination.parent
    while not current.exists() and current != current.parent:
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise AgentKitValidationError(
            "Agent Kit materialization parent must be a real directory"
        )


__all__ = ["MaterializedAgentKit", "materialize_agent_kit"]
