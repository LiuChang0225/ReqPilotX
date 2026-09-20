"""Register and re-read actual internal files through the shared fact capture.

Registration metadata is explicitly supplied by the user/host. It is not proof
of human approval. Fact wording and locators can only come from actual files;
an Agent's assessment candidate cannot manufacture a registered fact catalog.
"""
from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path
import shutil

from app.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
from app.application.agent_workflow.source_item_analysis import validate_knowledge_catalog
from app.application.knowledge_evidence_capture import capture_knowledge_facts
from artifact_tools import read, save, required, new_output


def _source(root, value):
    target = (root / value).resolve(strict=True)
    if root.resolve() not in target.parents or not target.is_file():
        raise ValueError("Registered evidence source must resolve inside its registration directory")
    return target


def _capture_catalog(registration, directory):
    catalog = {"schema_version": "project-knowledge-facts/v1", "project_id": registration["project_id"],
        "knowledge_base_version": registration["knowledge_base_version"], "evidence_catalog": {}, "documents": [], "gaps": [], "selection": registration.get("selection", "EXPLICIT_DOCUMENTS")}
    seen = set()
    for item in sorted(registration["documents"], key=lambda value: value["record"]["id"]):
        record = item["record"]
        if record["id"] in seen:
            raise ValueError("Duplicate registered evidence document identity")
        seen.add(record["id"])
        source = _source(directory, item["source_path"])
        captured = capture_knowledge_facts(record, source, project_id=registration["project_id"], as_of=date.fromisoformat(registration["as_of"]))
        catalog["documents"].append(captured["document"])
        catalog["evidence_catalog"].update(captured["facts"])
        catalog["gaps"].extend({"document_id": record["id"], **gap} for gap in captured["gaps"])
    catalog["catalog_sha256"] = canonical_json_sha256(catalog)
    return catalog


def register(args):
    input_path = required(args.input, "--input"); value = read(input_path)
    declaration = value.get("declaration", {})
    if (value.get("schema_version") != "reqpilot-evidence-input/v1" or declaration.get("origin") not in {"USER_SUPPLIED", "HOST_REGISTERED"}
            or not str(declaration.get("reference", "")).strip() or not value.get("documents")):
        raise ValueError("Evidence registration needs an explicit user/host supplied document list and declaration reference")
    # Validate byte identity before allocating an output directory.
    sources = []
    for item in value["documents"]:
        source = (input_path.parent / item["source_path"]).resolve(strict=True)
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != item["record"].get("source_sha256"):
            raise ValueError("Evidence original does not match the declared file SHA256")
        sources.append(source)
    output = new_output(args.output_dir)
    source_dir = output / "knowledge-sources"; source_dir.mkdir(mode=0o700)
    registration = {"schema_version": "reqpilot-evidence-registration/v1", "project_id": value["project_id"], "knowledge_base_version": value["knowledge_base_version"],
        "as_of": value.get("as_of", date.today().isoformat()), "declaration": declaration, "documents": [], "selection": value.get("selection", "EXPLICIT_DOCUMENTS"),
        "authority_boundary": "Explicit user/host registration metadata; actual fact text and locators are recaptured from source files. Human approval is not claimed."}
    for number, (item, source) in enumerate(zip(value["documents"], sources), 1):
        target = source_dir / f"{number:03d}-{source.name}"; shutil.copyfile(source, target); target.chmod(0o600)
        registration["documents"].append({"source_path": str(target.relative_to(output)), "record": item["record"]})
    catalog = _capture_catalog(registration, output)
    registration["catalog_sha256"] = catalog["catalog_sha256"]
    registration["registration_sha256"] = canonical_json_sha256(registration)
    save(output, "evidence-registration.json", registration); save(output, "knowledge-catalog.json", catalog)
    return {"status": "EVIDENCE_REGISTERED", "output_dir": str(output), "catalog_sha256": catalog["catalog_sha256"], "document_count": len(catalog["documents"]),
        "fact_count": len(catalog["evidence_catalog"]), "human_acceptance": "NOT_CLAIMED"}


def verify_catalog(path):
    catalog = validate_knowledge_catalog(read(path))
    if not catalog["evidence_catalog"] and not catalog.get("documents"):
        return catalog
    registration_path = path.parent / "evidence-registration.json"
    if not registration_path.is_file():
        raise ValueError("A nonempty knowledge catalog requires evidence-registration.json and actual registered source files; a self-signed catalog is not evidence")
    registration = read(registration_path)
    if registration.get("schema_version") != "reqpilot-evidence-registration/v1" or registration.get("registration_sha256") != canonical_json_sha256({k: v for k, v in registration.items() if k != "registration_sha256"}):
        raise ValueError("Evidence registration integrity changed")
    recaptured = _capture_catalog(registration, path.parent)
    if catalog != recaptured or registration.get("catalog_sha256") != catalog["catalog_sha256"]:
        raise ValueError("Knowledge facts differ from the actual registered original files")
    return catalog


def copy_registration(catalog_path, output):
    verify_catalog(catalog_path)
    registration_path = catalog_path.parent / "evidence-registration.json"
    if not registration_path.is_file():
        return
    registration = read(registration_path)
    save(output, "evidence-registration.json", registration)
    for item in registration["documents"]:
        source = _source(catalog_path.parent, item["source_path"])
        target = output / item["source_path"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, target); target.chmod(0o600)


def verify_packet_evidence(packet, packet_path):
    catalog_path = packet_path.parent / "knowledge-catalog.json"
    if not catalog_path.is_file():
        if packet.get("evidence_catalog"):
            raise ValueError("Analysis with internal facts requires its actual source-bound evidence registration")
        return None
    catalog = verify_catalog(catalog_path)
    if (catalog["catalog_sha256"] != packet.get("catalog_sha256") or catalog["evidence_catalog"] != packet.get("evidence_catalog")
            or catalog.get("documents", []) != packet.get("knowledge_documents", []) or catalog.get("gaps", []) != packet.get("catalog_gaps", [])):
        raise ValueError("Analysis packet facts differ from the registered knowledge catalog")
    return catalog_path


def verify_source_packet(document_map, baseline, tables, packet, catalog, *, annotations=None, decomposition=None):
    """Rebuild from the actual source artifacts, not a packet's self-declared SHA."""
    from app.application.agent_workflow.source_item_analysis import build_source_item_analysis_packet
    expected = build_source_item_analysis_packet(document_map, baseline, tables, catalog,
        decomposition=decomposition, selected_unit_ids=packet.get("selected_unit_ids"), image_annotations=annotations)
    if packet != expected:
        raise ValueError("Analysis packet differs from the actual current source, classes, image interpretation, derivation or full unit registry")
    return expected


def freeze_analysis_source(output, document_map, baseline, tables, *, annotations=None, decomposition=None):
    directory = output / "analysis-source"; directory.mkdir(mode=0o700)
    for name, value in (("document-map.json", document_map.model_dump(mode="json")), ("source-requirement-baseline.json", baseline.model_dump(mode="json")), ("table-previews.json", tables)):
        save(directory, name, value)
    if annotations is not None:
        save(directory, "image-annotations.json", annotations)
    if decomposition is not None:
        save(directory, "source-item-decomposition.json", decomposition)


def verify_frozen_analysis_source(packet_path, packet, catalog, *, baseline_dir=None):
    from app.application.agent_workflow import source_requirement_baseline as core
    directory = baseline_dir or packet_path.parent / "analysis-source"
    if not directory.is_dir() and (packet_path.parent / "document-map.json").is_file() and (packet_path.parent / "source-requirement-baseline.json").is_file():
        directory = packet_path.parent
    if not directory.is_dir():
        raise ValueError("Assessment compilation requires its frozen analysis-source directory or an explicit current --baseline-dir")
    document_map = core.DocumentMapArtifactV1.model_validate(read(directory / "document-map.json"))
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(read(directory / "source-requirement-baseline.json"))
    annotations_path = directory / "image-annotations.json"; decomposition_path = directory / "source-item-decomposition.json"
    verify_source_packet(document_map, baseline, read(directory / "table-previews.json"), packet, catalog,
        annotations=read(annotations_path) if annotations_path.is_file() else None, decomposition=read(decomposition_path) if decomposition_path.is_file() else None)
    return directory
