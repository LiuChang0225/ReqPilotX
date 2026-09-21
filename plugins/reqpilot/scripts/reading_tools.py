"""Deterministic reading projection; no headings, deduplication or judgments."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path

from reqpilotx.domain.extraction.source_ir import SourceIRSnapshot


MAX_CHARS = 30000
MAX_RECORDS = 80


def _compact(value):
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def reading_packet(args):
    if args.source_ir is None or args.output_dir is None:
        raise ValueError('reading-packet requires --source-ir and a new --output-dir')
    source = Path(args.source_ir).expanduser().resolve(strict=True)
    original = source.read_bytes()
    SourceIRSnapshot.model_validate_json(original)
    data = json.loads(original)
    output = Path(args.output_dir).expanduser().resolve()
    if output.exists():
        raise ValueError('Output must not exist; use a new immutable reading directory')
    output.mkdir(parents=True, mode=0o700)
    groups = defaultdict(list)
    cell_count = 0
    image_count = 0
    text_chars = 0
    for index, node in enumerate(data['nodes']):
        page = node['native_locator'].get('page_number')
        pointer = f'/nodes/{index}'
        value = _compact(node)
        if value.get('visual_locator') == value.get('native_locator'):
            value.pop('visual_locator', None)
            value['visual_locator_same_as_native'] = True
        lines = value.pop('pdf_text_lines', None)
        if lines:
            value['pdf_text_lines_ref'] = {'json_pointer': pointer + '/pdf_text_lines', 'count': len(lines)}
        table = value.get('table_payload')
        cells = table.pop('cells') if table else []
        if table:
            table['cell_records'] = len(cells)
        groups[page].append({'record': 'node', 'json_pointer': pointer, **value})
        text_chars += len(node.get('raw_text', ''))
        image_count += bool(node.get('asset_ref'))
        for cell_index, cell in enumerate(cells):
            groups[page].append({'record': 'cell', 'source_node_id': node['source_node_id'],
                                 'table_id': table['table_id'],
                                 'json_pointer': pointer + f'/table_payload/cells/{cell_index}', **cell})
            cell_count += 1
            text_chars += len(cell.get('raw_text', ''))
    # Keep capture diagnostics, assets and optional visual observations accessible.
    # They are evidence, not accepted source semantics or a reading completion flag.
    groups[None].append({'record': 'capture_metadata', 'json_pointer': '',
                         **_compact({key: value for key, value in data.items() if key not in {'nodes', 'assets'}})})
    for index, asset in enumerate(data.get('assets', [])):
        groups[None].append({'record': 'asset', 'json_pointer': f'/assets/{index}', **_compact(asset)})
    parts = []

    def flush(page, records, number):
        if not records:
            return
        name = f'page-{page:04d}' if page is not None else 'unpaged'
        path = output / f'{name}-part-{number:03d}.jsonl'
        content = '\n'.join(records) + '\n'
        path.write_text(content, encoding='utf-8')
        path.chmod(0o600)
        parts.append({'path': str(path), 'page': page, 'records': len(records),
                      'characters': len(content), 'sha256': hashlib.sha256(content.encode()).hexdigest(),
                      'oversized_record': len(content) > MAX_CHARS,
                      'read': {'path': str(path), 'start_line': 1, 'line_count': len(records)}})

    for page in sorted(groups, key=lambda item: (item is None, item or 0)):
        current, size, number = [], 0, 1
        for record in groups[page]:
            line = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
            if current and (size + len(line) + 1 > MAX_CHARS or len(current) >= MAX_RECORDS):
                flush(page, current, number)
                current, size, number = [], 0, number + 1
            current.append(line)
            size += len(line) + 1
        flush(page, current, number)
    manifest = {
        'schema_version': 'reqpilot-source-reading/v1',
        'source_ir_path': str(source), 'source_ir_sha256': hashlib.sha256(original).hexdigest(),
        'source_ir_id': data['source_ir_id'], 'source_sha256': data['source_sha256'],
        'capture_status': data['status'], 'semantic_authority': 'NONE',
        'inventory': {'nodes': len(data['nodes']), 'cells': cell_count, 'image_occurrences': image_count,
                      'assets': len(data.get('assets', [])), 'raw_text_characters': text_chars},
        'projection': 'Full node/cell text, table relations, native locators and assets. Null fields omitted; identical visual locator aliases native locator; PDF line geometry remains at exact SourceIR pointers. No semantic deduplication. Capture order is not accepted reading order. Generated inventory does not mean Agent-read or visually verified.',
        'asset_paths': 'storage_key is unchanged capture metadata, not a guessed filesystem path; use the capture manifest to resolve local assets.',
        'parts': parts,
    }
    path = output / 'reading-manifest.json'
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    path.chmod(0o600)
    return {'status': 'READING_PACKET_READY', 'manifest_path': str(path),
            'source_ir_sha256': manifest['source_ir_sha256'], 'inventory': manifest['inventory'],
            'parts': parts, 'semantic_authority': 'NONE',
            'next': 'Read each required part once using its returned read arguments. Track read parts, visual checks and unresolved ranges separately. Oversized records remain intact: use exact SourceIR pointers for narrower fields. Generate again only if SourceIR changes.'}
