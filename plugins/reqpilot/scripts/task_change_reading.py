"""Small current-bundle reading packets and actual source-bound image payloads.

The task service supplies verified bundle/source/cache paths and exact pins.
These readers never discover a current task, cross versions, or accept content
on behalf of the Agent. Only image materialization writes a derived cache file.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_decomposition import (
    build_source_item_decomposition_packet, validate_source_item_decomposition,
)
from app.application.agent_workflow.source_item_visual import (
    build_source_item_image_scope_packet, rebind_source_item_image_annotations,
)
from task_change_contracts import source_item_record
from task_reading import _read, _hash, _size, _next, _limits, _text, _window


def _bundle(directory, source_sha, map_sha, baseline_sha, *, annotations=None, decomposition=None):
    from revision_tools import bundle
    directory = Path(directory)
    document_map, baseline, _, tables = bundle(directory)
    if (document_map.source_sha256, document_map.artifact_sha256, baseline.artifact_sha256) != (source_sha, map_sha, baseline_sha):
        raise ValueError('Reading bundle is not the selected source/Map/baseline version')
    if annotations is None and (directory / 'image-annotations.json').is_file():
        annotations = _read(directory / 'image-annotations.json')
    if decomposition is None and (directory / 'source-item-decomposition.json').is_file():
        decomposition = _read(directory / 'source-item-decomposition.json')
    if annotations is not None:
        if annotations.get('baseline_sha256') != baseline.artifact_sha256:
            raise ValueError('Annotations are not pinned to this exact baseline')
        annotations = rebind_source_item_image_annotations(document_map, baseline, annotations)
    if decomposition is not None:
        validate_source_item_decomposition(baseline, decomposition, table_previews=tables)
    return document_map, baseline, tables, annotations, decomposition


def _facts(record, document_map, baseline, tables):
    if record.requirementness is core.Requirementness.REQUIREMENT and (record.text_bindings or record.table_fragments):
        packet = build_source_item_decomposition_packet(baseline, record.source_record_id, table_previews=tables)
        facts, shared = packet['segments'], packet['shared_source_context']
    else:
        facts = [{'segment_index': i, 'text': b.text, 'page': b.page, 'bbox': b.bbox,
            'source_ref': {'kind': 'TEXT', 'content_id': b.content_id, 'start_offset': b.start_offset, 'end_offset': b.end_offset},
            'precision': b.precision} for i, b in enumerate(record.text_bindings)]
        shared = []; seen = set()
        if record.table_fragments:
            preview = tables.get(record.logical_table_scope_id)
            if not preview or preview['source_sha256'] != baseline.source_sha256:
                raise ValueError('Missing same-source table evidence')
            fragments = {f['fragment_id']: f for f in preview['fragments']}
            for binding in record.table_fragments:
                fragment = fragments[binding.fragment_id]
                cells = {c.get('cell_id', c.get('id')): c for c in fragment['cells']}
                for cid in binding.cell_ids:
                    if (binding.fragment_id, cid) in seen:
                        continue
                    seen.add((binding.fragment_id, cid)); cell = cells[cid]
                    if cell['text'].strip():
                        facts.append({'segment_index': len(facts), 'text': cell['text'], 'page': binding.page, 'bbox': cell['bbox'],
                            'source_ref': {'kind': 'TABLE_CELL', 'table_id': binding.fragment_id, 'cell_id': cid,
                                'start_offset': 0, 'end_offset': len(cell['text'])}, 'precision': 'CELL_TEXT_RANGE'})
            for fragment in preview['fragments']:
                for cell in fragment['cells']:
                    cid = cell.get('cell_id', cell.get('id'))
                    if cid in set(record.shared_header_cell_ids) | set(record.shared_table_cell_ids):
                        shared.append({'text': cell['text'], 'page': fragment['page'], 'source_ref': {
                            'kind': 'TABLE_CELL', 'table_id': fragment['fragment_id'], 'cell_id': cid}})
    contents = {c.content_id: c for n in document_map.nodes for c in n.source_content}
    # This is an explicit compiler binding; it is not inferred from omitted text.
    shared = [*shared, *[{'text': contents[cid].text, 'page': contents[cid].page,
        'source_ref': {'kind': 'TEXT', 'content_id': cid}, 'relation': 'EXPLICIT_SHARED_SOURCE_CONTENT'}
        for cid in record.shared_source_content_ids]]
    return facts, shared


def _image_scope(document_map, baseline, tables, node):
    if not any(a.anchor_kind in {'IMAGE', 'FIGURE'} for a in node.source_anchors):
        return None
    return build_source_item_image_scope_packet(document_map, node_id=node.node_id, baseline=baseline, table_previews=tables)


def _images(packet, record):
    if packet is None:
        return []
    values = []
    for binding in packet['images']:
        values.append({**binding, 'relation': 'OWNED_IMAGE_SOURCE' if any(b.anchor_id == binding['anchor_id'] for b in record.image_bindings)
            else 'SAME_MAP_NODE_ASSOCIATION_CANDIDATE'})
    for relation in packet.get('table_image_relations', []):
        values.append({**relation['image_binding'], 'relation': 'TABLE_EMBEDDED_IMAGE',
            'table_source_item_id': relation['table_source_record_id'], 'logical_table_scope_id': relation['logical_table_scope_id'],
            'fragment_id': relation['fragment_id'], 'cell_ids': relation['cell_ids']})
    return values


def _base(document_map, baseline, record):
    return {'schema_version': 'reqpilot-task-item-reading/v1', 'source_sha256': baseline.source_sha256,
        'document_map_artifact_sha256': document_map.artifact_sha256, 'baseline_sha256': baseline.artifact_sha256,
        'source_item_id': record.source_record_id, 'source_display_id': record.source_display_id,
        'semantic_acceptance': 'NOT_CLAIMED'}


def _json_part(base, value, arguments, *, offset, max_chars, link_transform=None):
    if link_transform is not None:
        value = link_transform(value)
    full = {**base, 'data': value, 'complete': True, 'budget_scope': 'RESULT_JSON_CHARACTERS', 'next_read': None}
    if offset == 0 and _size(full) <= max_chars:
        return full
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return _text({**base, 'encoding': 'JSON_TEXT', 'reassembly': 'Concatenate all text chunks from offset 0, then parse JSON'},
        text, offset=offset, max_chars=max_chars, reader='source_item_read', arguments=arguments)


def source_item_read(bundle_dir, *, source_item_id, expected_source_sha256, expected_map_sha256,
        expected_baseline_sha256, part=None, offset=0, limit=20, max_chars=12000, annotations=None, decomposition=None,
        link_transform=None):
    """Default: whole ordinary item; large source/context/metadata has exact reads."""
    _limits(offset, limit, max_chars)
    document_map, baseline, tables, annotations, decomposition = _bundle(bundle_dir, expected_source_sha256,
        expected_map_sha256, expected_baseline_sha256, annotations=annotations, decomposition=decomposition)
    record = source_item_record(baseline, source_item_id)
    node = next(n for n in document_map.nodes if n.node_id == record.node_id)
    facts, shared = _facts(record, document_map, baseline, tables)
    image_packet = _image_scope(document_map, baseline, tables, node)
    images = _images(image_packet, record)
    for image in images:
        image['read'] = _next('source_item_image', {'source_item_id': record.source_display_id, 'image_anchor_id': image['anchor_id']})
    annotation = (annotations or {}).get('records', {}).get(record.source_record_id, {})
    from task_context_reading import context_inventory, context_part
    context_candidates, context_review, linked_records = context_inventory(document_map, baseline, record, annotation)
    original_part = part
    part = context_part(part, context_review['context_bindings_sha256'])
    linked_context = []
    if part in {'linked_context', 'decomposition_packet'}:
        for linked in linked_records:
            linked_facts, linked_shared = _facts(linked, document_map, baseline, tables)
            linked_context.append({'source_item_id': linked.source_record_id, 'source_display_id': linked.source_display_id,
                'relation': 'EXPLICIT_CONTEXT_LINK', 'source': linked_facts, 'shared_source_context': linked_shared,
                'ancestor_path': list(linked.ancestor_path)})
    from app.application.agent_workflow.source_item_delivery import project_source_item_number
    siblings = [r for r in baseline.records if r.node_id == record.node_id]
    number, numbers = project_source_item_number(document_map, baseline, node, record, len(siblings) == 1,
        annotation, tables, source_text='\n'.join(f['text'] for f in facts))
    identity = {key: getattr(record, key) for key in ('node_id', 'source_order', 'label', 'ancestor_path', 'requirement_display_id')}
    identity.update(source_form=record.source_form.value, requirementness=record.requirementness.value,
        requirement_kinds=[k.value for k in record.requirement_kinds], non_requirement_role=record.non_requirement_role.value if record.non_requirement_role else None,
        original_number=number, source_numbers=numbers, confidence=record.confidence,
        decision_summary=record.decision_summary, reason_codes=list(record.reason_codes))
    sections = {'identity': identity, 'source': facts, 'shared_context': shared, 'images': images,
        'annotations': annotation, 'context_review': context_review, 'decomposition': {'children': (decomposition or {}).get('records', {}).get(record.source_record_id, []),
            'disposition': (decomposition or {}).get('dispositions', {}).get(record.source_record_id),
            'review_scope': (decomposition or {}).get('review_scope'),
            'authority': 'AGENT_WORKING_PROPOSAL', 'packet_available': bool(record.requirementness is core.Requirementness.REQUIREMENT and facts)}}
    base = _base(document_map, baseline, record)
    arguments = {'source_item_id': record.source_display_id, 'part': original_part}
    if part in {'context_candidates', 'linked_context'}:
        arguments['part'] = part + '@' + context_review['context_bindings_sha256']
        context_base = {**base, 'context_bindings_sha256': context_review['context_bindings_sha256'], 'part': part}
        if part == 'linked_context':
            return _json_part(context_base, linked_context, arguments, offset=offset, max_chars=max_chars, link_transform=link_transform)
        entries = link_transform(context_candidates) if link_transform else context_candidates
        return _window(context_base, entries, field='candidates', offset=offset, limit=limit, max_chars=max_chars,
            reader='source_item_read', arguments=arguments)
    if part == 'scope':
        if record.image_bindings:
            raise ValueError('Existing IMAGE items use visual/classification changes; scope replacement is TEXT/TABLE')
        from scope_tools import scope_template
        scope = scope_template(document_map, baseline, tables, core._record_scope_id(record))
        return _json_part(base, scope, arguments, offset=offset, max_chars=max_chars, link_transform=link_transform)
    if part == 'image_scope':
        return _json_part(base, image_packet, arguments, offset=offset, max_chars=max_chars, link_transform=link_transform)
    if part == 'decomposition_packet':
        arguments['part'] = part + '@' + context_review['context_bindings_sha256']
        packet = build_source_item_decomposition_packet(baseline, record.source_record_id, table_previews=tables)
        packet = {**packet, 'linked_source_context': linked_context, 'context_review': context_review,
            'linked_context_namespace': 'External source items explicitly linked by annotations; not parent segment_index ranges. The parent_evidence_sha256 retains its existing parent-facts contract.'}
        return _json_part(base, packet, arguments, offset=offset, max_chars=max_chars, link_transform=link_transform)
    if part and ':' in part:
        section, index = part.split(':', 1)
        if section not in {'source', 'shared_context'} or not index.isdecimal() or int(index) >= len(sections[section]):
            raise ValueError('Unknown item text part')
        fact = sections[section][int(index)]
        return _text({**base, 'part': part, 'source_ref': fact['source_ref']}, fact['text'], offset=offset,
            max_chars=max_chars, reader='source_item_read', arguments=arguments)
    if part in {'source', 'shared_context', 'images'}:
        entries = deepcopy(sections[part])
        for i, entry in enumerate(entries):
            if 'text' in entry:
                text = entry['text']; entry.update(text_total_characters=len(text), text_complete=True)
                if _size(entry) > max_chars // 2:
                    entry['text'] = text[:min(512, max_chars // 6)]; entry['text_complete'] = False
                    entry['next_text_read'] = _next('source_item_read', {'source_item_id': record.source_display_id,
                        'part': part + ':' + str(i), 'offset': len(entry['text']), 'max_chars': max_chars})
        return _window({**base, 'part': part}, entries, field='entries', offset=offset, limit=limit,
            max_chars=max_chars, reader='source_item_read', arguments=arguments)
    if part in sections:
        return _json_part(base, sections[part], arguments, offset=offset, max_chars=max_chars, link_transform=link_transform)
    if part is not None:
        raise ValueError('Unknown item reading part')
    links = {name: _next('source_item_read', {'source_item_id': record.source_display_id, 'part': name + '@' + context_review['context_bindings_sha256'] if name == 'decomposition_packet' else name})
        for name in ('scope', 'image_scope', 'decomposition_packet')
        if (name != 'scope' or not record.image_bindings) and (name != 'image_scope' or image_packet is not None)
        and (name != 'decomposition_packet' or sections['decomposition']['packet_available'])}
    full = {**base, **sections, 'reads': links, 'complete': True, 'next_read': None, 'budget_scope': 'RESULT_JSON_CHARACTERS',
        'range_namespace': 'segment_index and offsets refer to this parent decomposition packet; source_ref carries original absolute positions'}
    if offset == 0 and _size(full) <= max_chars:
        return full
    entries = []
    for name, value in sections.items():
        entry = {'part': name, 'read': _next('source_item_read', {'source_item_id': record.source_display_id, 'part': name}),
            'content_complete': False}
        if _size(value) < max_chars // 3:
            entry.update(data=value, content_complete=True)
        entries.append(entry)
    return _window({**base, 'reads': links, 'completeness_scope': 'section index only; follow content_complete=false reads'},
        entries, field='sections', offset=offset, limit=limit, max_chars=max_chars, reader='source_item_read', arguments=arguments)


def source_item_image(bundle_dir, *, source_item_id, image_anchor_id, expected_source_sha256,
        expected_map_sha256, expected_baseline_sha256, source_path, image_dir):
    """Materialize one actual owned-node anchor through the established renderer."""
    document_map, baseline, tables, _, _ = _bundle(bundle_dir, expected_source_sha256, expected_map_sha256, expected_baseline_sha256)
    record = source_item_record(baseline, source_item_id)
    node = next(n for n in document_map.nodes if n.node_id == record.node_id)
    values = _images(_image_scope(document_map, baseline, tables, node), record)
    image = next((i for i in values if i['anchor_id'] == image_anchor_id), None)
    if image is None:
        raise ValueError('Image anchor is not in this SourceItem owning Map node')
    return _render_image(image, source_path, image_dir, expected_source_sha256,
        {**_base(document_map, baseline, record), 'node_id': record.node_id})


def _render_image(image, source_path, image_dir, expected_source_sha256, base):
    source = Path(source_path).resolve(strict=True)
    if _hash(source.read_bytes()) != expected_source_sha256:
        raise ValueError('Image original differs from the selected task source')
    if source.suffix.lower() != '.pdf':
        raise ValueError('This image transport slice currently supports PDF sources')
    from app.services.exporter import _build_export_source_image_context, _load_matrix_figure_payload
    context = _build_export_source_image_context(source_file_path=source, source_sha256=expected_source_sha256)
    location = image.get('native_locator') or {'format': 'pdf', 'page_number': image['page'], 'bbox': image['bbox']}
    loaded = _load_matrix_figure_payload({**image, 'kind': 'image', 'page_number': image['page'],
        'source_bbox': image['bbox'], 'location': location}, source_image_context=context)
    if loaded is None:
        raise ValueError('Original anchor image payload could not be rendered')
    payload, _, _ = loaded
    from PIL import Image
    with Image.open(payload) as decoded:
        decoded.load(); width, height = decoded.size
        mime = Image.MIME.get(decoded.format)
        extension = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/tiff': '.tiff', 'image/bmp': '.bmp', 'image/gif': '.gif'}.get(mime)
    if extension is None:
        raise ValueError('Unsupported actual image payload type')
    raw = payload.getvalue(); digest = hashlib.sha256(raw).hexdigest()
    root = Path(image_dir).resolve(); root.mkdir(parents=True, exist_ok=True)
    target = root / (digest + extension)
    if target.exists():
        if target.is_symlink() or _hash(target.read_bytes()) != digest:
            raise ValueError('Source image cache payload changed')
    else:
        with NamedTemporaryFile(dir=root, prefix='.image-', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    return {**base, 'image_anchor_id': image['anchor_id'], 'page': image['page'],
        'bbox': image['bbox'], 'relation': image['relation'], 'path': str(target), 'mime_type': mime, 'sha256': digest,
        'width': width, 'height': height, 'original_authority': 'SOURCE_BOUND_IMAGE_PAYLOAD',
        'visual_interpretation': 'NOT_CREATED_BY_READING', 'semantic_acceptance': 'NOT_CLAIMED'}


def _initial_image_scope(map_dir, node_id, source_sha, map_sha, baseline_dir, baseline_sha):
    directory = Path(map_dir)
    document_map = core.DocumentMapArtifactV1.model_validate(_read(directory / 'document-map.json'))
    if (document_map.source_sha256, document_map.artifact_sha256) != (source_sha, map_sha):
        raise ValueError('Image scope Map is not the selected task version')
    baseline = None; tables = _read(directory / 'table-previews.json')
    if baseline_dir is not None:
        _, baseline, tables, _, _ = _bundle(baseline_dir, source_sha, map_sha, baseline_sha)
    elif baseline_sha is not None:
        raise ValueError('A selected baseline pin requires its actual bundle')
    packet = build_source_item_image_scope_packet(document_map, node_id=node_id, baseline=baseline, table_previews=tables)
    return packet, {'schema_version': 'reqpilot-task-image-reading/v1', 'source_sha256': source_sha,
        'document_map_artifact_sha256': map_sha, 'baseline_sha256': baseline_sha,
        'node_id': node_id, 'semantic_acceptance': 'NOT_CLAIMED'}


def image_scope_read(map_dir, *, node_id, expected_source_sha256, expected_map_sha256,
        baseline_dir=None, expected_baseline_sha256=None, offset=0, limit=20, max_chars=12000, link_transform=None):
    """Read a real pre-SRC image packet without pretending it is an existing item."""
    _limits(offset, limit, max_chars)
    packet, base = _initial_image_scope(map_dir, node_id, expected_source_sha256, expected_map_sha256,
        baseline_dir, expected_baseline_sha256)
    entries = []
    for key in ('images', 'table_image_relations', 'direct_source_context'):
        for value in packet.get(key, []):
            entry = {'kind': key, **value}
            binding = value['image_binding'] if key == 'table_image_relations' else value
            if key != 'direct_source_context':
                entry['read'] = _next('image_scope_image', {'node_id': node_id, 'image_anchor_id': binding['anchor_id']})
            entries.append(entry)
    # Ordinary scopes fit as one packet. Large metadata is a lossless JSON
    # stream, so even one large table relation cannot create a no-progress loop.
    value = {key: packet[key] for key in ('scope_id', 'node_label', 'node_title', 'ancestor_path',
        'packet_sha256', 'original_authority', 'interpretation_authority')}
    value['entries'] = entries
    # Public task links must be transformed before JSON is split into text.
    # A later transport traversal cannot inspect or safely rewrite partial JSON.
    if link_transform is not None:
        value = link_transform(value)
    full = {**base, **value, 'complete': True, 'next_read': None, 'budget_scope': 'RESULT_JSON_CHARACTERS'}
    if offset == 0 and _size(full) <= max_chars:
        return full
    return _text({**base, 'encoding': 'JSON_TEXT', 'reassembly': 'Concatenate all text chunks from offset 0, then parse JSON'},
        json.dumps(value, ensure_ascii=False, separators=(',', ':')), offset=offset, max_chars=max_chars,
        reader='image_scope_read', arguments={'node_id': node_id})


def image_scope_image(map_dir, *, node_id, image_anchor_id, expected_source_sha256, expected_map_sha256,
        source_path, image_dir, baseline_dir=None, expected_baseline_sha256=None):
    packet, base = _initial_image_scope(map_dir, node_id, expected_source_sha256, expected_map_sha256,
        baseline_dir, expected_baseline_sha256)
    bindings = [{**image, 'relation': 'STANDALONE_IMAGE_CANDIDATE'} for image in packet['images']]
    bindings += [{**value['image_binding'], 'relation': 'TABLE_EMBEDDED_IMAGE',
        'table_source_item_id': value['table_source_record_id']} for value in packet.get('table_image_relations', [])]
    image = next((value for value in bindings if value['anchor_id'] == image_anchor_id), None)
    if image is None:
        raise ValueError('Image anchor is not in the selected original Map node')
    return _render_image(image, source_path, image_dir, expected_source_sha256, base)
