"""Expose proven page/table/render views over an existing immutable capture."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from reqpilotx.adapters.documents.pdf_reading_views import (
    _page_markdown, _table_html, _write_page_renders, _write_contact_sheet,
)
from reqpilotx.adapters.documents.pdf_source_crop_renderer import inspect_pdf_page_geometry
from reqpilotx.domain.extraction.source_ir import SourceIRSnapshot


def reading_workspace(args):
    if not args.source or not args.source_ir or not args.output_dir:
        raise ValueError('reading-workspace needs --source, --source-ir and a new --output-dir')
    source, ir_path = args.source.resolve(strict=True), args.source_ir.resolve(strict=True)
    snapshot = SourceIRSnapshot.model_validate_json(ir_path.read_bytes())
    if source.suffix.lower() != '.pdf':
        raise ValueError('This reading-workspace entry currently supports PDF originals')
    geometry = inspect_pdf_page_geometry(source, expected_source_sha256=snapshot.source_sha256)
    output = args.output_dir.resolve()
    if output.exists():
        raise ValueError('Use a new reading workspace; existing evidence is immutable')
    for node in snapshot.nodes:
        if node.native_locator.page_number not in range(1, len(geometry) + 1):
            raise ValueError('A SourceIR node is outside the original PDF pages')
    output.mkdir(parents=True, mode=0o700)
    cwd = Path.cwd().resolve()

    def address(path):
        return str(path.relative_to(cwd)) if path.is_relative_to(cwd) else str(path)

    def save(name, value):
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        path.chmod(0o600)
        return address(path)

    source_copy, ir_copy = output / 'source.pdf', output / 'source_ir.json'
    shutil.copyfile(source, source_copy)
    shutil.copyfile(ir_path, ir_copy)
    renders, thumbnails = _write_page_renders(output, source_copy,
        expected_source_sha256=snapshot.source_sha256, page_count=len(geometry))
    _write_contact_sheet(output / 'contact-sheet.png', thumbnails)
    by_page = defaultdict(list)
    for node in snapshot.nodes:
        by_page[node.native_locator.page_number].append(node)
    assets, gaps = [], []
    capture_manifest = ir_path.parent / 'manifest.json'
    capture = json.loads(capture_manifest.read_text()) if capture_manifest.is_file() else {}
    captured_assets = {item['asset_id']: item for item in capture.get('artifacts', {}).get('assets', [])}
    sidecars = {}
    for key in ('native_outline', 'pdf_annotations'):
        relative = capture.get('artifacts', {}).get(key)
        if not relative:
            continue
        candidate = ir_path.parent / relative
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(ir_path.parent):
            raise ValueError('Capture sidecar leaves its source directory')
        value = json.loads(candidate.read_bytes())
        if value.get('source_sha256') != snapshot.source_sha256:
            raise ValueError('Capture sidecar belongs to another original')
        sidecars[key] = save('source-metadata/' + key + '.json', value)
    for asset in snapshot.assets:
        entry = captured_assets.get(asset.asset_id)
        asset_path = None
        if entry:
            candidate = ir_path.parent / entry['path']
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(ir_path.parent):
                raise ValueError('Capture asset path escapes the bound capture')
            data = candidate.read_bytes()
            if hashlib.sha256(data).hexdigest() != asset.content_sha256:
                raise ValueError('Capture image differs from its SourceIR identity')
            destination = output / 'assets' / (asset.asset_id + candidate.suffix)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            asset_path = address(destination)
        else:
            gaps.append({'asset_id': asset.asset_id, 'reason': 'ASSET_FILE_UNAVAILABLE_USE_ORIGINAL_PAGE'})
        assets.append({**asset.model_dump(mode='json', exclude_none=True), 'path': asset_path})
    asset_paths = {item['asset_id']: item['path'] for item in assets}
    pages, tables, placements = [], [], []
    for page_number, page_geometry in enumerate(geometry, 1):
        nodes = by_page[page_number]
        texts = tuple(SimpleNamespace(source_handle=n.source_node_id, kind=n.kind.value,
                                      bbox=n.native_locator.bbox, text=n.raw_text)
                      for n in nodes if n.table_payload is None and n.raw_text)
        page_tables = []
        for node in nodes:
            if node.table_payload:
                table = node.table_payload.model_dump(mode='json', exclude_none=True)
                json_path = save('tables/' + table['table_id'] + '.json', {
                    'source_node_id': node.source_node_id, 'source_sha256': snapshot.source_sha256,
                    'native_locator': node.native_locator.model_dump(mode='json', exclude_none=True),
                    **table})
                html_path = output / 'tables' / (table['table_id'] + '.html')
                html_path.write_text(_table_html(table), encoding='utf-8')
                record = {'source_node_id': node.source_node_id, 'table_id': table['table_id'],
                          'page': page_number, 'json_path': json_path, 'html_path': address(html_path),
                          'cell_count': len(table['cells']), 'bbox': node.native_locator.bbox}
                tables.append(record)
                page_tables.append(record)
            if node.asset_ref:
                placements.append({'source_node_id': node.source_node_id, 'asset_id': node.asset_ref,
                    'page': page_number, 'bbox': node.native_locator.bbox,
                    'parent_source_node_id': node.parent_source_node_id,
                    'asset_path': asset_paths.get(node.asset_ref), 'semantic_owner': 'NOT_DECIDED'})
        md = output / 'pages' / f'P{page_number:04d}.md'
        md.parent.mkdir(parents=True, exist_ok=True)
        wording = _page_markdown({'page_number': page_number,
            'evidence_requirement': 'READ_SOURCE_AND_VERIFY_VISUALLY', 'bounds': page_geometry.bounds,
            'table_ids': [item['table_id'] for item in page_tables],
            'asset_ids': [n.asset_ref for n in nodes if n.asset_ref]}, texts)
        wording += '\n## Captured tables (candidates, not business boundaries)\n\n'
        wording += '\n'.join(f"- {item['table_id']}: {item['html_path']} ; exact cells: {item['json_path']}" for item in page_tables) + '\n'
        wording += '\n## Image occurrences (may belong inside a table or clause)\n\n'
        wording += '\n'.join(f"- {item['source_node_id']} at {item['bbox']}: {item['asset_path'] or 'use original PDF region'}"
                             for item in placements if item['page'] == page_number) + '\n'
        md.write_text(wording, encoding='utf-8')
        pages.append({'page': page_number, 'bounds': page_geometry.bounds,
            'rotation_cw_degrees': page_geometry.rotation_cw_degrees,
            'markdown_path': address(md), 'markdown_lines': len(wording.splitlines()),
            'high_render': {**renders[page_number], 'path': address(output / renders[page_number]['path'])},
            'table_ids': [item['table_id'] for item in page_tables],
            'image_source_ids': [n.source_node_id for n in nodes if n.asset_ref],
            'native_text_characters': sum(len(t.text) for t in texts),
            'reading_status': 'NOT_RECORDED'})
    indexes = {'pages': save('pages/index.json', pages), 'tables': save('tables/index.json', tables),
               'assets': save('assets/index.json', assets), 'placements': save('assets/placements.json', placements)}
    manifest = {'schema_version': 'reqpilot-reading-workspace/v1',
        'path_base': 'COMMAND_WORKING_DIRECTORY', 'source_sha256': snapshot.source_sha256,
        'source_ir_sha256': hashlib.sha256(ir_copy.read_bytes()).hexdigest(),
        'source_path': address(source_copy), 'source_ir_path': address(ir_copy),
        'capture_status': snapshot.status.value, 'semantic_authority': 'NONE',
        'contact_sheet_path': address(output / 'contact-sheet.png'), **indexes,
        'capture_sidecars': sidecars,
        'counts': {'pages': len(pages), 'nodes': len(snapshot.nodes), 'tables': len(tables),
                   'cells': sum(len(n.table_payload.cells) for n in snapshot.nodes if n.table_payload),
                   'image_occurrences': len(placements), 'assets': len(assets)},
        'capture_diagnostics': [d.model_dump(mode='json') for d in snapshot.diagnostics], 'asset_gaps': gaps,
        'notes': 'Same original and capture; no reparse, OCR, semantic deduplication or hierarchy decisions. MD text and table cells may overlap. HTML preserves cells/merges but original pixels establish images and layout. Base renders omit annotation overlays; read the annotation sidecar and request annotations=true on the original PDF when relevant. All pages remain accessible, including pages with no captured nodes. Generated files are not proof of Agent reading.'}
    manifest_path = save('reading-workspace.json', manifest)
    return {'status': 'READING_WORKSPACE_READY', 'manifest_path': manifest_path,
        'source_sha256': snapshot.source_sha256, 'counts': manifest['counts'],
        'contact_sheet_path': manifest['contact_sheet_path'], **indexes,
        'next': 'Read the page index, then full page MD/table views and original high renders for the chosen semantic scope. Use returned paths directly. For detail pass page, bbox and scale to reqpilot_read when offered. Reuse this workspace while source and capture hashes match; track actual reads and unresolved ranges separately.'}
