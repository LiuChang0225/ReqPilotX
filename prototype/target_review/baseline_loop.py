"""Version a reviewed baseline using explicit, hash-bound Agent decisions.

This tool applies decisions; it does not infer semantics or dispatch an Agent.
Original text is immutable. Superseded units remain in the private lineage ledger.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

from prototype.target_review.server import canonical
from prototype.target_review.source_order import freeze_section_positions

CLASSES = {'F': 'PRODUCT_SYSTEM.FUNCTIONAL', 'Q': 'PRODUCT_SYSTEM.QUALITY',
           'C': 'PRODUCT_SYSTEM.CONSTRAINT'}
PATHS = set(CLASSES.values()) | {'PROJECT', 'PROCESS'}
TOPICS = {'REGULATORY': '法规', 'VERIFICATION': '试验验证',
          'LOGISTICS_PACKAGING': '物流包装', 'STANDARDS': '标准依据'}


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def load_bundle(directory):
    bundle = json.loads((directory / 'review-bundle.json').read_text())
    if bundle['revision'] != digest({k: v for k, v in bundle.items() if k != 'revision'}):
        raise ValueError('Input revision mismatch')
    if hashlib.sha256((directory / 'source.pdf').read_bytes()).hexdigest() != bundle['source']['sha256']:
        raise ValueError('Input PDF mismatch')
    return bundle


def normalize_coordinates(bundle, pdf_path):
    """Convert explicitly declared capture frames to PDF points; never clamp errors."""
    import fitz
    transforms = {}
    with fitz.open(pdf_path) as pdf:
        for page in bundle['pages']:
            number = page.get('number', page.get('page'))
            rect = pdf[number - 1].rect
            sx, sy = rect.width / page['width'], rect.height / page['height']
            # Tiny serialization/rounding differences are not a different frame.
            if abs(sx - 1) > .001 or abs(sy - 1) > .001:
                transforms[number] = {'x': sx, 'y': sy, 'from': [page['width'], page['height']],
                                      'to': [rect.width, rect.height]}
            page.update(number=number, width=rect.width, height=rect.height,
                        coordinateSpace='PDF_POINTS')
    def rect(value, scale):
        return [n * scale['x' if i % 2 == 0 else 'y'] for i, n in enumerate(value)]
    def visit(value, inherited=None):
        if isinstance(value, list):
            for child in value:
                visit(child, inherited)
        elif isinstance(value, dict):
            page = value.get('page', inherited)
            scale = transforms.get(page)
            for key, child in list(value.items()):
                if scale and key in {'bbox', 'clip'} and isinstance(child, list) and len(child) == 4:
                    value[key] = rect(child, scale)
                elif scale and key == 'rects':
                    value[key] = [rect(r, scale) for r in child]
                elif scale and key == 'quad':
                    value[key] = [[x * scale['x'], y * scale['y']] for x, y in child]
                elif scale and key == 'cells' and isinstance(child, list) and child and isinstance(child[0], list):
                    value[key] = [[rect(r, scale) if r else r for r in row] for row in child]
                else:
                    visit(child, page)
    for key in ('items', 'lines', 'tables', 'figures', 'outline', 'issues'):
        visit(bundle.get(key))
    return transforms


def audit(bundle, pdf_path):
    import fitz
    errors, observations = [], []
    ids = [t['id'] for i in bundle['items'] for t in [i, *i.get('derived', [])]]
    if len(set(ids)) != len(ids):
        errors.append({'kind': 'DUPLICATE_TARGET'})
    sections = {s['id']: s for s in bundle['outline']}
    pages = {p.get('number', p.get('page')): p for p in bundle['pages']}
    with fitz.open(pdf_path) as pdf:
        if set(pages) != set(range(1, len(pdf) + 1)):
            errors.append({'kind': 'PAGE_LEDGER_GAP'})
        mismatched = [n for n, p in pages.items() if abs(p['width'] - pdf[n-1].rect.width) > 1
                      or abs(p['height'] - pdf[n-1].rect.height) > 1]
        if mismatched:
            errors.append({'kind': 'COORDINATE_FRAME_MISMATCH', 'pages': mismatched})
    referenced_pages = set()
    for section in sections.values():
        seen, current = set(), section
        while current:
            if current['id'] in seen:
                errors.append({'kind': 'OUTLINE_CYCLE', 'id': section['id']})
                break
            seen.add(current['id'])
            parent = current.get('parentId')
            if parent and parent not in sections:
                errors.append({'kind': 'MISSING_OUTLINE_PARENT', 'id': section['id'], 'parent': parent})
            current = sections.get(parent)
    for item in bundle['items']:
        if item['sectionId'] not in sections:
            errors.append({'kind': 'MISSING_SECTION', 'id': item['id']})
        for target in [item, *item.get('derived', [])]:
            if not target.get('sourceRefs'):
                errors.append({'kind': 'MISSING_SOURCE', 'id': target['id']})
            for ref in target.get('sourceRefs', []):
                number = ref['page']
                referenced_pages.add(number)
                if number not in pages:
                    errors.append({'kind': 'UNKNOWN_PAGE', 'id': target['id']})
                    continue
                page = pages[number]
                for lid in ref.get('lineIds', []):
                    fact = bundle['lines'].get(lid)
                    if not fact or fact.get('page') != number:
                        errors.append({'kind': 'BROKEN_LINE_REF', 'id': target['id'], 'line': lid})
                for r in ref.get('rects', []):
                    if not (len(r) == 4 and -.1 <= r[0] < r[2] <= page['width'] + .1
                            and -.1 <= r[1] < r[3] <= page['height'] + .1):
                        errors.append({'kind': 'INVALID_RECT', 'id': target['id'], 'page': number, 'rect': r})
            meta = target.get('reviewMetadata', {})
            if set(meta.get('classifications', [])) - PATHS:
                errors.append({'kind': 'UNKNOWN_CLASS', 'id': target['id']})
            if meta.get('classificationStatus') == 'LEGACY_RETAINED':
                observations.append({'kind': 'AGENT_REVIEW_REMAINS', 'id': target['id']})
        seen_text = {}
        for child in item.get('derived', []):
            text = ''.join(child['text'].split())
            if text in seen_text:
                observations.append({'kind': 'DUPLICATE_DERIVED_TEXT', 'id': child['id'], 'other': seen_text[text]})
            seen_text[text] = child['id']
    order_check = None
    if bundle.get('ui') and all('reviewMetadata' in target for item in bundle['items']
                                for target in [item, *item.get('derived', [])]):
        from types import SimpleNamespace
        from prototype.target_review.react_contract import matrix
        snapshot = matrix(SimpleNamespace(bundle=bundle, source_revision=bundle.get('revision', 'audit'),
                            state=lambda _: {'revision': 0, 'targets': {}}))
        nodes = {node['node_id']: node for node in snapshot['nodes']}
        originals = []
        def visit(node_id):
            node = nodes[node_id]
            if node['source_kind'] == 'SOURCE_ITEM':
                originals.append(node)
            for child_id in node['child_ids']:
                visit(child_id)
        for root_id in snapshot['root_node_ids']:
            visit(root_id)
        backsteps = [{'kind': 'SOURCE_ORDER_BACKTRACK', 'before': left['node_id'], 'after': right['node_id']}
                     for left, right in zip(originals, originals[1:])
                     if left['source_order'] > right['source_order']]
        errors.extend(backsteps)
        order_check = {'rootIds': snapshot['root_node_ids'], 'originalCount': len(originals),
                       'originalBacksteps': backsteps,
                       'missingStructurePositions': [s['id'] for s in bundle['outline'] if 'sourceOrder' not in s]}
    return {'errors': errors, 'observations': observations, 'pages': len(pages), 'sourceOrdering': order_check,
            'pagesWithItemRefs': len(referenced_pages),
            'originals': len(bundle['items']), 'derived': len(ids) - len(bundle['items']),
            'logicalTables': len(bundle.get('tables', [])),
            'sourceTextCheckedByTool': False, 'semanticAccuracyEstimated': False}


def metadata(bundle, target, parent=None):
    old = target.get('reviewMetadata')
    if old:
        return old
    section = next(s for s in bundle['outline'] if s['id'] == (parent or target)['sectionId'])
    refs = target.get('sourceRefs') or (parent or target)['sourceRefs']
    target.setdefault('sourceRefs', deepcopy(refs))
    req = target.get('requirementness', 'REQUIREMENT' if target.get('class') in ('F', 'Q', 'C', 'M') else 'NON_REQUIREMENT')
    number = section.get('label', '')
    if parent:
        number += '/D' + str(parent.get('derived', []).index(target) + 1)
    result = {'number': number, 'title': target.get('label', next(iter(target['text'].splitlines()), '原文表格单元')),
              'classifications': [CLASSES[target['class']]] if target.get('class') in CLASSES else [],
              'topics': [], 'requirementness': req,
              'contentRole': target.get('contentRole') or ('' if req == 'REQUIREMENT' else target.get('class', 'CONTEXT')),
              'reason': '已有候选，本轮尚未复核语义。' + target.get('reason', ''), 'classificationStatus': 'LEGACY_RETAINED',
              'sourceOrder': min(r['page'] * 1000000 + min((q[1] for q in r.get('rects', [])), default=0) * 100 for r in refs),
              'analysisEligibility': 'ELIGIBLE' if req == 'REQUIREMENT' else 'NOT_ELIGIBLE',
              'baselineStatus': '已有候选；本轮尚未复核语义'}
    target['reviewMetadata'] = result
    if not parent and not target.get('sourcePresentation'):
        # Never pass an authored summary off as a verbatim source title.
        line_ids = section.get('sourceHeadingLineIds', section.get('headingLineIds', []))
        line_ids = [lid for lid in line_ids if lid in bundle['lines'] and bundle['lines'][lid]['text']]
        if line_ids:
            target['sourcePresentation'] = {'number': number, 'numberRole': 'SOURCE', 'titleRole': 'SOURCE_HEADING',
                'titleRanges': [{'lineId': lid, 'start': 0, 'end': len(bundle['lines'][lid]['text'])} for lid in line_ids]}
        else:
            target['sourcePresentation'] = {'number': number, 'numberRole': 'AGENT_ASSIGNED',
                'titleRole': 'AGENT_ASSIGNED', 'title': target.get('label', result['title'])}
    return result


def native_segment(fragment, logical_id, ordinal, count):
    """Alias captured topology, retaining cell IDs/text; no semantic row splitting."""
    cells = []
    # Captured grid starts identify merged spans without inventing new cells.
    starts = {axis: {} for axis in ('row', 'column')}
    for cell in fragment['cellsFlat']:
        starts['row'].setdefault(cell['row'], []).append(cell['bbox'][1])
        starts['column'].setdefault(cell.get('column', cell.get('col')), []).append(cell['bbox'][0])
    import statistics
    edges = {axis: {index: statistics.median(values) for index, values in values_by_index.items()}
             for axis, values_by_index in starts.items()}
    def span(cell, axis):
        index = cell['row'] if axis == 'row' else cell.get('column', cell.get('col'))
        stop = cell['bbox'][3 if axis == 'row' else 2]
        return max([1, *[n - index + 1 for n, edge in edges[axis].items() if n >= index and edge < stop - .1]])
    for cell in fragment['cellsFlat']:
        cells.append({**deepcopy(cell), 'column': cell.get('column', cell.get('col')),
                      'row_span': cell.get('rowSpan', cell.get('row_span', span(cell, 'row'))),
                      'column_span': cell.get('columnSpan', cell.get('column_span', span(cell, 'column'))),
                      'page': cell.get('page', fragment['page'])})
    rows = fragment.get('row_count') or max((c['row'] + c['row_span'] for c in cells), default=0)
    cols = fragment.get('col_count') or max((c['column'] + c['column_span'] for c in cells), default=0)
    return {'id': fragment.get('id', fragment['tableId']), 'type': 'TABLE', 'page': fragment['page'],
            'bbox': fragment['bbox'], 'row_count': rows, 'column_count': cols, 'cells': cells,
            'header_rows': [0] if not fragment.get('header', {}).get('external', False) else [],
            'logical_table_id': logical_id, 'segment_index': ordinal, 'segment_count': count,
            'projection_mode': 'SOURCE_TABLE_RECORDS'}


def project_tables(bundle):
    existing = {t['id'] for t in bundle.get('reviewTables', [])}
    existing.update(block.get('logical_table_id', block['id'])
                    for item in bundle['items'] for block in item.get('displayBlocks', [])
                    if block.get('type') == 'TABLE')
    sections = {s['id'] for s in bundle['outline']}
    for table in bundle.get('tables', []):
        if table['id'] in existing:
            continue
        fragments = table.get('fragments', [])
        if not fragments or not all(f.get('cellsFlat') for f in fragments):
            continue
        segments = [native_segment(f, table['id'], n, len(fragments)) for n, f in enumerate(fragments)]
        related = [i for i in bundle['items'] if i.get('table', {}).get('logicalId') == table['id']]
        if not related:
            # Whole-table context remains accessible under its original chapter.
            section = table.get('section')
            if section not in sections:
                continue
            records = []
        else:
            section = table.get('section', related[0]['sectionId'])
            if section not in sections:
                section = related[0]['sectionId']
            records = []
            for item in related:
                selection = item['table']['selected']
                rows, headers = [], []
                for selected in selection:
                    seg = next(s for s in segments if s['id'] == selected['tableId'])
                    for row in selected['rows']:
                        rows.append({'table_id': seg['id'], 'row_index': row,
                                     'cell_ids': [c['id'] for c in seg['cells'] if c['row'] == row]})
                for table_id, indexes in item['table'].get('headers', []):
                    seg = next(s for s in segments if s['id'] == table_id)
                    cells = [c for c in seg['cells'] if c['row'] in indexes]
                    headers.append({'table_id': table_id, 'axis': 'ROW', 'cell_ids': [c['id'] for c in cells],
                                    'text': ' / '.join(c['text'] for c in cells)})
                records.append({'record_id': item['id'], 'record_kind': 'ROW', 'row_bindings': rows,
                                'column_bindings': [], 'header_bindings': headers, 'figure_refs': []})
        # Several tables in a chapter must have separate owners, not overwrite it.
        owner = 'table-context/' + table['id']
        bundle['outline'].append({'id': owner, 'parentId': section, 'page': segments[0]['page'],
                                 'label': '', 'title': table.get('label', '原文表格'),
                                 'sourceOrder': segments[0]['page'] * 1000000 + segments[0]['bbox'][1] * 100})
        for item in related:
            item['sectionId'] = owner
        bundle.setdefault('reviewTables', []).append({'id': table['id'], 'sectionId': owner, 'segments': segments,
            'records': records, 'sourceRefs': [{'page': s['page'], 'rects': [s['bbox']], 'precision': 'table'} for s in segments]})


def apply_decisions(bundle, decisions):
    changes = []
    index = {t['id']: t for i in bundle['items'] for t in [i, *i.get('derived', [])]}
    for decision in decisions:
        target = index[decision['targetId']]
        if digest(target['text']) != decision['expectedTextSha256']:
            raise ValueError('Decision source changed: ' + target['id'])
        if set(decision['metadata'].get('classifications', [])) - PATHS:
            raise ValueError('Unknown classification')
        before = deepcopy(target)
        target['reviewMetadata'].update(decision['metadata'])
        target['reviewMetadata'].update(classificationStatus='AGENT_SELF_REVIEWED',
                                       baselineStatus='Agent 已依据原文与反馈复核')
        if 'derived' in decision:
            target['derived'] = deepcopy(decision['derived'])
            for child in target['derived']:
                meta = metadata(bundle, child, target)
                meta.update(child.pop('decisionMetadata', {}))
                meta.update(classificationStatus='AGENT_SELF_REVIEWED', baselineStatus='Agent 已依据原文与反馈复核')
        if 'associations' in decision:
            target['analysisAssociations'] = deepcopy(decision['associations'])
        changes.append({'targetId': target['id'], 'evidence': decision['evidence'],
                        'beforeSha256': digest(before), 'afterSha256': digest(target), 'before': before})
    return changes


def stage(input_dir, decision_file, output_dir):
    if output_dir.exists():
        raise ValueError('Successors are immutable: choose a new output directory')
    plan = json.loads(decision_file.read_text())
    for feedback in plan.get('feedbackInputs', []):
        path = Path(feedback['path'])
        if hashlib.sha256(path.read_bytes() if path.exists() else b'').hexdigest() != feedback['sha256']:
            raise ValueError('Feedback changed; refresh before staging: ' + str(path))
    original = load_bundle(input_dir)
    if plan['sourceRevision'] != original['revision']:
        raise ValueError('Plan revision mismatch')
    bundle = deepcopy(original)
    before_audit = audit(bundle, input_dir / 'source.pdf')
    overlays = []
    if plan.get('overlay'):
        overlay = load_bundle(Path(plan['overlay']['directory']))
        if overlay['source']['sha256'] != bundle['source']['sha256'] or overlay['revision'] != plan['overlay']['revision']:
            raise ValueError('Overlay source/version mismatch')
        removed = set(plan['overlay'].get('supersededIds', []))
        old_items = {i['id']: i for i in bundle['items']}
        for item in overlay['items']:
            if item['id'] in old_items and item['text'] != old_items[item['id']]['text']:
                raise ValueError('Overlay changed original text: ' + item['id'])
        replacement = {i['id']: deepcopy(i) for i in overlay['items']}
        overlays = [deepcopy(i) for i in bundle['items'] if i['id'] in removed or i['id'] in replacement]
        bundle['items'] = [replacement.pop(i['id'], i) for i in bundle['items'] if i['id'] not in removed] + list(replacement.values())
        outline = {s['id']: s for s in bundle['outline']}
        outline.update({s['id']: deepcopy(s) for s in overlay['outline']})
        bundle['outline'] = list(outline.values())
        bundle['reviewTables'] = deepcopy(overlay.get('reviewTables', []))
        bundle['lines'].update(deepcopy(overlay['lines']))
        bundle['inheritedReviewState'] = deepcopy(overlay.get('ui', {}))
    for item in bundle['items']:
        metadata(bundle, item)
        for child in item.get('derived', []):
            metadata(bundle, child, item)
    changes = apply_decisions(bundle, plan['decisions'])
    for issue in plan.get('newIssues', []):
        target = next(i for i in bundle['items'] if i['id'] == issue['targetId'])
        if digest(target['text']) != issue['expectedTextSha256']:
            raise ValueError('Issue refers to changed source')
        bundle.setdefault('issues', []).append(deepcopy(issue))
    transforms = normalize_coordinates(bundle, input_dir / 'source.pdf')
    for item in bundle['items']:
        for target in [item, *item.get('derived', [])]:
            if any(ref['page'] in transforms for ref in target['sourceRefs']):
                target['reviewMetadata']['sourceOrder'] = min(
                    ref['page'] * 1000000 + min((r[1] for r in ref.get('rects', [])), default=0) * 100
                    for ref in target['sourceRefs'])
    project_tables(bundle)
    repaired_order_ids = freeze_section_positions(bundle)
    bundle['ui'] = {'caseId': plan['caseId'], 'reviewVersion': plan.get('reviewVersion', 1),
                    'versionLabel': plan.get('versionLabel', 'B1 · 多文档工作基线'),
                    'scope': plan['scope'], 'initialNodeId': plan['initialNodeId'], 'topics': TOPICS}
    bundle['baselineLoop'] = {'schema': 'reqpilot-baseline-loop/v1', 'sourceRevision': original['revision'],
        'decisionSha256': hashlib.sha256(decision_file.read_bytes()).hexdigest(),
        'coordinateTransforms': {str(k): v for k, v in transforms.items()}, 'humanAcceptedWholeDocument': False,
        'sourceOrderRepairs': repaired_order_ids,
        'internalEvidenceAvailable': False, 'classificationAuthority': 'reviewMetadata',
        'legacyFields': 'Historical class/reason/proxyReview remain source-version evidence; do not use them over reviewMetadata.',
        'createdAt': datetime.now(timezone.utc).isoformat()}
    bundle.pop('revision', None)
    bundle['revision'] = digest(bundle)
    after_audit = audit(bundle, input_dir / 'source.pdf')
    current = {i['id']: i for i in bundle['items']}
    preserved = [i['id'] for i in original['items'] if i['id'] in current and current[i['id']]['text'] == i['text']]
    missing = [i['id'] for i in original['items'] if i['id'] not in current]
    allowed_missing = set(plan.get('overlay', {}).get('supersededIds', []))
    if len(preserved) + len(missing) != len(original['items']) or set(missing) != allowed_missing:
        raise ValueError('Unaccounted original text change or source removal')
    after_audit['sourceConservation'] = {'unchangedOriginalTexts': len(preserved),
        'supersededOriginalIds': missing, 'oldOriginalsPreservedInInputBundle': len(original['items']),
        'newOriginalIds': [tid for tid in current if tid not in {i['id'] for i in original['items']}]}
    if after_audit['errors']:
        raise ValueError(json.dumps(after_audit['errors'][:12], ensure_ascii=False))
    output_dir.mkdir(parents=True)
    shutil.copy2(input_dir / 'source.pdf', output_dir / 'source.pdf')
    # Local evidence copies survive deletion of a historical worktree.
    for name, value in [('review-bundle.json', bundle), ('input-bundle.json', original),
                        ('audit-before.json', before_audit), ('audit-after.json', after_audit),
                        ('change-ledger.json', {'changes': changes, 'overlayPreviousItems': overlays, 'plan': plan})]:
        (output_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    return {'revision': bundle['revision'], **{k: after_audit[k] for k in ('originals', 'derived', 'pages', 'logicalTables')},
            'decisions': len(changes), 'coordinatePagesFixed': len(transforms), 'errors': len(after_audit['errors']),
            'remainingAgentReview': sum(o['kind'] == 'AGENT_REVIEW_REMAINS' for o in after_audit['observations'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--decisions', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage(args.input, args.decisions, args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
