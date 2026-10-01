"""One ordered Matrix for source hierarchy, all content and linked assessments.

The existing graph owns identities and analysis scope. Table sheets restore the
source geometry; compact Matrix rows keep the individual judgments discoverable.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import replace
from xlsxwriter.utility import xl_col_to_name

from reqpilotx.domain.requirement_matrix import matrix_conclusion_contract


def response_units(nodes):
    # A source item can share a display row with a structure node, so its stable
    # source ID need not equal node_id. Use the same analysis scope as row
    # judgments and group summaries instead of inferring it from those IDs.
    return [node for node in nodes if matrix_conclusion_contract(node).analysis_target]


def _text(node):
    return str(node.get('display_text') or node.get('requirement_text') or '')


VISUAL_READING_LABEL = 'Agent图片释读（以原图为准）'
_DECISION_LABELS = {'COMPLIANT': '符合', 'NON_COMPLIANT': '不符合', 'PARTIALLY_COMPLIANT': '部分符合',
    'PENDING_EVIDENCE': '待内部证据', 'NEEDS_CLARIFICATION': '待澄清', 'NEED_CLARIFICATION': '待澄清',
    'INSUFFICIENT_INFORMATION': '信息不足', 'NOT_APPLICABLE': '不适用', 'ACCEPT': '接受',
    'REJECT': '拒绝', 'DEVIATION_ACCEPT': '偏差接受', 'UNKNOWN': '待分析'}


def _engineer_ai_background(node):
    ai = node.get('ai_assessment') or {}
    if not ai: return ''
    lines = ['AI分析建议（保留，不覆盖人工意见）：' + _DECISION_LABELS.get(ai.get('decision'), ai.get('decision', ''))]
    if ai.get('reason'): lines.append(ai['reason'])
    if ai.get('clarification_question'): lines.append('AI待补充：' + ai['clarification_question'])
    return '\n'.join(lines)


def visual_reading(node, baseline):
    """Read an optional annotation without promoting it to customer source text."""
    item = node.get('source_item') or {}
    reading = item.get('visual_interpretation')
    if not isinstance(reading, dict) or not isinstance(reading.get('text'), str) or not reading['text'].strip():
        return ''
    anchors = {ref.get('anchor_id') for ref in [*node.get('figure_refs', []), *item.get('image_bindings', [])]}
    supplied = set(reading.get('image_anchor_ids') or [])
    mismatch = (reading.get('authority') != 'AGENT_VISUAL_INTERPRETATION'
                or reading.get('source_sha256') != baseline.get('source_sha256')
                or not supplied or not supplied.issubset(anchors)
                or ('baseline_sha256' in reading and reading['baseline_sha256'] != baseline.get('artifact_sha256'))
                or ('source_item_id' in reading and reading['source_item_id'] != item.get('source_item_id')))
    if mismatch:
        return '释读的来源、条目或版本关联不匹配，未展示其内容；请重新核对原图。'
    # Older sealed IMAGE annotations omit the optional baseline/item pins; their
    # source hash and bound image anchors still have to match this snapshot.
    lines = ['待人工复核'] if reading.get('review_state', 'UNREVIEWED') == 'UNREVIEWED' else []
    lines.append(reading['text'])
    for issue in reading.get('unresolved') or []:
        message = issue if isinstance(issue, str) else json.dumps(issue, ensure_ascii=False)
        lines.append('待核对：' + message)
    return '\n'.join(lines)


def structure_label(node):
    """Describe the source object independently of its requirement judgment."""
    if node.get('decomposition'):
        return '派生条目'
    kind = node.get('source_kind')
    if kind in {'DOCUMENT', 'DOCUMENT_ROOT', 'DOCUMENT_UNIT'}:
        return '文档'
    if kind == 'ATTACHMENT':
        return '附件'
    if kind in {'COVER', 'TABLE_OF_CONTENTS', 'DOCUMENT_CONTROL'}:
        return {'COVER': '封面', 'TABLE_OF_CONTENTS': '目录', 'DOCUMENT_CONTROL': '文控信息'}[kind]
    if kind == 'SECTION':
        return '章节' if int(node.get('depth') or 0) <= 1 else '子章节'
    form = (node.get('source_item') or {}).get('source_form')
    if form == 'TABLE_BRANCH':
        return '表格分支'
    role = node.get('display_role')
    if role == 'TABLE':
        return '表格'
    if role == 'TABLE_RECORD':
        return '表格条目'
    if form == 'IMAGE':
        return '图片'
    if kind == 'CLAUSE' or form in {'NUMBERED_CLAUSE', 'NATIVE_LIST'} or role == 'CLAUSE':
        return '条目'
    return '内容'


def scope_summary(node, by_id):
    ids, pending = set(), [node['node_id']]
    children = {}
    for candidate in by_id.values():
        children.setdefault(candidate.get('_visible_parent_id', candidate.get('parent_id')), []).append(candidate['node_id'])
    while pending:
        key = pending.pop()
        if key in ids:
            continue
        ids.add(key); pending.extend(children.get(key, []))
    sources = [by_id[key] for key in ids if by_id[key].get('source_item')]
    required = sum((n['source_item'].get('requirementness') == 'REQUIREMENT') for n in sources)
    tables = {key for node_id in ids for key in by_id[node_id].get('logical_table_ids', [])}
    result = f'原文 {len(sources)} 项（需求 {required} / 非需求 {len(sources)-required}）'
    if tables:
        result += f' · 原表 {len(tables)} 张'
    derived = sum(bool(by_id[key].get('decomposition')) for key in ids)
    if derived:
        result += f' · 派生 {derived} 项'
    return result


def _compact_part(part):
    """Match NativeStructuredTables' occupied-boundary view, preserving source IDs."""
    if not part.cells:
        return part
    boundaries = sorted({x for c in part.cells for x in (c['column'], c['column']+c.get('column_span', 1))})
    positions = {value: i for i, value in enumerate(boundaries)}
    cells = [{**c, 'column': positions[c['column']],
              'column_span': positions[c['column']+c.get('column_span', 1)] - positions[c['column']]}
             for c in part.cells]
    images = [{**im, 'column': positions.get(im['column'], im['column'])} for im in part.cell_images]
    merged = [{'row': c['row'], 'column': c['column'], 'row_span': c.get('row_span', 1), 'column_span': c.get('column_span', 1)}
              for c in cells if c.get('row_span', 1)>1 or c.get('column_span', 1)>1]
    return replace(part, cells=cells, cell_images=images, merged_regions=merged)


def _carry_text(node):
    notes = (node.get('source_item') or node.get('decomposition') or {}).get('revision_carry', [])
    values = []
    for note in notes:
        if note.get('operation') != 'SOURCE_VALIDATED_REBIND':
            continue
        message = str(note.get('message') or '')
        origin = note.get('origin_baseline_sha256') or note.get('from_baseline_sha256')
        if origin:
            message += f' 来源基线：{origin}。'
        if message:
            values.append(message)
    return '\n'.join(dict.fromkeys(values))


def _result(node):
    contract = matrix_conclusion_contract(node)
    manual = node.get('manual_decision') or {}
    ai = node.get('ai_assessment') or {}
    current = node.get('analysis_result_state') == 'CURRENT'
    token = (manual.get('decision') or ai.get('decision', '')) if current else ''
    token = token or contract.effective_conclusion
    labels = _DECISION_LABELS
    label = labels.get(token, token) or '待分析'
    engineer = (node.get('source_item') or node.get('decomposition') or {}).get('engineer_response') or {}
    old_opinion = ''
    if engineer.get('state') == 'STALE':
        record = engineer['record']
        old_opinion = '前版工程师工作意见已失效，需重新确认：' + labels.get(record.get('decision'), record.get('decision', '')) + '\n' + record.get('reason', '')
        if record.get('conditions'): old_opinion += '\n原适用条件：' + record['conditions']
        if record.get('evidence_requests'): old_opinion += '\n原证据请求：' + record['evidence_requests']
    if node.get('analysis_result_state') == 'STALE_REFERENCE':
        return ('工程师意见需重确认' if old_opinion else '需重新分析'), '\n'.join(v for v in (_carry_text(node), old_opinion) if v)
    if current and manual.get('decision'):
        reason = str(manual.get('reason') or '')
        if engineer and ai:
            background = _engineer_ai_background(node)
            reason += '\n\n' + (background if len(reason) + len(background) < 30000
                                else 'AI分析建议仍完整保留，请通过“完整内容”查看。')
        return ('工程师工作意见：' if engineer else '人工：') + label, reason
    if current and token:
        label = ('AI建议：' + label) if contract.effective_conclusion else label
        return label, '\n'.join(v for v in (ai.get('reason'),
            '需补充：' + ai['clarification_question'] if ai.get('clarification_question') else '', _carry_text(node), old_opinion) if v)
    policy = (node.get('source_item') or node.get('decomposition') or {}).get('analysis_policy') or {}
    if policy.get('status') == 'SKIPPED':
        return '未执行符合性分析', '本次已选择跳过。' if policy.get('reason') == 'USER_SKIPPED' else '未提供内部依据；原文整理和工程师响应仍可继续。'
    return label, _carry_text(node)


def group_assessment(node, by_id):
    """Summarize eligible descendants without making an engineering commitment."""
    children = {}
    for candidate in by_id.values():
        parent = candidate.get('_visible_parent_id', candidate.get('parent_id'))
        children.setdefault(parent, []).append(candidate['node_id'])
    descendants, stack = [], list(children.get(node['node_id'], []))
    while stack:
        child = by_id[stack.pop(0)]
        if matrix_conclusion_contract(child).analysis_target:
            descendants.append(child)
        stack.extend(children.get(child['node_id'], []))
    if not descendants:
        return '', ''
    counts = Counter(matrix_conclusion_contract(c).effective_conclusion for c in descendants)
    completed = len(descendants) - counts['']
    analysis = Counter()
    for child in descendants:
        if child.get('analysis_result_state') == 'STALE_REFERENCE':
            analysis['STALE'] += 1
            continue
        token = ''
        if child.get('analysis_result_state') == 'CURRENT':
            token = (child.get('manual_decision') or {}).get('decision') or (child.get('ai_assessment') or {}).get('decision') or ''
        token = token or matrix_conclusion_contract(child).effective_conclusion
        analysis[token if token and token != 'UNKNOWN' else 'NOT_ANALYZED'] += 1
    skipped = sum((c.get('source_item') or c.get('decomposition') or {}).get('analysis_policy', {}).get('status') == 'SKIPPED'
        and not matrix_conclusion_contract(c).effective_conclusion and c.get('analysis_result_state') != 'STALE_REFERENCE' for c in descendants)
    analyzed = len(descendants) - analysis['NOT_ANALYZED'] - analysis['STALE']
    evidence = analysis['PENDING_EVIDENCE'] + analysis['INSUFFICIENT_INFORMATION']
    clarification = analysis['NEEDS_CLARIFICATION'] + analysis['NEED_CLARIFICATION']
    bad = sum(counts[k] for k in ('NON_COMPLIANT', 'REJECT'))
    partial = sum(counts[k] for k in ('PARTIALLY_COMPLIANT', 'DEVIATION_ACCEPT'))
    if bad:
        status = '存在不符合'
    elif partial:
        status = '存在条件/偏离'
    elif skipped == len(descendants):
        status = '未执行符合性分析'
    elif analyzed < len(descendants):
        status = '部分已分析 · 仍需补全' if analyzed else '需重新分析' if analysis['STALE'] else '待分析'
    elif completed == len(descendants):
        status = '已完成逐项判断'
    elif evidence or clarification:
        status = '已分析 · 待证据/澄清' if evidence and clarification else '已分析 · 待内部证据' if evidence else '已分析 · 待澄清'
    else:
        status = '已完成逐项判断'
    summary = f"已分析 {analyzed}/{len(descendants)} · 有效判断 {completed}/{len(descendants)}"
    if bad: summary += f" · 不符合 {bad}"
    if partial: summary += f" · 条件/偏离 {partial}"
    if evidence: summary += f" · 待内部证据 {evidence}"
    if clarification: summary += f" · 待澄清 {clarification}"
    if skipped: summary += f" · 未执行符合性分析 {skipped}"
    if analysis['NOT_ANALYZED'] > skipped: summary += f" · 待分析 {analysis['NOT_ANALYZED'] - skipped}"
    if analysis['STALE']: summary += f" · 需重新分析 {analysis['STALE']}"
    return status, summary


def write_response_workbook(*, workbook, formats, snapshot, nodes, table_details, table_catalog,
                            source_image_context, project_name, customer_name, source_file_name,
                            version_label, export_mode, compare_version_label, compare_matrix_snapshot):
    # Reuse the established source table/image renderer rather than re-creating
    # table values from flattened requirement text.
    from reqpilotx.services import exporter as e

    units = response_units(nodes)
    by_id = {n['node_id']: n for n in nodes}
    source_nodes = [n for n in nodes if n.get('source_item')]
    visual_readings = {n['node_id']: visual_reading(n, snapshot.get('source_item_baseline') or {}) for n in source_nodes}
    matrix_rows = {n['node_id']: i + 4 for i, n in enumerate(nodes)}
    navigation_rows = dict(matrix_rows)
    table_details = {key: {field: '' if field in {'title', 'owner_label', 'section_code'}
                                  and value == '未命名原文节点' else value
                            for field, value in detail.items()}
                     for key, detail in table_details.items()}

    main = workbook.add_worksheet('ComplianceMatrix')
    used = {'ComplianceMatrix', '图文依据', '_ReqPilotXMeta'}
    table_names = {}
    for i, table_id in enumerate(table_catalog.first_seen_order, 1):
        name = e._matrix_table_sheet_name(index=i, detail=table_details.get(table_id, {}), used_names=used)
        table_names[table_id] = name; used.add(name)
    table_rows, table_analysis, stats = {}, {}, {'cell_images': 0, 'cell_images_inserted': 0, 'cell_images_failed': 0, 'fallbacks': 0, 'warnings': []}
    rendered_parts = {}
    order = list(table_catalog.first_seen_order)
    for i, table_id in enumerate(order):
        detail = dict(table_details.get(table_id, {}))
        if not detail.get('owner_node_id'):
            owner = next((n for n in nodes if table_id in e._matrix_node_logical_table_ids(n)
                          and not n.get('source_item') and not n.get('decomposition')), None)
            if owner:
                detail['owner_node_id'] = owner['node_id']
        rendered_parts[table_id] = [_compact_part(p) for p in table_catalog.parts_by_logical_id.get(table_id, [])]
        rows, counts = e._append_matrix_logical_table_sheet(workbook=workbook, formats=formats,
            snapshot=snapshot, logical_table_id=table_id, sheet_name=table_names[table_id],
            parts=rendered_parts[table_id], detail=detail,
            nodes=nodes, matrix_row_by_node=navigation_rows, source_image_context=source_image_context,
            previous_sheet_name=table_names[order[i-1]] if i else '',
            next_sheet_name=table_names[order[i+1]] if i+1 < len(order) else '')
        table_rows[table_id] = rows
        # Navigation sits outside the original table and its print area. Keep
        # one judgment surface while allowing a direct return from each row.
        sheet = workbook.get_worksheet_by_name(table_names[table_id])
        navigation_col = max((c['column'] + c.get('column_span', 1)
                              for p in rendered_parts[table_id] for c in p.cells), default=3) + 1
        sheet.set_column(navigation_col, navigation_col, 22)
        sheet.write(2, navigation_col, '返回逐项判断', formats.header)
        row_targets = {}
        for node in source_nodes:
            if table_id not in e._matrix_node_logical_table_ids(node) or not node.get('table_record_id'):
                continue
            target_row = rows.get(node['node_id']) or rows.get(node['table_record_id'])
            if target_row:
                row_targets.setdefault(target_row, []).append(node)
        for target_row, targets in row_targets.items():
            target = targets[0] if len(targets) == 1 else by_id.get(detail.get('owner_node_id'), targets[0])
            label = (target.get('requirement_code') or target['source_item']['source_display_id']) if len(targets) == 1 else f'{len(targets)} 项：返回主表范围'
            sheet.write_url(target_row - 1, navigation_col,
                f"internal:'ComplianceMatrix'!A{matrix_rows[target['node_id']]}", None, label)
        table_analysis[table_id] = (sheet, navigation_col, row_targets, detail)
        # Vertical continuation is allowed; never split one original table into
        # unrelated horizontal print strips through a fixed percentage.
        sheet.fit_to_pages(1, 0)
        for key in ('cell_images', 'cell_images_inserted', 'cell_images_failed', 'fallbacks'):
            stats[key] += int(counts.get(key) or 0)
        stats['warnings'].extend(counts.get('warnings') or [])

    body = workbook.add_format({'font_name': e._workbook_font_name(), 'font_size': 11,
        'valign': 'top', 'text_wrap': True, 'bottom': 1, 'bottom_color': '#E4E4E4'})
    link = workbook.add_format({'font_name': e._workbook_font_name(), 'font_size': 11,
        'valign': 'top', 'text_wrap': True, 'font_color': '#005E46', 'underline': True})

    def setup(sheet, title, headers, widths, subtitle):
        sheet.hide_gridlines(2); sheet.set_zoom(90)
        sheet.merge_range(0, 0, 0, len(headers)-1, title, formats.meta_fill)
        sheet.set_row(0, 24)
        sheet.merge_range(1, 0, 1, len(headers)-1, subtitle, formats.meta_value)
        sheet.write_row(2, 0, headers, formats.header); sheet.set_row(2, 30)
        for col, width in enumerate(widths): sheet.set_column(col, col, width)
        sheet.freeze_panes(3, 2); sheet.repeat_rows(0, 2)
        sheet.set_landscape(); sheet.set_paper(8); sheet.fit_to_pages(1, 0)
        sheet.set_margins(.25, .25, .35, .35)

    def finish(sheet, count, columns):
        sheet.autofilter(2, 0, max(2, count + 2), columns-1)
        sheet.print_area(0, 0, max(2, count + 2), columns-1)

    def height(texts, widths, maximum=300):
        lines = max((sum(max(1, (sum(2 if ord(c) > 255 else 1 for c in line) + int(width)-1)//int(width))
                         for line in str(text or '').split('\n')) for text, width in zip(texts, widths)), default=1)
        return min(maximum, max(36, lines * 14 + 8))

    def table_target(node):
        for table_id in e._matrix_node_logical_table_ids(node):
            if table_id in table_names:
                mapping = table_rows.get(table_id, {})
                row = mapping.get(node['node_id']) or mapping.get(node.get('table_record_id')) or 1
                original = by_id.get((node.get('decomposition') or {}).get('source_item_id'), node)
                owned = {key for ref in original.get('source_refs', []) for key in ref.get('cell_ids', [])}
                record_id = node.get('table_record_id') or original['node_id']
                projection = table_details.get(table_id, {}).get('projection') or {}
                record = next((r for r in projection.get('records', []) if r.get('record_id') == record_id), {})
                bindings = record.get('row_bindings', [])
                column = 0
                for part in rendered_parts.get(table_id, []):
                    part_bindings = [b for b in bindings if b.get('table_id') == part.physical_table_id]
                    source_row = min((b['row_index'] for b in part_bindings), default=None)
                    cells = [c for c in part.cells if str(c.get('text') or '').strip()
                             and ({c.get('cell_id') or c.get('id'), *c.get('source_cell_ids', [])} & owned)]
                    if cells:
                        direct = [c for c in cells if source_row is not None and c['row'] == source_row]
                        column = min(direct or cells, key=lambda c: (c['row'], c['column']))['column']; break
                return f"internal:'{table_names[table_id]}'!{xl_col_to_name(column)}{row}", table_names[table_id]
        return None

    # Keep shared original text once, at its real owning row in this Matrix.
    context_ids, context_owners, owner_contexts = {}, {}, {}
    bound_content = {}
    for node in source_nodes:
        for ref in node.get('source_refs', []):
            if ref.get('content_id') and ref.get('start') == 0:
                bound_content.setdefault(ref['content_id'], []).append((ref.get('end'), node['node_id']))
    for node in nodes:
        item, derived = node.get('source_item') or {}, node.get('decomposition') or {}
        values = item.get('shared_source_content', []) + derived.get('shared_source_context', [])
        for value in values:
            identity = {key: value.get(key) for key in ('content_id', 'source_ref', 'page', 'bbox', 'text')}
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            owner = value.get('source_owner_node_id')
            if owner not in by_id:
                owner = derived.get('source_item_id') or node.get('parent_id') or node['node_id']
            if key not in context_owners:
                existing = next((node_id for end, node_id in bound_content.get(value.get('content_id'), [])
                                 if end == len(value.get('text') or '')), None)
                if existing:
                    owner = existing
                context_owners[key] = owner
                # A complete SRC binding already displays this content (including
                # its proved PDF reading view). Titles are on the Map's own row.
                if not existing and not value.get('is_title'):
                    owner_contexts.setdefault(owner, []).append(value)
            context_ids.setdefault(node['node_id'], []).append(context_owners[key])

    # Overflow is a reading view of the same complete Matrix values, never a
    # replacement source index or another requirement/assessment ledger.
    evidence = None; evidence_cursor = 3; evidence_links = {}; figure_links = {}; image_count = 0; missing_images = 0; figure_placements = {}
    def figure_key(figure):
        return e._matrix_figure_identity(figure)
    rendered_figures = {figure_key(im) for parts in table_catalog.parts_by_logical_id.values() for part in parts for im in part.cell_images}
    def add_evidence(node, text='', figures=(), sections=()):
        nonlocal evidence, evidence_cursor, image_count, missing_images
        if evidence is None:
            evidence = workbook.add_worksheet('图文依据')
            setup(evidence, '图文依据', ['来源条目', '完整原文 / 说明 / 原图', '返回主项'], [20, 110, 18], '长内容按原顺序完整显示；每段可返回主项。原表保留在各自工作表。')
        start = evidence_cursor + 1
        source_label = (node.get('source_item') or {}).get('source_display_id') or node.get('requirement_code') or node.get('display_title') or node.get('label') or '原文图像'
        back = f"internal:'ComplianceMatrix'!A{navigation_rows[node['node_id']]}" if node['node_id'] in navigation_rows else None
        section_values = [('原文全文', text)] if text else []
        section_values.extend(sections)
        for label, full in section_values:
            if not full:
                continue
            owner_label = node.get('requirement_code') or (node.get('source_item') or {}).get('source_display_id') or f"主表第 {navigation_rows[node['node_id']]} 行"
            evidence.write_row(evidence_cursor, 0, [owner_label, label], formats.meta_fill)
            evidence.set_row(evidence_cursor, 26)
            if back: evidence.write_url(evidence_cursor, 2, back, link, '返回主项')
            evidence_cursor += 1
            # Reuse the existing lossless splitter, with a conservative width
            # and ample line spacing below Excel's 409.5pt row-height limit.
            prepared = e._prepare_text_rows_for_height(full, 650, max_height_points=320)
            if ''.join(item.text for item in prepared) != full:
                raise ValueError('Complete-content text changed while preparing readable rows')
            for item in prepared:
                evidence.write_row(evidence_cursor, 0, [owner_label, item.text], body)
                evidence.set_row(evidence_cursor, max(36, item.row_height))
                if back: evidence.write_url(evidence_cursor, 2, back, link, '返回主项')
                evidence_cursor += 1
        for figure in figures:
            figure_links[figure_key(figure)] = f"internal:'图文依据'!B{evidence_cursor + 1}"
            rendered_figures.add(figure_key(figure))
            payload = e._load_matrix_figure_payload(figure, source_image_context=source_image_context)
            page = figure.get('page') or figure.get('page_number')
            evidence.write(evidence_cursor, 0, source_label + (f' · P{page}' if page else ''), body)
            if payload:
                buffer, width, img_height = payload
                scale = min(1, 700/max(width, 1), 470/max(img_height, 1))
                evidence.insert_image(evidence_cursor, 1, 'source.png', {'image_data': buffer,
                    'x_scale': scale, 'y_scale': scale, 'object_position': 2, 'x_offset': 4, 'y_offset': 4})
                figure_placements.setdefault(figure_key(figure), []).append({'sheet': '图文依据', 'row': evidence_cursor + 1, 'column': 2})
                evidence.set_row(evidence_cursor, max(50, img_height*scale*.75+8)); image_count += 1
            else:
                missing_images += 1
                evidence.write(evidence_cursor, 1, '原图暂不可读取；请回到原文核对。', body)
            evidence_cursor += 1
        evidence_links[node['node_id']] = f"internal:'图文依据'!A{start}"
        if back:
            evidence.write_url(start-1, 2, back, link, '返回主项' if section_values else '返回对应范围')
        return evidence_links[node['node_id']]

    # Shared table figures may cross physical rows. Keep one original image
    # and register its target before writing each related SRC's navigation.
    for node in nodes:
        if e._matrix_node_logical_table_ids(node):
            remaining = [figure for figure in e._matrix_node_figure_refs(node) if figure_key(figure) not in rendered_figures]
            if remaining:
                add_evidence(node, figures=remaining)

    def shared_figure_targets(node):
        return list(dict.fromkeys(figure_links[figure_key(figure)] for figure in e._matrix_node_figure_refs(node)
                                  if figure_key(figure) in figure_links))

    categories = {'PRODUCT_SYSTEM.FUNCTIONAL': '功能', 'PRODUCT_SYSTEM.QUALITY': '质量',
        'PRODUCT_SYSTEM.CONSTRAINT': '约束', 'PROJECT': '项目', 'PROCESS': '过程'}
    topics = {'VERIFICATION': '试验验证', 'REGULATORY': '法规', 'LOGISTICS_PACKAGING': '物流包装', 'STANDARDS': '标准'}
    roles = {'METADATA': '文控信息', 'REVISION_HISTORY': '修订记录', 'CONTEXT': '背景/上下文',
        'DEFINITION': '定义', 'RATIONALE': '理由', 'EXAMPLE': '示例', 'NOTE': '注释', 'REFERENCE': '参考'}
    headers = ['SRC', 'REQ No.', '原文编号', '标题 / 原文内容', '结构类型', '内容判定', '内容角色', '需求类别', '主题', '判断 / 汇总', '说明 / 待补充', '来源 / 原表', '完整内容']
    source_col = headers.index('来源 / 原表')
    baseline_version = (snapshot.get('source_item_baseline') or {}).get('revision')
    setup(main, 'Compliance Matrix', headers, [13, 18, 13, 90, 14, 12, 16, 18, 16, 24, 44, 26, 22],
          f'{project_name} · {source_file_name} · 原文基线 v{baseline_version or "—"} · {len(source_nodes)} 条原文 · {len(units)} 个分析单元 · {"草稿" if export_mode == "DRAFT" else "正式"}')
    main.outline_settings(True, False, True, False)
    heading_formats, main_values = {}, {}
    source_context_nodes = {}
    for candidate in nodes:
        identity = (candidate.get('source_item') or {}).get('source_item_id')
        if identity:
            if identity in source_context_nodes:
                raise ValueError('Ambiguous source context identity in visible matrix')
            source_context_nodes[identity] = candidate
    for row, node in enumerate(nodes, 3):
        item = node.get('source_item') or {}
        derived = node.get('decomposition') or {}
        table = table_target(node)
        structure = not item and not derived
        title = node.get('display_title') or node.get('label') or ''
        # Document Map's explicit empty-title sentinel is not a source heading.
        if title == '未命名原文节点':
            title = ''
        text = _text(node)
        if structure:
            direct = [str(block.get('text') or '') for block in node.get('content_blocks', [])
                      if str(block.get('type') or '').upper() == 'TEXT' and str(block.get('text') or '').strip()]
            text = '\n'.join(dict.fromkeys([title, *direct]))
        elif not text:
            text = title or '原表条目'
        elif node.get('structure_node_id') == node['node_id']:
            source_title = item.get('source_title', '')
            if source_title and source_title not in text:
                text = source_title + '\n' + text
        for content in owner_contexts.get(node['node_id'], []):
            original = str(content.get('text') or '')
            if original and ''.join(original.split()) not in ''.join(text.split()):
                text += ('\n' if text else '') + original
        figures = [] if table else e._matrix_node_figure_refs(node)
        evidence_link = None
        inline_figures = []
        legacy_source_overflow = len(text) > 3000
        detail_figures = figures if legacy_source_overflow else ()
        if legacy_source_overflow: figures = []
        if figures:
            for figure in figures:
                payload = e._load_matrix_figure_payload(figure, source_image_context=source_image_context)
                if payload:
                    inline_figures.append((figure, payload))
                else:
                    evidence_link = add_evidence(node, figures=[figure])
        if structure or (item and node.get('child_ids') and not matrix_conclusion_contract(node).analysis_target):
            result, reason = group_assessment(node, by_id)
            if item.get('requirementness') == 'REQUIREMENT': reason = '完整原文保留，逐项判断见派生子项。' + ('\n'+reason if reason else '')
            if structure:
                reason = scope_summary(node, by_id) + ('\n' + reason if reason else '')
        elif item.get('requirementness') == 'NON_REQUIREMENT':
            result, reason = '保留，不单独判符合', ''
        else:
            result, reason = _result(node)
        shared_images = shared_figure_targets(node)
        if shared_images:
            reason = '共享原图：点击本说明查看。' + ('\n' + reason if reason else '')
        carry_text = _carry_text(node)
        if carry_text and carry_text not in reason:
            reason += ('\n' if reason else '') + carry_text
        if derived.get('group_logic') == 'ANY_OF':
            reason = f"备选组 {derived.get('source_requirement_id') or ''}：按原文任选条件判断。" + ('\n' + reason if reason else '')
        refs = list(dict.fromkeys(context_ids.get(node['node_id'], [])))
        refs = [key for key in refs if key in matrix_rows and key != node['node_id']]
        if refs: reason += ('\n' if reason else '') + '共用原文见主表行 ' + '、'.join(str(matrix_rows[key]) for key in refs)
        linked_contexts = []
        for identity in dict.fromkeys(item.get('context_source_ids') or []):
            target = source_context_nodes.get(identity)
            if target is None or target['node_id'] not in matrix_rows or target['node_id'] == node['node_id']:
                raise ValueError('Linked source context is missing, hidden or self-referential in visible matrix')
            linked_contexts.append(target)
        if linked_contexts:
            labels = []
            for target in linked_contexts:
                label = target['source_item'].get('source_display_id') or ''
                if target.get('source_number'):
                    label += ' · ' + str(target['source_number'])
                labels.append(f"{label}（主表第{matrix_rows[target['node_id']]}行）")
            reason += ('\n' if reason else '') + '关联原文与条件：' + '；'.join(labels)
        reading = visual_readings.get(node['node_id'], '')
        reading_comment = ''
        if reading:
            if table:
                reason += ('\n' if reason else '') + 'Agent图片释读见原表分析区。'
            else:
                preview = reading
                if height([reason + '\n' + reading], [44], 10000) > 190:
                    preview = reading + '\n完整释读见本单元格批注。'
                    reading_comment = reading
                reason += ('\n\n' if reason else '') + VISUAL_READING_LABEL + '\n' + preview
        depth = min(int(node.get('depth') or 0), 7)
        # Keep complete values in the original D/K columns. The added M column
        # provides an explicit lossless reading path without replacing L links.
        e._ensure_excel_cell_text(text, context='Complete Matrix original text')
        e._ensure_excel_cell_text(reason, context='Complete Matrix explanation')
        source_overflow = (not table and e._estimate_total_text_height(text, max(20, 90-depth*2)*7) > 220) or legacy_source_overflow
        reason_overflow = e._estimate_total_text_height(reason, 44*7) > 220
        engineer_ai_overflow = ''
        if ((node.get('source_item') or node.get('decomposition') or {}).get('engineer_response')
                and node.get('manual_decision') and node.get('ai_assessment')):
            background = _engineer_ai_background(node)
            if len(str(node['manual_decision'].get('reason') or '')) + len(background) >= 30000:
                engineer_ai_overflow = background
                reason_overflow = True
        complete_sections = []
        if source_overflow: complete_sections.append(('原文全文', text))
        if reason_overflow: complete_sections.append(('说明全文', reason))
        if engineer_ai_overflow: complete_sections.append(('AI分析建议（保留）', engineer_ai_overflow))
        complete_link = add_evidence(node, sections=complete_sections, figures=detail_figures) if complete_sections else None
        complete_label = '查看原文与说明' if source_overflow and reason_overflow else '查看完整原文' if source_overflow else '查看完整说明' if reason_overflow else ''
        if legacy_source_overflow: evidence_link = complete_link
        # Export the extraction dimensions independently. A table is a source
        # form; metadata is a content role; neither answers requirementness.
        requirementness = '需求' if derived else {'REQUIREMENT': '需求', 'NON_REQUIREMENT': '非需求'}.get(item.get('requirementness'), '')
        content_role = roles.get(item.get('non_requirement_role'), item.get('non_requirement_role') or '')
        values = [item.get('source_display_id') or derived.get('source_display_id') or '',
                  node.get('requirement_code'), node.get('source_number'), text, structure_label(node), requirementness, content_role,
                  ' / '.join(categories.get(k, k) for k in node.get('classification_paths', [])),
                  ' / '.join(topics.get(k, k) for k in node.get('topic_tags', [])), result, reason,
                  e._matrix_source_label(node), complete_label]
        main_values[node['node_id']] = values
        main.write_row(row, 0, values, body)
        if shared_images:
            main.write_url(row, headers.index('说明 / 待补充'), shared_images[0], body, reason)
        elif linked_contexts:
            main.write_url(row, headers.index('说明 / 待补充'),
                f"internal:'ComplianceMatrix'!A{matrix_rows[linked_contexts[0]['node_id']]}", body, reason)
        if reading_comment:
            main.write_comment(row, headers.index('说明 / 待补充'), VISUAL_READING_LABEL + '\n' + reading_comment, {'x_scale': 2, 'y_scale': 3})
        if complete_link:
            main.write_url(row, headers.index('完整内容'), complete_link, link, complete_label)
        format_key = (depth, structure)
        if format_key not in heading_formats:
            heading_formats[format_key] = workbook.add_format({'font_name': e._workbook_font_name(),
                'font_size': 11, 'valign': 'top', 'align': 'left', 'text_wrap': True, 'indent': depth,
                'bold': structure, 'bg_color': '#EDF4EF' if structure else '#FFFFFF',
                'bottom': 1, 'bottom_color': '#E4E4E4'})
        main.write(row, 3, text, heading_formats[format_key])
        if table:
            main.write_url(row, source_col, table[0], link, table[1] + (f" · P{node.get('page_start') or ''}" if not any(ref.get("native_locator") for ref in node.get("source_refs", [])) else " · " + e._matrix_source_label(node)))
        elif evidence_link:
            main.write_url(row, source_col, evidence_link, link, '查看原图 / 完整条款')
        elif derived.get('source_item_id') in matrix_rows:
            main.write_url(row, source_col, f"internal:'ComplianceMatrix'!A{matrix_rows[derived['source_item_id']]}", link, '返回原文父项')
        # Start expanded so filters and links land on visible items. Engineers
        # may collapse the outline; hiding rows is never a second export scope.
        row_height = height([text, reason], [90-depth*2, 44], 220)
        offset = row_height / .75 + 4
        for figure, (buffer, width, img_height) in inline_figures:
            scale = min(1, 580/max(width,1), 170/max(img_height,1))
            if offset + img_height*scale > 525:
                evidence_link = add_evidence(node, figures=[figure])
                main.write_url(row, source_col, evidence_link, link, '查看完整原图')
                continue
            main.insert_image(row, 3, 'source.png', {'image_data': buffer, 'object_position': 2,
                'x_scale': scale, 'y_scale': scale, 'x_offset': depth*8+4, 'y_offset': offset})
            rendered_figures.add(figure_key(figure)); image_count += 1
            figure_placements.setdefault(figure_key(figure), []).append({'sheet': 'ComplianceMatrix', 'row': row + 1, 'column': 4})
            offset += img_height*scale+8; row_height = max(row_height, offset*.75)
        main.set_row(row, row_height, None, {'level': depth})
    finish(main, len(nodes), len(headers))

    # Same-sheet analysis is a second reading surface over the same objects.
    # Response cells reference the main Matrix, so editing its conclusion does
    # not leave a conflicting copy next to the original table.
    analysis_headers = ['SRC / REQ（返回主表）', '内容判定', '内容角色', '需求类别', '主题',
                        '判定依据（需求提取）', '响应判断', '响应分析 / 依据 / 缺口']
    analysis_widths = [22, 12, 16, 18, 16, 42, 24, 52]
    mirrored = {1: '内容判定', 2: '内容角色', 3: '需求类别', 4: '主题', 6: '判断 / 汇总'}
    def response_detail(node):
        status, reason = _result(node)
        if (node.get('source_item') or {}).get('requirementness') == 'NON_REQUIREMENT':
            return '\n'.join(value for value in ('保留原文，不单独形成符合性结论。', _carry_text(node)) if value)
        children = [by_id[key] for key in node.get('child_ids', []) if key in by_id
                    and (by_id[key].get('decomposition') or {}).get('source_item_id') == node['node_id']]
        if children:
            return '\n'.join(value for value in (_carry_text(node), *[f"{c.get('requirement_code') or '派生条目'}：{_result(c)[0]}\n{response_detail(c)}" for c in children]) if value)
        if node.get('analysis_result_state') != 'CURRENT':
            return reason or ('历史结果已失效，需重新分析。' if node.get('analysis_result_state') == 'STALE_REFERENCE' else '尚未形成技术响应。')
        basis = (node.get('ai_assessment') or {}).get('analysis_basis') if not node.get('manual_decision') else ''
        return '\n'.join(value for value in (reason, '依据：' + basis if basis else '') if value) or '尚未形成技术响应。'

    for table_id, (sheet, column, row_targets, detail) in table_analysis.items():
        owner = by_id.get(detail.get('owner_node_id'))
        scope_nodes = [n for targets in row_targets.values() for n in targets]
        has_readings = any(visual_readings.get(n['node_id']) for n in scope_nodes)
        table_headers = analysis_headers + ([VISUAL_READING_LABEL] if has_readings else [])
        table_widths = analysis_widths + ([52] if has_readings else [])
        status, summary = group_assessment(owner, by_id) if owner else ('', '')
        sheet.merge_range(0, column, 0, column+len(table_headers)-1, f'本表分析 · 原文基线 v{baseline_version or "—"}', formats.meta_fill)
        sheet.merge_range(1, column, 1, column+len(table_headers)-1,
            f'{len(scope_nodes)} 个原文条目 · {status or "仅保留原表"} · {summary}\n响应判断引用主表；长说明点条目编号返回主表“完整内容”。分析区不进入默认原表打印范围。', formats.meta_value)
        sheet.write_row(2, column, table_headers, formats.header)
        sheet.set_row(1, max(sheet.row_sizes.get(1, [15])[0], 40))
        sheet.set_row(2, 32)
        for offset, width in enumerate(table_widths):
            sheet.set_column(column+offset, column+offset, width)
        sheet.write_url(2, 0, f"internal:'{table_names[table_id]}'!{xl_col_to_name(column)}1", link,
                        ('显示名（AI） · ' if ((detail.get('projection') or {}).get('title_presentation') or {}).get('authority') == 'AGENT_DISPLAY_LABEL' else '')
                        + '原表保真视图 · 查看本表逐项分析 →')
        sheet.freeze_panes(3, 0)
        sheet.set_footer('&L原表与同表分析 · 响应判断与主表同步&R&P / &N', {'margin': 0.2})
        for source_row, targets in row_targets.items():
            row = source_row-1
            ids = [n.get('requirement_code') or n['source_item']['source_display_id'] for n in targets]
            target = targets[0] if len(targets) == 1 else (owner or targets[0])
            sheet.write_url(row, column, f"internal:'ComplianceMatrix'!A{matrix_rows[target['node_id']]}", link, '\n'.join(ids))
            for offset, field in mirrored.items():
                main_column = headers.index(field)
                refs = [f"'ComplianceMatrix'!{xl_col_to_name(main_column)}{matrix_rows[n['node_id']]}" for n in targets]
                formula = '=(' + ')&CHAR(10)&('.join(f'IF({ref}="","",{ref})' for ref in refs) + ')'
                value = '\n'.join(str(main_values[n['node_id']][main_column] or '') for n in targets)
                sheet.write_formula(row, column+offset, formula, body, value)
            rationale = '\n'.join(str(n['source_item'].get('decision_summary') or '未记录原文判定依据。') for n in targets)
            analysis = '\n'.join(response_detail(n) for n in targets)
            sheet.write(row, column+5, rationale, body)
            sheet.write(row, column+7, analysis, body)
            reading = '\n'.join(visual_readings.get(n['node_id'], '') for n in targets).strip()
            if has_readings:
                preview = reading
                if height([reading], [52], 10000) > 380:
                    preview = reading[:520] + '\n完整释读见本单元格批注。'
                    sheet.write_comment(row, column+8, VISUAL_READING_LABEL + '\n' + reading, {'x_scale': 2, 'y_scale': 3})
                image_targets = list(dict.fromkeys(url for n in targets for url in shared_figure_targets(n)))
                if image_targets:
                    sheet.write_url(row, column+8, image_targets[0], body, '查看共享原图 →\n' + preview)
                else:
                    sheet.write(row, column+8, preview, body)
            for offset, full in ((5, rationale), (7, analysis)):
                if height([full], [analysis_widths[offset]], 10000) > 400:
                    sheet.write_comment(row, column+offset, full[:32700], {'x_scale': 2, 'y_scale': 3})
            sheet.set_row(row, max(sheet.row_sizes.get(row, [15])[0], height([rationale, analysis, reading], [42, 52, 52], 400)))

    # A figure shared by rows or owned by a structural scope must not disappear
    # merely because that scope is not a response row.
    for node in nodes:
        remaining = [f for f in e._matrix_node_figure_refs(node) if figure_key(f) not in rendered_figures]
        if remaining:
            add_evidence(node, figures=remaining)
    if evidence: finish(evidence, evidence_cursor-3, 3)
    if missing_images or stats['cell_images_failed']:
        raise ValueError(f"SOURCE_IMAGE_EXPORT_INCOMPLETE: figure failures={missing_images}; table cell failures={stats['cell_images_failed']}")
    e._append_hidden_matrix_meta_sheet(workbook=workbook, formats=formats, snapshot=snapshot, nodes=nodes,
        matrix_row_by_node=navigation_rows, matrix_row_span_by_node={k:(v,v) for k,v in navigation_rows.items()},
        table_sheet_names=table_names, project_name=project_name, customer_name=customer_name,
        version_label=version_label, export_mode=export_mode, compare_version_label=compare_version_label,
        compare_matrix_snapshot=compare_matrix_snapshot, table_stats=stats,
        oversized_text_detail_count=len(evidence_links), figure_detail_reference_count=image_count,
        unresolved_figure_reference_count=missing_images)
    return {'rendered_source_keys': sorted(rendered_figures), 'figure_placements': figure_placements,
            'inserted_placement_count': image_count + stats['cell_images_inserted']}
