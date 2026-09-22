"""Verify supplied Office evidence by replaying the original native adapters."""
from __future__ import annotations

import hashlib
from pathlib import Path

from reqpilotx.domain.extraction.source_ir import SourceIRSnapshot


def verify_office_source_ir(source_path, snapshot):
    from reqpilotx.adapters.documents.preflight import LocalDocumentPreflight
    from reqpilotx.adapters.documents.docx_source_ir_adapter import DocxSourceIRAdapter
    from reqpilotx.adapters.documents.xlsx_source_ir_adapter import XlsxSourceIRAdapter
    path = Path(source_path)
    snapshot = SourceIRSnapshot.model_validate(snapshot)
    if hashlib.sha256(path.read_bytes()).hexdigest() != snapshot.source_sha256:
        raise ValueError('Office original bytes differ from the supplied source evidence')
    preflight = LocalDocumentPreflight().inspect(path)
    format_name = preflight.identity.detected_format.value
    adapters = {'docx': DocxSourceIRAdapter, 'xlsx': XlsxSourceIRAdapter}
    if format_name not in adapters:
        raise ValueError('Native Office Map currently supports original DOCX and XLSX')
    replay = adapters[format_name]().extract(path, preflight=preflight, created_at=snapshot.created_at)
    if (replay.source_sha256 != snapshot.source_sha256 or replay.source_ir_id != snapshot.source_ir_id
            or replay.nodes != snapshot.nodes or replay.assets != snapshot.assets):
        raise ValueError('Office locator/text/table/image evidence differs from replaying the original file')
    return format_name


def validate_office_image_reference(source_path, source_sha256, figure, *, cache=None):
    """Resolve the cited native object and its bytes from the original package."""
    from datetime import datetime, timezone
    from reqpilotx.adapters.documents.preflight import LocalDocumentPreflight
    from reqpilotx.adapters.documents.docx_source_ir_adapter import DocxSourceIRAdapter
    from reqpilotx.adapters.documents.xlsx_source_ir_adapter import XlsxSourceIRAdapter
    from reqpilotx.domain.extraction.source_ir import NativeLocator
    path = Path(source_path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != source_sha256 or figure.get('source_sha256') != source_sha256:
        raise ValueError('Office image original SHA mismatch')
    cache = cache if cache is not None else {}
    key = (str(path), source_sha256)
    if key not in cache:
        preflight = LocalDocumentPreflight().inspect(path)
        adapter = {'docx': DocxSourceIRAdapter, 'xlsx': XlsxSourceIRAdapter}.get(preflight.identity.detected_format.value)
        if adapter is None:
            raise ValueError('Office image needs its original DOCX/XLSX package')
        cache[key] = adapter().extract(path, preflight=preflight, created_at=datetime.now(timezone.utc))
    snapshot = cache[key]
    handle = figure.get('source_handle') or figure.get('anchor_id')
    node = next((node for node in snapshot.nodes if node.source_node_id == handle and node.kind.value in {'image', 'figure'}), None)
    loc = NativeLocator.model_validate(figure.get('location') or figure.get('native_locator'))
    asset = next((asset for asset in snapshot.assets if node is not None and asset.asset_id == node.asset_ref), None)
    if node is None or asset is None or node.native_locator != loc or node.asset_ref != figure.get('asset_id') or asset.content_sha256 != figure.get('content_sha256'):
        raise ValueError('Office image locator/asset differs from the actual original object')
    if figure.get('native_locator') is not None and NativeLocator.model_validate(figure['native_locator']) != loc:
        raise ValueError('Office image native locator disagrees with its displayed location')
    return True


def capture_native_office_source(source_path, expected_source_sha256):
    from datetime import datetime, timezone
    from reqpilotx.adapters.documents.preflight import LocalDocumentPreflight
    from reqpilotx.adapters.documents.docx_source_ir_adapter import DocxSourceIRAdapter
    from reqpilotx.adapters.documents.xlsx_source_ir_adapter import XlsxSourceIRAdapter
    path = Path(source_path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_source_sha256:
        raise ValueError('Native source preview original SHA mismatch')
    preflight = LocalDocumentPreflight().inspect(path)
    adapter = {'docx': DocxSourceIRAdapter, 'xlsx': XlsxSourceIRAdapter}.get(preflight.identity.detected_format.value)
    if adapter is None:
        raise ValueError('Native source preview supports original DOCX and XLSX')
    return adapter().extract(path, preflight=preflight, created_at=datetime.now(timezone.utc))


def build_native_office_source_view(source_path, expected_source_sha256, *, image_url_prefix=''):
    from urllib.parse import quote, urlencode
    snapshot = capture_native_office_source(source_path, expected_source_sha256)
    assets = {asset.asset_id: asset for asset in snapshot.assets}
    nodes = []
    for node in sorted(snapshot.nodes, key=lambda value: value.source_order):
        image = None
        if node.asset_ref and node.asset_ref in assets:
            asset = assets[node.asset_ref]
            image = {'asset_id': asset.asset_id, 'content_sha256': asset.content_sha256,
                'media_type': asset.media_type, 'pixel_width': None, 'pixel_height': None}
            if image_url_prefix:
                image['url'] = image_url_prefix.rstrip('/') + '/' + quote(node.source_node_id, safe='') + '?' + urlencode({'source_sha256': snapshot.source_sha256, 'content_sha256': asset.content_sha256})
        nodes.append({'source_node_id': node.source_node_id, 'parent_source_node_id': node.parent_source_node_id,
            'kind': node.kind.value, 'source_sequence': node.source_order,
            'native_locator': node.native_locator.model_dump(mode='json', exclude_none=True), 'text': node.raw_text,
            'table': node.table_payload.model_dump(mode='json') if node.table_payload else None, 'image': image})
    return {'schema_version': 'reqpilot-native-source-view/v1', 'source_sha256': snapshot.source_sha256,
        'source_format': snapshot.nodes[0].native_locator.format.value if snapshot.nodes else Path(source_path).suffix.lstrip('.').lower(),
        'source_file_name': Path(source_path).name, 'status': snapshot.status.value, 'authority': 'ORIGINAL_SOURCE_NATIVE_CAPTURE',
        'nodes': nodes, 'diagnostics': [item.model_dump(mode='json') for item in snapshot.diagnostics]}


def read_native_office_source_image(source_path, expected_source_sha256, source_node_id, content_sha256):
    """Read one actual original object; a digest alone never selects another one."""
    snapshot = capture_native_office_source(source_path, expected_source_sha256)
    node = next((node for node in snapshot.nodes if node.source_node_id == source_node_id and node.kind.value in {'image', 'figure'}), None)
    asset = next((asset for asset in snapshot.assets if node is not None and asset.asset_id == node.asset_ref), None)
    if node is None or asset is None or asset.content_sha256 != content_sha256:
        raise ValueError('Native image object or content SHA does not match the original source')
    from reqpilotx.services.exporter import _build_export_source_image_context, _source_package_asset_payload
    context = _build_export_source_image_context(source_file_path=source_path, source_sha256=expected_source_sha256)
    payload = _source_package_asset_payload({'content_sha256': content_sha256}, source_image_context=context)
    if payload is None:
        raise ValueError('Native image original payload is unavailable or not a supported raster')
    data, width, height = payload
    return {'bytes': data.getvalue(), 'media_type': asset.media_type, 'width': width, 'height': height,
        'source_sha256': snapshot.source_sha256, 'content_sha256': asset.content_sha256,
        'source_node_id': node.source_node_id, 'native_locator': node.native_locator.model_dump(mode='json', exclude_none=True)}


def verify_office_map_against_source(source_path, document_map, table_previews, *, baseline=None):
    """Revalidate exported/imported Office locations against actual source bytes."""
    from reqpilotx.application.agent_workflow import source_requirement_baseline as core
    from reqpilotx.domain.extraction.source_ir import NativeLocator
    mapping = core.DocumentMapArtifactV1.model_validate(document_map)
    snapshot = capture_native_office_source(source_path, mapping.source_sha256)
    if any(node.native_locator.format.value != mapping.source_format for node in snapshot.nodes):
        raise ValueError('Office Map original format changed')
    nodes = {node.source_node_id: node for node in snapshot.nodes}
    assets = {asset.asset_id: asset for asset in snapshot.assets}
    tables = {node.table_payload.table_id: node for node in snapshot.nodes if node.table_payload}
    for owner in mapping.nodes:
        for content in owner.source_content:
            source = nodes.get(content.content_id)
            if (source is None or content.text != source.raw_text or content.native_locator != source.native_locator
                    or content.source_sequence != source.source_order or content.source_handles != (source.source_node_id,)):
                raise ValueError('Office Map text/location differs from its actual original source: ' + content.content_id)
        for anchor in owner.source_anchors:
            source = nodes.get(anchor.source_handle)
            if source is None or anchor.native_locator != source.native_locator or anchor.source_sequence != source.source_order:
                raise ValueError('Office Map anchor location differs from its actual original object: ' + anchor.anchor_id)
            if anchor.anchor_kind in {'IMAGE', 'FIGURE'}:
                asset = assets.get(source.asset_ref)
                if source.kind.value not in {'image', 'figure'} or asset is None or anchor.asset_id != asset.asset_id or anchor.content_sha256 != asset.content_sha256:
                    raise ValueError('Office Map image differs from its actual original asset: ' + anchor.anchor_id)
            if anchor.anchor_kind == 'TABLE':
                if source.table_payload is None or anchor.table_token != source.table_payload.table_id:
                    raise ValueError('Office Map table anchor does not resolve to the original TableIR')
    if set(table_previews) != {table.scope_id for table in mapping.logical_tables}:
        raise ValueError('Office table preview scopes differ from the original Map')
    for table in mapping.logical_tables:
        preview = table_previews[table.scope_id]
        request = core.build_source_requirement_scope_request(mapping, preview)
        if preview.get('source_sha256') != mapping.source_sha256 or len(request.fragments) != len(table.fragments):
            raise ValueError('Office table preview source/fragment count changed')
        for fragment, mapped in zip(request.fragments, table.fragments):
            source = tables.get(fragment.table_token)
            if (source is None or (fragment.table_token, fragment.row_range, fragment.native_locator, fragment.source_sequence)
                    != (mapped.table_token, mapped.row_range, mapped.native_locator, mapped.source_sequence)
                    or fragment.native_locator != source.native_locator or fragment.source_sequence != source.source_order):
                raise ValueError('Office table fragment differs from its actual native source')
            original = source.table_payload; lo, hi = fragment.row_range
            expected = {cell.cell_id: cell for cell in original.cells if cell.row < hi and cell.row + cell.row_span > lo}
            if (not 0 <= lo < hi <= original.row_count or fragment.row_count != original.row_count
                    or fragment.column_count != original.column_count or {cell.cell_id for cell in fragment.cells} != set(expected)):
                raise ValueError('Office table grid changed from its actual original region')
            for cell in fragment.cells:
                raw = expected[cell.cell_id]
                if ((cell.row, cell.column, cell.row_span, cell.column_span, cell.text, cell.native_locator)
                        != (raw.row, raw.column, raw.row_span, raw.column_span, raw.raw_text, raw.native_locator)):
                    raise ValueError('Office table cell differs from its actual original value/location: ' + cell.cell_id)
    if baseline is not None:
        from reqpilotx.application.agent_workflow.document_map_revision import _replay
        _replay(mapping, core.SourceRequirementBaselineArtifactV1.model_validate(baseline), table_previews)
    return {'source_sha256': snapshot.source_sha256, 'source_ir_id': snapshot.source_ir_id,
        'source_format': mapping.source_format, 'status': 'ORIGINAL_NATIVE_LOCATIONS_VERIFIED'}
