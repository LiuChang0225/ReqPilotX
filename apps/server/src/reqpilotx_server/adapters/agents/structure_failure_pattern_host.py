from __future__ import annotations

from reqpilotx_server.resource_paths import development_path

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import runpy
from typing import Any, Callable

from reqpilotx_server.adapters.agents.portable_kit import load_portable_agent_kit
from reqpilotx_server.application.agent_workflow.structure_tool_loop import (
    StructureMaterializationObservationV1,
)


DEFAULT_AGENT_KIT_ROOT = development_path() / "agent-kit"


@dataclass(frozen=True, slots=True)
class StructureFailurePatternHostResult:
    kit_version: str
    kit_bundle_sha256: str
    validation_report: dict[str, Any]
    observed_signals: tuple[str, ...]
    selected_failure_patterns: dict[str, Any]


class StructureFailurePatternHost:
    """Execute the exact locked Kit selector and validator as Host tools."""

    def __init__(self, kit_root: Path = DEFAULT_AGENT_KIT_ROOT) -> None:
        self.kit_root = kit_root.resolve(strict=True)
        self._kit = load_portable_agent_kit(self.kit_root)
        (
            self._load_catalog,
            self._select_patterns,
            self._validate_materialization,
        ) = _load_kit_functions(self.kit_root)
        self._catalog = self._load_catalog(
            self.kit_root / "references" / "structure-failure-patterns.json"
        )

    def select(self, signals: tuple[str, ...]) -> dict[str, Any]:
        return self._select_patterns(self._catalog, signals)

    def validate_and_select(
        self,
        observation: StructureMaterializationObservationV1,
    ) -> StructureFailurePatternHostResult:
        report = self._validate_materialization(observation.model_dump(mode="json"))
        signals = tuple(
            sorted(
                {
                    str(signal)
                    for finding in report.get("findings", [])
                    if isinstance(finding, dict)
                    for signal in finding.get("signals", [])
                    if str(signal)
                }
            )
        )
        selection = self.select(signals)
        return StructureFailurePatternHostResult(
            kit_version=self._kit.identity.kit_version,
            kit_bundle_sha256=self._kit.identity.bundle_sha256,
            validation_report=report,
            observed_signals=signals,
            selected_failure_patterns=selection,
        )


@lru_cache(maxsize=4)
def _load_kit_functions(
    kit_root: Path,
) -> tuple[
    Callable[[Path], dict[str, Any]],
    Callable[[dict[str, Any], tuple[str, ...]], dict[str, Any]],
    Callable[[dict[str, Any]], dict[str, Any]],
]:
    selector = runpy.run_path(
        str(kit_root / "scripts" / "select_structure_failure_patterns.py")
    )
    validator = runpy.run_path(
        str(kit_root / "scripts" / "validate_structure_materialization.py")
    )
    return (
        selector["load_catalog"],
        selector["select_patterns"],
        validator["validate_materialization"],
    )


__all__ = [
    "DEFAULT_AGENT_KIT_ROOT",
    "StructureFailurePatternHost",
    "StructureFailurePatternHostResult",
]
