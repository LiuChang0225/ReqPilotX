from __future__ import annotations

from typing import TypeAlias

from app.application.extraction.resolve_structure import (
    validate_structure_tree_against_source_ir,
)
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.domain.extraction.structure import StructureTreeSnapshot
from app.domain.extraction.structure_revision import EffectiveStructureSnapshotV1


StructureSnapshot: TypeAlias = (
    StructureTreeSnapshot | EffectiveStructureSnapshotV1
)


def require_structure_snapshot(value: object) -> StructureSnapshot:
    """Accept only a deterministic base or a Core-validated effective snapshot."""

    if not isinstance(
        value,
        (StructureTreeSnapshot, EffectiveStructureSnapshotV1),
    ):
        raise TypeError(
            "structure must be a StructureTreeSnapshot or "
            "EffectiveStructureSnapshotV1"
        )
    return value


def structure_snapshot_id(snapshot: StructureSnapshot) -> str:
    """Return the only identity downstream derivations may bind to."""

    if isinstance(snapshot, EffectiveStructureSnapshotV1):
        return snapshot.effective_snapshot_id
    return snapshot.structure_snapshot_id


def validate_structure_snapshot_against_source_ir(
    snapshot: StructureSnapshot,
    source_ir: SourceIRSnapshot,
) -> None:
    """Keep base validation strict and fail closed on effective-source mismatch."""

    require_structure_snapshot(snapshot)
    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    if isinstance(snapshot, StructureTreeSnapshot):
        validate_structure_tree_against_source_ir(snapshot, source_ir)
        return
    if (
        snapshot.source_ir_id != source_ir.source_ir_id
        or snapshot.source_sha256 != source_ir.source_sha256
    ):
        raise ValueError("effective Structure is not bound to this SourceIR")


__all__ = [
    "StructureSnapshot",
    "require_structure_snapshot",
    "structure_snapshot_id",
    "validate_structure_snapshot_against_source_ir",
]
