export const CLASS_LABELS = Object.freeze({F: '功能要求', Q: '质量要求', C: '约束要求', M: '复合要求', CONTEXT: '背景信息', DEFINITION: '定义', REFERENCE: '引用', NOTE: '说明', METADATA: '文档信息', REVISION_HISTORY: '修订记录', EXAMPLE: '示例', UNRESOLVED: '待补证据'});
export const REQUIREMENT_CLASSES = new Set(['F', 'Q', 'C', 'M']);
export const TABLE_SELECTION_EVIDENCE_LABEL = '派生需求 · 当前表格行与共享内容';
export const PROXY_ACTION_LABELS = Object.freeze({
  KEEP_SINGLE: '保留为单一核对单元',
  KEEP_WITH_GAP: '保留并标出待澄清信息',
  SPLIT: '拆分为独立工程项',
  SHARED_SCOPE: '保留为共享适用约束',
  EXTRACT_WITH_CONTEXT: '提取独立义务并保留共同约束',
  RECLASSIFY_SUPPLY: '重判为供货范围约束',
  KEEP_EXISTING: '回归检查后维持首版派生',
  KEEP_MISSING: '保持缺失输入，不生成要求',
});

export function normalizedSearch(value) {
  return String(value ?? '').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g, ' ').trim();
}

export function effectiveTarget(target, review) {
  const saved = review.targets?.[target.id];
  return {...target, ...(saved?.patch ?? {}), reviewStatus: saved?.status ?? 'unreviewed', notes: saved?.notes ?? [], lastEventId: saved?.lastEventId, actor: saved?.actor};
}

export function proxyState(item) {
  const proxy = item?.proxyReview;
  if (!proxy) return null;
  return {
    actionLabel: PROXY_ACTION_LABELS[proxy.action] ?? proxy.action,
    statusLabel: proxy.readiness === 'clarification-needed'
      ? '存在待澄清信息，等待最终评判'
      : '代理候选已就绪，等待最终评判',
    needsClarification: proxy.readiness === 'clarification-needed',
  };
}

// A saved human or QA state always takes precedence over an Agent origin label.
export function targetReviewLabel(target, review, parent = null) {
  const effective = effectiveTarget(target, review);
  const labels = {unreviewed:'Agent 初稿', reviewed:'人工已核对', changed:'人工已修改 · 待复核', 'needs-review':'已记录问题'};
  if (effective.actor?.kind === 'qa' && effective.reviewStatus !== 'unreviewed') {
    return `交互验证 · ${labels[effective.reviewStatus].replace('人工','测试')}`;
  }
  if (effective.reviewStatus !== 'unreviewed') return labels[effective.reviewStatus];
  if (target.origin === 'agent-proxy') return '代理派生 · 待最终评判';
  if (target.proxyReview) return '代理复核 · 待最终评判';
  if (parent?.proxyReview?.action === 'KEEP_EXISTING') return '代理回归保留 · 待最终评判';
  return labels.unreviewed;
}

export function targetNeedsAttention(target, review) {
  const effective = effectiveTarget(target, review);
  return Boolean(target.issues?.length)
    || effective.class === 'UNRESOLVED'
    || ['changed','needs-review'].includes(effective.reviewStatus);
}

export function needsAttention(item, review) {
  return [item,...(item.derived ?? [])].some(target => targetNeedsAttention(target, review));
}

export function derivedTreeLabel(item, derived, review) {
  const index = Math.max(0, (item.derived ?? []).findIndex(candidate => candidate.id === derived.id));
  const text = effectiveTarget(derived, review).text.replace(/\s+/g, ' ').trim();
  return `要求 ${index + 1} · ${text}`;
}

export function sourceParentId(target) {
  return target.parentId ?? target.id;
}

export function pageEvidenceCaption(page, pdfPage, totalPages) {
  const printed = String(page?.printedLabel ?? '').trim();
  return `PDF ${pdfPage} / ${totalPages}${printed ? ` · 文内页码 ${printed}` : ''} · 原文像素`;
}

export function resolvedTreeOrder(directOrder, childOrders, fallbackOrder) {
  if (Number.isFinite(directOrder)) return directOrder;
  const valid = childOrders.filter(Number.isFinite);
  return valid.length ? Math.min(...valid) : fallbackOrder;
}

export function normalizedCellVisualRefs(cell, fallbackPage) {
  if (!Array.isArray(cell?.visualRefs)) return [];
  return cell.visualRefs.flatMap((ref, refIndex) => {
    if (!ref || typeof ref !== 'object') return [];
    const page = Number.isInteger(ref.page) ? ref.page : fallbackPage;
    const candidates = Array.isArray(ref.rects) ? ref.rects : Array.isArray(ref.bbox) ? [ref.bbox] : [];
    return candidates.flatMap((bbox, boxIndex) => {
      const valid = Array.isArray(bbox)
        && bbox.length === 4
        && bbox.every(Number.isFinite)
        && bbox[2] > bbox[0]
        && bbox[3] > bbox[1]
        && Number.isInteger(page)
        && page > 0;
      if (!valid) return [];
      const fallback = `单元格视觉 ${refIndex + 1}.${boxIndex + 1}`;
      return [{page,bbox,label:String(ref.label ?? ref.alt ?? fallback).trim() || fallback}];
    });
  });
}

export function draftIsDirty(draft) {
  return Boolean(draft.note?.trim()) || draft.class !== draft.baseClass || draft.text !== draft.baseText;
}

export function matchesItem(item, query, filter, review) {
  const effective = effectiveTarget(item, review);
  const q = normalizedSearch(query);
  if (q && !normalizedSearch([item.label, item.sectionId, item.text, item.reason, item.proxyReview?.reason, item.proxyReview?.sharedContext, ...(item.derived ?? []).map(d => effectiveTarget(d, review).text)].join(' ')).includes(q)) return false;
  if (filter === 'requirements' && !REQUIREMENT_CLASSES.has(effective.class)) return false;
  if (filter === 'issues' && !needsAttention(item,review)) return false;
  if (filter === 'reviewed' && effective.reviewStatus !== 'reviewed') return false;
  if (filter === 'proxy' && !item.proxyReview && !(item.derived ?? []).some(d => d.origin === 'agent-proxy')) return false;
  return true;
}

export function bounds(rects) {
  if (!rects.length) return null;
  return [Math.min(...rects.map(r => r[0])), Math.min(...rects.map(r => r[1])), Math.max(...rects.map(r => r[2])), Math.max(...rects.map(r => r[3]))];
}

export function tableGrid(fragment) {
  const round = n => Math.round(n * 1000) / 1000;
  const xs = [...new Set(fragment.cellsFlat.flatMap(c => [round(c.bbox[0]), round(c.bbox[2])]))].sort((a,b) => a-b);
  const ys = [...new Set(fragment.cellsFlat.flatMap(c => [round(c.bbox[1]), round(c.bbox[3])]))].sort((a,b) => a-b);
  const cells = fragment.cellsFlat.map(cell => ({...cell,
    startRow: ys.indexOf(round(cell.bbox[1])), endRow: ys.indexOf(round(cell.bbox[3])),
    startCol: xs.indexOf(round(cell.bbox[0])), endCol: xs.indexOf(round(cell.bbox[2]))}));
  return {xs, ys, cells};
}

// Slice from actual geometry. A merged cell is inherited from its real owner,
// never filled from a neighbouring non-merged cell or a nonempty-value heuristic.
export function tableSlice(fragment, selectedRows = null) {
  const grid = tableGrid(fragment);
  const rows = selectedRows ? [...new Set(selectedRows)].sort((a,b) => a-b) : Array.from({length: grid.ys.length-1}, (_,i) => i);
  const seen = new Set();
  return {grid, rows: rows.map(row => ({row, cells: grid.cells.filter(c => c.startRow <= row && c.endRow > row && !seen.has(c.id)).sort((a,b) => a.startCol-b.startCol).map(c => {
    seen.add(c.id);
    return {...c, rowSpan: rows.filter(r => r >= row && r < c.endRow).length, colSpan: c.endCol-c.startCol, inherited: c.startRow < row};
  })}))};
}

export function cellContext(fragment, cell) {
  const {cells}=tableGrid(fragment), roles=fragment.roles??{};
  const headerRows=roles.columnHeaderRows??[], noteRows=roles.noteRows??[];
  const headerCols=roles.rowHeaderRelations?.[String(cell.startCol)]??roles.rowHeaderColumns??[];
  const columnHeaders=cells.filter(c=>c.id!==cell.id&&headerRows.includes(c.startRow)&&c.startCol<cell.endCol&&c.endCol>cell.startCol);
  const rowHeaders=cells.filter(c=>c.id!==cell.id&&!headerRows.includes(c.startRow)&&!noteRows.includes(c.startRow)&&headerCols.includes(c.startCol)&&c.startRow<cell.endRow&&c.endRow>cell.startRow);
  const notes=cells.filter(c=>noteRows.includes(c.startRow));
  const fallback=columnHeaders.length?[]:(roles.columnLabels??[]).slice(cell.startCol,cell.endCol).filter(Boolean);
  const headerTexts=[...new Set([...rowHeaders,...columnHeaders].map(c=>c.text.replace(/\s+/g,' ').trim()).filter(Boolean))];
  return {headerIds:[...new Set([...rowHeaders,...columnHeaders].map(c=>c.id))], noteIds:notes.map(c=>c.id), label:[roles.description,...headerTexts,...fallback,cell.text.replace(/\s+/g,' ').trim()||'原文空白单元格'].filter(Boolean).join('；')};
}

export function sourceRowRects(item, tables) {
  if (!item.table) return [];
  const logical = tables.find(t => t.id === item.table.logicalId);
  return item.table.selected.flatMap(selection => {
    const fragment = logical?.fragments.find(f => f.tableId === selection.tableId);
    if (!fragment) return [];
    const {ys} = tableGrid(fragment);
    return selection.rows.map(row => ({page: fragment.page, rects: [[fragment.bbox[0], ys[row], fragment.bbox[2], ys[row+1]]], precision: 'table-row', lineIds: []}));
  });
}

export function sourceBlocks(item, bundle) {
  const allTables = (item.matrixTables ?? []).map(id => bundle.tables.find(t => t.id === id)).filter(Boolean);
  const fragments = allTables.flatMap(t => t.fragments);
  const blocks = [];
  let last = null;
  for (const id of item.lineIds ?? []) {
    const line = bundle.lines[id];
    if (!line) continue;
    if (allTables.some(t => t.label.replace(/\s/g,'') === line.text.replace(/\s/g,''))) {last = null; continue;}
    const [x0,y0,x1,y1] = line.bbox;
    const inside = fragments.some(f => f.page === line.page && (x0+x1)/2 >= f.bbox[0]-1 && (x0+x1)/2 <= f.bbox[2]+1 && (y0+y1)/2 >= f.bbox[1]-1 && (y0+y1)/2 <= f.bbox[3]+1);
    if (inside) {last = null; continue;}
    if (last?.page === line.page) last.lines.push(line);
    else {last = {type:'text', page:line.page, y:y0, block:line.block, lines:[line]}; blocks.push(last);}
  }
  for (const table of allTables) {
    for (const fragment of table.fragments) blocks.push({type:'table', page:fragment.page, y:fragment.bbox[1], table, fragment});
  }
  if (item.figure) blocks.push({type:'figure', page:item.figure.page, y:item.figure.bbox[1], figure:item.figure});
  return blocks.sort((a,b) => a.page-b.page || a.y-b.y);
}

export function targetPayload(target, review, action, draft, mutationId) {
  const patch = {};
  if (action === 'correct') {
    const current = effectiveTarget(target, review);
    if (draft.class !== (draft.baseClass ?? current.class)) patch.class = draft.class;
    if (target.parentId && draft.text.trim() !== (draft.baseText ?? current.text)) patch.text = draft.text.trim();
  }
  return {mutationId, expectedRevision:review.revision, sourceRevision:review.sourceRevision, targetId:target.id, action, note: draft.note ?? '', patch,
    ...(action === 'undo' ? {undoOf: review.targets?.[target.id]?.lastEventId} : {})};
}
