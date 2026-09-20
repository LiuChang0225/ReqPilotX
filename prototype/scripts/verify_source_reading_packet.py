"""Reconstruct and verify a reading projection against its exact SourceIR.

This checks captured data fidelity, not PDF glyph coverage or Agent understanding.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.domain.extraction.source_ir import SourceIRSnapshot


def verify(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text())
    original = Path(manifest['source_ir_path']).read_bytes()
    if hashlib.sha256(original).hexdigest() != manifest['source_ir_sha256']:
        raise ValueError('SourceIR changed after reading-packet generation')
    source = json.loads(original)
    nodes, assets, metadata, cells = {}, {}, [], {}
    total_bytes = total_lines = 0
    for part in manifest['parts']:
        content = Path(part['path']).read_bytes()
        if hashlib.sha256(content).hexdigest() != part['sha256']:
            raise ValueError('Reading part digest mismatch: ' + part['path'])
        lines = content.decode().splitlines()
        if len(lines) != part['records'] or len(content.decode()) != part['characters']:
            raise ValueError('Reading part count mismatch')
        total_bytes += len(content)
        total_lines += len(lines)
        for line in lines:
            value = json.loads(line)
            kind, pointer = value.pop('record'), value.pop('json_pointer')
            if kind == 'node':
                index = int(pointer.split('/')[2])
                if pointer != f'/nodes/{index}':
                    raise ValueError('Wrong node pointer')
                if index in nodes:
                    raise ValueError('Duplicate source node')
                if value.pop('visual_locator_same_as_native', False):
                    value['visual_locator'] = value['native_locator']
                geometry = value.pop('pdf_text_lines_ref', None)
                if geometry:
                    if geometry['json_pointer'] != pointer + '/pdf_text_lines':
                        raise ValueError('Wrong PDF geometry pointer')
                    value['pdf_text_lines'] = source['nodes'][index]['pdf_text_lines']
                    if len(value['pdf_text_lines']) != geometry['count']:
                        raise ValueError('Wrong PDF line count')
                nodes[index] = value
            elif kind == 'cell':
                index, cell_index = int(pointer.split('/')[2]), int(pointer.split('/')[5])
                if pointer != f'/nodes/{index}/table_payload/cells/{cell_index}':
                    raise ValueError('Wrong cell pointer')
                key = (index, cell_index)
                if key in cells:
                    raise ValueError('Duplicate cell')
                owner = source['nodes'][index]
                if value.pop('source_node_id') != owner['source_node_id'] or value.pop('table_id') != owner['table_payload']['table_id']:
                    raise ValueError('Cell owner mismatch')
                cells[key] = value
            elif kind == 'asset':
                index = int(pointer.split('/')[2])
                if pointer != f'/assets/{index}':
                    raise ValueError('Wrong asset pointer')
                if index in assets:
                    raise ValueError('Duplicate asset')
                assets[index] = value
            elif kind == 'capture_metadata':
                if pointer != '':
                    raise ValueError('Wrong capture metadata pointer')
                metadata.append(value)
            else:
                raise ValueError('Unknown reading record')
    if len(metadata) != 1 or set(nodes) != set(range(len(source['nodes']))) or set(assets) != set(range(len(source.get('assets', [])))):
        raise ValueError('Incomplete capture inventory')
    expected_cells = {(i, j) for i, node in enumerate(source['nodes']) for j in range(len((node.get('table_payload') or {}).get('cells', [])))}
    if set(cells) != expected_cells:
        raise ValueError('Incomplete cell inventory')
    for index, node in nodes.items():
        table = node.get('table_payload')
        if table is not None:
            table['cells'] = [cells[key] for key in sorted(cells) if key[0] == index]
            if table.pop('cell_records') != len(table['cells']):
                raise ValueError('Table cell count mismatch')
    rebuilt = {**metadata[0], 'nodes': [nodes[i] for i in sorted(nodes)], 'assets': [assets[i] for i in sorted(assets)]}
    expected = SourceIRSnapshot.model_validate_json(original)
    if SourceIRSnapshot.model_validate(rebuilt) != expected:
        raise ValueError('Reconstructed source fields differ')
    # This is the old full nodes JSON representation, not all capture sidecars.
    old = json.dumps(source['nodes'], ensure_ascii=False, indent=2)
    return {'status': 'CAPTURED_DATA_EQUIVALENT_WITH_DECLARED_GEOMETRY_REFERENCES',
            'semantic_quality': 'NOT_TESTED', 'original_visual_coverage': 'NOT_TESTED',
            'source_ir_sha256': manifest['source_ir_sha256'],
            'nodes': len(nodes), 'cells': len(cells), 'assets': len(assets),
            'image_occurrences': sum(bool(node.get('asset_ref')) for node in source['nodes']),
            'old_nodes_bytes': len(old.encode()), 'old_nodes_lines': len(old.splitlines()),
            'packet_bytes': total_bytes, 'packet_records': total_lines, 'packet_parts': len(manifest['parts']),
            'byte_reduction_percent': round(100 * (1 - total_bytes / len(old.encode())), 2),
            'oversized_parts': sum(p['oversized_record'] for p in manifest['parts'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = json.dumps(verify(args.manifest), ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(result)
    print(result, end='')
