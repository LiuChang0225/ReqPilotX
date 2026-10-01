"""Source-bound image SourceItems for the app and portable Agent workflow.

The original is a Map-bound image region. Agent labels and visual readings are
explicit annotations; they are never inserted as customer-verbatim text.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, field_validator, model_validator

from reqpilotx.application.agent_workflow import source_requirement_baseline as core
from reqpilotx.application.agent_workflow.source_item_ledger import source_item_position
from reqpilotx.application.agent_workflow.source_locations import original_format, native_location_fields, source_position
from reqpilotx.application.agent_workflow.source_path_labels import compose_source_path_segment


class SourceItemVisualError(ValueError):
    pass


class SourceItemImageDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_key: str = Field(pattern=r"^[a-z][a-z0-9._:-]{0,199}$")
    display_label: str = Field(min_length=1, max_length=1000)
    image_anchor_ids: tuple[str, ...] = Field(min_length=1, max_length=512)
    requirementness: core.Requirementness
    requirement_kinds: tuple[core.RequirementKind, ...] = Field(default=(), max_length=5)
    non_requirement_role: core.NonRequirementRole | None = None
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    decision_summary: str = Field(min_length=1, max_length=1000)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    visual_interpretation: str = Field(default="", max_length=20000)

    @field_validator("reason_codes")
    @classmethod
    def reasons(cls, value):
        return core._validate_reason_codes(value)

    @model_validator(mode="after")
    def validate_decision(self):
        if len(self.image_anchor_ids) != len(set(self.image_anchor_ids)):
            raise ValueError("Image anchors must be unique in an item")
        if tuple(sorted(set(self.requirement_kinds), key=lambda item: item.value)) != self.requirement_kinds:
            raise ValueError("Image requirement kinds must be unique and sorted")
        if self.requirementness is core.Requirementness.REQUIREMENT:
            if not self.requirement_kinds or self.non_requirement_role is not None:
                raise ValueError("A requirement image needs classes and no non-requirement role")
        elif self.requirement_kinds or self.non_requirement_role is None:
            raise ValueError("A non-requirement image needs its retained content role")
        return self


class SourceItemImageScopeCandidateV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["source-item-image-scope-candidate/v1"] = "source-item-image-scope-candidate/v1"
    node_id: str = Field(min_length=1, max_length=200)
    packet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: tuple[SourceItemImageDecisionV1, ...] = Field(max_length=4096)


def _binding(document_map, anchor):
    return core.SourceImageBindingV1(source_sha256=document_map.source_sha256,
        anchor_id=anchor.anchor_id, source_handle=anchor.source_handle, asset_id=anchor.asset_id,
        page=anchor.page, bbox=anchor.bbox, source_bounds=anchor.source_bounds, precision=anchor.precision,
        **native_location_fields(anchor), **({"content_sha256": anchor.content_sha256} if anchor.content_sha256 else {}))


def image_node_table_scope_ids(document_map, node_id):
    node = next((n for n in document_map.nodes if n.node_id == node_id), None)
    if node is None:
        raise SourceItemVisualError("Image scope requires a real Document Map node")
    scopes = {b.logical_table_scope_id for b in node.content_blocks if b.logical_table_scope_id}
    scopes.update(t.scope_id for t in document_map.logical_tables if t.owner_unit_id == node_id)
    images = [a for a in node.source_anchors if a.anchor_kind in {"IMAGE", "FIGURE"}]
    related = []
    for scope_id in sorted(scopes):
        tables = [a for a in node.source_anchors if a.logical_table_scope_id == scope_id and a.anchor_kind == "TABLE"]
        # Without a measured table extent remain conservative. Disjoint original
        # regions cannot share cells and need no unnecessary table dependency.
        if document_map.source_format:
            # DOCX cell-embedded images have an explicit native table path.
            # XLSX floating drawing anchors alone do not make an image cell data.
            if any(i.native_locator.format.value == "docx" and i.native_locator.table_index is not None
                   and i.native_locator.body_element_index == t.native_locator.body_element_index
                   and i.native_locator.table_index == t.native_locator.table_index for i in images for t in tables):
                related.append(scope_id)
            continue
        if not tables or any(i.page == t.page and min(i.bbox[2], t.bbox[2]) > max(i.bbox[0], t.bbox[0])
                             and min(i.bbox[3], t.bbox[3]) > max(i.bbox[1], t.bbox[1]) for i in images for t in tables):
            related.append(scope_id)
    return related


def build_source_item_image_scope_packet(document_map, *, node_id: str, baseline=None, table_previews=None) -> dict[str, Any]:
    document_map = core.DocumentMapArtifactV1.model_validate(document_map)
    node = next((node for node in document_map.nodes if node.node_id == node_id), None)
    if node is None:
        raise SourceItemVisualError("Image scope requires a real Document Map node")
    anchors = sorted((anchor for anchor in node.source_anchors if anchor.anchor_kind in {"IMAGE", "FIGURE"}),
                     key=lambda anchor: (*source_position(anchor), anchor.anchor_id) if original_format(anchor.native_locator) != "pdf" else (anchor.page, anchor.bbox[1], anchor.bbox[0], anchor.anchor_id))
    if not anchors or len({anchor.anchor_id for anchor in anchors}) != len(anchors):
        raise SourceItemVisualError("Image scope requires unique original image anchors")
    related_scopes = image_node_table_scope_ids(document_map, node_id)
    table_relations = []
    if related_scopes:
        if baseline is None:
            raise SourceItemVisualError("Image scope has related tables; compile their D3 source ownership before image decisions")
        baseline = core.SourceRequirementBaselineArtifactV1.model_validate(baseline)
        pending = set(related_scopes) - set(baseline.covered_scope_ids)
        previews = table_previews or {}
        if pending or set(related_scopes) - set(previews):
            raise SourceItemVisualError("Image scope waits for complete related table source ownership and previews: " + ",".join(sorted(pending or set(related_scopes) - set(previews))))
        from reqpilotx.application.agent_workflow.source_item_figure_relations import table_source_figure_relations
        relation_map = table_source_figure_relations(document_map, baseline, previews)
        by_anchor = {anchor.anchor_id: anchor for anchor in anchors}
        for source_id, refs in relation_map["records"].items():
            for ref in refs:
                if ref["anchor_id"] in by_anchor:
                    table_relations.append(core.SourceItemTableImageRelationV1(node_id=node_id,
                        image_binding=_binding(document_map, by_anchor[ref["anchor_id"]]),
                        table_source_record_id=source_id, logical_table_scope_id=ref["logical_table_scope_id"],
                        fragment_id=ref["table_fragment_id"], cell_ids=tuple(sorted(ref["source_cell_ids"]))))
        excluded = {relation.image_binding.anchor_id for relation in table_relations}
        anchors = [anchor for anchor in anchors if anchor.anchor_id not in excluded]
    values = {"schema_version": "source-item-image-scope-packet/v1", "scope_id": f"image:{node_id}",
        "source_sha256": document_map.source_sha256, "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256, "node_id": node_id,
        "node_label": node.label, "node_title": node.title, "ancestor_path": list(node.ancestor_path),
        **({"structural_role": node.kind} if node.kind in core.FRONT_MATTER_KINDS else {}),
        "images": [_binding(document_map, anchor).model_dump(mode="json") for anchor in anchors],
        "direct_source_context": [content.model_dump(mode="json") for content in node.source_content],
        "original_authority": "SOURCE_IMAGE_PIXELS", "interpretation_authority": "AGENT_VISUAL_INTERPRETATION"}
    if related_scopes:
        values.update(related_table_scope_ids=related_scopes,
                      table_image_relations=[r.model_dump(mode="json") for r in sorted(table_relations, key=lambda r: r.image_binding.anchor_id)])
    return {**values, "packet_sha256": core.canonical_json_sha256(values)}


def _image_evidence_sha(record):
    return core.canonical_json_sha256({"node_id": record.node_id,
        "bindings": [binding.model_dump(mode="json") for binding in record.image_bindings]})


def _seal_annotations(values):
    values = {key: value for key, value in values.items() if key != "annotations_sha256"}
    return {**values, "annotations_sha256": core.canonical_json_sha256(values)}


def rebind_source_item_image_annotations(document_map, baseline, annotations):
    """Explicitly retain Agent readings only when the original pixels are unchanged."""
    document_map = core.DocumentMapArtifactV1.model_validate(document_map)
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if annotations.get("schema_version") == "source-item-annotations/v1":
        if (annotations.get("annotations_sha256") != _seal_annotations(annotations)["annotations_sha256"]
                or annotations.get("source_sha256") != baseline.source_sha256
                or annotations.get("document_map_artifact_sha256") != document_map.artifact_sha256):
            raise SourceItemVisualError("Source annotations changed or belong to another original Map")
        by_id = {record.source_record_id: record for record in baseline.records}
        for sid, annotation in annotations.get("records", {}).items():
            if sid not in by_id or annotation.get("annotation_source_evidence_sha256") != _visual_source_evidence(by_id[sid]):
                raise SourceItemVisualError("Source annotation is stale for changed original bindings")
        return _seal_annotations({**deepcopy(annotations), "baseline_sha256": baseline.artifact_sha256})
    if annotations.get("schema_version") == "source-item-visual-readings/v1":
        return rebind_source_item_visual_readings(document_map, baseline, annotations)
    if annotations.get("schema_version") != "source-item-image-annotations/v1":
        raise SourceItemVisualError("Unknown image annotation contract")
    if annotations.get("annotations_sha256") != _seal_annotations(annotations)["annotations_sha256"]:
        raise SourceItemVisualError("Image annotations changed after compilation")
    if (annotations.get("source_sha256") != baseline.source_sha256
            or annotations.get("document_map_artifact_sha256") != document_map.artifact_sha256
            or baseline.document_map_artifact_sha256 != document_map.artifact_sha256):
        raise SourceItemVisualError("Image annotations belong to different source or Map")
    by_id = {record.source_record_id: record for record in baseline.records}
    values = deepcopy(annotations)
    values["records"] = {key: value for key, value in annotations.get("records", {}).items() if key in by_id}
    for source_id, annotation in values["records"].items():
        record = by_id[source_id]
        if not record.image_bindings:
            if (annotation.get("annotation_source_evidence_sha256") or annotation.get("source_evidence_sha256")) != _visual_source_evidence(record):
                raise SourceItemVisualError("Source annotation is stale for changed original text/table bindings")
            continue
        core.validate_source_image_record_against_map(document_map, record)
        if annotation.get("image_evidence_sha256") != _image_evidence_sha(record):
            raise SourceItemVisualError("Image annotation is stale for changed original image bindings")
    values["baseline_sha256"] = baseline.artifact_sha256
    values["source_display_high_watermark"] = max(int(values.get("source_display_high_watermark", 0)), core._baseline_display_high_watermark(baseline))
    if annotations["baseline_sha256"] != baseline.artifact_sha256:
        values["rebound_from_baseline_sha256"] = annotations["baseline_sha256"]
    return _seal_annotations(values)


def _visual_source_evidence(record):
    return core.canonical_json_sha256({"node_id": record.node_id,
        "text_bindings": [b.model_dump(mode="json") for b in record.text_bindings],
        "table_fragments": [b.model_dump(mode="json") for b in record.table_fragments],
        "image_bindings": [b.model_dump(mode="json") for b in record.image_bindings]})


def _source_extent_evidence(document_map, baseline, record, references, table_previews):
    if not isinstance(references, list) or not references:
        raise SourceItemVisualError("A declared source-number extent needs explicit original ranges")
    nodes = {node.node_id: node for node in document_map.nodes}
    content = {item.content_id: item for item in nodes[record.node_id].source_content}
    result, seen = [], set()
    for reference in references:
        kind = reference.get("kind"); start, end = reference.get("start_offset"), reference.get("end_offset")
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise SourceItemVisualError("Source-number extent needs exact half-open character ranges")
        if kind == "TEXT":
            if set(reference) != {"kind", "content_id", "start_offset", "end_offset"}:
                raise SourceItemVisualError("Source-number text extent has unsupported fields")
            item = content.get(reference["content_id"])
            if item is None or not any(binding.content_id == item.content_id and binding.start_offset <= start < end <= binding.end_offset for binding in record.text_bindings):
                raise SourceItemVisualError("Source-number extent escapes the parent's actual text bindings")
            text, page, bbox = item.text, item.page, list(item.bbox) if item.bbox is not None else None
            native = native_location_fields(item)
        elif kind == "TABLE_CELL":
            if set(reference) != {"kind", "fragment_id", "cell_id", "start_offset", "end_offset"}:
                raise SourceItemVisualError("Source-number cell extent has unsupported fields")
            binding = next((binding for binding in record.table_fragments if binding.fragment_id == reference["fragment_id"] and reference["cell_id"] in binding.cell_ids), None)
            preview = (table_previews or {}).get(record.logical_table_scope_id)
            if not binding or not preview or preview.get("source_sha256") != baseline.source_sha256:
                raise SourceItemVisualError("Source-number extent escapes the parent's actual table bindings")
            fragment = next((item for item in preview["fragments"] if item["fragment_id"] == binding.fragment_id), None)
            cell = next((item for item in (fragment or {}).get("cells", []) if item.get("cell_id", item.get("id")) == reference["cell_id"]), None)
            if cell is None:
                raise SourceItemVisualError("Source-number extent needs an actual bound original cell")
            text, page, bbox = cell["text"], binding.page, list(cell["bbox"]) if cell["bbox"] is not None else None
            native = native_location_fields(cell)
        else:
            raise SourceItemVisualError("Source-number extents may only cite owned TEXT or TABLE_CELL ranges")
        if end > len(text):
            raise SourceItemVisualError("Source-number extent exceeds its actual original text")
        identity = core.canonical_json_sha256(reference)
        if identity in seen:
            raise SourceItemVisualError("A source-number extent repeats an original range")
        seen.add(identity)
        result.append({"reference": deepcopy(reference), "text": text[start:end], "page": page, "bbox": bbox, **native})
    return result


def _source_number_evidence(document_map, baseline, source_item_id, number, reference, table_previews, source_extent=None):
    records = {record.source_record_id: record for record in baseline.records}
    record = records.get(source_item_id)
    if record is None or not isinstance(number, str) or not number or not isinstance(reference, dict):
        raise SourceItemVisualError("Original numbering needs an existing source, exact number and source reference")
    kind = reference.get("kind")
    start, end = reference.get("start_offset"), reference.get("end_offset")
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise SourceItemVisualError("Original number offsets must be an exact half-open source range")
    nodes = {node.node_id: node for node in document_map.nodes}; node = nodes[record.node_id]
    extents = _source_extent_evidence(document_map, baseline, record, source_extent, table_previews) if source_extent is not None else None
    text, page, bbox = None, None, None
    native = {}
    if kind in {"TEXT", "MAP_HEADING"}:
        if set(reference) != {"kind", "content_id", "start_offset", "end_offset"}:
            raise SourceItemVisualError("Original number text reference has unsupported fields")
        content = next((content for content in node.source_content if content.content_id == reference["content_id"]), None)
        if content is None:
            raise SourceItemVisualError("Number reference must belong to the source item's actual Map node")
        if kind == "TEXT":
            owned_binding = next((binding for binding in record.text_bindings if binding.content_id == content.content_id and binding.start_offset <= start < end <= binding.end_offset), None)
            if owned_binding is None:
                raise SourceItemVisualError("Number reference is outside the source item's bound original text")
            if content.text[owned_binding.start_offset:start].strip():
                line_start = max(owned_binding.start_offset, content.text.rfind("\n", owned_binding.start_offset, start) + 1)
                covering = [extent for extent in (extents or []) if extent["reference"].get("kind") == "TEXT" and extent["reference"].get("content_id") == content.content_id
                    and extent["reference"]["start_offset"] <= start < end <= extent["reference"]["end_offset"]]
                if content.text[line_start:start].strip() or not covering:
                    raise SourceItemVisualError("A source number must occupy the leading numbering position, not an arbitrary value in prose")
        else:
            direct_text = [item for item in baseline.records if item.node_id == record.node_id and item.text_bindings]
            if not content.is_title or not direct_text or direct_text[0].source_record_id != source_item_id:
                raise SourceItemVisualError("A Map heading number belongs only to its first directly owned text item and exact source label")
            if content.text[:start].strip():
                raise SourceItemVisualError("Map heading number must occupy its visible leading position")
            if node.label != number:
                # Older Agent Maps sometimes stored the whole heading as label.
                # A separately selected exact original numeric prefix can repair
                # its projection without rewriting that Map or its source units.
                from .source_item_numbering import _prefix
                prefix = _prefix(content.text)
                if prefix is None or prefix.group('label') != number or (prefix.start('label'), prefix.end('label')) != (start, end):
                    raise SourceItemVisualError("Heading number must match its source label or exact original numeric heading prefix")
        text, page, bbox = content.text, content.page, list(content.bbox) if content.bbox is not None else None
        native = native_location_fields(content)
    elif kind == "TABLE_CELL":
        if set(reference) != {"kind", "fragment_id", "cell_id", "start_offset", "end_offset"}:
            raise SourceItemVisualError("Original number cell reference has unsupported fields")
        binding = next((binding for binding in record.table_fragments if binding.fragment_id == reference["fragment_id"] and reference["cell_id"] in binding.cell_ids), None)
        preview = (table_previews or {}).get(record.logical_table_scope_id)
        if binding is None or not preview or preview.get("source_sha256") != baseline.source_sha256:
            raise SourceItemVisualError("Number cell must be owned by this SourceItem in the same-source original table")
        fragment = next((fragment for fragment in preview["fragments"] if fragment["fragment_id"] == binding.fragment_id), None)
        cell = next((cell for cell in (fragment or {}).get("cells", []) if cell.get("cell_id", cell.get("id")) == reference["cell_id"]), None)
        if cell is None or cell["text"][:start].strip():
            raise SourceItemVisualError("Original number must occupy the leading position of its owned original cell")
        text, page, bbox = cell["text"], binding.page, list(cell["bbox"]) if cell["bbox"] is not None else None
        native = native_location_fields(cell)
    else:
        raise SourceItemVisualError("Use TEXT, TABLE_CELL or the actual owning MAP_HEADING for source numbering")
    if end > len(text) or text[start:end] != number:
        raise SourceItemVisualError("Source number is not the exact text at its cited original range")
    if extents is not None and kind in {"TEXT", "TABLE_CELL"}:
        def contains_number(extent):
            ref = extent["reference"]
            keys = ("content_id",) if kind == "TEXT" else ("fragment_id", "cell_id")
            return ref.get("kind") == kind and all(ref.get(key) == reference.get(key) for key in keys) and ref["start_offset"] <= start < end <= ref["end_offset"]
        if not any(contains_number(extent) for extent in extents):
            raise SourceItemVisualError("The declared clause extent must contain its own source number occurrence")
    if re.match(r"[0-9A-Za-z]|[.．][0-9]", text[end:]):
        raise SourceItemVisualError("Source number reference truncates its visible identifier or selects a numeric value with a unit")
    value = {"source_sha256": baseline.source_sha256, "document_map_artifact_sha256": document_map.artifact_sha256,
        "source_item_id": source_item_id, "node_id": record.node_id, "reference": deepcopy(reference), "text": number,
        "page": page, "bbox": bbox, **native, "authority": "ORIGINAL_SOURCE_NUMBER_LOCATION"}
    if extents is not None:
        value["source_extent"] = extents
    return value


def validate_source_item_number_evidence(document_map, baseline, source_item_id, number, evidence, *, table_previews=None):
    document_map = core.DocumentMapArtifactV1.model_validate(document_map)
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if not isinstance(evidence, dict):
        raise SourceItemVisualError("Original number requires its source-bound location evidence")
    extents = evidence.get("source_extent")
    expected = _source_number_evidence(document_map, baseline, source_item_id, number, evidence.get("reference"), table_previews,
        [extent["reference"] for extent in extents] if extents is not None else None)
    if evidence != expected:
        raise SourceItemVisualError("Original-number evidence differs from actual source text or location")
    return expected


def validate_source_item_numbers(document_map, baseline, source_item_id, annotation, *, table_previews=None):
    values = annotation.get("source_numbers")
    if values is None:
        if annotation.get("source_number") and annotation.get("source_number_evidence"):
            return [{"value": annotation["source_number"], "evidence": validate_source_item_number_evidence(document_map, baseline, source_item_id,
                annotation["source_number"], annotation["source_number_evidence"], table_previews=table_previews)}]
        return []  # Legacy scalar-only annotations retain the delivery bridge's existing checks.
    if not isinstance(values, list):
        raise SourceItemVisualError("Source numbers must be an explicit ordered collection")
    output, seen = [], set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {"value", "evidence"}:
            raise SourceItemVisualError("Each source number needs a value and its compiled source evidence")
        evidence = validate_source_item_number_evidence(document_map, baseline, source_item_id, value["value"], value["evidence"], table_previews=table_previews)
        identity = core.canonical_json_sha256(evidence["reference"])
        if identity in seen:
            raise SourceItemVisualError("The same original number occurrence is repeated")
        seen.add(identity); output.append({"value": value["value"], "evidence": evidence})
    scalar = output[0]["value"] if len(output) == 1 else ""
    if annotation.get("source_number", "") != scalar:
        raise SourceItemVisualError("Single-value compatibility projection differs from the original number collection")
    return output


def compile_source_item_annotation_updates(document_map, baseline, updates, *, base_annotations=None, table_previews=None):
    """Explicit source-number/topic/context edits; no structure or source rewrite."""
    from reqpilotx.application.agent_workflow.source_item_delivery import TOPICS
    document_map = core.DocumentMapArtifactV1.model_validate(document_map)
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if baseline.source_sha256 != document_map.source_sha256 or baseline.document_map_artifact_sha256 != document_map.artifact_sha256:
        raise SourceItemVisualError("Source annotation update requires the current original Map")
    records = {record.source_record_id: record for record in baseline.records}
    if base_annotations:
        if base_annotations.get("baseline_sha256") != baseline.artifact_sha256:
            raise SourceItemVisualError("Base annotations belong to another baseline")
        if base_annotations.get("schema_version") in {"source-item-annotations/v1", "source-item-visual-readings/v1", "source-item-image-annotations/v1"}:
            base_annotations = rebind_source_item_image_annotations(document_map, baseline, base_annotations)
    values = deepcopy(base_annotations or {"schema_version": "source-item-annotations/v1", "source_sha256": baseline.source_sha256,
        "document_map_artifact_sha256": document_map.artifact_sha256, "baseline_sha256": baseline.artifact_sha256, "records": {}, "authority": "AGENT_WORKING_ANNOTATIONS"})
    if values.get("schema_version") not in {"source-item-annotations/v1", "source-item-visual-readings/v1", "source-item-image-annotations/v1"}:
        values.update(schema_version="source-item-annotations/v1", source_sha256=baseline.source_sha256,
            document_map_artifact_sha256=document_map.artifact_sha256, authority="AGENT_WORKING_ANNOTATIONS")
    if set(values.get("records", {})) - set(records) or not updates:
        raise SourceItemVisualError("Annotation updates need existing source identities and explicit changes")
    if values["schema_version"] == "source-item-annotations/v1":
        for sid, annotation in values["records"].items():
            annotation["annotation_source_evidence_sha256"] = _visual_source_evidence(records[sid])
    seen = set()
    for update_index, update in enumerate(updates):
        sid = update.get("source_item_id")
        location = f"annotation_updates[{update_index}] source_item_id={sid}"
        if sid not in records or sid in seen or set(update) - {"source_item_id", "source_number", "source_number_ref", "source_numbers", "topics", "context_source_ids", "reason"}:
            raise SourceItemVisualError(f"{location}: Annotation updates contain an unknown/duplicate source or unsupported field")
        if not str(update.get("reason", "")).strip() or not ({"source_number", "source_numbers", "topics", "context_source_ids"} & set(update)):
            raise SourceItemVisualError(f"{location}: An annotation update needs actual changed fields and a public source-grounded reason")
        seen.add(sid); annotation = values["records"].setdefault(sid, {})
        if "source_numbers" in update:
            if "source_number" in update or "source_number_ref" in update or not isinstance(update["source_numbers"], list):
                raise SourceItemVisualError(f"{location}.source_numbers: Supply either a scalar number or an explicit source-number collection")
            compiled_numbers = []
            for number_index, entry in enumerate(update["source_numbers"]):
                number_location = f"{location}.source_numbers[{number_index}]"
                if set(entry) - {"value", "source_number_ref", "source_extent"}:
                    raise SourceItemVisualError(f"{number_location}: Unknown source-number declaration fields")
                try:
                    evidence = _source_number_evidence(document_map, baseline, sid, entry["value"], entry["source_number_ref"], table_previews, entry.get("source_extent"))
                except SourceItemVisualError as exc:
                    raise SourceItemVisualError(f"{number_location}: {exc}") from exc
                compiled_numbers.append({"value": entry["value"], "evidence": evidence})
            annotation["source_numbers"] = compiled_numbers
            annotation["source_number"] = compiled_numbers[0]["value"] if len(compiled_numbers) == 1 else ""
            if len(compiled_numbers) == 1:
                annotation["source_number_evidence"] = compiled_numbers[0]["evidence"]
            else:
                annotation.pop("source_number_evidence", None)
            try:
                validate_source_item_numbers(document_map, baseline, sid, annotation, table_previews=table_previews)
            except SourceItemVisualError as exc:
                raise SourceItemVisualError(f"{location}.source_numbers: {exc}") from exc
        if "source_number" in update:
            annotation.pop("source_numbers", None)
            number = update["source_number"]
            if number:
                try:
                    annotation["source_number_evidence"] = _source_number_evidence(document_map, baseline, sid, number, update.get("source_number_ref"), table_previews)
                except SourceItemVisualError as exc:
                    raise SourceItemVisualError(f"{location}.source_number_ref: {exc}") from exc
            elif number != "" or update.get("source_number_ref"):
                raise SourceItemVisualError(f"{location}.source_number_ref: Clear an original number only with an empty string and no replacement source claim")
            else:
                annotation.pop("source_number_evidence", None)
            annotation["source_number"] = number
        elif "source_number_ref" in update:
            raise SourceItemVisualError(f"{location}.source_number_ref: A number reference must accompany the original number")
        for key, allowed in (("topics", TOPICS), ("context_source_ids", set(records))):
            if key in update:
                selected = update[key]
                if not isinstance(selected, list) or len(selected) != len(set(selected)) or set(selected) - allowed or (key == "context_source_ids" and sid in selected):
                    raise SourceItemVisualError(f"{location}.{key}: Annotation topic/context IDs must be unique, controlled and source-bound")
                annotation[key] = list(selected)
        annotation["annotation_source_evidence_sha256"] = _visual_source_evidence(records[sid])
        annotation["annotation_update_reason"] = update["reason"]
    return _seal_annotations(values)


def compile_source_item_visual_readings(document_map, baseline, readings, *, base_annotations=None):
    """Explicit image interpretation associated with existing mixed/table SRCs.

    Association is an Agent decision within the original owning Map node, not
    a claim that every image fits one physical table row. No SRC/REQ is created.
    """
    document_map = core.DocumentMapArtifactV1.model_validate(document_map)
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if baseline.document_map_artifact_sha256 != document_map.artifact_sha256 or baseline.source_sha256 != document_map.source_sha256:
        raise SourceItemVisualError("Visual readings need the current original Map")
    records = {r.source_record_id: r for r in baseline.records}
    anchors = {a.anchor_id: (n.node_id, a) for n in document_map.nodes for a in n.source_anchors if a.anchor_kind in {"IMAGE", "FIGURE"}}
    if base_annotations and base_annotations.get("baseline_sha256") != baseline.artifact_sha256:
        raise SourceItemVisualError("Base annotations belong to another baseline")
    if base_annotations and base_annotations.get("schema_version") == "source-item-image-annotations/v1":
        base_annotations = rebind_source_item_image_annotations(document_map, baseline, base_annotations)
    output = deepcopy((base_annotations or {}).get("records", {}))
    if set(output) - set(records):
        raise SourceItemVisualError("Unknown source annotation identity")
    requested = list(readings)
    explicit_ids = {reading["source_item_id"] for reading in requested}
    inherited = []
    for sid, annotation in output.items():
        interpretation = annotation.get("visual_interpretation")
        if sid in explicit_ids or not interpretation:
            continue
        record = records[sid]
        if (interpretation.get("authority") != "AGENT_VISUAL_INTERPRETATION"
                or interpretation.get("source_sha256") != baseline.source_sha256):
            raise SourceItemVisualError("An inherited visual reading has unknown original identity or authority")
        if record.image_bindings:
            if annotation.get("image_evidence_sha256") != _image_evidence_sha(record):
                raise SourceItemVisualError("An inherited IMAGE reading no longer matches its original pixels")
        elif annotation.get("source_evidence_sha256") != _visual_source_evidence(record):
            raise SourceItemVisualError("An inherited mixed-source reading no longer matches its original bindings")
        if not str(interpretation.get("text", "")).strip():
            # The image compiler permits an untranscribed source image. Absence
            # of a reading is preserved as absence, never invented prose.
            annotation.pop("visual_interpretation", None)
            annotation.pop("related_images", None)
            continue
        inherited.append({"source_item_id": sid, "image_anchor_ids": interpretation["image_anchor_ids"],
            "text": interpretation["text"], "association_reason": annotation.get("association_reason") or "This SourceItem owns the cited original image bindings.",
            "unresolved": deepcopy(interpretation.get("unresolved", []))})
    seen = set()
    for reading in [*inherited, *requested]:
        sid = reading["source_item_id"]
        if sid not in records or sid in seen:
            raise SourceItemVisualError("Visual reading source is absent or duplicated")
        seen.add(sid)
        record = records[sid]
        ids = reading["image_anchor_ids"]
        if not ids or len(ids) != len(set(ids)) or set(ids) - set(anchors):
            raise SourceItemVisualError("Visual reading needs actual unique image anchors")
        if any(anchors[i][0] != record.node_id for i in ids):
            raise SourceItemVisualError("Visual reading cannot change an original image owner")
        if not str(reading.get("text", "")).strip() or not str(reading.get("association_reason", "")).strip():
            raise SourceItemVisualError("Visual interpretation and association reason are required")
        images = [{**_binding(document_map, anchors[i][1]).model_dump(mode="json"),
                   "relation": "AGENT_CONFIRMED_IMAGE_CONTEXT", "caption": "原文图像"} for i in ids]
        prior_interpretation = output.get(sid, {}).get("visual_interpretation") or {}
        unchanged = (prior_interpretation.get("text") == reading["text"]
            and prior_interpretation.get("image_anchor_ids") == ids
            and prior_interpretation.get("unresolved", []) == reading.get("unresolved", []))
        output[sid] = {**output.get(sid, {}), "source_evidence_sha256": _visual_source_evidence(record),
            "related_images": images, "association_reason": reading["association_reason"],
            "visual_interpretation": {"text": reading["text"], "authority": "AGENT_VISUAL_INTERPRETATION",
                "source_sha256": baseline.source_sha256, "baseline_sha256": baseline.artifact_sha256,
                "source_item_id": sid, "image_anchor_ids": ids, "review_state": prior_interpretation.get("review_state", "UNREVIEWED") if unchanged else "UNREVIEWED",
                "unresolved": deepcopy(reading.get("unresolved", []))}}
    identity_history = {key: deepcopy(base_annotations[key]) for key in ("image_identity_registry", "source_display_high_watermark", "predecessor_annotations_sha256") if base_annotations and key in base_annotations}
    return _seal_annotations({**identity_history, "schema_version": "source-item-visual-readings/v1",
        "source_sha256": baseline.source_sha256, "document_map_artifact_sha256": document_map.artifact_sha256,
        "baseline_sha256": baseline.artifact_sha256, "records": output, "authority": "AGENT_WORKING_ANNOTATIONS"})


def rebind_source_item_visual_readings(document_map, baseline, annotations):
    if annotations.get("annotations_sha256") != _seal_annotations(annotations)["annotations_sha256"]:
        raise SourceItemVisualError("Visual readings changed after compilation")
    if annotations.get("source_sha256") != baseline.source_sha256 or annotations.get("document_map_artifact_sha256") != document_map.artifact_sha256:
        raise SourceItemVisualError("Visual readings belong to another original source")
    records = {r.source_record_id: r for r in baseline.records}
    if set(annotations["records"]) - set(records):
        raise SourceItemVisualError("A visual reading source has been removed")
    readings = []
    for sid, value in annotations["records"].items():
        interpretation = value.get("visual_interpretation")
        if not interpretation:
            if value.get("related_images"):
                raise SourceItemVisualError("Related visual evidence requires an explicit interpretation binding")
            continue
        if (interpretation.get("authority") != "AGENT_VISUAL_INTERPRETATION"
                or interpretation.get("source_sha256") != annotations["source_sha256"]
                or interpretation.get("baseline_sha256") != annotations["baseline_sha256"]
                or interpretation.get("source_item_id") != sid):
            raise SourceItemVisualError("Visual interpretation identity or authority changed")
        if value.get("source_evidence_sha256") != _visual_source_evidence(records[sid]):
            raise SourceItemVisualError("Visual reading is stale for changed source bindings")
        readings.append({"source_item_id": sid, "image_anchor_ids": interpretation["image_anchor_ids"],
            "text": interpretation["text"], "association_reason": value["association_reason"],
            "unresolved": interpretation.get("unresolved", [])})
    base = {**annotations, "baseline_sha256": baseline.artifact_sha256}
    rebuilt = compile_source_item_visual_readings(document_map, baseline, readings, base_annotations=base)
    for sid, value in annotations["records"].items():
        if value.get("related_images") != rebuilt["records"][sid].get("related_images"):
            raise SourceItemVisualError("Visual image pixels/bindings changed")
    return rebuilt


def rebind_source_item_annotations(previous_map, document_map, previous_baseline, baseline, annotations, *, table_previews=None,
                                  previous_table_previews=None, map_revision_receipt=None):
    """Retain proved unchanged source annotations and disclose retired bindings.

    A replaced original is not the same analysis subject. Its prior annotation
    stays in the old version; this helper never guesses which successor inherits
    a source number, graphical interpretation or contextual applicability.
    """
    previous_map = core.DocumentMapArtifactV1.model_validate(previous_map)
    document_map = core.DocumentMapArtifactV1.model_validate(document_map)
    previous_baseline = core.SourceRequirementBaselineArtifactV1.model_validate(previous_baseline)
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if previous_baseline.source_sha256 != baseline.source_sha256:
        raise SourceItemVisualError("Annotation carry cannot cross original source revisions")
    if annotations.get("baseline_sha256") != previous_baseline.artifact_sha256:
        raise SourceItemVisualError("Annotation predecessor differs from the actual previous baseline")
    map_changed = previous_map.artifact_sha256 != document_map.artifact_sha256
    changed_context = set()
    if map_changed:
        from reqpilotx.application.agent_workflow.source_item_decomposition import _map_rebind_context_changes
        changed_context = _map_rebind_context_changes(previous_baseline, baseline, map_revision_receipt,
            previous_table_previews or {}, table_previews or {})
    known_schemas = {"source-item-annotations/v1", "source-item-image-annotations/v1", "source-item-visual-readings/v1"}
    if annotations.get("schema_version") in known_schemas:
        annotations = rebind_source_item_image_annotations(previous_map, previous_baseline, annotations)
    old = {record.source_record_id: record for record in previous_baseline.records}
    new = {record.source_record_id: record for record in baseline.records}
    if set(annotations.get("records", {})) - set(old):
        raise SourceItemVisualError("Annotation history contains an unknown predecessor source")
    kept, stale, retired, context_stale = {}, [], [], []
    for sid, original in annotations.get("records", {}).items():
        if sid not in new:
            retired.append(sid); continue
        same_source = _visual_source_evidence(old[sid]) == _visual_source_evidence(new[sid])
        if map_changed and sid not in changed_context:
            same_source = old[sid].text_bindings == new[sid].text_bindings and old[sid].table_fragments == new[sid].table_fragments and old[sid].image_bindings == new[sid].image_bindings
        if sid in changed_context or not same_source:
            stale.append(sid); continue
        value = deepcopy(original)
        if map_changed:
            for key in ("source_evidence_sha256", "annotation_source_evidence_sha256"):
                if key in value:
                    value[key] = _visual_source_evidence(new[sid])
            if "image_evidence_sha256" in value:
                value["image_evidence_sha256"] = _image_evidence_sha(new[sid])
        missing_context = sorted(set(value.get("context_source_ids", [])) - set(new))
        if missing_context:
            context_stale.append({"source_item_id": sid, "retired_context_source_ids": missing_context})
            value["context_source_ids"] = [ref for ref in value["context_source_ids"] if ref in new]
        evidence = value.get("source_number_evidence")
        if evidence and previous_map.artifact_sha256 != document_map.artifact_sha256:
            try:
                value["source_number_evidence"] = _source_number_evidence(document_map, baseline, sid, value["source_number"], evidence["reference"], table_previews,
                    [extent["reference"] for extent in evidence["source_extent"]] if "source_extent" in evidence else None)
            except SourceItemVisualError:
                stale.append(sid); continue
        if value.get("source_numbers") is not None and previous_map.artifact_sha256 != document_map.artifact_sha256:
            try:
                for entry in value["source_numbers"]:
                    proof = entry["evidence"]
                    entry["evidence"] = _source_number_evidence(document_map, baseline, sid, entry["value"], proof["reference"], table_previews,
                        [extent["reference"] for extent in proof["source_extent"]] if "source_extent" in proof else None)
            except SourceItemVisualError:
                stale.append(sid); continue
        kept[sid] = value
    values = {**deepcopy(annotations), "records": kept,
        "document_map_artifact_sha256": document_map.artifact_sha256, "source_sha256": baseline.source_sha256}
    if values.get("schema_version") not in known_schemas:
        values.update(schema_version="source-item-annotations/v1", authority="AGENT_WORKING_ANNOTATIONS")
        for sid, value in values["records"].items():
            value["annotation_source_evidence_sha256"] = _visual_source_evidence(new[sid])
    values = rebind_source_item_image_annotations(document_map, baseline, _seal_annotations(values))
    return {"artifact": values, "retained_source_item_ids": sorted(kept), "stale_source_item_ids": sorted(stale),
        "retired_source_item_ids": sorted(retired), "stale_context_references": context_stale}


def compile_source_item_images(document_map, decisions, *, previous_baseline=None, previous_annotations=None, table_previews=None):
    """Compile complete image-node scopes into the existing SourceItem baseline.

    Each decision is SourceItemImageScopeCandidateV1. Every image in its packet
    belongs exactly once to an item. Unaffected source items and identities stay.
    Returns (SourceRequirementBaselineArtifactV1, validation receipt, annotations).
    """
    document_map = core.DocumentMapArtifactV1.model_validate(document_map)
    if not isinstance(decisions, list) or not decisions:
        raise SourceItemVisualError("Image compilation needs an explicit nonempty scope decision list")
    previous = core.SourceRequirementBaselineArtifactV1.model_validate(previous_baseline) if previous_baseline is not None else None
    if previous and (previous.source_sha256 != document_map.source_sha256
                     or previous.document_map_artifact_sha256 != document_map.artifact_sha256):
        raise SourceItemVisualError("Image successor must use the same source and Document Map")
    candidates = [SourceItemImageScopeCandidateV1.model_validate(decision) for decision in decisions]
    if len({candidate.node_id for candidate in candidates}) != len(candidates):
        raise SourceItemVisualError("Image node scope may be decided once in a revision")
    previous_records = {record.source_record_id: record for record in previous.records} if previous else {}
    image_history = {}
    old_annotations = {}
    high_watermark = core._baseline_display_high_watermark(previous)
    if previous_annotations is not None:
        if previous is None:
            raise SourceItemVisualError("Image annotation history needs its previous baseline")
        bound = rebind_source_item_image_annotations(document_map, previous, previous_annotations)
        image_history = dict(bound.get("image_identity_registry", {}))
        old_annotations = deepcopy(bound["records"])
        high_watermark = max(high_watermark, int(bound.get("source_display_high_watermark", 0)))
    elif any(record.image_bindings for record in previous_records.values()):
        raise SourceItemVisualError("Image revisions require their annotation/identity history")
    for record in previous_records.values():
        if record.image_bindings:
            existing = image_history.get(record.source_record_id)
            if existing not in {None, record.source_display_id}:
                raise SourceItemVisualError("Image identity history drifted")
            image_history[record.source_record_id] = record.source_display_id
    scopes = {f"image:{candidate.node_id}" for candidate in candidates}
    records = [record for record in previous_records.values()
               if not (record.image_bindings and f"image:{record.node_id}" in scopes)]
    current_display_ids = {record.source_display_id: record.source_record_id for record in records}
    new_annotations = {}
    table_relations = [r for r in (previous.table_image_relations if previous else ()) if f"image:{r.node_id}" not in scopes]
    for candidate in candidates:
        packet = build_source_item_image_scope_packet(document_map, node_id=candidate.node_id, baseline=previous, table_previews=table_previews)
        if candidate.packet_sha256 != packet["packet_sha256"]:
            raise SourceItemVisualError("Image decisions belong to a different source packet")
        if packet.get("structural_role") and candidate.items:
            raise SourceItemVisualError("Front-matter images remain readable on the whole Map block; do not create independent image SRCs")
        images = {image["anchor_id"]: core.SourceImageBindingV1.model_validate(image) for image in packet["images"]}
        table_relations.extend(core.SourceItemTableImageRelationV1.model_validate(r) for r in packet.get("table_image_relations", []))
        claimed = set()
        keys = set()
        for item in candidate.items:
            if item.item_key in keys or claimed.intersection(item.image_anchor_ids) or set(item.image_anchor_ids) - set(images):
                raise SourceItemVisualError("Image item keys/anchors overlap or do not resolve in this scope")
            keys.add(item.item_key)
            claimed.update(item.image_anchor_ids)
            bindings = tuple(sorted((images[key] for key in item.image_anchor_ids), key=lambda image: (*source_position(image), image.anchor_id) if original_format(image.native_locator) != "pdf" else (image.page, image.bbox[1], image.bbox[0], image.anchor_id)))
            identity_sha = core.canonical_json_sha256({"document_map_artifact_sha256": document_map.artifact_sha256,
                "scope_id": packet["scope_id"], "evidence_kind": "IMAGE",
                "bindings": [binding.model_dump(mode="json") for binding in bindings]})
            source_id = "source-record-" + identity_sha[:32]
            if source_id in image_history:
                display_id = image_history[source_id]
            else:
                high_watermark += 1
                display_id = f"SRC-{high_watermark:04d}"
                image_history[source_id] = display_id
            if display_id in current_display_ids and current_display_ids[display_id] != source_id:
                raise SourceItemVisualError("Image source identity collides with another current item")
            current_display_ids[display_id] = source_id
            record = core.SourceRequirementRecordV1(source_record_id=source_id, source_display_id=display_id,
                requirement_display_id="REQ-" + display_id.removeprefix("SRC-") if item.requirementness is core.Requirementness.REQUIREMENT else None,
                source_order=0, document_map_artifact_id=document_map.artifact_id,
                document_map_artifact_sha256=document_map.artifact_sha256, node_id=candidate.node_id,
                ancestor_path=tuple(packet["ancestor_path"] + [compose_source_path_segment(packet["node_label"], packet["node_title"])]),
                label=item.display_label, source_form=core.SourceRecordForm.IMAGE, image_bindings=bindings,
                pages=tuple(sorted({binding.page for binding in bindings if binding.page is not None})), requirementness=item.requirementness,
                requirement_kinds=item.requirement_kinds, non_requirement_role=item.non_requirement_role,
                confidence=item.confidence, decision_summary=item.decision_summary, reason_codes=item.reason_codes,
                evidence_state="SUFFICIENT" if item.confidence >= 0.75 else "CONFLICTING" if item.confidence >= 0.5 else "INSUFFICIENT")
            core.validate_source_image_record_against_map(document_map, record)
            records.append(record)
            old_interpretation = old_annotations.get(source_id, {}).get("visual_interpretation") or {}
            unchanged_reading = (old_interpretation.get("text") == item.visual_interpretation and old_interpretation.get("image_anchor_ids") == list(item.image_anchor_ids))
            new_annotations[source_id] = {**deepcopy(old_annotations.get(source_id, {})), "display_label_authority": "AGENT_DISPLAY_LABEL",
                "image_evidence_sha256": _image_evidence_sha(record),
                "visual_interpretation": {"text": item.visual_interpretation, "authority": "AGENT_VISUAL_INTERPRETATION",
                    "source_sha256": document_map.source_sha256, "image_anchor_ids": list(item.image_anchor_ids),
                    "review_state": old_interpretation.get("review_state", "UNREVIEWED") if unchanged_reading else "UNREVIEWED"}, "original_text_authority": "SOURCE_IMAGE_PIXELS"}
        if claimed != set(images):
            raise SourceItemVisualError("Every image anchor in the selected node scope needs one SourceItem disposition")
    ordered = tuple(record.model_copy(update={"source_order": index}) for index, record in enumerate(sorted(records, key=source_item_position)))
    values = {"revision": previous.revision + 1 if previous else 1, "status": "SLICE_COMPLETE",
        "source_sha256": document_map.source_sha256, "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "predecessor_artifact_id": previous.artifact_id if previous else "",
        "predecessor_artifact_sha256": previous.artifact_sha256 if previous else "",
        "covered_scope_ids": tuple(sorted(scopes | set(previous.covered_scope_ids if previous else ()))),
        "records": ordered, "context_dispositions": previous.context_dispositions if previous else (),
        "source_display_high_watermark": high_watermark,
        "table_image_relations": tuple(sorted(table_relations, key=lambda r: (r.node_id, r.image_binding.anchor_id))),
        "text_context_dispositions": previous.text_context_dispositions if previous else (),
        "diagnostic_codes": previous.diagnostic_codes if previous else ()}
    draft = core.SourceRequirementBaselineArtifactV1.model_construct(**values, artifact_id="source-requirement-baseline-" + "0"*32, artifact_sha256="0"*64)
    digest = core.canonical_json_sha256(core._baseline_hash_payload(draft))
    artifact = core.SourceRequirementBaselineArtifactV1(**values, artifact_id="source-requirement-baseline-" + digest[:32], artifact_sha256=digest)
    validation = core._compile_validation_receipt_from_artifact(document_map=document_map, artifact=artifact)
    active_ids = {record.source_record_id for record in artifact.records}
    annotations = _seal_annotations({"schema_version": "source-item-image-annotations/v1", "baseline_sha256": artifact.artifact_sha256,
        "source_sha256": document_map.source_sha256, "document_map_artifact_sha256": document_map.artifact_sha256,
        "source_display_high_watermark": high_watermark, "image_identity_registry": image_history,
        "records": {**{key: value for key, value in old_annotations.items() if key in active_ids}, **new_annotations},
        "predecessor_annotations_sha256": previous_annotations.get("annotations_sha256", "") if previous_annotations else "",
        "authority": "AGENT_WORKING_ANNOTATIONS"})
    if previous_annotations and previous_annotations.get("schema_version") == "source-item-visual-readings/v1":
        annotations = compile_source_item_visual_readings(document_map, artifact, [], base_annotations=annotations)
    return artifact, validation, annotations


__all__ = ["SourceItemImageDecisionV1", "SourceItemImageScopeCandidateV1", "SourceItemVisualError",
           "build_source_item_image_scope_packet", "compile_source_item_images", "rebind_source_item_image_annotations", "image_node_table_scope_ids",
           "compile_source_item_visual_readings", "rebind_source_item_visual_readings", "compile_source_item_annotation_updates", "validate_source_item_number_evidence", "validate_source_item_numbers", "rebind_source_item_annotations"]
