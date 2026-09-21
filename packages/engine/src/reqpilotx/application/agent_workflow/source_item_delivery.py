"""One source-owned delivery projection for the mature workbench and Codex plugin.

Document Map/SourceItem remain the authorities. This module performs no structure
parsing, classification inference, database write, or model call. Tables retain native
cells and records, and decisions require explicit version-bound evidence.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from reqpilotx.application.agent_workflow.source_requirement_baseline import (
    DocumentMapArtifactV1, SourceRequirementBaselineArtifactV1, Requirementness,
    canonical_json_sha256, validate_source_image_record_against_map,
)
from reqpilotx.application.agent_workflow.source_item_ledger import (
    project_source_item_ledger, source_item_display_label,
)
from reqpilotx.domain.requirement_matrix import (
    MatrixNodeStateV1, MatrixLogicalTableV1, RequirementMatrixSnapshotV1,
)

from reqpilotx.application.agent_workflow.source_locations import source_location_ref, native_location_fields

CLASS_PATHS = {'FUNCTIONAL': 'PRODUCT_SYSTEM.FUNCTIONAL', 'QUALITY': 'PRODUCT_SYSTEM.QUALITY',
               'CONSTRAINT': 'PRODUCT_SYSTEM.CONSTRAINT', 'PROJECT': 'PROJECT', 'PROCESS': 'PROCESS'}
TOPICS = {'REGULATORY', 'VERIFICATION', 'LOGISTICS_PACKAGING', 'STANDARDS'}


class DeliveryError(ValueError):
    pass


def source_item_image_export_refs(record):
    return [{**binding.model_dump(mode='json'), 'kind': 'image',
        'page_number': binding.page, 'source_bbox': list(binding.bbox) if binding.bbox is not None else None,
        'caption': record.label, 'caption_authority': 'AGENT_DISPLAY_LABEL',
        'location': source_location_ref(binding)}
        for binding in record.image_bindings]


def merge_source_item_related_images(document_map, node, existing, related, source_id):
    """Annotations can add captions, never erase or replace original geometry."""
    from reqpilotx.application.agent_workflow.source_item_visual import _binding
    merged = {value['anchor_id']: deepcopy(value) for value in existing}
    anchors = {anchor.anchor_id: anchor for anchor in node.source_anchors if anchor.anchor_kind in {'IMAGE', 'FIGURE'}}
    for annotation in related:
        anchor_id = annotation.get('anchor_id')
        if anchor_id not in anchors:
            raise DeliveryError(f'Image annotation leaves its original Map owner: {source_id}/{anchor_id}')
        original_binding = _binding(document_map, anchors[anchor_id])
        binding = original_binding.model_dump(mode='json')
        authoritative = {**binding, 'kind': 'image', 'page_number': binding['page'], 'source_bbox': binding['bbox'],
            'location': source_location_ref(original_binding)}
        for field, expected in authoritative.items():
            if field in annotation and annotation[field] != expected:
                raise DeliveryError(f'Image annotation conflicts with original source: {source_id}/{anchor_id}/{field}')
        display = {key: deepcopy(annotation[key]) for key in ('caption', 'caption_authority', 'relation') if key in annotation}
        merged[anchor_id] = {**merged.get(anchor_id, {}), **display, **authoritative}
    return list(merged.values())


def _bound_records(value, baseline, name, extra_ids=()):
    if value is None:
        return {}
    if value.get('baseline_sha256') != baseline.artifact_sha256:
        raise DeliveryError(f'{name} belongs to a different source baseline')
    records = value.get('records', {})
    known = {r.source_record_id for r in baseline.records} | set(extra_ids)
    if not isinstance(records, dict) or set(records) - known:
        raise DeliveryError(f'{name} contains an unknown source identity')
    return records


def _segment(fragment, logical_id):
    cells = []
    for raw in fragment['cells']:
        cell = deepcopy(raw)
        cell.update(id=cell.get('cell_id', cell.get('id')), page=fragment['page'])
        cells.append(cell)
    return {'id': fragment['fragment_id'], 'table_id': fragment['fragment_id'], 'type': 'TABLE', 'page': fragment['page'],
            'page_number': fragment['page'], 'bbox': list(fragment['bbox']) if fragment['bbox'] is not None else None,
            'source_bounds': list(fragment['source_bounds']) if fragment['source_bounds'] is not None else None,
            **native_location_fields(fragment),
            'logical_table_id': logical_id, 'row_count': fragment['row_count'],
            'column_count': fragment['column_count'], 'cells': cells,
            'source_refs': [{'page': fragment['page'], 'page_number': fragment['page'], 'bbox': list(fragment['bbox'])}] if fragment.get('native_locator') is None else [native_location_fields(fragment)]}


def _record_projection(record, segments):
    by_id = {s['id']: s for s in segments}
    rows, columns, headers = [], [], []
    for fragment in record.table_fragments:
        segment = by_id.get(fragment.fragment_id)
        if segment is None:
            raise DeliveryError('Missing physical table fragment: ' + fragment.fragment_id)
        cells = {c['id']: c for c in segment['cells']}
        if set(fragment.cell_ids) - set(cells):
            raise DeliveryError('Source record references missing original cells')
        for row in range(fragment.row_start, fragment.row_end):
            bound = [c['id'] for c in cells.values() if c['id'] in fragment.cell_ids
                     and c['row'] <= row < c['row'] + c.get('row_span', 1)]
            rows.append({'table_id': segment['id'], 'row_index': row, 'cell_ids': bound})
        for column in range(segment['column_count']):
            bound = [c['id'] for c in cells.values() if c['id'] in fragment.cell_ids
                     and c['column'] <= column < c['column'] + c.get('column_span', 1)]
            columns.append({'table_id': segment['id'], 'column_index': column, 'cell_ids': bound})
    for segment in segments:
        selected = [c for c in segment['cells'] if c['id'] in record.shared_header_cell_ids]
        if selected:
            headers.append({'table_id': segment['id'], 'axis': 'ROW',
                            'cell_ids': [c['id'] for c in selected],
                            'text': ' / '.join(c['text'] for c in selected)})
    return {'record_id': record.source_record_id, 'record_kind': record.source_form.value,
            'label': record.label, 'row_bindings': rows, 'column_bindings': columns,
            'header_bindings': headers, 'shared_cell_ids': list(record.shared_table_cell_ids),
            'figure_refs': []}


def _apply_assessment(fields, decision, evidence_catalog):
    evidence = decision.get('evidence', [])
    roles = set()
    for citation in evidence:
        fact = evidence_catalog.get(citation.get('fact_id'))
        if not fact or not citation.get('quote') or citation['quote'] not in fact.get('text', ''):
            raise DeliveryError('Assessment citation is not grounded in the provided evidence catalog')
        roles.add(fact.get('role', ''))
    if decision.get('decision') in {'COMPLIANT', 'PARTIALLY_COMPLIANT', 'NON_COMPLIANT'}:
        if not evidence or not roles.intersection({'INTERNAL_KNOWLEDGE', 'PRODUCT_EVIDENCE', 'PROJECT_EVIDENCE'}):
            raise DeliveryError('A compliance conclusion requires internal product/project evidence, not only the customer requirement')
    fields.update(analysis_status='ASSESSED', analysis_result_state='CURRENT',
        ai_assessment={'decision': decision['decision'], 'reason': decision.get('reason', ''),
                       'analysis_basis': '\n'.join(
                           f"{evidence_catalog[c['fact_id']].get('document_name') or evidence_catalog[c['fact_id']].get('document_id') or c['fact_id']}: {c['quote']}"
                           for c in evidence),
                       'clarification_question': decision.get('clarification_question', ''),
                       'assessment_mode': 'agent-evidence-bound'})


def source_item_carry_notes(decomposition=None, assessments=None):
    """Project existing carry receipts into review notes; create no decisions."""
    decomposition, assessments = decomposition or {}, assessments or {}
    notes = {}
    def add(target, value):
        notes.setdefault(target, []).append({'operation': 'SOURCE_VALIDATED_REBIND', **value})
    split_carry = decomposition.get('carry_forward', {})
    if split_carry.get('operation') == 'SOURCE_VALIDATED_REBIND':
        for sid in split_carry.get('stale_parent_ids', []):
            add(sid, {'stale_parent_ids': [sid], 'from_baseline_sha256': split_carry.get('from_baseline_sha256', ''),
                'message': '前版派生未通过本版来源与分类一致性核验，需检查拆分；旧派生保留在历史版本。'})
        for sid in split_carry.get('retained_parent_ids', []):
            if sid in decomposition.get('records', {}) and sid not in split_carry.get('stale_parent_ids', []):
                add(sid, {'retained_parent_ids': [sid], 'from_baseline_sha256': split_carry.get('from_baseline_sha256', ''),
                    'message': '沿用前版派生；本轮未重新拆分。'})
    carry = assessments.get('carry_forward', {})
    for uid, receipt in assessments.get('unit_input_receipts', {}).items():
        if uid not in assessments.get('records', {}) or receipt.get('operation') != 'SOURCE_VALIDATED_REBIND':
            continue
        origin = receipt
        while isinstance(origin.get('origin_input_receipt'), dict):
            origin = origin['origin_input_receipt']
        add(uid, {'retained_unit_ids': [uid], 'origin_baseline_sha256': origin.get('baseline_sha256', ''),
            'from_baseline_sha256': carry.get('from_baseline_sha256', ''),
            'message': '沿用前版分析建议；本轮未重新分析。'})
    if carry.get('operation') == 'SOURCE_VALIDATED_REBIND':
        owners = {child_id: sid for sid, registry in decomposition.get('identity_registry', {}).items() for child_id in registry}
        displays = {child_id: display for registry in decomposition.get('identity_registry', {}).values() for child_id, display in registry.items()}
        active_children = {child['singular_requirement_id'] for children in decomposition.get('records', {}).values() for child in children}
        for uid in carry.get('stale_unit_ids', []):
            if uid in assessments.get('records', {}):
                continue
            target = uid if uid in active_children else owners.get(uid, uid)
            display = displays.get(uid)
            add(target, {'stale_unit_ids': [uid], 'from_baseline_sha256': carry.get('from_baseline_sha256', ''),
                'message': (f'{display}：' if display else '') + '前版分析未通过本版输入一致性核验，需重新分析；旧建议保留在历史版本。'})
    return notes


def _number_reference_range(reference):
    if not isinstance(reference, dict):
        return None
    kind = reference.get('kind')
    if kind in {'TEXT', 'MAP_HEADING'}:
        identity = ('TEXT', reference.get('content_id'))
    elif kind == 'TABLE_CELL':
        identity = ('TABLE_CELL', reference.get('fragment_id') or reference.get('table_id'), reference.get('cell_id'))
    else:
        return None
    start, end = reference.get('start_offset'), reference.get('end_offset')
    if (any(not isinstance(value, str) or not value for value in identity)
            or type(start) is not int or type(end) is not int or not 0 <= start < end):
        return None
    return identity, start, end


def _number_extent_covers(excerpts, extent):
    references = [_number_reference_range(excerpt.get('source_ref')) for excerpt in excerpts]
    ranges = [_number_reference_range(part.get('reference')) for part in extent]
    if not references or any(ref is None for ref in references) or not ranges or any(ref is None for ref in ranges):
        return False
    for identity, start, end in references:
        cursor = start
        for lo, hi in sorted((lo, hi) for key, lo, hi in ranges if key == identity):
            if lo > cursor:
                break
            if hi > cursor:
                cursor = hi
            if cursor >= end:
                break
        if cursor < end:
            return False
    return True


def _number_list_label(entries, *, parent_context=False):
    """Keep the typed source_number display bounded; complete evidence stays in metadata."""
    prefix = '父项原条号 ' if parent_context else '原条号 '
    suffix = '；本项见原文依据' if parent_context else ''
    values = [str(entry['value']) for entry in entries]
    full = prefix + '、'.join(values) + suffix
    if len(full) <= 160:
        return full
    ending = f'…（共{len(values)}处）' + suffix
    selected = []
    for value in values:
        if len(prefix + '、'.join([*selected, value]) + ending) > 160:
            break
        selected.append(value)
    return prefix + '、'.join(selected) + ending


def _derived_source_number(child, entries, parent_number):
    """Project proven original numbers; Snn identity never supplies an original number."""
    if not entries:
        return parent_number, {'role': 'SINGLE_PARENT_NUMBER' if parent_number else 'UNNUMBERED', 'numbers': []}
    excerpts = child.get('source_excerpts', [])
    references = [_number_reference_range(excerpt.get('source_ref')) for excerpt in excerpts]
    matches = []
    for entry in entries:
        evidence = entry['evidence']
        extent = evidence.get('source_extent')
        number_ref = _number_reference_range(evidence.get('reference'))
        direct = bool(number_ref and any(ref and ref[0] == number_ref[0]
            and ref[1] <= number_ref[1] < number_ref[2] <= ref[2] for ref in references))
        if (extent and _number_extent_covers(excerpts, extent)) or (not extent and (direct or len(entries) == 1)):
            matches.append((entry, 'EXACT_SOURCE_NUMBER' if extent or direct else 'SINGLE_PARENT_NUMBER'))
    if len(matches) == 1 and len(matches[0][0]['value']) <= 160:
        entry, role = matches[0]
        return entry['value'], {'role': role, 'numbers': [deepcopy(entry)]}
    return _number_list_label(entries, parent_context=True), {'role': 'PARENT_NUMBER_CONTEXT', 'numbers': deepcopy(entries)}


def project_source_item_number(document_map, baseline, node, record, is_one_to_one, annotation, table_previews, source_text=""):
    """The same original-number projection for Matrix and the source workbench."""
    ann = annotation or {}
    # Explicit collections and clears own precedence, including a clear on a
    # one-to-one clause. A program observation must never restore a cleared ID.
    if record and 'source_numbers' in ann:
        from reqpilotx.application.agent_workflow.source_item_visual import validate_source_item_numbers
        entries = validate_source_item_numbers(document_map, baseline, record.source_record_id, ann, table_previews=table_previews)
        number = (entries[0]['value'] if len(entries) == 1 and len(entries[0]['value']) <= 160 else _number_list_label(entries)) if entries else ''
        return number, entries
    if record and ann.get('source_number') == '':
        return '', []
    if record and 'source_number' not in ann:
        from reqpilotx.application.agent_workflow.source_item_numbering import observed_source_item_number
        observed = observed_source_item_number(document_map, baseline, record, table_previews=table_previews)
        if observed is not None:
            return observed['value'], [observed]
    original_number = node.label
    entries = []
    if record and ann.get('source_number') and ann.get('source_number_evidence'):
        original_number = ann['source_number']
    elif record and not is_one_to_one:
        original_number = ann.get('source_number', '')
    if original_number and record and ann.get('source_number') and not ann.get('source_numbers'):
        if ann.get('source_number_evidence'):
            from reqpilotx.application.agent_workflow.source_item_visual import validate_source_item_number_evidence
            validate_source_item_number_evidence(document_map, baseline, record.source_record_id,
                original_number, ann['source_number_evidence'], table_previews=table_previews)
        else:
            cells = {cid for binding in record.table_fragments for cid in binding.cell_ids}
            text = source_text + '\n' + '\n'.join(c['text'] for preview in table_previews.values()
                for fragment in preview['fragments'] for c in fragment['cells'] if c['cell_id'] in cells)
            if original_number not in text:
                raise DeliveryError('Claimed source number is not present in bound source')
    if record and ann.get('source_number_evidence') and ann.get('source_number'):
        entries = [{'value': ann['source_number'], 'evidence': deepcopy(ann['source_number_evidence'])}]
    return original_number, entries


def build_delivery_snapshot(document_map, baseline, table_previews, *, annotations=None,
                            decomposition=None, assessments=None, manual_decisions=None, analysis_policy=None, project_id='portable', document_id=None):
    document_map = DocumentMapArtifactV1.model_validate(document_map)
    baseline = SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if (baseline.document_map_artifact_sha256 != document_map.artifact_sha256
            or baseline.source_sha256 != document_map.source_sha256):
        raise DeliveryError('SourceItem/Document Map lineage mismatch')
    annotation_records = _bound_records(annotations, baseline, 'Annotations')
    if annotations and annotations.get('schema_version') in {'source-item-image-annotations/v1', 'source-item-visual-readings/v1', 'source-item-annotations/v1'}:
        from reqpilotx.application.agent_workflow.source_item_visual import rebind_source_item_image_annotations
        # _bound_records already rejects stale baseline pins. This verifies the
        # sealed annotation and exact image binding without silently adopting it.
        rebind_source_item_image_annotations(document_map, baseline, annotations)
    if decomposition is not None:
        from reqpilotx.application.agent_workflow.source_item_decomposition import validate_source_item_decomposition
        validate_source_item_decomposition(baseline, decomposition, table_previews=table_previews)
    decomposition_records = _bound_records(decomposition, baseline, 'Decomposition')
    singular_ids = {c['singular_requirement_id'] for children in decomposition_records.values() for c in children}
    assessment_records = _bound_records(assessments, baseline, 'Assessments', singular_ids)
    evidence_catalog = (assessments or {}).get('evidence_catalog', {})
    carry_notes = source_item_carry_notes(decomposition, assessments)
    ledger = project_source_item_ledger(document_map=document_map, artifact=baseline)
    native = {n.node_id: n for n in document_map.nodes}
    records = {r.source_record_id: r for r in baseline.records}
    for record in records.values():
        validate_source_image_record_against_map(document_map, record)
    bound_images = {(record.node_id, binding.anchor_id) for record in records.values() for binding in record.image_bindings}
    if document_map.source_format:
        bound_images.update((relation.node_id, relation.image_binding.anchor_id) for relation in baseline.table_image_relations)
    rows = {r['row_id']: r for r in ledger['rows']}
    ordered = []
    def walk(row_id, depth):
        ordered.append((rows[row_id], depth))
        for child in rows[row_id]['child_row_ids']:
            walk(child, depth + 1)
    for root in ledger['root_row_ids']:
        walk(root, 0)
    record_node_ids = {r['source_item_id']: r['row_id'] for r in rows.values() if r.get('source_item_id')}
    if isinstance(table_previews, list):
        table_previews = {t['logical_table_scope_id']: t for t in table_previews}
    from reqpilotx.application.agent_workflow.source_item_figure_relations import table_source_figure_relations
    figure_relations = table_source_figure_relations(document_map, baseline, table_previews)
    from reqpilotx.application.agent_workflow.source_requirement_baseline import document_map_table_presentation
    segments, tables = {}, []
    for table in document_map.logical_tables:
        preview = table_previews.get(table.scope_id)
        if preview is None:
            raise DeliveryError('Missing original table preview: ' + table.scope_id)
        if preview.get('source_sha256') != baseline.source_sha256:
            raise DeliveryError('Table preview belongs to a different source')
        if preview.get('logical_table_scope_id') != table.scope_id:
            raise DeliveryError('Table preview scope mismatch')
        parts = [_segment(f, table.scope_id) for f in preview['fragments']]
        shared_ids = {cid for r in records.values() if r.logical_table_scope_id == table.scope_id
                      for cid in r.shared_header_cell_ids}
        if not any(r.logical_table_scope_id == table.scope_id for r in records.values()):
            shared_ids.update(cell_id for context in baseline.context_dispositions
                if context.logical_table_scope_id == table.scope_id and context.role.value == 'HEADER'
                for cell_id in context.cell_ids)
        for part in parts:
            part['header_rows'] = sorted({c['row'] for c in part['cells'] if c['id'] in shared_ids})
            part['cell_images'] = deepcopy(figure_relations['fragments'].get(part['id'], []))
            presentation = preview.get('presentation') or {}
            canonical = presentation.get('canonical_projection') or {}
            mapping = next((fragment for fragment in canonical.get('fragments', []) if fragment['fragment_id'] == part['id']), None)
            if canonical.get('status') == 'EXACT' and mapping:
                part['canonical_column_projection'] = {'status': 'EXACT',
                    'column_count': canonical['canonical_column_count'],
                    'cell_mappings': deepcopy(mapping['cell_mappings'])}
        segments[table.scope_id] = parts
        linked_rows = [r for r in rows.values() if table.scope_id in r['logical_table_scope_ids']]
        table_records = [r for r in records.values() if r.logical_table_scope_id == table.scope_id]
        mode = 'RECORDS' if table_records else 'WHOLE_TABLE'
        title_presentation = document_map_table_presentation(table)
        display_title = title_presentation.get('display_title', table.title)
        projection = {'projection_id': table.scope_id, 'mode': mode,
                      'table_fact': {'logical_table_id': table.scope_id, 'table_parts': parts},
                      'records': [_record_projection(r, parts) for r in table_records],
                      'structured_segments': parts, 'title': display_title,
                      **({'title_presentation': title_presentation} if title_presentation else {})}
        detail = MatrixLogicalTableV1(logical_table_id=table.scope_id, projection_id=table.scope_id,
            projection_mode=mode, record_count=len(table_records),
            physical_table_ids=tuple(p['id'] for p in parts),
            node_ids=tuple(r['row_id'] for r in linked_rows), projection=projection).model_dump(mode='json')
        detail.update(title=display_title, structured_segments=parts)
        tables.append(detail)
    bound_content = {(r.node_id, b.content_id) for r in records.values() for b in r.text_bindings}
    node_models, metadata, number_entries = [], {}, {}
    source_catalog = {c.content_id: c for n in native.values() for c in n.source_content}
    source_content_owners = {c.content_id: n.node_id for n in native.values() for c in n.source_content}
    for ordinal, (row, depth) in enumerate(ordered):
        node = native[row['structure_node_id']]
        record = records.get(row['source_item_id'])
        ann = annotation_records.get(record.source_record_id, {}) if record else {}
        tags = ann.get('topics', [])
        if set(tags) - TOPICS:
            raise DeliveryError('Unknown topic; keep categories and topics separate')
        context_ids = ann.get('context_source_ids', [])
        if set(context_ids) - set(records):
            raise DeliveryError('Unknown shared-context source item')
        text = '\n'.join(b.text for b in record.text_bindings) if record else ''
        source_refs = []
        if record:
            source_refs = [{'page': b.page, 'bbox': list(b.bbox) if b.bbox is not None else None, **native_location_fields(b), 'precision': b.precision,
                            'kind': 'TEXT', 'content_id': b.content_id, 'start': b.start_offset,
                            'end': b.end_offset, 'owner_structure_node_id': node.node_id}
                           for b in record.text_bindings]
            source_refs += [{'page': b.page, 'bbox': list(b.bbox) if b.bbox is not None else None, **native_location_fields(b), 'kind': 'TABLE',
                             'table_id': b.fragment_id, 'cell_ids': list(b.cell_ids)} for b in record.table_fragments]
            source_refs += [{**binding.model_dump(mode='json'), 'kind': 'IMAGE',
                             'page_number': binding.page,
                             'location': source_location_ref(binding)}
                            for binding in record.image_bindings]
        else:
            source_refs = [a.model_dump(mode='json') for a in node.source_anchors]
        role = 'CLAUSE' if record else 'STRUCTURE'
        if row['is_table_structure']:
            role = 'TABLE'
        elif record and record.table_fragments:
            role = 'TABLE_RECORD'
        title = node.title if row['row_role'] == 'DOCUMENT_STRUCTURE' else source_item_display_label(
            record, node=node, has_structure_children=any(n.parent_id == node.node_id for n in native.values()))
        original_number, entries = project_source_item_number(document_map, baseline, node, record,
            row['is_one_to_one_item'], ann, table_previews, text)
        if record and entries:
            number_entries[record.source_record_id] = entries
        blocks = []
        if text:
            blocks.append({'id': record.source_record_id, 'type': 'TEXT', 'text': text})
        if not record:
            blocks.extend({'id': c.content_id, 'type': 'TEXT', 'text': c.text}
                          for c in node.source_content if not c.is_title and (node.node_id, c.content_id) not in bound_content)
        if row['is_table_structure']:
            blocks.extend(deepcopy(part) for tid in row['logical_table_scope_ids'] for part in segments[tid])
        is_requirement = record is not None and record.requirementness is Requirementness.REQUIREMENT
        has_children = bool(record and decomposition_records.get(record.source_record_id))
        eligible = is_requirement and not has_children
        fields = dict(node_id=row['row_id'], parent_id=row['parent_row_id'], child_ids=row['child_row_ids'],
            depth=depth, source_order=ordinal * 1000, display_role=role,
            source_kind='SOURCE_ITEM' if record else node.kind,
            source_number=original_number, display_title=title, label=title,
            display_text=text, summary='', content_blocks=blocks, source_refs=source_refs,
            page_start=min(record.pages if record else node.pages, default=None), page_end=max(record.pages if record else node.pages, default=None),
            structure_node_id=node.node_id, requirement_id=record.source_record_id if is_requirement else None,
            requirement_entry_id=record.source_record_id if is_requirement else None,
            requirement_code=record.requirement_display_id or '' if record else '', requirement_text=text,
            table_record_id=record.source_record_id if record and record.table_fragments else None,
            logical_table_ids=row['logical_table_scope_ids'], matrix_visibility='VISIBLE',
            disposition='ANALYZE' if eligible else 'RETAIN', analysis_eligibility='ELIGIBLE' if eligible else 'NOT_ELIGIBLE',
            analysis_status='NOT_STARTED' if eligible else 'NOT_APPLICABLE')
        figures = [dict(a.model_dump(mode='json'), **({'source_sha256': baseline.source_sha256} if document_map.source_format else {}), kind=a.anchor_kind.lower(),
                        page_number=a.page, source_bbox=list(a.bbox) if a.bbox is not None else None,
                        location=source_location_ref(a))
                   for a in node.source_anchors if a.anchor_kind in {'IMAGE', 'FIGURE'}
                   and (node.node_id, a.anchor_id) not in bound_images]
        if row['row_role'] == 'DOCUMENT_STRUCTURE':
            fields['figure_refs'] = figures
        if record and record.image_bindings:
            fields['figure_refs'] = source_item_image_export_refs(record)
        if record and figure_relations['records'].get(record.source_record_id):
            fields['figure_refs'] = deepcopy(figure_relations['records'][record.source_record_id])
        if record and ann.get('related_images'):
            fields['figure_refs'] = merge_source_item_related_images(document_map, node, fields.get('figure_refs', []),
                ann['related_images'], record.source_record_id)
        decision = assessment_records.get(record.source_record_id) if record else None
        if decision:
            if not eligible:
                raise DeliveryError('Assessment targets a non-active analysis unit')
            _apply_assessment(fields, decision, evidence_catalog)
        record_notes = carry_notes.get(record.source_record_id, []) if record else []
        if record_notes and not decision:
            fields['disposition_reason'] = '\n'.join(item['message'] for item in record_notes)
            if eligible and any(item.get('stale_unit_ids') or item.get('stale_parent_ids') for item in record_notes):
                fields['analysis_result_state'] = 'STALE_REFERENCE'
        node_models.append(MatrixNodeStateV1(**fields))
        if record:
            metadata[row['row_id']] = {'source_item_id': record.source_record_id,
                'source_display_id': record.source_display_id, 'requirement_display_id': record.requirement_display_id,
                'requirementness': record.requirementness.value, 'requirement_kinds': [k.value for k in record.requirement_kinds],
                'non_requirement_role': record.non_requirement_role.value if record.non_requirement_role else None,
                'classification_paths': [CLASS_PATHS[k.value] for k in record.requirement_kinds],
                'topics': tags, 'context_source_ids': context_ids,
                'shared_source_content': [{**source_catalog[c].model_dump(mode='json'),
                    'source_owner_node_id': source_content_owners[c]} for c in record.shared_source_content_ids if c in source_catalog],
                'decision_summary': record.decision_summary, 'source_form': record.source_form.value}
            if record_notes:
                metadata[row['row_id']]['revision_carry'] = deepcopy(record_notes)
            # Only a source-backed heading may be prepended to a 1:1 clause.
            # Parser fallback labels are navigation aids, not customer content.
            metadata[row['row_id']]['source_title'] = node.title if node.title and any(
                c.is_title and ''.join(node.title.split()) in ''.join(c.text.split())
                for c in node.source_content) else ''
            metadata[row['row_id']]['ancestor_path'] = list(record.ancestor_path)
            if number_entries.get(record.source_record_id):
                metadata[row['row_id']]['source_numbers'] = deepcopy(number_entries[record.source_record_id])
                metadata[row['row_id']]['source_number_role'] = 'SOURCE_NUMBER_COLLECTION' if len(number_entries[record.source_record_id]) > 1 else 'EXACT_SOURCE_NUMBER'
            if ann.get('visual_interpretation') and annotations.get('schema_version') == 'source-item-visual-readings/v1':
                metadata[row['row_id']]['visual_interpretation'] = deepcopy(ann['visual_interpretation'])
            if record.image_bindings:
                interpretation = ann.get('visual_interpretation')
                if interpretation is not None:
                    if (not isinstance(interpretation, dict)
                            or interpretation.get('authority') != 'AGENT_VISUAL_INTERPRETATION'
                            or interpretation.get('source_sha256') != baseline.source_sha256
                            or not isinstance(interpretation.get('text'), str)
                            or not interpretation.get('image_anchor_ids')
                            or set(interpretation['image_anchor_ids']) - {binding.anchor_id for binding in record.image_bindings}):
                        raise DeliveryError('Image interpretation must be a separately labelled, source-bound Agent reading')
                metadata[row['row_id']].update(original_representation='IMAGE',
                    display_label_authority='AGENT_DISPLAY_LABEL',
                    image_bindings=[binding.model_dump(mode='json') for binding in record.image_bindings],
                    visual_interpretation=deepcopy(interpretation))
    if decomposition_records:
        model_index = {n.node_id: n for n in node_models}
        for source_id in sorted(decomposition_records, key=lambda sid: (records[sid].source_order, sid)):
            children = decomposition_records[source_id]
            parent_id = record_node_ids[source_id]
            parent = model_index[parent_id]
            ids = [child['singular_requirement_id'] for child in children]
            replacement = parent.model_copy(update={'child_ids': tuple([*parent.child_ids, *ids])})
            node_models[node_models.index(parent)] = replacement
            metadata[parent_id]['decomposition_children'] = ids
            for ordinal, child in enumerate(children, 1):
                original_number, number_projection = _derived_source_number(child, number_entries.get(source_id, []), parent.source_number)
                refs = [{'page': e['page'], 'bbox': e['bbox'], 'precision': e['precision'],
                         **e['source_ref']} for e in child['source_excerpts']]
                fields = dict(node_id=child['singular_requirement_id'], parent_id=parent_id,
                    child_ids=[], depth=parent.depth+1, source_order=parent.source_order+ordinal,
                    display_role='ATOMIC_REQUIREMENT', source_kind='SINGULAR_REQUIREMENT',
                    source_number=original_number, display_title=child['text'], display_text=child['text'],
                    label=child['text'], summary='', source_refs=refs,
                    page_start=min(e['page'] for e in child['source_excerpts']),
                    page_end=max(e['page'] for e in child['source_excerpts']),
                    structure_node_id=parent.structure_node_id,
                    requirement_id=child['singular_requirement_id'], requirement_entry_id=child['singular_requirement_id'],
                    requirement_code=child['requirement_display_id'], requirement_text=child['text'],
                    logical_table_ids=parent.logical_table_ids, table_record_id=parent.table_record_id,
                    matrix_visibility='VISIBLE', disposition='ANALYZE', analysis_eligibility='ELIGIBLE',
                    analysis_status='NOT_STARTED', classification_paths=[CLASS_PATHS[k] for k in child['requirement_kinds']],
                    topic_tags=metadata[parent_id]['topics'], decomposition=deepcopy(child),
                    source_excerpts=[{'id': f"{child['singular_requirement_id']}-{index}", 'role': 'DIRECT',
                                      'text': excerpt['text'], 'pages': [excerpt['page']]}
                                     for index, excerpt in enumerate(child['source_excerpts'])])
                fields['decomposition']['source_number_projection'] = number_projection
                child_notes = carry_notes.get(child['singular_requirement_id'], [])
                if child_notes:
                    fields['decomposition']['revision_carry'] = deepcopy(child_notes)
                    fields['disposition_reason'] = '\n'.join(item['message'] for item in child_notes)
                    if (not assessment_records.get(child['singular_requirement_id'])
                            and any(item.get('stale_unit_ids') for item in child_notes)):
                        fields['analysis_result_state'] = 'STALE_REFERENCE'
                if assessment_records.get(child['singular_requirement_id']):
                    _apply_assessment(fields, assessment_records[child['singular_requirement_id']], evidence_catalog)
                node_models.append(MatrixNodeStateV1(**fields))
                for table in tables:
                    if table['logical_table_id'] in parent.logical_table_ids:
                        table['node_ids'].append(child['singular_requirement_id'])
    if manual_decisions is not None:
        from reqpilotx.application.agent_workflow.source_item_manual_decisions import build_manual_decision_context, manual_decision_projection
        manual_context = build_manual_decision_context(document_map, baseline, table_previews,
            decomposition=decomposition, annotations=annotations)
        manual_projection = manual_decision_projection(manual_decisions, manual_context)
        updated_nodes = []
        for node in node_models:
            opinion = manual_projection.get(node.requirement_id or node.node_id)
            if opinion is None:
                updated_nodes.append(node); continue
            values = node.model_dump(mode='python')
            if opinion.get('manual_decision'):
                values.update(manual_decision=opinion['manual_decision'], stale_manual_decision=None,
                    stale_ai_assessment=None, analysis_status='DECIDED', analysis_result_state='CURRENT')
            elif opinion['state'] == 'STALE' and node.ai_assessment is None:
                record = opinion['record']
                values.update(analysis_result_state='STALE_REFERENCE', analysis_status='NOT_STARTED',
                    manual_decision=None, compliance_status='',
                    stale_manual_decision={'decision': record['decision'], 'reason': record['reason'], 'decided_at': record['decided_at']})
            if node.node_id in metadata:
                metadata[node.node_id]['engineer_response'] = deepcopy(opinion)
            elif values.get('decomposition') is not None:
                values['decomposition']['engineer_response'] = deepcopy(opinion)
            updated_nodes.append(MatrixNodeStateV1.model_validate(values))
        node_models = updated_nodes
    counts = {'total_nodes': len(node_models), 'visible_nodes': len(node_models),
              'analyze_nodes': sum(n.disposition.value == 'ANALYZE' for n in node_models),
              'retained_nodes': sum(n.disposition.value == 'RETAIN' for n in node_models),
              'analysis_eligible_nodes': sum(n.analysis_eligibility.value == 'ELIGIBLE' for n in node_models),
              'assessed_nodes': sum(n.analysis_status.value in {'ASSESSED', 'DECIDED'} for n in node_models),
              'stale_result_nodes': sum(n.analysis_result_state.value == 'STALE_REFERENCE' for n in node_models),
              'logical_tables': len(tables)}
    if manual_decisions is not None:
        counts['decided_nodes'] = sum(n.analysis_status.value == 'DECIDED' for n in node_models)
    typed_tables = [MatrixLogicalTableV1.model_validate({k:v for k,v in t.items() if k not in {'title','structured_segments'}}) for t in tables]
    snapshot = RequirementMatrixSnapshotV1(state='READY', project_id=project_id,
        document_id=document_id or baseline.source_sha256[:20], requirement_set_id=baseline.artifact_id,
        requirement_set_status='DRAFT', tree_sha256=baseline.artifact_sha256,
        root_node_ids=ledger['root_row_ids'], nodes=node_models, logical_tables=typed_tables,
        counts=counts).model_dump(mode='json')
    snapshot['logical_tables'] = tables
    for node in snapshot['nodes']:
        if node['node_id'] in metadata:
            node['source_item'] = metadata[node['node_id']]
            node['classification_paths'] = metadata[node['node_id']]['classification_paths']
            node['topic_tags'] = metadata[node['node_id']]['topics']
    snapshot['source_item_baseline'] = {'artifact_id': baseline.artifact_id, 'artifact_sha256': baseline.artifact_sha256,
        'document_map_artifact_id': document_map.artifact_id, 'document_map_artifact_sha256': document_map.artifact_sha256,
        'source_sha256': baseline.source_sha256, 'revision': baseline.revision, 'status': baseline.status,
        'source_item_count': len(records), 'requirement_count': sum(r.requirementness is Requirementness.REQUIREMENT for r in records.values()),
        'non_requirement_count': sum(r.requirementness is Requirementness.NON_REQUIREMENT for r in records.values())}
    if manual_decisions is not None:
        snapshot['source_item_baseline'].update(manual_decisions_sha256=manual_decisions['artifact_sha256'],
            manual_decisions_revision=manual_decisions['revision'], human_acceptance='NOT_CLAIMED')
    if analysis_policy is not None:
        if (analysis_policy.get('source_sha256') != baseline.source_sha256
                or analysis_policy.get('status') not in {'AVAILABLE', 'SKIPPED'}
                or analysis_policy.get('reason') not in {'', 'USER_SKIPPED', 'NO_INTERNAL_EVIDENCE'}):
            raise DeliveryError('Analysis policy must bind the original source and an explicit execution choice')
        snapshot['source_item_baseline']['analysis_policy'] = deepcopy(analysis_policy)
        if analysis_policy['status'] == 'SKIPPED':
            for node in snapshot['nodes']:
                metadata = node.get('source_item') or node.get('decomposition')
                if metadata and node.get('analysis_eligibility') == 'ELIGIBLE' and not node.get('ai_assessment'):
                    metadata['analysis_policy'] = deepcopy(analysis_policy)
    snapshot['delivery_sha256'] = canonical_json_sha256({k:v for k,v in snapshot.items() if k != 'delivery_sha256'})
    return snapshot


def export_delivery_xlsx(snapshot: dict[str, Any], *, source_file_path='', source_sha256='',
                         project_name='ReqPilotX', customer_name='', source_file_name='', image_export_receipt=None, export_mode='DRAFT', version_label=None) -> bytes:
    """Reuse the established native-table/figure Excel renderer, without publishing."""
    from reqpilotx.services.exporter import export_compliance_matrix
    if export_mode not in {'DRAFT', 'FORMAL'}:
        raise DeliveryError('Invalid delivery export mode')
    expected = snapshot.get('delivery_sha256')
    if expected != canonical_json_sha256({k:v for k,v in snapshot.items() if k != 'delivery_sha256'}):
        raise DeliveryError('Delivery snapshot changed after validation')
    source = snapshot['source_item_baseline']
    if source_sha256 and source_sha256 != source['source_sha256']:
        raise DeliveryError('Export source differs from the delivery baseline')
    with TemporaryDirectory(prefix='reqpilotx-delivery-') as directory:
        _, path = export_compliance_matrix(project_name, customer_name, Path(directory),
            source_file_name=source_file_name or (Path(source_file_path).name if source_file_path else ''), matrix_snapshot=snapshot,
            version_label=version_label or f"SourceItem-{source['revision']}", export_mode=export_mode,
            source_file_path=source_file_path, source_sha256=source['source_sha256'], image_export_receipt=image_export_receipt)
        return Path(path).read_bytes()
