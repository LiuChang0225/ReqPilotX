"""Replay portable Map source decisions before shared same-source migration."""
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib

from artifact_tools import read, compile_map
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.document_map_revision import compile_document_map_revision, rebind_baseline_for_document_map_revision


def _inside(root, value):
    path = (root / value).resolve(strict=True)
    if root.resolve() not in path.parents:
        raise ValueError("Map source inputs must be inside the candidate Map directory")
    return path


def compile_map_transition(previous_map, previous_baseline, previous_receipt, tables, candidate_dir,
        *, text_scope_recovery=None, feedback_ids=(), source_identity_allocation=None):
    receipt = read(candidate_dir / "map-compilation-receipt.json")
    inputs = receipt.get("source_inputs")
    if not inputs:
        raise ValueError("Map revision requires current compile-map output with original source inputs and decisions; do not hand-edit a sealed Map")
    source = _inside(candidate_dir, inputs["source_path"])
    source_ir = _inside(candidate_dir, inputs["source_ir_path"])
    decisions = _inside(candidate_dir, inputs["decisions_path"])
    if (hashlib.sha256(source.read_bytes()).hexdigest() != previous_map.source_sha256
            or hashlib.sha256(source_ir.read_bytes()).hexdigest() != inputs["source_ir_sha256"]
            or hashlib.sha256(decisions.read_bytes()).hexdigest() != inputs["decisions_sha256"]):
        raise ValueError("Map source inputs changed after compilation")
    with TemporaryDirectory(prefix="reqpilot-map-replay-") as directory:
        replay = Path(directory) / "replayed-map"
        compile_map(Namespace(source=source, source_ir=source_ir, input=decisions, output_dir=replay,
            _source_display_name=inputs.get('source_display_name', '')))
        candidate = core.DocumentMapArtifactV1.model_validate(read(replay / "document-map.json"))
        if candidate.model_dump(mode="json") != read(candidate_dir / "document-map.json") or read(replay / "table-previews.json") != read(candidate_dir / "table-previews.json"):
            raise ValueError("Candidate Map differs from replaying its original source decisions")
    mapped = compile_document_map_revision(previous_map, candidate)
    rebound = rebind_baseline_for_document_map_revision(previous_map, mapped["document_map"], previous_baseline, previous_receipt, tables,
        text_scope_recovery=text_scope_recovery, feedback_ids=feedback_ids, source_identity_allocation=source_identity_allocation)
    return mapped, rebound
