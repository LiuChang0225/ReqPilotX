"""Source-bound adapters into the existing ReqPilotX models and compilers.

Decision files are ephemeral Agent inputs. Durable Map, SourceItem, ledger and
delivery formats are the existing application contracts, not a plugin database.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from argparse import Namespace
from pathlib import Path
from typing import Any

from reqpilotx.application.agent_workflow import source_requirement_baseline as core
from reqpilotx.domain.extraction.source_ir import SourceIRSnapshot


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save(root: Path, name: str, payload: Any) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    path.chmod(0o600)


def new_output(path: Path | None) -> Path:
    if path is None:
        raise ValueError("--output-dir is required")
    path = path.expanduser().resolve()
    if path.exists():
        raise ValueError("Output must not exist; use a new immutable revision directory")
    path.mkdir(parents=True, mode=0o700)
    return path


def required(value: Path | None, flag: str) -> Path:
    if value is None:
        raise ValueError(f"{flag} is required")
    return value.expanduser().resolve(strict=True)


def _pdf_locator(node: Any, bounds: dict[int, list[float]]) -> tuple[int, list[float], list[float]]:
    locator = node.native_locator
    page = locator.page_number
    if locator.format.value != "pdf" or page is None or locator.bbox is None:
        raise ValueError("Final Map/SourceItem projection currently requires measured PDF locators; preserve this native locator as an explicit unsupported projection gap")
    if page not in bounds:
        raise ValueError("Capture locator points outside the pinned original PDF")
    return page, [float(value) for value in locator.bbox], bounds[page]


def _validate_map_fields(decisions):
    """Locate missing fields before binding sources or creating output files."""
    errors = []
    for collection, identity, fields in (
        ('nodes', 'node_id', ('node_id', 'kind', 'title')),
        ('logical_tables', 'scope_id', ('scope_id', 'owner_node_id', 'title', 'role', 'fragments')),
    ):
        entries = decisions.get(collection, [])
        if not isinstance(entries, list):
            errors.append(f'{collection}: expected a list')
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f'{collection}[{index}]: expected an object')
                continue
            path = f'{collection}/{entry.get(identity, index)}'
            for field in fields:
                if field not in entry:
                    hint = ''
                    if field == 'title' and collection == 'nodes':
                        hint = '; provide heading/numbered_body in a Map edit, or an explicit title with source evidence. Whole front matter may use a display title with real content/table evidence and empty label'
                    errors.append(f'{path}/{field}: required{hint}')
    if errors:
        raise ValueError('Map input fields need correction: ' + '; '.join(errors))


def _task_source_name(args, source_sha256):
    """Source filename metadata, never a substitute for quoted customer text."""
    name = getattr(args, '_source_display_name', '') or ''
    if getattr(args, 'workspace', None):
        from reqpilotx.application.agent_workflow.task_workspace import find_task_workspace
        task = find_task_workspace(explicit=args.workspace)
        if task is None or task[1]['source']['sha256'] != source_sha256:
            raise ValueError('Workspace must own the original source')
        name = task[1]['source']['display_name']
    if name and (not isinstance(name,str) or Path(name).name != name or not name.strip()):
        raise ValueError('Source display name must be a plain filename')
    return name


def compile_map(args: Namespace) -> dict:
    source_path = required(args.source, "--source")
    snapshot = SourceIRSnapshot.model_validate_json(required(args.source_ir, "--source-ir").read_text(encoding="utf-8"))
    decisions = read(required(args.input, "--input"))
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_name = _task_source_name(args, source_hash)
    edit_request, edit_binding = None, None
    if isinstance(decisions, dict) and decisions.get('schema_version') == 'reqpilot-map-edit/v1':
        from map_edit_tools import bind_map_edit
        edit_request = decisions
        compiled_request=edit_request;outline_binding=None
        if 'outline' in edit_request:
            from task_contracts import MapEditRequest
            from compact_map import expand_outline
            typed=MapEditRequest.model_validate(edit_request).to_payload()
            expanded=expand_outline(snapshot,typed.pop('outline'),source_path,source_display_name=source_name)
            compiled_request={**typed,'nodes':expanded['nodes'],'logical_tables':expanded['logical_tables']}
            outline_binding=expanded['receipt']
        decisions, edit_binding = bind_map_edit(snapshot, compiled_request, source_path, getattr(args, 'map_dir', None), source_display_name=source_name)
        if outline_binding is not None:edit_binding['outline']=outline_binding
    if source_hash != snapshot.source_sha256 or decisions.get("source_sha256") != source_hash:
        raise ValueError("Original, SourceIR and map decisions must bind the same source SHA")
    version = decisions.get('schema_version')
    if version not in {'reqpilot-map-decisions/v1', 'reqpilot-map-decisions/v2'}:
        raise ValueError('Expected reqpilot-map-decisions/v1 or v2')
    _validate_map_fields(decisions)
    ranged = version == 'reqpilot-map-decisions/v2'
    if any(getattr(node, 'pdf_text_trace', None) for node in snapshot.nodes):
        from pdf_text_tools import verify_trace_nodes
        verify_trace_nodes(snapshot, source_path)
    if ranged:
        allowed = {'schema_version','source_sha256','nodes','logical_tables','source_dispositions'}
        if set(decisions) - allowed:
            raise ValueError('Map v2 unknown top-level fields: ' + ', '.join(sorted(set(decisions)-allowed)))
    formats = {node.native_locator.format.value for node in snapshot.nodes}
    native_format = next(iter(formats)) if len(formats) == 1 and formats <= {"docx", "xlsx"} else None
    if ranged and native_format:
        raise ValueError('Map v2 ranges currently bind PDF originals; use v1 for native Office')
    if native_format:
        from reqpilotx.application.agent_workflow.office_source_evidence import verify_office_source_ir
        if verify_office_source_ir(source_path, snapshot) != native_format:
            raise ValueError("Office source format changed")
        bounds = {}
    else:
        import pymupdf
        with pymupdf.open(source_path) as pdf:
            bounds = {index + 1: [0.0, 0.0, float(page.rect.width), float(page.rect.height)] for index, page in enumerate(pdf)}
    assets = {asset.asset_id: asset for asset in snapshot.assets}
    def locator(source):
        return (None, None, None) if native_format else _pdf_locator(source, bounds)
    def native_fields(source, sequence=None):
        if not native_format:
            return {}
        return {"native_locator": source.native_locator.model_dump(mode="json", exclude_none=True),
                "source_sequence": source.source_order if sequence is None else sequence}
    source_by_id = {node.source_node_id: node for node in snapshot.nodes}
    node_specs = decisions.get("nodes", [])
    if not node_specs:
        raise ValueError("Map decisions must contain real document/section nodes")
    spec_by_id = {str(item["node_id"]): item for item in node_specs}
    if len(spec_by_id) != len(node_specs):
        raise ValueError("Map decision node IDs must be unique")
    owner_by_source: dict[str, str] = {}
    owned_ranges: dict[str, list[tuple[int, int, str]]] = {}
    compiled_nodes: dict[str, dict] = {}
    positions: dict[str, tuple[int, float, float]] = {}

    def source_position(source: Any) -> tuple[int, float, float]:
        if native_format:
            from reqpilotx.application.agent_workflow.source_locations import source_position as original_position
            from types import SimpleNamespace
            return original_position(SimpleNamespace(native_locator=source.native_locator, source_sequence=source.source_order))
        page, bbox, _ = locator(source)
        return (page, bbox[1], bbox[0], source.source_order, 0) if ranged else (page, bbox[1], bbox[0])

    def claim_range(source_id, owner, lo, hi):
        source = source_by_id.get(source_id)
        if source is None or source.table_payload is not None or source.asset_ref:
            raise ValueError(f'{owner}: text range needs a real text source, got {source_id}')
        if type(lo) is not int or type(hi) is not int or not 0 <= lo < hi <= len(source.raw_text):
            raise ValueError(f'{owner}: invalid half-open text range {source_id}[{lo}:{hi}]; length={len(source.raw_text)}')
        prior = owned_ranges.setdefault(source_id, [])
        for a, b, previous in prior:
            if lo < b and a < hi:
                raise ValueError(f'{owner}: text range {source_id}[{lo}:{hi}] overlaps {previous}[{a}:{b}]')
        prior.append((lo, hi, owner))
        owner_by_source[source_id] = 'RANGED_TEXT'
        return source

    def claim(source_id: str, owner: str) -> Any:
        if source_id not in source_by_id:
            raise ValueError("Unknown source handle: " + source_id)
        source = source_by_id[source_id]
        if ranged and source.raw_text and source.table_payload is None and not source.asset_ref:
            return claim_range(source_id, owner, 0, len(source.raw_text))
        previous = owner_by_source.get(source_id)
        if previous and previous != owner:
            raise ValueError("Source has conflicting owners: " + source_id)
        owner_by_source[source_id] = owner
        return source_by_id[source_id]

    def text_range_entry(value, owner, is_title):
        if not isinstance(value, dict) or set(value) != {'source_node_id', 'start_offset', 'end_offset'}:
            raise ValueError(f'{owner}: source ranges require source_node_id, start_offset, end_offset')
        lo, hi = value['start_offset'], value['end_offset']
        source = claim_range(value['source_node_id'], owner, lo, hi)
        page, bbox, source_bounds = locator(source)
        lines = [line for line in source.pdf_text_lines if line.start_offset < hi and line.end_offset > lo]
        covered_geometry = {i for line in lines for i in range(max(lo, line.start_offset), min(hi, line.end_offset))}
        if any(i not in covered_geometry and not source.raw_text[i].isspace() for i in range(lo, hi)):
            lines = []
        if lines:
            boxes = [line.bbox for line in lines]
            bbox = [float(min(b[0] for b in boxes)), float(min(b[1] for b in boxes)),
                    float(max(b[2] for b in boxes)), float(max(b[3] for b in boxes))]
        return {'source': source, 'text': source.raw_text[lo:hi], 'range': [lo, hi],
                'id': f'{source.source_node_id}:{lo}:{hi}', 'is_title': is_title,
                'page': page, 'bbox': bbox, 'source_bounds': source_bounds,
                'precision': 'VISUAL_TEXT_OBSERVATION' if source.visual_text_ref else ('PDF_TEXT_DRAWING_STREAM' if source.pdf_text_trace else ('SOURCE_IR_TEXT_RANGE_LINE_BOX' if lines else 'SOURCE_IR_TEXT_RANGE_BLOCK_BOX')),
                'position': (page, bbox[1], bbox[0], source.source_order, lo)}

    for spec in node_specs:
        node_id = str(spec["node_id"])
        if ranged:
            allowed = {'node_id','parent_id','kind','title','label','title_source_ids','content_source_ids',
                       'title_source_ranges','content_source_ranges','title_reference_source_ids','evidence_refs'}
            if set(spec)-allowed:
                raise ValueError(f'{node_id}: unknown Map node fields {sorted(set(spec)-allowed)}; allowed: {sorted(allowed)}')
        if spec.get("kind") not in {"DOCUMENT", "ATTACHMENT", "SECTION", *core.FRONT_MATTER_KINDS}:
            raise ValueError("Map nodes are document units, real sections or whole COVER/TABLE_OF_CONTENTS/DOCUMENT_CONTROL blocks; tables remain content")
        parent = spec.get("parent_id")
        if parent is not None and parent not in spec_by_id:
            raise ValueError("Map parent does not resolve")
        if parent is not None and spec_by_id[parent].get("kind") in core.FRONT_MATTER_KINDS:
            raise ValueError("Front matter is a whole block, not a parent for directory-entry sections; place actual body scopes separately")
        title_sources = spec.get("title_source_ids", [])
        source_ids = list(dict.fromkeys([*title_sources, *spec.get("content_source_ids", [])]))
        selected = [claim(source_id, node_id) for source_id in source_ids]
        selected.sort(key=source_position)
        entries = []
        if not ranged and any(key in spec for key in ('title_source_ranges', 'content_source_ranges', 'title_reference_source_ids', 'evidence_refs')):
            raise ValueError(f'{node_id}: source ranges/title references require reqpilot-map-decisions/v2')
        if ranged:
            for source in selected:
                page, bbox, source_bounds = locator(source)
                entries.append({'source': source, 'text': source.raw_text, 'range': None, 'id': source.source_node_id,
                    'is_title': source.source_node_id in title_sources, 'page': page, 'bbox': bbox,
                    'source_bounds': source_bounds, 'precision': 'VISUAL_TEXT_OBSERVATION' if source.visual_text_ref else ('PDF_TEXT_DRAWING_STREAM' if source.pdf_text_trace else 'SOURCE_IR_BLOCK'), 'position': source_position(source)})
            entries += [text_range_entry(value, node_id, True) for value in spec.get('title_source_ranges', [])]
            entries += [text_range_entry(value, node_id, False) for value in spec.get('content_source_ranges', [])]
        title = str(spec["title"])
        label = str(spec.get("label", ""))
        title_text = "\n".join(source_by_id[source_id].raw_text for source_id in title_sources)
        reference_sources = []
        if ranged:
            title_text = '\n'.join(entry['text'] for entry in entries if entry['is_title'])
            for source_id in spec.get('title_reference_source_ids', []):
                source = source_by_id.get(source_id)
                if source is None or source.table_payload is not None or not source.raw_text:
                    raise ValueError(f'{node_id}: title reference must cite a real text source: {source_id}')
                reference_sources.append(source)
            if reference_sources and title_text:
                raise ValueError(f'{node_id}: use direct title evidence or auxiliary title references, not both')
            if reference_sources:
                title_text = '\n'.join(source.raw_text for source in reference_sources)
        authority = "SOURCE_VISIBLE"
        if not title_text:
            allowed_whole_label = spec["kind"] in core.FRONT_MATTER_KINDS and (bool(selected) or bool(entries) or any(t.get("owner_node_id") == node_id for t in decisions.get("logical_tables", []))) and not label
            if not allowed_whole_label and (spec["kind"] != "DOCUMENT" or title not in {source_path.name, source_name} or not title or label):
                raise ValueError(f'{node_id}: cite direct title_source_ids/ranges or v2 title_reference_source_ids. For numbered body without a separate heading, use reqpilot-map-edit/v1 numbered_body with source and label; do not turn the obligation into title-only text. Untitled DOCUMENT may use the exact filename; whole COVER/TABLE_OF_CONTENTS/DOCUMENT_CONTROL may use a display title with real content/table evidence and empty label. Do not delete the role to bypass this error.')
            authority = "AGENT_DISPLAY_LABEL"
        elif title not in title_text or (label and label not in title_text):
            raise ValueError(f'{node_id}: title/label must occur verbatim in its cited source text; preserve line breaks for split titles')
        if reference_sources:
            authority = 'SOURCE_REFERENCE'
        if any(entry['is_title'] and entry['source'].pdf_text_trace for entry in entries):
            authority = 'PDF_TEXT_OBJECT_REVIEW_REQUIRED'
        if any(entry['is_title'] and entry['source'].visual_text_ref for entry in entries):
            authority = 'VISUAL_TRANSCRIPTION_REVIEW_REQUIRED'
        content = []
        anchors = []
        for source in selected:
            if source.table_payload is not None:
                raise ValueError("A physical table must be declared in logical_tables, not flattened into source_content")
            page, bbox, source_bounds = locator(source)
            anchors.append({"anchor_id": source.source_node_id, "source_handle": source.source_node_id, "page": page, "bbox": bbox, "source_bounds": source_bounds, "anchor_kind": source.kind.value.upper() if source.kind.value in {"image", "figure"} else "SOURCE", "precision": "SOURCE_IR_BLOCK", "source_text_sha256": core.canonical_text_sha256(source.raw_text), "asset_id": source.asset_ref or "", **native_fields(source), **({"content_sha256": assets[source.asset_ref].content_sha256} if native_format and source.asset_ref else {})})
            if source.raw_text.strip():
                content.append({"content_id": source.source_node_id, "page": page, "bbox": bbox, "source_bounds": source_bounds, "source_handles": [source.source_node_id], "text": source.raw_text, "text_sha256": core.canonical_text_sha256(source.raw_text), "precision": "SOURCE_IR_BLOCK", "is_title": source.source_node_id in title_sources, **native_fields(source)})
        compiled_nodes[node_id] = {"node_id": node_id, "parent_id": parent, "kind": spec["kind"], "label": label, "title": title, "title_authority": authority, "pages": sorted({item["page"] for item in anchors if item["page"] is not None}), "source_anchors": anchors, "source_content": content, "content_blocks": []}
        positions[node_id] = min((source_position(source) for source in selected), default=(10**9, 0.0, 0.0))
        if ranged:
            content, anchors = [], []
            for entry in sorted(entries, key=lambda entry: entry['position']):
                source = entry['source']
                if source.table_payload is not None:
                    raise ValueError(f'{node_id}: captured table belongs in logical_tables')
                common = {key: entry[key] for key in ('page', 'bbox', 'source_bounds', 'precision')}
                if entry['range'] is not None:
                    common['source_text_range'] = entry['range']
                anchors.append({**common, 'anchor_id': entry['id'], 'source_handle': source.source_node_id,
                    'anchor_kind': source.kind.value.upper() if source.kind.value in {'image', 'figure'} else 'SOURCE',
                    'source_text_sha256': core.canonical_text_sha256(entry['text']), 'asset_id': source.asset_ref or ''})
                if entry['text'].strip():
                    content.append({**common, 'content_id': entry['id'], 'source_handles': [source.source_node_id],
                        'text': entry['text'], 'text_sha256': core.canonical_text_sha256(entry['text']), 'is_title': entry['is_title']})
            references = []
            for source in reference_sources:
                page, bbox, source_bounds = locator(source)
                references.append({'anchor_id': 'title-ref-' + core.canonical_json_sha256([node_id, source.source_node_id])[:32],
                    'source_handle': source.source_node_id, 'page': page, 'bbox': bbox, 'source_bounds': source_bounds,
                    'anchor_kind': 'TITLE_REFERENCE', 'precision': 'SOURCE_IR_BLOCK',
                    'source_text_sha256': core.canonical_text_sha256(source.raw_text)})
            compiled_nodes[node_id].update(source_anchors=anchors, source_content=content,
                pages=sorted({entry['page'] for entry in entries}), title_reference_anchors=references)
            positions[node_id] = min((entry['position'] for entry in entries), default=(10**9, 0.0, 0.0))
            evidence = []
            for ref in spec.get('evidence_refs', []):
                if not isinstance(ref, dict) or set(ref) - {'source_node_id', 'row_range'}:
                    raise ValueError(f'{node_id}: evidence_refs need source_node_id and optional original row_range')
                source = source_by_id.get(ref.get('source_node_id'))
                if source is None:
                    raise ValueError(f'{node_id}: unknown evidence source')
                page, box, source_bounds = locator(source)
                fields = {}
                if source.table_payload:
                    table = source.table_payload
                    lo, hi = ref.get('row_range', [0, table.row_count])
                    if type(lo) is not int or type(hi) is not int or not 0 <= lo < hi <= table.row_count:
                        raise ValueError(f'{node_id}: evidence row_range must select original table rows')
                    cells = [c for c in table.cells if c.row < hi and c.row + c.row_span > lo]
                    if not cells or any(c.row < lo or c.row + c.row_span > hi for c in cells):
                        raise ValueError(f'{node_id}: evidence row_range is empty or cuts a merged cell')
                    boxes = [locator(c)[1] for c in cells]
                    box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
                    fields = {'table_token': table.table_id, 'row_range': [lo, hi]}
                elif 'row_range' in ref:
                    raise ValueError(f'{node_id}: row_range requires table evidence')
                evidence.append({'anchor_id': 'evidence-' + core.canonical_json_sha256([node_id, ref])[:32],
                    'page': page, 'bbox': box, 'source_bounds': source_bounds, 'source_handle': source.source_node_id,
                    'source_text_sha256': core.canonical_text_sha256(source.raw_text),
                    'anchor_kind': 'EVIDENCE_REFERENCE', 'precision': 'SOURCE_IR_REFERENCE', **fields})
            if evidence:
                compiled_nodes[node_id]['evidence_anchors'] = evidence
            if any(entry['source'].pdf_text_trace for entry in entries):
                compiled_nodes[node_id]['finding_codes'] = ['PDF_DRAWING_TEXT_VISIBILITY_REQUIRES_REVIEW']
            if any(entry['source'].visual_text_ref for entry in entries):
                compiled_nodes[node_id].setdefault('finding_codes', []).append('VISUAL_TRANSCRIPTION_REQUIRES_REVIEW')

    tables = []
    previews: dict[str, dict] = {}
    table_rows: dict[str, set[int]] = {}
    for table in decisions.get("logical_tables", []):
        scope_id = str(table["scope_id"])
        if ranged and set(table)-{'scope_id','owner_node_id','title','role','fragments','agent_display_label'}:
            raise ValueError(f'{scope_id}: unknown logical-table fields')
        owner = str(table["owner_node_id"])
        if owner not in compiled_nodes or scope_id in previews:
            raise ValueError("Logical table owner must resolve and scopes must be unique")
        fragments = []
        map_fragments = []
        for item in table["fragments"]:
            if ranged and set(item)-{'source_node_id','row_range'}:
                raise ValueError(f'{scope_id}: fragment supports source_node_id and original row_range only')
            source = source_by_id.get(item["source_node_id"])
            if source is None or source.table_payload is None:
                raise ValueError("Logical fragment must refer to an existing TableIR source node")
            if source.source_node_id in owner_by_source:
                raise ValueError("Table source cannot also be a text/heading source")
            payload = source.table_payload
            lo, hi = item.get("row_range", [0, payload.row_count])
            if not (0 <= lo < hi <= payload.row_count):
                raise ValueError("Table fragment row range is invalid")
            used = table_rows.setdefault(source.source_node_id, set())
            if used.intersection(range(lo, hi)):
                raise ValueError("Physical table regions overlap")
            used.update(range(lo, hi))
            page, bbox, source_bounds = locator(source)
            cells = []
            for cell in payload.cells:
                if cell.row < hi and cell.row + cell.row_span > lo:
                    if cell.row < lo or cell.row + cell.row_span > hi:
                        raise ValueError("A proposed logical table boundary cuts a merged cell")
                    _, cell_box, _ = locator(cell)
                    cells.append({"cell_id": cell.cell_id, "row": cell.row - lo if ranged else cell.row, "column": cell.column, "row_span": cell.row_span, "column_span": cell.column_span, "text": cell.raw_text, "bbox": cell_box, **native_fields(cell, source.source_order)})
            if ranged:
                if not cells:
                    raise ValueError(f'{scope_id}: selected original rows [{lo}:{hi}] contain no captured cells')
                boxes = [cell['bbox'] for cell in cells]
                bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
            fragment_id = f"{payload.table_id}:{lo}:{hi}"
            fragments.append({"fragment_id": fragment_id, "table_token": payload.table_id, "page": page, "row_range": [lo, hi], "row_count": hi - lo if ranged else payload.row_count, "column_count": payload.column_count, "bbox": bbox, "source_bounds": source_bounds, "cells": cells, **native_fields(source)})
            map_fragments.append({"table_token": payload.table_id, "page": page, "row_range": [lo, hi], **native_fields(source)})
            node = compiled_nodes[owner]
            node["pages"] = sorted(set(node["pages"]) | ({page} if page is not None else set()))
            node["source_anchors"].append({"anchor_id": fragment_id, "source_handle": source.source_node_id, "page": page, "bbox": bbox, "source_bounds": source_bounds, "anchor_kind": "TABLE", "precision": "SOURCE_IR_TABLE", "logical_table_scope_id": scope_id, "table_token": payload.table_id, "row_range": [lo, hi], **native_fields(source)})
            positions[owner] = min(positions[owner], (page, bbox[1], bbox[0], source.source_order, lo) if ranged else source_position(source))
        unit_id = owner
        seen = set()
        while compiled_nodes[unit_id]["parent_id"]:
            if unit_id in seen:
                raise ValueError("Map parent cycle")
            seen.add(unit_id)
            unit_id = compiled_nodes[unit_id]["parent_id"]
        tables.append({"scope_id": scope_id, "title": str(table["title"]), "owner_unit_id": unit_id, "role": str(table["role"]), "pages": sorted({item["page"] for item in fragments if item["page"] is not None}), "fragments": map_fragments, "source_numbering_anomaly": table.get("source_numbering_anomaly", ""), "capture_gap": table.get("capture_gap", "")})
        if "agent_display_label" in table:
            # This is an explicit display decision, never an inferred source title.
            label = table["agent_display_label"]
            if not isinstance(label, str) or not label.strip():
                raise ValueError("agent_display_label must be a nonempty explicit Agent display decision")
            tables[-1]["agent_display_label"] = label
        previews[scope_id] = {"source_sha256": source_hash, "logical_table_scope_id": scope_id, "fragments": fragments}
        compiled_nodes[owner]["content_blocks"].append({"kind": "LOGICAL_TABLE", "logical_table_scope_id": scope_id})

    for _ in range(len(compiled_nodes)):
        for node in compiled_nodes.values():
            if node["parent_id"]:
                parent = compiled_nodes[node["parent_id"]]
                parent["pages"] = sorted(set(parent["pages"]) | set(node["pages"]))
                positions[parent["node_id"]] = min(positions[parent["node_id"]], positions[node["node_id"]])
    for node in compiled_nodes.values():
        if positions[node["node_id"]][0] >= 10**9:
            raise ValueError("A structural node needs source evidence directly or through children")
    def depth(node_id: str) -> int:
        seen = set()
        while node_id:
            if node_id in seen:
                raise ValueError("Map parent cycle")
            seen.add(node_id)
            node_id = compiled_nodes[node_id]["parent_id"]
        return len(seen)
    ordered = sorted(compiled_nodes.values(), key=lambda item: (positions[item["node_id"]], depth(item["node_id"])))
    for order, node in enumerate(ordered):
        node["order"] = order
    dispositions = decisions.get("source_dispositions", [])
    for entry in dispositions:
        if entry.get("disposition") not in {"PAGE_FURNITURE", "DUPLICATE_CAPTURE", "NON_BUSINESS", "UNRESOLVED"} or not entry.get("reason"):
            raise ValueError("Source dispositions need an explicit role and evidence-based reason")
        if entry["source_node_id"] in table_rows:
            raise ValueError("A captured table cannot be both mapped content and discarded source")
        if ranged and ('start_offset' in entry or 'end_offset' in entry):
            claim_range(entry['source_node_id'], 'disposition:' + entry['disposition'], entry.get('start_offset'), entry.get('end_offset'))
        else:
            claim(entry["source_node_id"], "disposition:" + entry["disposition"])
    partial_table_nodes = []
    for source_id, rows in table_rows.items():
        payload = source_by_id[source_id].table_payload
        if rows != set(range(payload.row_count)):
            partial_table_nodes.append(source_id)
    # DOCX emits paragraph nodes inside cells as well as the native TableIR.
    # Prove these aliases against the original cell and its mapped row; do not
    # duplicate the words as standalone source items or discard child images.
    table_cell_aliases = []
    if native_format == "docx":
        for source in snapshot.nodes:
            parent = source_by_id.get(source.parent_source_node_id)
            loc = source.native_locator
            if parent is None or parent.source_node_id not in table_rows or source.kind.value not in {"paragraph", "list_item", "heading"}:
                continue
            cells = [cell for cell in parent.table_payload.cells if cell.row == loc.row_index and cell.column == loc.column_index]
            if loc.row_index not in table_rows[parent.source_node_id] or not cells or source.raw_text not in cells[0].raw_text:
                raise ValueError("Native table paragraph does not resolve to its actual mapped cell: " + source.source_node_id)
            if source.source_node_id in owner_by_source:
                raise ValueError("A native table paragraph cannot also be standalone Map content: " + source.source_node_id)
            table_cell_aliases.append({"source_node_id": source.source_node_id, "table_source_node_id": parent.source_node_id,
                "cell_id": cells[0].cell_id, "native_locator": loc.model_dump(mode="json", exclude_none=True)})
    uncovered = sorted(set(source_by_id) - set(owner_by_source) - set(table_rows) - {item["source_node_id"] for item in table_cell_aliases})
    unresolved = [entry["source_node_id"] for entry in dispositions if entry["disposition"] == "UNRESOLVED"]
    partial_text_nodes = []
    if ranged:
        for source_id, ranges in owned_ranges.items():
            covered = {i for lo, hi, _ in ranges for i in range(lo, hi)}
            if any(i not in covered and not character.isspace() for i, character in enumerate(source_by_id[source_id].raw_text)):
                partial_text_nodes.append(source_id)
    # Native cells do not automatically cover an adapter's supplemental notes.
    # Preserve these as explicit gaps unless the exact words already occur in
    # source content owned by the Map. This does not invent new note geometry.
    owned_texts = [content["text"] for node in compiled_nodes.values() for content in node["source_content"]]
    supplemental_gaps = []
    native_capture_metadata = []
    for source_id in table_rows:
        source = source_by_id[source_id]
        for index, note in enumerate(source.table_payload.notes):
            if native_format == "xlsx" and note.split("=", 1)[0] in {"sheet_state", "used_region", "hidden_rows", "hidden_columns", "excel_table"}:
                # The XLSX adapter creates these structural metadata values;
                # replay above verified them against the original package.
                native_capture_metadata.append({"source_node_id": source_id, "value": note})
                continue
            if note.strip() and not any(note in text for text in owned_texts):
                supplemental_gaps.append({"source_node_id": source_id, "kind": "TABLE_NOTE", "note_index": index, "reason": "Exact table note lacks a locatable Map content binding; inspect the original."})
    capture_gaps = [item.model_dump(mode="json") for item in snapshot.diagnostics]
    evidence_state = "INSUFFICIENT" if uncovered or unresolved or partial_table_nodes or partial_text_nodes or supplemental_gaps or snapshot.status.value != "COMPLETE" else "SUFFICIENT"
    public_reference = {"schema_version": "native-structure-reference-public/v1", "source_sha256": source_hash, **({"source_format": native_format} if native_format else {}), "artifact_id": "portable-map-" + core.canonical_json_sha256(decisions)[:32], "document_tree": {"nodes": ordered, "root_ids": [node["node_id"] for node in ordered if not node["parent_id"]]}, "logical_tables": tables, "structure_proposals": [], "continuation": {"evidence_state": evidence_state}}
    artifact = core.compile_document_map_artifact(public_reference)
    text_requests = [core.build_source_item_text_scope_request(artifact, node_id=node.node_id) for node in artifact.nodes if node.source_content]
    table_requests = [core.build_source_requirement_scope_request(artifact, preview) for preview in previews.values()]
    output = new_output(args.output_dir)
    save(output, "document-map.json", artifact.model_dump(mode="json"))
    save(output, "native-structure-reference.json", public_reference)
    save(output, "table-previews.json", previews)
    save(output, "scope-requests.json", {"text_requests": [item.model_dump(mode="json") for item in text_requests], "table_requests": [item.model_dump(mode="json") for item in table_requests]})
    save(output, "map-decisions.json", decisions)
    save(output, "map-source-ir.json", snapshot.model_dump(mode="json"))
    source_copy = output / "map-source" / source_path.name
    source_copy.parent.mkdir(mode=0o700)
    shutil.copyfile(source_path, source_copy); source_copy.chmod(0o600)
    receipt = {"schema_version": "reqpilot-map-compilation/v1", "source_sha256": source_hash, "document_map_artifact_id": artifact.artifact_id, "document_map_artifact_sha256": artifact.artifact_sha256, "source_ir_id": snapshot.source_ir_id, "capture_status": snapshot.status.value, "evidence_state": evidence_state, "uncovered_source_node_ids": uncovered, "unresolved_source_node_ids": unresolved, "partially_covered_table_source_ids": partial_table_nodes, "table_supplemental_gaps": supplemental_gaps, "source_dispositions": dispositions, "capture_diagnostics": capture_gaps, "human_acceptance": "NOT_CLAIMED", "semantic_verification": "NOT_RUN"}
    if ranged:
        receipt.update(decision_schema=version, table_row_coordinates='FRAGMENT_LOCAL_WITH_ORIGINAL_ROW_RANGE',
                       partially_covered_text_source_ids=sorted(partial_text_nodes))
    if native_format:
        receipt["table_cell_source_aliases"] = table_cell_aliases
        receipt["native_capture_metadata"] = native_capture_metadata
    receipt["source_inputs"] = {"source_path": str(source_copy.relative_to(output)), "source_ir_path": "map-source-ir.json", "decisions_path": "map-decisions.json",
        "source_ir_sha256": hashlib.sha256((output / "map-source-ir.json").read_bytes()).hexdigest(), "decisions_sha256": hashlib.sha256((output / "map-decisions.json").read_bytes()).hexdigest()}
    if source_name: receipt['source_inputs']['source_display_name'] = source_name
    save(output, "map-compilation-receipt.json", receipt)
    if edit_binding is not None:
        save(output, 'map-edit-request.json', edit_request)
        save(output, 'map-edit-binding.json', edit_binding)
    from map_review_tools import map_review, review_response
    review_source_path = output/'map-source-ir.json'
    if review_source_path.is_relative_to(Path.cwd()):
        review_source_path = review_source_path.relative_to(Path.cwd())
    review = map_review(artifact, previews, receipt, snapshot, decisions, source_ir_path=review_source_path)
    from map_audit_tools import build_map_audit
    audit = build_map_audit(snapshot.model_dump(mode='json'), artifact.model_dump(mode='json'), previews, decisions,
        page_count=len(bounds) if not native_format else None)
    save(output, 'map-audit.json', audit)
    save(output, 'source-findings.json', audit['source_findings'])
    review['source_audit'] = {'summary': audit['summary'], 'pages': audit['pages'],
        'numbered_body_groups': audit['numbered_body_groups'],
        'numbered_parent_change_leads': audit['numbered_parent_change_leads'],
        'audit_read': {'path': str(output / 'map-audit.json'), 'json_pointer': ''},
        'next': 'Inspect gaps on the authorized scope pages and the expanded tree order before claiming scope completion. CLI: audit-map --map-dir <this-output> --page <page>. Parallel captures are review leads, not automatically missing obligations.'}
    save(output, 'map-review.json', review)
    return {"status": "COMPILED", "evidence_state": evidence_state, "node_count": len(artifact.nodes), "logical_table_count": len(tables), "text_scope_count": len(text_requests), "uncovered_source_count": len(uncovered), "output_dir": str(output), "semantic_verification": "NOT_RUN", **review_response(review, output)}


def _compile_items_with_guidance(operation, **kwargs):
    try:
        return operation(**kwargs)
    except core.SourceRequirementBaselineError as exc:
        if str(exc) != 'SOURCE_ITEM_STRUCTURE_ONLY_SPAN_NOT_TITLE':
            raise
        raise ValueError(f'{exc}: scope={getattr(exc,"scope_id",None)}, node={getattr(exc,"node_id",None)}, '
            f'content_id={getattr(exc,"content_id",None)}. The current Map does not identify this span as a title. '
            'Decide its role from the original: actual explanatory/normative body needs a SourceItem; '
            'a pure heading authored as body needs a local Map title/content correction. A pure table caption '
            'can stay in the logical table title with exact evidence_refs and an explicit source disposition '
            'instead of item body; preserve original access and any operative text. Regenerate scope requests '
            'after the local Map correction. Do not invent a caption chapter or create a NON_REQUIREMENT SRC solely to silence '
            'the validator, and do not mark substantive body as title. Preserve unaffected content and prior versions.') from exc


def _canonical_item_reason_codes(value):
    """Order set-like transport codes; source arrays and semantic choices stay intact."""
    if isinstance(value, list):
        return [_canonical_item_reason_codes(item) for item in value]
    if isinstance(value, dict):
        return {key: sorted(set(item)) if key == 'reason_codes' and isinstance(item,list) and all(isinstance(code,str) for code in item)
                else _canonical_item_reason_codes(item) for key,item in value.items()}
    return value


def compile_items(args: Namespace) -> dict:
    directory = required(args.map_dir, "--map-dir")
    artifact = core.DocumentMapArtifactV1.model_validate_json((directory / "document-map.json").read_text(encoding="utf-8"))
    scopes = read(directory / "scope-requests.json")
    if artifact.source_format:
        from reqpilotx.application.agent_workflow.office_source_evidence import verify_office_map_against_source
        source = getattr(args, "source", None)
        if source is None:
            inputs = read(directory / "map-compilation-receipt.json").get("source_inputs", {})
            source = (directory / inputs["source_path"]).resolve(strict=True)
            if directory.resolve() not in source.parents:
                raise ValueError("Office Map original must be within its source-input directory or explicitly provided with --source")
        verify_office_map_against_source(source, artifact, read(directory / "table-previews.json"))
    decisions = read(required(args.input, "--input"))
    if decisions.get("schema_version") != "reqpilot-item-decisions/v1" or decisions.get("source_sha256") != artifact.source_sha256 or decisions.get("document_map_artifact_sha256") != artifact.artifact_sha256:
        raise ValueError("Item decisions must bind this source and exact Document Map")
    text_by_id = {item["scope_id"]: core.SourceItemTextScopeRequestV1.model_validate(item) for item in scopes["text_requests"]}
    table_by_id = {item["logical_table_scope_id"]: core.SourceRequirementScopeRequestV1.model_validate(item) for item in scopes["table_requests"]}
    text_requests, text_candidates, table_requests, table_candidates = [], [], [], []
    seen = set()
    for selection in decisions.get("text_decisions", []):
        scope_id = selection["scope_id"]
        if scope_id in seen:
            raise ValueError("Duplicate text scope decisions")
        seen.add(scope_id)
        request = text_by_id[scope_id]
        values = {key: value for key, value in selection.items() if key != "scope_id"}
        values.update({"source_sha256": artifact.source_sha256, "document_map_artifact_id": artifact.artifact_id, "document_map_artifact_sha256": artifact.artifact_sha256, "request_sha256": request.request_sha256, "scope_id": scope_id})
        text_requests.append(request)
        text_candidates.append(core.SourceItemTextSliceCandidateV1.model_validate(_canonical_item_reason_codes(values)))
    for selection in decisions.get("table_decisions", []):
        scope_id = selection["logical_table_scope_id"]
        if scope_id in seen:
            raise ValueError("Duplicate table scope decisions")
        seen.add(scope_id)
        request = table_by_id[scope_id]
        values = {key: value for key, value in selection.items() if key != "logical_table_scope_id"}
        values.update({"source_sha256": artifact.source_sha256, "document_map_artifact_id": artifact.artifact_id, "document_map_artifact_sha256": artifact.artifact_sha256, "request_sha256": request.request_sha256, "logical_table_scope_id": scope_id})
        table_requests.append(request)
        table_candidates.append(core.SourceRequirementSliceCandidateV1.model_validate(_canonical_item_reason_codes(values)))
    previous = None
    growth = None
    if args.baseline_dir:
        previous = core.SourceRequirementBaselineArtifactV1.model_validate_json((args.baseline_dir / "source-requirement-baseline.json").read_text(encoding="utf-8"))
        if set(previous.covered_scope_ids).intersection(seen):
            raise ValueError("Existing scopes require an explicit review-revise scope_revision; compile-items only grows previously uncovered scopes")
        previous_receipt = core.SourceRequirementBaselineValidationReceiptV1.model_validate(read(args.baseline_dir / "validation-receipt.json"))
        baseline, validation, growth = _compile_items_with_guidance(core.grow_source_requirement_baseline, document_map=artifact, previous_artifact=previous, previous_validation_receipt=previous_receipt,
            requests=tuple(table_requests), candidates=tuple(table_candidates), text_requests=tuple(text_requests), text_candidates=tuple(text_candidates))
    else:
        baseline, validation = _compile_items_with_guidance(core.compile_source_requirement_baseline, document_map=artifact, requests=tuple(table_requests), candidates=tuple(table_candidates), text_requests=tuple(text_requests), text_candidates=tuple(text_candidates), status="SLICE_COMPLETE")
    output = new_output(args.output_dir)
    for name in ("document-map.json", "table-previews.json", "scope-requests.json", "map-compilation-receipt.json"):
        if (directory / name).is_file():
            shutil.copyfile(directory / name, output / name)
    save(output, "source-requirement-baseline.json", baseline.model_dump(mode="json"))
    save(output, "validation-receipt.json", validation.model_dump(mode="json"))
    if growth:
        from copy import deepcopy
        from revision_tools import state, carry_state, validate_state, STATE_FILES
        from reqpilotx.application.agent_workflow.source_item_visual import rebind_source_item_annotations
        old_values = state(args.baseline_dir); values = deepcopy(old_values); tables = read(directory / "table-previews.json")
        if values.get("annotations"):
            values["annotations"] = rebind_source_item_annotations(artifact, artifact, previous, baseline, values["annotations"], table_previews=tables)["artifact"]
        carried = carry_state(artifact, previous, baseline, tables, old_values, values)
        evidence_path = args.baseline_dir / "knowledge-catalog.json"
        validate_state(artifact, baseline, tables, values, evidence_path)
        for key, value in values.items():
            save(output, STATE_FILES[key], value)
        if evidence_path.is_file():
            from evidence_tools import copy_registration
            copy_registration(evidence_path, output)
        save(output, "growth-receipt.json", growth.model_dump(mode="json")); save(output, "carry-forward.json", carried)
    missing = sorted((set(text_by_id) | set(table_by_id)) - set(baseline.covered_scope_ids))
    save(output, "item-compilation-receipt.json", {"schema_version": "reqpilot-item-compilation/v1", "source_sha256": artifact.source_sha256, "baseline_artifact_id": baseline.artifact_id, "baseline_artifact_sha256": baseline.artifact_sha256, "unprocessed_scope_ids": missing, "semantic_verification": "NOT_RUN", "full_source_completion": "NOT_CLAIMED", "human_acceptance": "NOT_CLAIMED"})
    return {"status": baseline.status, "compilation_mode": "SOURCE_ORDER_GROWTH" if growth else "INITIAL", "source_item_count": len(baseline.records), "requirement_count": validation.requirement_count, "non_requirement_count": validation.non_requirement_count, "unprocessed_scope_count": len(missing), "output_dir": str(output), "semantic_verification": "NOT_RUN"}


def schemas(args: Namespace) -> dict:
    names = ("DocumentMapArtifactV1", "SourceItemTextSliceCandidateV1", "SourceRequirementSliceCandidateV1", "SourceRequirementBaselineArtifactV1", "SourceRequirementClassificationCoordinationDecisionV1")
    payload = {name: getattr(core, name).model_json_schema() for name in names}
    from reqpilotx.application.agent_workflow.source_item_visual import SourceItemImageScopeCandidateV1
    payload["SourceItemImageScopeCandidateV1"] = SourceItemImageScopeCandidateV1.model_json_schema()
    if args.output_dir:
        output = new_output(args.output_dir)
        for name, value in payload.items():
            save(output, name + ".schema.json", value)
        return {"status": "WRITTEN", "schemas": list(payload), "output_dir": str(output)}
    return payload


def inspect_source(args: Namespace) -> dict:
    snapshot = SourceIRSnapshot.model_validate_json(required(args.source_ir, "--source-ir").read_text(encoding="utf-8"))
    if getattr(args, 'view', 'raw') == 'authoring':
        from source_authoring_tools import authoring_view
        return authoring_view(snapshot, args)
    if getattr(args, 'query', None) or getattr(args, 'capture_index', None):
        raise ValueError('query/capture_index require --view authoring')
    pages = set(args.page or [])
    ids = set(args.source_id or [])
    known = {node.source_node_id for node in snapshot.nodes}
    if ids - known:
        raise ValueError("Unknown current SourceIR handle: " + ", ".join(sorted(ids - known)))
    selected = [node for node in snapshot.nodes if (not pages or node.native_locator.page_number in pages) and (not ids or node.source_node_id in ids)]
    if pages or ids:
        nodes = [node.model_dump(mode="json") for node in selected]
    else:
        nodes = [{"source_node_id": node.source_node_id, "kind": node.kind.value, "native_locator": node.native_locator.model_dump(mode="json"), "text_characters": len(node.raw_text), "text_preview": node.raw_text[:160], "table_id": node.table_payload.table_id if node.table_payload else None} for node in selected]
    return {"schema_version": "reqpilot-source-inspection/v1", "source_sha256": snapshot.source_sha256, "source_ir_id": snapshot.source_ir_id, "capture_status": snapshot.status.value, "mode": "FULL_SELECTED_EVIDENCE" if pages or ids else "NAVIGATION_ONLY", "nodes": nodes, "diagnostics": [item.model_dump(mode="json") for item in snapshot.diagnostics if not pages or item.native_locator is None or item.native_locator.page_number in pages], "semantic_authority": "NONE"}


def analysis_policy_input(value, source_sha256, baseline_sha256=None):
    """Normalize an explicit policy import, never reinterpret stored review state."""
    allowed={'schema_version','source_sha256','baseline_sha256','status','reason','reason_code','note','mode','compliance_claims','human_acceptance'}
    if not isinstance(value,dict) or set(value)-allowed:
        raise ValueError('Analysis policy has unknown fields; use status and a declared reason/reason_code')
    if value.get('source_sha256') != source_sha256:
        raise ValueError('Analysis policy must bind the original source and an explicit execution choice')
    if 'baseline_sha256' in value and value['baseline_sha256'] != baseline_sha256:
        raise ValueError('Analysis policy baseline differs from the current import baseline')
    if value.get('schema_version','reqpilot-analysis-policy/v1') != 'reqpilot-analysis-policy/v1':
        raise ValueError('Unsupported analysis policy schema version')
    status=value.get('status');reason=value.get('reason');code=value.get('reason_code');note=value.get('note')
    if not isinstance(status,str) or any(v is not None and not isinstance(v,str) for v in (reason,code,note)):
        raise ValueError('Analysis policy status, reasons and note must be text')
    if value.get('mode') is not None and value['mode'] != ('SKIP' if status=='SKIPPED' else 'ANALYZE'):
        raise ValueError('Analysis policy mode conflicts with its declared status')
    codes={'','USER_SKIPPED','NO_INTERNAL_EVIDENCE'}
    if code is not None:
        if code not in codes or reason in codes and reason != code:
            raise ValueError('Analysis policy reason and reason_code conflict or are unsupported')
        if reason and reason not in codes:
            note='\n'.join(dict.fromkeys(v for v in (reason,note) if v))
        reason=code
    if status not in {'AVAILABLE','SKIPPED'} or reason not in codes or status=='SKIPPED' and not reason:
        raise ValueError('Analysis policy requires AVAILABLE or SKIPPED and a supported reason code')
    if status=='AVAILABLE' and (reason or value.get('mode')=='SKIP'):
        raise ValueError('Available analysis policy conflicts with a skip declaration')
    if value.get('compliance_claims',0) != 0 or value.get('human_acceptance','NOT_CLAIMED') != 'NOT_CLAIMED':
        raise ValueError('Analysis policy cannot import compliance or human acceptance claims')
    result={'source_sha256':source_sha256,'status':status,'reason':reason}
    if note:
        if not isinstance(note,str):raise ValueError('Analysis policy note must be text')
        result['note']=note
    return result


def deliver(args: Namespace) -> dict:
    # The API/UI and portable plugin call this same pure bridge.
    from reqpilotx.application.agent_workflow import source_item_delivery as delivery
    directory = required(args.baseline_dir, "--baseline-dir")
    document_map = core.DocumentMapArtifactV1.model_validate_json((directory / "document-map.json").read_text(encoding="utf-8"))
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate_json((directory / "source-requirement-baseline.json").read_text(encoding="utf-8"))
    source_path = args.source.expanduser().resolve(strict=True) if args.source else None
    if source_path and hashlib.sha256(source_path.read_bytes()).hexdigest() != baseline.source_sha256:
        raise ValueError("Export original differs from the source bound to this baseline")
    if document_map.source_format:
        if source_path is None:
            raise ValueError("Office delivery requires the original DOCX/XLSX for native location verification")
        from reqpilotx.application.agent_workflow.office_source_evidence import verify_office_map_against_source
        verify_office_map_against_source(source_path, document_map, read(directory / "table-previews.json"), baseline=baseline)
    review = read(args.input) if args.input else {}
    if set(review) & {'manual_decisions', 'engineer_responses'}:
        raise ValueError('Inline export JSON cannot supply engineer opinions; use a verified user-import artifact')
    explicit_annotations = getattr(args, "annotations", None)
    if explicit_annotations:
        if review.get("annotations") is not None:
            raise ValueError("Supply the annotation artifact once")
        from reqpilotx.application.agent_workflow.source_item_visual import rebind_source_item_image_annotations
        review["annotations"] = rebind_source_item_image_annotations(document_map, baseline, read(explicit_annotations))
    image_annotations_path = directory / "image-annotations.json"
    if image_annotations_path.is_file() and not explicit_annotations:
        from reqpilotx.application.agent_workflow.source_item_visual import rebind_source_item_image_annotations
        image_annotations = rebind_source_item_image_annotations(document_map, baseline, read(image_annotations_path))
        supplied_annotations = review.get("annotations")
        if supplied_annotations is None:
            review["annotations"] = image_annotations
        else:
            if supplied_annotations.get("baseline_sha256") != baseline.artifact_sha256:
                raise ValueError("Review annotations belong to a different source baseline")
            merged = dict(image_annotations["records"])
            for source_id, values in supplied_annotations.get("records", {}).items():
                merged[source_id] = {**merged.get(source_id, {}), **values}
            review["annotations"] = {"baseline_sha256": baseline.artifact_sha256, "records": merged}
    if getattr(args, "decomposition", None):
        if review.get("decomposition") is not None:
            raise ValueError("Supply the sealed decomposition once, via --decomposition or review JSON")
        review["decomposition"] = read(args.decomposition)
    elif review.get("decomposition") is None and (directory / 'source-item-decomposition.json').is_file():
        review['decomposition'] = read(directory / 'source-item-decomposition.json')
    assessment_path = getattr(args, 'assessments', None)
    if getattr(args, "assessments", None):
        if review.get("assessments") is not None:
            raise ValueError("Supply the assessment artifact once")
        review["assessments"] = read(args.assessments)
    elif review.get('assessments') is None and (directory / 'source-item-assessments.json').is_file():
        assessment_path = directory / 'source-item-assessments.json'
        review['assessments'] = read(assessment_path)
    if review.get("assessments") is not None:
        from reqpilotx.application.agent_workflow.source_item_analysis import validate_source_item_assessments
        packet_path = getattr(args, "analysis_packet", None)
        if packet_path is None and assessment_path is not None:
            packet_path = assessment_path.parent / "analysis-packet.json"
        if packet_path is None:
            raise ValueError("Every assessment input requires its sealed artifact and exact --analysis-packet")
        packet = read(packet_path)
        from evidence_tools import verify_packet_evidence, verify_source_packet
        catalog_path = verify_packet_evidence(packet, packet_path)
        if catalog_path is None:
            from revision_tools import empty_catalog_from_packet
            catalog = empty_catalog_from_packet(packet)
        else:
            catalog = read(catalog_path)
        verify_source_packet(document_map, baseline, read(directory / "table-previews.json"), packet, catalog,
            annotations=review.get("annotations"), decomposition=review.get("decomposition"))
        assessments = review["assessments"]
        validate_source_item_assessments(packet, assessments)
        if assessments["baseline_sha256"] != baseline.artifact_sha256:
            raise ValueError("Assessment artifact belongs to a different baseline")
        if assessments.get("decomposition_sha256", "") != (review.get("decomposition") or {}).get("artifact_sha256", ""):
            raise ValueError("Assessment artifact belongs to a different decomposition")
        review["assessments"] = assessments
    manual = None
    explicit_manual = getattr(args, 'manual_decisions', None)
    manual_path = (explicit_manual or directory / 'manual-decisions.json').expanduser().resolve()
    if explicit_manual and not manual_path.is_file():
        raise ValueError('The requested engineer-opinion artifact is missing')
    if manual_path.is_file():
        from reqpilotx.application.agent_workflow.source_item_manual_decisions import build_manual_decision_context, validate_manual_decisions
        context = build_manual_decision_context(document_map, baseline, read(directory / 'table-previews.json'),
            decomposition=review.get('decomposition'), annotations=review.get('annotations'))
        if manual_path.parent != directory:
            from manual_tools import verify_import
            previous_path = manual_path.parent / 'previous-manual-decisions.json'
            manual = verify_import(manual_path, context, read(previous_path) if previous_path.is_file() else None)
        else:
            manual = validate_manual_decisions(read(manual_path), context)
    explicit_policy = getattr(args, 'analysis_policy', None)
    policy_path = required(explicit_policy, '--analysis-policy') if explicit_policy else directory / 'analysis-policy.json'
    if explicit_policy and not policy_path.is_file():
        raise ValueError('The explicitly supplied analysis policy file is missing')
    policy = read(policy_path) if policy_path.exists() else None
    if explicit_policy:
        policy = analysis_policy_input(policy, baseline.source_sha256, baseline.artifact_sha256)
    snapshot = delivery.build_delivery_snapshot(document_map=document_map, baseline=baseline, table_previews=read(directory / "table-previews.json"), annotations=review.get("annotations"), decomposition=review.get("decomposition"), assessments=review.get("assessments"), manual_decisions=manual, analysis_policy=policy)
    source_name = _task_source_name(args, baseline.source_sha256)
    output = new_output(args.output_dir)
    save(output, "matrix-snapshot.json", snapshot)
    from reqpilotx.application.agent_workflow.source_item_workbench_metadata import numbering_findings
    source_findings = numbering_findings(document_map)
    source_findings['baseline_sha256'] = baseline.artifact_sha256
    save(output, 'source-findings.json', source_findings)
    image_export_receipt = {}
    workbook = delivery.export_delivery_xlsx(snapshot, source_file_path=str(source_path) if source_path else "", source_sha256=baseline.source_sha256,
        image_export_receipt=image_export_receipt, source_file_name=source_name)
    (output / "matrix.xlsx").write_bytes(workbook)
    (output / "matrix.xlsx").chmod(0o600)
    save(output, "delivery-receipt.json", {"schema_version": "reqpilot-portable-delivery/v1", "source_sha256": baseline.source_sha256, "baseline_artifact_id": baseline.artifact_id, "baseline_artifact_sha256": baseline.artifact_sha256, "xlsx_sha256": hashlib.sha256(workbook).hexdigest(), "source_document_bound_for_visual_export": source_path is not None, "image_export": image_export_receipt, "formal_customer_approval": "NOT_CLAIMED", "independent_agent_verification": "NOT_RUN"})
    return {"status": "DRAFT_EXPORTED", "output_dir": str(output), "baseline_artifact_id": baseline.artifact_id, "independent_agent_verification": "NOT_RUN",
        "source_findings_path": str(output / 'source-findings.json'), "source_findings_count": len(source_findings['findings']),
        "analysis_policy_status": policy.get('status') if policy else 'UNSPECIFIED'}


def decomposition_operation(args: Namespace) -> dict:
    from reqpilotx.application.agent_workflow.source_item_decomposition import (
        build_source_item_decomposition_packet, compile_source_item_decomposition,
    )
    directory = required(args.baseline_dir, "--baseline-dir")
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate_json((directory / "source-requirement-baseline.json").read_text(encoding="utf-8"))
    tables = read(directory / "table-previews.json")
    if args.command == "decomposition-packet":
        if not args.source_item_id:
            raise ValueError("Select one or more exact --source-item-id values from this baseline")
        packets = [build_source_item_decomposition_packet(baseline, source_id, table_previews=tables) for source_id in args.source_item_id]
        payload = {"schema_version": "reqpilot-decomposition-packets/v1", "baseline_sha256": baseline.artifact_sha256, "packets": packets}
        if args.output_dir:
            output = new_output(args.output_dir)
            save(output, "packets.json", payload)
            return {"status": "PACKETS_WRITTEN", "parent_count": len(packets), "output_dir": str(output)}
        return payload
    decision_input = read(required(args.input, "--input"))
    if decision_input.get("schema_version") != "reqpilot-decomposition-decisions/v1" or decision_input.get("baseline_sha256") != baseline.artifact_sha256:
        raise ValueError("Decomposition decisions must bind this exact source baseline")
    authorization = decision_input.get("authorization", {})
    authorized = authorization.get("source_item_ids", [])
    if not isinstance(authorization.get("request"), str) or not authorization["request"].strip() or not authorized:
        raise ValueError("Record the actual explicit user request and its authorized SourceItem scope before decomposition")
    decisions = decision_input.get("decisions")
    if not isinstance(decisions, list) or not {item["source_item_id"] for item in decisions}.issubset(set(authorized)):
        raise ValueError("Decomposition decisions exceed the explicitly authorized source scope")
    previous_path = getattr(args, "previous_decomposition", None)
    previous = read(previous_path) if previous_path else None
    artifact = compile_source_item_decomposition(baseline, decisions, table_previews=tables, previous=previous, authorized_source_item_ids=authorized)
    output = new_output(args.output_dir)
    save(output, "source-item-decomposition.json", artifact)
    save(output, "decomposition-compilation-receipt.json", {"schema_version": "reqpilot-decomposition-compilation/v1", "baseline_sha256": baseline.artifact_sha256, "artifact_sha256": artifact["artifact_sha256"], "authorization": authorization, "semantic_validation": "REQUIRES_SOURCE_REVIEW", "human_acceptance": "NOT_CLAIMED"})
    return {"status": "DECOMPOSITION_COMPILED", "review_scope": artifact["review_scope"], "scope_status": artifact["status"], "revision": artifact["revision"], "parent_count": len(artifact["records"]), "child_count": sum(len(children) for children in artifact["records"].values()), "retired_child_count": len(artifact["retired_singular_ids"]), "output_dir": str(output), "semantic_validation": "REQUIRES_SOURCE_REVIEW"}


def _empty_image_binding_note(document_map, directory, *, source_ir_path=None, node_ids=None):
    """Explain a zero owned-image result only from matching capture identities.

    Non-owning references and explicit exclusions are facts, not classifications:
    this helper never interprets disposition prose or changes the saved Map.
    """
    receipt_path = directory / 'map-compilation-receipt.json'
    try:
        receipt = read(receipt_path) if receipt_path.is_file() else {}
    except (OSError, ValueError):
        receipt = {}
    if not isinstance(receipt, dict): receipt = {}
    matches = (receipt.get('source_sha256') == document_map.source_sha256
        and receipt.get('document_map_artifact_sha256') == document_map.artifact_sha256)
    explicit_capture = source_ir_path is not None
    if not explicit_capture:
        if not matches: return None
        relative = (receipt.get('source_inputs') or {}).get('source_ir_path')
        if not relative or Path(relative).is_absolute(): return None
        source_ir_path = (directory / relative).resolve()
        if not source_ir_path.is_relative_to(directory.resolve()): return None
    try:
        source_ir_path = Path(source_ir_path).expanduser().resolve(strict=True)
        raw = source_ir_path.read_bytes()
        if not explicit_capture and hashlib.sha256(raw).hexdigest() != receipt['source_inputs'].get('source_ir_sha256'):
            return None
        snapshot = SourceIRSnapshot.model_validate_json(raw)
    except (OSError, ValueError, KeyError):
        return None
    if snapshot.source_sha256 != document_map.source_sha256: return None
    images = {node.source_node_id: node for node in snapshot.nodes if node.kind.value in {'image', 'figure'}}
    if not images: return None
    selected = set(node_ids) if node_ids else None
    owned = {anchor.source_handle for node in document_map.nodes for anchor in node.source_anchors if anchor.anchor_kind in {'IMAGE', 'FIGURE'}}
    references = {}
    for node in document_map.nodes:
        if selected is not None and node.node_id not in selected: continue
        for anchor in node.evidence_anchors:
            if anchor.source_handle in images and anchor.source_handle not in owned:
                references.setdefault(anchor.source_handle, []).append(node.node_id)
    exclusions = {value.get('source_node_id'): value['disposition'] for value in receipt.get('source_dispositions', [])
        if matches and value.get('source_node_id') in images and value.get('source_node_id') not in owned
        and value.get('disposition') in {'PAGE_FURNITURE', 'DUPLICATE_CAPTURE', 'NON_BUSINESS'}
        and (selected is None or value.get('source_node_id') in references)}
    handles = set(references) | set(exclusions)
    if not handles: return None
    entries = []
    for handle in sorted(handles, key=lambda key: images[key].source_order)[:8]:
        node = images[handle]; location = node.native_locator
        entries.append({'source_handle': handle, 'asset_id': node.asset_ref,
            'page': location.page_number, 'bbox': list(location.bbox) if location.bbox else None,
            'native_locator': location.model_dump(mode='json', exclude_none=True),
            'reference_node_ids': sorted(set(references.get(handle, []))),
            'disposition': exclusions.get(handle)})
    return {'code': 'NO_OWNED_IMAGE_BINDINGS', 'blocks_progress': False,
        'source_sha256': document_map.source_sha256, 'document_map_artifact_sha256': document_map.artifact_sha256,
        'capture_path': str(source_ir_path), 'reference_only_image_count': len(references),
        'excluded_image_count': len(exclusions), 'entries': entries, 'additional_entry_count': len(handles) - len(entries),
        'semantic_judgment': 'NOT_PERFORMED',
        'message': 'The matching capture contains images, but these images have only non-owning evidence references and/or explicit source exclusions. image-packet reads owned Map IMAGE/FIGURE anchors, so it returns none for them.',
        'next': 'Inspect the original. A supporting clause figure belongs in Map content, or as a whole image ref in numbered_body.continuations; preserve the existing text and remove only its conflicting exclusion. Then compile-annotations can add a reading on the existing clause SRC. evidence_refs alone do not bind that figure for display/reading. A verified logo or duplicate may correctly remain excluded; no extra IMAGE SRC or automatic ownership change is required.',
        'guide': 'skills/reqpilot/references/tools-images.md#supporting-figure-map-binding'}


def image_operation(args: Namespace) -> dict:
    from reqpilotx.application.agent_workflow.source_item_visual import build_source_item_image_scope_packet, compile_source_item_images
    directory = required(args.map_dir or args.baseline_dir, "--map-dir or --baseline-dir")
    document_map = core.DocumentMapArtifactV1.model_validate_json((directory / "document-map.json").read_text(encoding="utf-8"))
    previous = None
    previous_annotations = None
    tables_path = (args.baseline_dir or directory) / "table-previews.json"
    table_previews = read(tables_path) if tables_path.is_file() else {}
    if args.baseline_dir:
        previous = core.SourceRequirementBaselineArtifactV1.model_validate_json((args.baseline_dir / "source-requirement-baseline.json").read_text(encoding="utf-8"))
        annotation_path = args.baseline_dir / "image-annotations.json"
        previous_annotations = read(annotation_path) if annotation_path.is_file() else None
    if args.command == "image-packet":
        owned_nodes = {node.node_id for node in document_map.nodes if any(anchor.anchor_kind in {"IMAGE", "FIGURE"} for anchor in node.source_anchors)}
        if args.node_id and set(args.node_id) - {node.node_id for node in document_map.nodes}:
            raise ValueError('Image scope requires a real Document Map node')
        node_ids = [node.node_id for node in document_map.nodes if node.node_id in owned_nodes and (not args.node_id or node.node_id in args.node_id)]
        packets = [build_source_item_image_scope_packet(document_map, node_id=node_id, baseline=previous, table_previews=table_previews) for node_id in node_ids]
        payload = {"schema_version": "reqpilot-image-packets/v1", "source_sha256": document_map.source_sha256, "packets": packets}
        note = None
        if not any(packet['images'] or packet.get('table_image_relations') for packet in packets):
            note = _empty_image_binding_note(document_map, directory, source_ir_path=getattr(args, 'source_ir', None), node_ids=args.node_id)
            if note: payload['image_binding_note'] = note
        if not args.output_dir:
            return payload
        output = new_output(args.output_dir)
        renders = []
        image_renders, render_gaps = [], []
        if args.source:
            import pymupdf
            from PIL import Image
            from reqpilotx.services.exporter import _build_export_source_image_context, _load_matrix_figure_payload
            source = args.source.expanduser().resolve(strict=True)
            if hashlib.sha256(source.read_bytes()).hexdigest() != document_map.source_sha256:
                raise ValueError("Image rendering original does not match the Document Map")
            bound_images = {image["anchor_id"]: image for packet in packets for image in packet["images"]}
            for packet in packets:
                for relation in packet.get("table_image_relations", []):
                    bound_images[relation["image_binding"]["anchor_id"]] = relation["image_binding"]
            pages = sorted({image["page"] for image in bound_images.values() if image["page"] is not None})
            if pages:
                with pymupdf.open(source) as document:
                    for page in pages:
                        filename = f"source-page-{page}.png"
                        document[page - 1].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).save(output / filename)
                        renders.append({"page": page, "path": filename, "image_sha256": hashlib.sha256((output / filename).read_bytes()).hexdigest(), "source_sha256": document_map.source_sha256, "authority": "ORIGINAL_PAGE_RENDER"})
            context = _build_export_source_image_context(source_file_path=source, source_sha256=document_map.source_sha256)
            for anchor_id, binding in bound_images.items():
                figure = {**binding, "kind": "image", "page_number": binding["page"], "source_bbox": binding["bbox"],
                    "location": {key: value for key, value in binding["native_locator"].items() if value is not None} if binding.get("native_locator") else {"format": "pdf", "page_number": binding["page"], "bbox": binding["bbox"]}}
                loaded = _load_matrix_figure_payload(figure, source_image_context=context)
                if loaded is None:
                    render_gaps.append({"anchor_id": anchor_id, "code": "SOURCE_IMAGE_PAYLOAD_UNAVAILABLE"}); continue
                data, width, height = loaded
                with Image.open(data) as decoded:
                    extension = {"JPEG": "jpg", "PNG": "png", "TIFF": "tiff", "BMP": "bmp", "GIF": "gif"}.get(decoded.format)
                if extension is None:
                    render_gaps.append({"anchor_id": anchor_id, "code": "UNSUPPORTED_SOURCE_IMAGE_FORMAT"}); continue
                filename = f"{anchor_id}.{extension}"; payload_bytes = data.getvalue()
                (output / filename).write_bytes(payload_bytes); (output / filename).chmod(0o600)
                image_renders.append({"anchor_id": anchor_id, "path": filename, "width": width, "height": height,
                    "image_sha256": hashlib.sha256(payload_bytes).hexdigest(), "source_sha256": document_map.source_sha256,
                    "page": binding["page"], "bbox": binding["bbox"], **core.native_location_fields(binding), "authority": "SOURCE_BOUND_IMAGE_PAYLOAD",
                    "source_item_ids": sorted({relation["table_source_record_id"] for packet in packets for relation in packet.get("table_image_relations", []) if relation["image_binding"]["anchor_id"] == anchor_id})})
        payload["source_page_renders"] = renders
        payload["source_image_renders"] = image_renders
        payload["source_image_render_gaps"] = render_gaps
        save(output, "image-packets.json", payload)
        return {"status": "IMAGE_PACKETS_WRITTEN", "scope_count": len(packets), "image_count": sum(len(packet["images"]) for packet in packets), "rendered_pages": len(renders), "rendered_source_images": len(image_renders), "image_render_gap_count": len(render_gaps), "output_dir": str(output), **({'image_binding_note': note} if note else {})}
    decision_input = read(required(args.input, "--input"))
    if (decision_input.get("schema_version") != "reqpilot-image-decisions/v1"
            or decision_input.get("source_sha256") != document_map.source_sha256
            or decision_input.get("document_map_artifact_sha256") != document_map.artifact_sha256):
        raise ValueError("Image decisions must bind the current source and Document Map")
    baseline, validation, annotations = compile_source_item_images(document_map, decision_input["scopes"], previous_baseline=previous, previous_annotations=previous_annotations, table_previews=table_previews)
    output = new_output(args.output_dir)
    for name in ("document-map.json", "table-previews.json", "scope-requests.json", "map-compilation-receipt.json", "item-compilation-receipt.json"):
        if (directory / name).is_file():
            shutil.copyfile(directory / name, output / name)
    save(output, "source-requirement-baseline.json", baseline.model_dump(mode="json"))
    save(output, "validation-receipt.json", validation.model_dump(mode="json"))
    save(output, "image-annotations.json", annotations)
    return {"status": "IMAGE_SOURCE_ITEMS_COMPILED", "source_item_count": len(baseline.records), "image_item_count": sum(bool(record.image_bindings) for record in baseline.records), "image_count": validation.owned_image_count, "requirement_count": validation.requirement_count, "output_dir": str(output), "semantic_verification": "REQUIRES_SOURCE_REVIEW"}


def analysis_operation(args: Namespace) -> dict:
    from reqpilotx.application.agent_workflow.source_item_analysis import build_source_item_analysis_packet, compile_source_item_assessments, source_item_analysis_output_schema
    if args.command == "analysis-packet":
        directory = required(args.baseline_dir, "--baseline-dir")
        document_map = core.DocumentMapArtifactV1.model_validate_json((directory / "document-map.json").read_text(encoding="utf-8"))
        baseline = core.SourceRequirementBaselineArtifactV1.model_validate_json((directory / "source-requirement-baseline.json").read_text(encoding="utf-8"))
        if args.knowledge_catalog:
            from evidence_tools import verify_catalog
            catalog = verify_catalog(args.knowledge_catalog)
        else:
            values = {"schema_version": "project-knowledge-facts/v1", "project_id": "portable", "knowledge_base_version": "NO_CATALOG_SUPPLIED", "documents": [], "evidence_catalog": {}, "gaps": [{"code": "NO_REGISTERED_KNOWLEDGE_CATALOG", "message": "No registered internal evidence catalog was supplied to this command; a compliance conclusion cannot be formed."}], "selection": "NO_REGISTERED_CATALOG_SUPPLIED"}
            catalog = {**values, "catalog_sha256": core.canonical_json_sha256(values)}
        decomposition = read(args.decomposition) if args.decomposition else None
        images = None
        annotation_path = getattr(args, "annotations", None) or directory / "image-annotations.json"
        if annotation_path.is_file():
            from reqpilotx.application.agent_workflow.source_item_visual import rebind_source_item_image_annotations
            images = rebind_source_item_image_annotations(document_map, baseline, read(annotation_path))
        packet = build_source_item_analysis_packet(document_map, baseline, read(directory / "table-previews.json"), catalog, decomposition=decomposition, selected_unit_ids=args.unit_id, image_annotations=images)
        output = new_output(args.output_dir)
        save(output, "analysis-packet.json", packet)
        save(output, "knowledge-catalog.json", catalog)
        from evidence_tools import freeze_analysis_source
        freeze_analysis_source(output, document_map, baseline, read(directory / "table-previews.json"), annotations=images, decomposition=decomposition)
        if args.knowledge_catalog:
            from evidence_tools import copy_registration
            copy_registration(args.knowledge_catalog, output)
        save(output, "assessment-candidate.schema.json", source_item_analysis_output_schema(packet["selected_unit_ids"]))
        return {"status": "ANALYSIS_PACKET_FROZEN", "unit_count": len(packet["units"]), "registered_fact_count": len(packet["evidence_catalog"]), "output_dir": str(output)}
    packet_path = required(args.analysis_packet, "--analysis-packet")
    packet = read(packet_path)
    from evidence_tools import verify_packet_evidence, copy_registration, verify_frozen_analysis_source, freeze_analysis_source
    catalog_path = verify_packet_evidence(packet, packet_path)
    if catalog_path is None:
        from revision_tools import empty_catalog_from_packet
        catalog = empty_catalog_from_packet(packet)
    else:
        catalog = read(catalog_path)
    source_context = verify_frozen_analysis_source(packet_path, packet, catalog, baseline_dir=getattr(args, "baseline_dir", None))
    candidate = read(required(args.input, "--input"))
    previous = read(args.previous_assessments) if args.previous_assessments else None
    artifact = compile_source_item_assessments(packet, candidate, previous=previous, input_packet_ref={"path": "analysis-packet.json", "sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest()})
    output = new_output(args.output_dir)
    save(output, "source-item-assessments.json", artifact)
    save(output, "analysis-packet.json", packet)
    source_map = core.DocumentMapArtifactV1.model_validate(read(source_context / "document-map.json"))
    source_baseline = core.SourceRequirementBaselineArtifactV1.model_validate(read(source_context / "source-requirement-baseline.json"))
    source_annotations = source_context / "image-annotations.json"; source_decomposition = source_context / "source-item-decomposition.json"
    freeze_analysis_source(output, source_map, source_baseline, read(source_context / "table-previews.json"),
        annotations=read(source_annotations) if source_annotations.is_file() else None, decomposition=read(source_decomposition) if source_decomposition.is_file() else None)
    if catalog_path:
        save(output, "knowledge-catalog.json", read(catalog_path)); copy_registration(catalog_path, output)
    return {"status": artifact["status"], "assessed_unit_count": len(artifact["records"]), "pending_unit_count": artifact["pending_unit_count"], "unassessed_unit_count": artifact["unassessed_unit_count"], "output_dir": str(output), "semantic_validation": "REQUIRES_SOURCE_AND_EVIDENCE_REVIEW"}


def annotation_operation(args: Namespace) -> dict:
    from reqpilotx.application.agent_workflow.source_item_visual import compile_source_item_visual_readings, compile_source_item_annotation_updates
    directory = required(args.baseline_dir, "--baseline-dir")
    document_map = core.DocumentMapArtifactV1.model_validate(read(directory / "document-map.json"))
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(read(directory / "source-requirement-baseline.json"))
    value = read(required(args.input, "--input"))
    if (value.get("schema_version") != "reqpilot-annotation-decisions/v1" or value.get("source_sha256") != baseline.source_sha256
            or value.get("baseline_sha256") != baseline.artifact_sha256 or value.get("document_map_artifact_sha256") != document_map.artifact_sha256):
        raise ValueError("Annotation decisions must bind the exact current source, Map and baseline")
    if not value.get("visual_readings") and not value.get("annotation_updates"):
        raise ValueError("Supply explicit visual readings or source annotation changes")
    previous = required(args.annotations, "--annotations") if getattr(args, "annotations", None) else directory / "image-annotations.json"
    annotations = read(previous) if previous.is_file() else None
    if value.get("visual_readings"):
        annotations = compile_source_item_visual_readings(document_map, baseline, value["visual_readings"], base_annotations=annotations)
    if value.get("annotation_updates"):
        annotations = compile_source_item_annotation_updates(document_map, baseline, value["annotation_updates"], base_annotations=annotations, table_previews=read(directory / "table-previews.json"))
    output = new_output(args.output_dir)
    save(output, "image-annotations.json", annotations)
    save(output, "annotation-compilation-receipt.json", {"source_sha256": baseline.source_sha256, "baseline_sha256": baseline.artifact_sha256,
        "annotations_sha256": annotations["annotations_sha256"], "human_acceptance": "NOT_CLAIMED"})
    return {"status": "ANNOTATIONS_COMPILED", "output_dir": str(output), "baseline_sha256": baseline.artifact_sha256, "annotations_sha256": annotations["annotations_sha256"]}


def run_operation(args: Namespace) -> dict:
    return {"compile-map": compile_map, "compile-items": compile_items, "compile-images": image_operation, "compile-annotations": annotation_operation, "image-packet": image_operation, "analysis-packet": analysis_operation, "compile-assessments": analysis_operation, "schemas": schemas, "deliver": deliver, "inspect-source": inspect_source, "decomposition-packet": decomposition_operation, "compile-decomposition": decomposition_operation}[args.command](args)
