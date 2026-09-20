import {CLASS_LABELS, REQUIREMENT_CLASSES, TABLE_SELECTION_EVIDENCE_LABEL, effectiveTarget, targetNeedsAttention, needsAttention, derivedTreeLabel, sourceParentId, pageEvidenceCaption, resolvedTreeOrder, normalizedCellVisualRefs, draftIsDirty, matchesItem, proxyState, targetReviewLabel, bounds, tableGrid, tableSlice, cellContext, sourceRowRects, sourceBlocks, targetPayload} from './review-core.js';

const $ = selector => document.querySelector(selector);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const button = (text, className, action) => {
  const b = el('button', className, text); b.type = 'button'; b.addEventListener('click', action); return b;
};
let bundle, review, nodes, itemMap, sectionMap, tableMap;
let selectedNode = null, selectedDerived = null, currentPage = 1, activeRefs = [], refLabel = '';
let expanded = new Set(), visibleNodes = [], drafts = new Map(), saving = false, compactEvidence = false;
let toastTimer, imageReady = false, pendingMutation = null, detailValid = false, conflicts = new Set(), tableSerial = 0, bundleStale = false;

async function api(path, options) {
  const response = await fetch(path, {cache:'no-store', ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error ?? `请求失败（${response.status}）`);
  return data;
}
function toast(message) {
  clearTimeout(toastTimer); $('#toast').textContent = message; $('#toast').hidden = false;
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 6000);
}
function badge(code) { return el('span', `classification code-${code}`, CLASS_LABELS[code] ?? code); }
function reviewLabel(target, parent = null) { return targetReviewLabel(target, review, parent); }
function currentItem() { return selectedNode?.itemId ? itemMap.get(selectedNode.itemId) : null; }
function currentTarget() {
  const item = currentItem();
  return item?.derived.find(d => d.id === selectedDerived) ?? item;
}

function buildNodes() {
  nodes = new Map();
  const ownerNodeByItem = new Map();
  for (const s of bundle.outline) nodes.set(`sec:${s.id}`, {id:`sec:${s.id}`, kind:'section', source:s, label:[s.label,s.title].filter(Boolean).join(' '), parentId:s.parentId ? `sec:${s.parentId}` : null, children:[], page:s.page});
  for (const n of nodes.values()) if (n.parentId) nodes.get(n.parentId)?.children.push(n.id);
  for (const t of bundle.tables.filter(t => t.mode === 'rows')) {
    const n = {id:`table:${t.id}`, kind:'table', source:t, label:t.label, parentId:`sec:${t.section}`, children:[], page:t.fragments[0].page};
    nodes.set(n.id,n); nodes.get(n.parentId)?.children.push(n.id);
  }
  const bySection = new Map();
  for (const item of bundle.items) {
    if (!bySection.has(item.sectionId)) bySection.set(item.sectionId, []);
    bySection.get(item.sectionId).push(item);
  }
  for (const [sid, items] of bySection) {
    const section = nodes.get(`sec:${sid}`);
    const nonRows = items.filter(i => !i.table);
    if (nonRows.length === 1) {
      section.itemId = nonRows[0].id;
      ownerNodeByItem.set(nonRows[0].id, section.id);
    }
    for (const item of items) {
      if (!item.table && nonRows.length === 1) continue;
      const parentId = item.table ? `table:${item.table.logicalId}` : `sec:${sid}`;
      const n = {id:`item:${item.id}`, kind:'item', itemId:item.id, source:item, label:item.label, parentId, children:[], page:item.sourceRefs[0]?.page ?? section.page};
      nodes.set(n.id,n); nodes.get(parentId)?.children.push(n.id); ownerNodeByItem.set(item.id, n.id);
    }
  }
  for (const item of bundle.items) {
    const parentId = ownerNodeByItem.get(item.id);
    if (!parentId) continue;
    for (const [index, derived] of (item.derived ?? []).entries()) {
      const ref = derived.sourceRefs?.[0] ?? item.sourceRefs?.[0];
      const n = {id:`derived:${derived.id}`, kind:'derived', itemId:item.id, derivedId:derived.id, derivedIndex:index, source:derived, label:derived.text, parentId, children:[], page:ref?.page ?? nodes.get(parentId)?.page ?? 1};
      nodes.set(n.id,n); nodes.get(parentId)?.children.push(n.id);
    }
  }
  const orderMemo = new Map();
  function directOrder(n) {
    const item = n.itemId && itemMap.get(n.itemId);
    const ref = n.kind === 'derived' ? n.source.sourceRefs?.[0] ?? item?.sourceRefs?.[0] : item?.sourceRefs?.[0];
    if (ref) return n.page * 10000 + (ref.rects[0]?.[1] ?? 0);
    if (n.source.headingLineIds?.length) return n.page * 10000 + bundle.lines[n.source.headingLineIds[0]].bbox[1];
    if (n.source.fragments?.length) return n.page * 10000 + n.source.fragments[0].bbox[1];
    return null;
  }
  function order(n) {
    if (orderMemo.has(n.id)) return orderMemo.get(n.id);
    const result=resolvedTreeOrder(directOrder(n),n.children.map(id=>order(nodes.get(id))),n.page*10000);
    orderMemo.set(n.id,result);return result;
  }
  for (const n of nodes.values()) n.children.sort((a,b) => order(nodes.get(a))-order(nodes.get(b)));
}

function ancestors(node) {
  const result = []; let n = node;
  while (n) { result.unshift(n); n = nodes.get(n.parentId); }
  return result;
}
function expandPath(node) { for (const n of ancestors(node).slice(0,-1)) expanded.add(n.id); }
function nodeLabel(node) {
  return node.kind === 'derived' ? derivedTreeLabel(itemMap.get(node.itemId), node.source, review) : node.label;
}
function nodeForItem(id) { return [...nodes.values()].find(n => n.itemId === id && n.kind !== 'derived'); }
function sectionNode(sid) { return nodes.get(`sec:${sid}`); }

function renderTree(openMatches = false) {
  const query = $('#search').value, filter = $('#filter').value;
  const filtered = Boolean(query.trim()) || filter !== 'all';
  const matching = new Set(bundle.items.filter(i => matchesItem(i, query, filter, review)).map(i => i.id));
  const visible = new Set();
  for (const node of nodes.values()) {
    if (!filtered || matching.has(node.itemId)) {
      visible.add(node.id);
      for (const parent of ancestors(node)) visible.add(parent.id);
      if (openMatches && filtered) expandPath(node);
    }
  }
  const tree = $('#tree'); tree.replaceChildren(); visibleNodes = [];
  function walk(node, level) {
    if (!visible.has(node.id)) return;
    visibleNodes.push(node);
    const displayLabel = nodeLabel(node);
    const row = el('div', `tree-row${node.kind === 'derived' ? ' tree-derived' : ''}${selectedNode?.id === node.id ? ' selected' : ''}`);
    row.dataset.node = node.id; row.style.setProperty('--level', level);
    row.setAttribute('role','treeitem'); row.setAttribute('aria-level', String(level+1));
    row.setAttribute('aria-selected', String(selectedNode?.id === node.id));
    row.tabIndex = selectedNode?.id === node.id ? 0 : -1; row.title = displayLabel;
    if (node.children.length) {
      row.setAttribute('aria-expanded', String(expanded.has(node.id)));
      const toggle = button(expanded.has(node.id) ? '⌄' : '›','tree-disclosure', e => {
        e.stopPropagation(); expanded.has(node.id) ? expanded.delete(node.id) : expanded.add(node.id); renderTree(); focusTree(node.id);
      });
      toggle.tabIndex = -1; toggle.setAttribute('aria-label', `${expanded.has(node.id) ? '收起' : '展开'}${displayLabel}`); row.append(toggle);
    } else row.append(el('span','tree-spacer', ''));
    const label = el('span','tree-label', displayLabel); row.append(label);
    if (node.itemId) {
      const item = itemMap.get(node.itemId), target = node.kind === 'derived' ? node.source : item, eff = effectiveTarget(target, review);
      if (node.kind !== 'derived' && item.derived?.length) row.append(el('span','tree-child-count',`${item.derived.length}条`));
      const attention = node.kind === 'derived' ? targetNeedsAttention(target,review) : needsAttention(item,review);
      const proxy = node.kind === 'derived' ? target.origin === 'agent-proxy' : Boolean(item.proxyReview);
      const mark = attention ? '!' : eff.reviewStatus === 'reviewed' ? '✓' : proxy ? '代' : REQUIREMENT_CLASSES.has(eff.class) ? '·' : '';
      const flag = el('span', `tree-state ${mark==='!'?'attention':eff.reviewStatus === 'reviewed' ? 'reviewed':mark==='代'?'proxy':''}`, mark);
      flag.setAttribute('aria-label', mark === '!' ? '需要关注' : reviewLabel(target,item)); row.append(flag);
    } else row.append(el('span','tree-page', String(node.page)));
    row.addEventListener('click', () => { selectNode(node); focusTree(node.id); });
    row.addEventListener('keydown', treeKey); tree.append(row);
    if (expanded.has(node.id)) node.children.forEach(id => walk(nodes.get(id), level+1));
  }
  [...nodes.values()].filter(n => !n.parentId).sort((a,b) => a.page-b.page).forEach(n => walk(n,0));
  if (!visibleNodes.length) tree.append(el('p','empty-tree','没有匹配条目。试试章节号、参数或清除筛选。'));
  if (!tree.querySelector('[tabindex="0"]')) tree.querySelector('[role="treeitem"]')?.setAttribute('tabindex','0');
  tree.tabIndex = visibleNodes.length ? -1 : 0;
  const allDerived=bundle.items.flatMap(i=>i.derived ?? []);
  $('#tree-count').textContent = filtered ? `${matching.size} / ${bundle.items.length} 条原文 · 保留所属路径和派生要求` : `${bundle.items.length} 条原文 · ${allDerived.length} 条派生 · 按原文顺序`;
  const confirmed = bundle.items.filter(i => effectiveTarget(i, review).reviewStatus === 'reviewed').length;
  const derived=allDerived, checkedDerived=derived.filter(d=>effectiveTarget(d,review).reviewStatus==='reviewed').length;
  const proxyCount=bundle.proxyReview?.summary?.reviewedItems;
  const reviewKind=review.reviewerKind==='qa'?'交互验证':'人工确认';
  $('#review-count').textContent = `${proxyCount === undefined ? '' : `代理复核 ${proxyCount} 条 · `}${reviewKind}：原文 ${confirmed}/${bundle.items.length} · 派生 ${checkedDerived}/${derived.length}`;
}

function focusTree(id) { [...$('#tree').querySelectorAll('[data-node]')].find(e => e.dataset.node === id)?.focus({preventScroll:true}); }
function treeKey(event) {
  const node = nodes.get(event.currentTarget.dataset.node);
  const index = visibleNodes.indexOf(node); let next;
  if (event.key === 'ArrowDown') next = visibleNodes[Math.min(index+1,visibleNodes.length-1)];
  else if (event.key === 'ArrowUp') next = visibleNodes[Math.max(index-1,0)];
  else if (event.key === 'Home') next = visibleNodes[0];
  else if (event.key === 'End') next = visibleNodes.at(-1);
  else if (event.key === 'ArrowRight') {
    if (node.children.length && !expanded.has(node.id)) {expanded.add(node.id); renderTree(); focusTree(node.id);}
    else next = nodes.get(node.children[0]);
  } else if (event.key === 'ArrowLeft') {
    if (expanded.has(node.id)) {expanded.delete(node.id); renderTree(); focusTree(node.id);}
    else next = nodes.get(node.parentId);
  } else if (event.key === 'Enter' || event.key === ' ') next = node;
  else return;
  event.preventDefault();
  if (next) {selectNode(next); focusTree(next.id);}
}

function selectNode(node) {
  if (!node) return;
  selectedNode = node; selectedDerived = node.kind === 'derived' ? node.derivedId : null; expandPath(node);
  if (node.children.some(id => nodes.get(id)?.kind === 'derived')) expanded.add(node.id);
  renderTree(); renderDetail(); renderReview();
  const selectedRow = $('#tree .selected');
  if (selectedRow) {
    const box = selectedRow.getBoundingClientRect(), treeBox = $('#tree').getBoundingClientRect();
    if (box.top < treeBox.top || box.bottom > treeBox.bottom) selectedRow.scrollIntoView({block:'center'});
  }
  const item = currentItem();
  if (node.kind === 'derived') showDerivedEvidence(node.source,item);
  else if (item) showRefs(item.table ? sourceRowRects(item,bundle.tables) : item.sourceRefs, item.label);
  else if (node.kind === 'table') showRefs(node.source.fragments.map(f => ({page:f.page,rects:[f.bbox],precision:'table'})), node.label);
  else {
    const ls = node.source.headingLineIds?.map(id => bundle.lines[id]) ?? [];
    showRefs(ls.length ? [{page:node.page,rects:ls.map(l=>l.bbox),precision:'heading'}] : [{page:node.page,rects:[],precision:'page'}], node.label);
  }
  $('#detail').scrollTop=0;
}
function goSection(id) {
  const node = sectionNode(id); if (!node) return;
  $('#search').value=''; $('#filter').value='all';
  const tableNode = node.children.map(id=>nodes.get(id)).find(n=>n.kind==='table');
  selectNode(tableNode ?? node);
}

function headingBlock(node) {
  const header = el('header','detail-header');
  const path = el('nav','breadcrumbs'); path.setAttribute('aria-label','原文路径');
  for (const parent of ancestors(node).slice(0,-1)) path.append(button(nodeLabel(parent),'breadcrumb',()=>selectNode(parent)));
  header.append(path,el('h2','detail-title',nodeLabel(node)));
  const meta=el('div','detail-meta');
  if (node.kind === 'derived') {
    const item=itemMap.get(node.itemId), effective=effectiveTarget(node.source,review);
    meta.append(badge(effective.class),el('span','decision-origin',reviewLabel(node.source,item)),el('span','muted',`派生要求 ${node.derivedIndex+1}/${item.derived.length}`));
  }
  else if (node.itemId) {
    const item=itemMap.get(node.itemId);
    meta.append(badge(effectiveTarget(item,review).class),el('span','decision-origin',reviewLabel(item)));
    const proxy=proxyState(item); if(proxy) meta.append(el('span','proxy-action compact',proxy.actionLabel));
    meta.append(el('span','muted',`原文条目 ${item.ordinal}`));
  }
  else meta.append(el('span','muted',node.kind==='table' ? '同一逻辑表 · 原始片段按页连接' : '原文章节 · 标题不重复生成条目'));
  header.append(meta); return header;
}

function renderProxyJudgement(item, effective) {
  const proxy=item.proxyReview, state=proxyState(item);
  const judgement=el('section','judgement proxy-judgement');
  const heading=el('div','proxy-heading');
  heading.append(el('h3','section-label','代理判断'),el('span','proxy-action',state.actionLabel));
  judgement.append(heading);
  const classLine=el('div','proxy-class-change');
  classLine.append(el('span','small-muted','首版分类'),badge(proxy.previousClass),el('span','proxy-arrow','→'),el('span','small-muted','代理候选'),badge(item.class));
  judgement.append(classLine,el('p','reason',proxy.reason));
  if(proxy.sharedContext) {
    const context=el('div','proxy-context'); context.append(el('strong','','适用范围 / 共同约束'),el('p','',proxy.sharedContext)); judgement.append(context);
  }
  if(proxy.coveredItemIds?.length) judgement.append(el('p','proxy-coverage',`本判断关联 ${proxy.coveredItemIds.length} 条具体原文记录；共享范围本身不重复计作工程叶项。`));
  if(proxy.sourceRefs?.length) judgement.append(button('查看代理判断的共同原文依据','text-button',()=>showRefs(proxy.sourceRefs,'代理判断的共同原文依据')));
  const readiness=el('p',`proxy-readiness${state.needsClarification?' needs-clarification':''}`,state.statusLabel); readiness.setAttribute('role','status'); judgement.append(readiness);
  if(proxy.previousReason && proxy.previousReason!==proxy.reason) {
    const previous=el('details','previous-reason'); previous.append(el('summary','','查看首版判断依据'),el('p','',proxy.previousReason)); judgement.append(previous);
  }
  if(effective.class!==item.class) judgement.append(el('p','human-change',`人工分类：${CLASS_LABELS[effective.class]}；代理候选：${CLASS_LABELS[item.class]}。客户原文不变。`));
  return judgement;
}

function retainedDecisionText(item) {
  const proxy=item.proxyReview;
  if(!proxy) return item.class==='UNRESOLVED'?'证据不足，暂不生成参数或要求；保留原文入口等待补充附件。':REQUIREMENT_CLASSES.has(item.class)?'当前保留原条款为一个核对单元，未另造重复副本。如需要进一步拆分，可记录拆分建议。':'本条为保留的非需求内容，不生成派生需求。';
  if(proxy.action==='SHARED_SCOPE') return `代理判断：本条作为共享适用约束保留，已关联 ${proxy.coveredItemIds.length} 条具体记录，不重复生成工程叶项。`;
  if(proxy.action==='KEEP_WITH_GAP') return '代理判断：当前原文已经构成一个核对单元，但仍有缺口或待定信息；保留原文并等待澄清，不补造数值。';
  if(proxy.action==='KEEP_MISSING') return '代理判断：当前只有附件入口，正文证据缺失；不生成要求，等待补充附件。';
  if(proxy.action==='KEEP_SINGLE') return '代理判断：当前原文已是一个可独立核对的单元，无需再生成重复派生。';
  return '代理复核后维持当前原文单元，不新增派生。';
}

function renderDerivedFocus(item, derived) {
  const effective=effectiveTarget(derived,review), index=item.derived.findIndex(candidate=>candidate.id===derived.id);
  const focus=el('section','derived-focus');
  const head=el('div','section-heading'); head.append(el('h3','section-label',`当前派生要求 ${index+1}/${item.derived.length}`),el('span','small-muted','从下方父项原文派生'));
  const meta=el('div','derived-meta'); meta.append(badge(effective.class),el('span',derived.origin==='agent-proxy'?'proxy-origin':'small-muted',reviewLabel(derived,item)));
  focus.append(head,meta,el('p','derived-focus-text',effective.text),button('定位这条要求的原文依据','text-button',()=>showDerivedEvidence(derived,item)));
  if (effective.text!==derived.text) {const previous=el('details','previous-text'); previous.append(el('summary','',`查看${derived.origin==='agent-proxy'?'代理候选':'Agent 原稿'}`),el('p','',derived.text)); focus.append(previous);}
  if (derived.caveat) {const caveat=el('div','proxy-caveat'); caveat.append(el('strong','','代理说明（非要求正文）'),el('p','',derived.caveat)); focus.append(caveat);}
  return focus;
}

function renderDetail() {
  detailValid=false;
  try {paintDetail(); detailValid=true;}
  catch(error) {
    $('#detail').append(el('p','global-error',`此条内容未能完整呈现，已暂停本条反馈：${error.message}。请重新选择或刷新，不以残缺视图确认结果。`));
    $('#review-panel').replaceChildren();
    console.error('Review detail rendering failed', error);
  }
}

function paintDetail() {
  const detail=$('#detail'); detail.replaceChildren();
  if (!selectedNode) return;
  detail.append(headingBlock(selectedNode));
  if (selectedNode.kind==='table') {
    const table=selectedNode.source;
    if (table.context) detail.append(el('p','shared-context',table.context));
    detail.append(el('p','section-helper','点击表格单元格定位原页；左侧展开此表可逐行核对判断和拆分。'));
    for (const fragment of table.fragments) detail.append(renderTable(fragment, null, table.label));
    return;
  }
  const item=currentItem();
  if (!item) {
    detail.append(el('p','section-helper','选择下列原文分支查看完整内容、判断和证据。'));
    const list=el('div','section-children');
    for (const id of selectedNode.children) {const node=nodes.get(id); list.append(button(node.label,'branch-link',()=>selectNode(node)));}
    detail.append(list); return;
  }
  const eff=effectiveTarget(item,review);
  const focusedDerived=selectedDerived ? item.derived.find(candidate=>candidate.id===selectedDerived) : null;
  let judgement;
  if(item.proxyReview) judgement=renderProxyJudgement(item,eff);
  else {
    judgement=el('section','judgement'); judgement.append(el('h3','section-label','Agent 首版判断'), el('p','reason',item.reason));
    if (eff.class!==item.class) judgement.append(el('p','human-change',`人工分类：${CLASS_LABELS[eff.class]}；Agent 首版：${CLASS_LABELS[item.class]}。客户原文不变。`));
  }
  const issueIds=new Set(item.issues);
  const issues=bundle.issues.filter(i=>issueIds.has(i.id));
  const jumps=el('nav','detail-jumps'); jumps.setAttribute('aria-label','条款内容定位');
  const jumpTo=selector=>$('#detail').querySelector(selector)?.scrollIntoView({block:'start'});
  jumps.append(button('原文','text-button',()=>jumpTo('.original-section')));
  if(focusedDerived) jumps.prepend(button('当前派生要求','text-button',()=>jumpTo('.derived-focus')));
  if(item.proxyReview) jumps.append(button('代理判断','text-button proxy-jump',()=>jumpTo('.proxy-judgement')));
  jumps.append(button(item.derived.length?`拆分 ${item.derived.length}`:'拆分判断','text-button',()=>jumpTo('.derived-section')));
  if(issues.length) jumps.append(button(`需核对 ${issues.length}`,'text-button issue-jump',()=>jumpTo('.issues-panel')));
  detail.append(jumps);
  if(focusedDerived) detail.append(renderDerivedFocus(item,focusedDerived));
  const original=el('section','original-section');
  const title=el('div','section-heading'); title.append(el('h3','section-label',focusedDerived?'父项原文与共同条件':'原文内容'),el('span','small-muted',item.table?'完整业务行 · 共享单元格保留':'不改写客户原文')); original.append(title);
  if (item.table) renderSourceRow(original,item);
  else for (const block of sourceBlocks(item,bundle)) {
    if (block.type==='table') original.append(renderTable(block.fragment,null,block.table.label));
    else if (block.type==='figure') original.append(renderFigure(block.figure));
    else {
      const p=button(block.lines.map(l=>l.text).join('\n'),'source-paragraph',()=>showRefs([{page:block.page,rects:block.lines.map(l=>l.bbox),precision:'line'}], '原文段落'));
      p.title=`定位 PDF 第${block.page}页`; original.append(p);
    }
  }
  if (item.context) original.append(el('p','shared-context',item.context));
  if (item.inheritedSourceRefs?.length) {
    const context=el('details','inherited-evidence'); context.open=true; context.append(el('summary','', '共同依据（继承自原文，不是新增要求）'));
    for (const ref of item.inheritedSourceRefs) context.append(button(ref.lineIds.map(id=>bundle.lines[id].text).join('\n'),'source-paragraph inherited',()=>showRefs([ref],'共同依据')));
    original.append(context);
  }
  detail.append(original);
  detail.append(judgement);
  if (issues.length) detail.append(renderIssues(issues));
  const derived=el('section','derived-section');
  const derivedHead=el('div','section-heading'); derivedHead.append(el('h3','section-label',item.derived.length ? `拆分结果 · ${item.derived.length} 条草稿` : '拆分判断'));
  derivedHead.append(el('span','small-muted','派生表达与原文分开')); derived.append(derivedHead);
  if (!item.derived.length) derived.append(el('p','kept-unit',retainedDecisionText(item)));
  for (const d of item.derived) {
    const effective=effectiveTarget(d,review);
    const entry=el('article',`derived-entry${selectedDerived===d.id?' selected':''}`); entry.dataset.derived=d.id;
    const bar=el('div','derived-meta'); bar.append(badge(effective.class),el('span',d.origin==='agent-proxy'?'proxy-origin':'small-muted',reviewLabel(d,item)));
    const sourceButton=button('核对这条 ↗','text-button',()=>selectDerived(d)); bar.append(sourceButton);
    entry.append(bar,button(effective.text,'derived-text',()=>selectDerived(d)));
    if (effective.text!==d.text) {const previous=el('details','previous-text'); previous.append(el('summary','',`查看${d.origin==='agent-proxy'?'代理候选':'Agent 原稿'}`),el('p','',d.text)); entry.append(previous);}
    if (d.caveat) {const caveat=el('div','proxy-caveat'); caveat.append(el('strong','','代理说明（非要求正文）'),el('p','',d.caveat)); entry.append(caveat);}
    if (d.precision==='parent-clause') entry.append(el('p','evidence-granularity','证据定位到完整父条款/业务行，尚非逐词对应。'));
    if (d.precision==='figure-and-lines') entry.append(el('p','evidence-granularity','证据包含完整原图，未声称已精确绑定图中单个箭头或节点。'));
    derived.append(entry);
  }
  detail.append(derived);
  const technical=el('details','technical-details'); technical.append(el('summary','','来源与工件身份'));
  technical.append(el('p','',`来源 SHA-256：${bundle.source.sha256}`),el('p','',`结果版本：${bundle.revision}`),el('p','',`条目：${item.id}`));
  detail.append(technical);
}

function renderIssues(issues) {
  const wrapper=el('section','issues-panel'); wrapper.append(el('h3','section-label',`需要核对 · ${issues.length}`));
  for (const issue of issues) {
    const d=el('details','issue');
    d.open = false;
    const title=el('summary','',issue.title); d.append(title,el('p','',issue.impact),el('p','issue-question',issue.question),button('查看原文位置','text-button',()=>showRefs(issue.sourceRefs,issue.title)));
    wrapper.append(d);
  }
  return wrapper;
}

function renderSourceRow(container,item) {
  const table=tableMap.get(item.table.logicalId);
  const selection = new Map(item.table.selected.map(s=>[s.tableId,new Set(s.rows)]));
  for (const [id,rows] of item.table.headers ?? []) {
    if (selection.has(id)) for (const r of rows) selection.get(id).add(r);
    else {
      const fragment=table.fragments.find(f=>f.tableId===id);
      if (fragment) container.append(renderTable(fragment,rows,`共享表头 · PDF ${fragment.page}`));
    }
  }
  for (const [id,rows] of selection) {
    const fragment=table.fragments.find(f=>f.tableId===id);
    container.append(renderTable(fragment,[...rows],table.label));
  }
  container.append(button('查看所属完整原表','text-button',()=>selectNode(nodes.get(`table:${table.id}`))));
}

function renderTable(fragment,rows,label) {
  const wrapper=el('figure','source-table');
  const cap=el('figcaption','table-caption'); cap.append(el('span','',label),button(`PDF ${fragment.page}`,'text-button',()=>showRefs([{page:fragment.page,rects:[fragment.bbox],precision:'table'}],label))); wrapper.append(cap);
  const scroll=el('div','table-scroll'); scroll.tabIndex=0; scroll.setAttribute('role','region'); scroll.setAttribute('aria-label',`${label}，可横向滚动`);
  const table=el('table','native-table'); table.setAttribute('aria-label',label);
  const {grid,rows: sliced}=tableSlice(fragment,rows);
  const roles=fragment.roles??{}, instance=`source-table-${++tableSerial}`;
  const rendered=new Set(sliced.flatMap(r=>r.cells.map(c=>c.id)));
  const noteIds=grid.cells.filter(c=>(roles.noteRows??[]).includes(c.startRow)&&rendered.has(c.id)).map(c=>`${instance}-${c.id}`);
  if(noteIds.length)table.setAttribute('aria-describedby',noteIds.join(' '));
  const cols=el('colgroup'); for (let i=0;i<grid.xs.length-1;i++) {const col=el('col'); col.style.width=`${100*(grid.xs[i+1]-grid.xs[i])/(grid.xs.at(-1)-grid.xs[0])}%`; cols.append(col);} table.append(cols);
  const body=el('tbody');
  for (const row of sliced) {
    const tr=el('tr');
    for (const cell of row.cells) {
      const isColumnHeader=(roles.columnHeaderRows??[]).includes(cell.startRow);
      const isNote=(roles.noteRows??[]).includes(cell.startRow);
      const isRowHeader=!isColumnHeader&&!isNote&&(roles.rowHeaderColumns??[]).includes(cell.startCol);
      const td=el(isColumnHeader||isRowHeader?'th':'td',cell.inherited?'shared-cell':''); td.colSpan=cell.colSpan; td.rowSpan=cell.rowSpan; td.id=`${instance}-${cell.id}`;
      if(isColumnHeader)td.scope=cell.colSpan>1?'colgroup':'col';
      else if(isRowHeader)td.scope=cell.rowSpan>1?'rowgroup':'row';
      const context=cellContext(fragment,cell), headerIds=context.headerIds.filter(id=>rendered.has(id)).map(id=>`${instance}-${id}`);
      if(headerIds.length)td.setAttribute('headers',headerIds.join(' '));
      const value=button(cell.text,'cell-value',()=>showRefs([{page:fragment.page,rects:[cell.bbox],precision:'cell'}], `${label} · 单元格`));
      value.setAttribute('aria-label', `${context.label}；定位PDF第${fragment.page}页`);
      if(noteIds.length&&!isNote)value.setAttribute('aria-describedby',noteIds.join(' '));
      value.title=cell.inherited?'从原表真实合并单元格继承；点击定位':'定位原文单元格';
      td.append(value);
      const visuals=renderCellVisuals(cell,fragment,label);
      if(visuals)td.append(visuals);
      tr.append(td);
    }
    body.append(tr);
  }
  table.append(body); scroll.append(table); wrapper.append(scroll);
  if (rows && sliced.some(r=>r.cells.some(c=>c.inherited))) wrapper.append(el('p','small-muted','浅灰单元格来自原表真实合并区域，未从邻行填值。'));
  return wrapper;
}

function renderCellVisuals(cell,fragment,label) {
  if(!Array.isArray(cell.visualRefs)||!cell.visualRefs.length)return null;
  const refs=normalizedCellVisualRefs(cell,fragment.page), wrapper=el('div','cell-visuals');
  let invalid=cell.visualRefs.filter(ref=>normalizedCellVisualRefs({visualRefs:[ref]},fragment.page).length===0).length;
  for(const ref of refs) {
    const page=bundle.pages[ref.page-1], [x0,y0,x1,y1]=ref.bbox;
    if(!page||x0<0||y0<0||x1>page.width||y1>page.height){invalid+=1;continue;}
    const crop=button('','cell-visual',()=>showRefs([{page:ref.page,rects:[ref.bbox],precision:'cell-visual'}],`${label} · ${ref.label}`));
    crop.setAttribute('aria-label',`${ref.label}；定位PDF第${ref.page}页`);
    crop.style.aspectRatio=`${x1-x0}/${y1-y0}`;
    const img=el('img');img.src=`/pages/p-${String(ref.page).padStart(2,'0')}.png`;img.alt=ref.label;img.loading='lazy';
    img.style.width=`${page.width/(x1-x0)*100}%`;img.style.left=`${-x0/(x1-x0)*100}%`;img.style.top=`${-y0/(y1-y0)*100}%`;
    crop.append(img);wrapper.append(crop);
  }
  if(invalid)wrapper.append(el('span','cell-visual-gap',`${invalid}个单元格视觉证据缺少有效页内坐标`));
  return wrapper;
}

function renderFigure(figure) {
  const wrapper=el('figure','source-figure');
  const page=bundle.pages[figure.page-1], [x0,y0,x1,y1]=figure.bbox;
  const crop=button('','figure-crop',()=>showRefs([{page:figure.page,rects:[figure.bbox],precision:figure.role==='context'?'context':'figure'}],figure.label));
  crop.setAttribute('aria-label',`${figure.label}，定位PDF第${figure.page}页`);
  crop.style.aspectRatio=`${x1-x0}/${y1-y0}`;
  const img=el('img'); img.src=`/pages/p-${String(figure.page).padStart(2,'0')}.png`; img.alt=figure.label; img.loading='lazy';
  img.style.width=`${page.width/(x1-x0)*100}%`; img.style.left=`${-x0/(x1-x0)*100}%`; img.style.top=`${-y0/(y1-y0)*100}%`;
  crop.append(img); wrapper.append(crop,el('figcaption','figure-caption',`${figure.label} · PDF ${figure.page}${figure.role==='context'?' · 阅读上下文':figure.role==='missing-attachment'?' · 仅图标，附件本体缺失':''}`));
  return wrapper;
}

function selectDerived(d) {
  const node=nodes.get(`derived:${d.id}`);
  if (node) {selectNode(node);focusTree(node.id);return;}
  selectedDerived=d.id; const item=currentItem();renderReview();showDerivedEvidence(d,item);
}

function showDerivedEvidence(d,item) {
  if (d.tableSelection) {
    const f=bundle.tables.flatMap(t=>t.fragments).find(f=>f.tableId===d.tableSelection.tableId);
    const grid=tableGrid(f), all=[...d.tableSelection.rows,...d.tableSelection.sharedRows];
    showRefs([{page:f.page,rects:all.map(r=>[f.bbox[0],grid.ys[r],f.bbox[2],grid.ys[r+1]]),precision:'row-and-shared-note'}], TABLE_SELECTION_EVIDENCE_LABEL);
  } else showRefs(d.precision==='parent-clause'&&item.table?[...sourceRowRects(item,bundle.tables),...(item.inheritedSourceRefs??[])]:d.sourceRefs?.length?d.sourceRefs:item.sourceRefs,'派生需求依据');
}

function showRefs(refs,label) {
  activeRefs=refs.filter(r=>r.page>=1&&r.page<=bundle.pages.length); refLabel=label;
  if (!activeRefs.length) {activeRefs=[{page:selectedNode?.page??1,rects:[],precision:'page'}];}
  showPage(activeRefs[0].page,true);
}
function showPage(page,focus=false) {
  currentPage=Math.max(1,Math.min(bundle.pages.length,Number(page)||1));
  $('#page-input').value=currentPage; $('#previous-page').disabled=currentPage===1; $('#next-page').disabled=currentPage===bundle.pages.length;
  $('#evidence-caption').textContent=pageEvidenceCaption(bundle.pages[currentPage-1],currentPage,bundle.pages.length);
  $('#evidence-source-note').replaceChildren();
  const pages=[...new Set(activeRefs.map(r=>r.page))];
  $('#evidence-source-note').append(el('span','',refLabel || '原文浏览'));
  if(pages.length>1) for (const p of pages) $('#evidence-source-note').append(button(`PDF ${p}`,`evidence-page-chip${p===currentPage?' active':''}`,()=>showPage(p,true)));
  else $('#evidence-source-note').append(el('span','small-muted','定位区域 ≠ 已核准'));
  const img=$('#page-image'), url=`/pages/p-${String(currentPage).padStart(2,'0')}.png`;
  img.alt=`${bundle.source.name}，PDF第${currentPage}页，未经改写的页面渲染`;
  $('#page-error').hidden=true;
  if(img.getAttribute('src')!==url) {imageReady=false; img.src=url;}
  drawOverlay();
  if(focus&&imageReady) focusEvidence();
}
function drawOverlay() {
  const page=bundle.pages[currentPage-1]; const svg=$('#page-overlay');
  svg.setAttribute('viewBox',`0 0 ${page.width} ${page.height}`); svg.replaceChildren();
  const refs=activeRefs.filter(r=>r.page===currentPage);
  for (const ref of refs) for(const box of ref.rects) {
    const rect=document.createElementNS('http://www.w3.org/2000/svg','rect');
    for(const [k,v] of Object.entries({x:box[0],y:box[1],width:box[2]-box[0],height:box[3]-box[1]})) rect.setAttribute(k,String(v));
    rect.setAttribute('class',ref.precision==='context'?'context-highlight':'evidence-highlight'); svg.append(rect);
  }
}
function focusEvidence() {
  const boxes=activeRefs.filter(r=>r.page===currentPage).flatMap(r=>r.rects);
  const rect=bounds(boxes); if(!rect) return;
  const paper=$('#page-paper'), scroller=$('#page-scroller');
  const scale=paper.clientWidth/bundle.pages[currentPage-1].width;
  scroller.scrollTop=Math.max(0,rect[1]*scale - 70);
  scroller.scrollLeft=Math.max(0,rect[0]*scale - 25);
}
function applyZoom() {const z=$('#zoom').value; $('#page-paper').style.width=z==='fit'?'100%':`${Number(z)*100}%`; requestAnimationFrame(focusEvidence);}

function pageClick(event) {
  if(!bundle||!imageReady) return;
  const rect=$('#page-paper').getBoundingClientRect(), page=bundle.pages[currentPage-1];
  const x=(event.clientX-rect.left)*page.width/rect.width, y=(event.clientY-rect.top)*page.height/rect.height;
  const candidates=[];
  for(const item of bundle.items) {
    const refs=item.table?sourceRowRects(item,bundle.tables):item.sourceRefs;
    for(const ref of refs.filter(r=>r.page===currentPage)) for(const b of ref.rects) {
      if(x>=b[0]&&x<=b[2]&&y>=b[1]&&y<=b[3]) candidates.push({item,area:(b[2]-b[0])*(b[3]-b[1])});
    }
  }
  candidates.sort((a,b)=>a.area-b.area);
  if(candidates.length) {selectNode(nodeForItem(candidates[0].item.id)); toast('已选择这段原文对应的条目');}
}

function draftFor(target) {
  if(!drafts.has(target.id)) {const effective=effectiveTarget(target,review); drafts.set(target.id,{class:effective.class,text:effective.text,baseClass:effective.class,baseText:effective.text,note:'',open:false});}
  return drafts.get(target.id);
}
function renderReview(focus=null) {
  const panel=$('#review-panel'); panel.replaceChildren();
  const target=currentTarget();
  if(!target||!detailValid) {panel.append(el('p','review-hint',detailValid?'在左侧选择具体原文条目后，可标记核对、记录问题或修改。':'内容尚未完整呈现，本条反馈暂不可用。')); return;}
  const draft=draftFor(target), effective=effectiveTarget(target,review), dirty=draftIsDirty(draft), conflict=conflicts.has(target.id);
  const bar=el('div','review-actions');
  const identity=el('div','review-target'); identity.append(el('strong','',target.parentId?'当前：派生需求':'当前：原文条目'),el('span',target.origin==='agent-proxy'||target.proxyReview?'proxy-origin':'small-muted',reviewLabel(target,currentItem()))); bar.append(identity);
  if(target.parentId) bar.append(button('返回原文条目','text-button',()=>{const parent=nodeForItem(sourceParentId(target));if(parent){selectNode(parent);focusTree(parent.id);}else{selectedDerived=null;renderDetail();renderReview();}}));
  const edit=button(draft.open?'收起修改':'记录问题 / 修改','quiet-button',()=>{draft.open=!draft.open; renderReview(draft.open?'editor':'edit');}); edit.id='edit-feedback';
  const confirm=button(review.reviewerKind==='qa'?'验证“标记已核对”':'标记本条已核对','primary-button',()=>saveAction('confirm'));
  confirm.id='confirm-feedback'; confirm.disabled=saving||dirty||conflict||bundleStale; edit.disabled=saving; bar.append(edit,confirm); panel.append(bar);
  if(conflict) {
    const notice=el('div','feedback-conflict'); notice.append(el('p','','其它窗口已更新反馈。正文与分类已载入最新状态，本地草稿仍保留；请重新核对后继续。'));
    notice.append(el('p','',`当前有效分类：${CLASS_LABELS[effective.class]}`));
    if(target.parentId) notice.append(el('p','',`当前有效派生内容：${effective.text}`));
    notice.append(button('已重新核对最新内容','quiet-button',()=>{conflicts.delete(target.id);renderReview('edit');})); panel.append(notice);
  }
  {
    const unsaved=el('div','unsaved-note'); unsaved.hidden=!dirty; unsaved.setAttribute('role','status'); unsaved.append(el('span','','本条有未保存编辑，请先保存或放弃，再确认/撤销。'),button('放弃本条草稿','text-button',()=>{drafts.delete(target.id);pendingMutation=null;renderReview('edit');}));panel.append(unsaved);
  }
  if(effective.notes.length) {
    const latest=effective.notes.at(-1); const note=el('p','saved-note',`已保存：${latest.text}`); panel.append(note);
  }
  if(effective.lastEventId) {const undo=button('撤销本条最近反馈','text-button undo-button',()=>saveAction('undo')); undo.disabled=saving||dirty||conflict||bundleStale; panel.append(undo);}
  const restoreFocus=()=>{if(focus)requestAnimationFrame(()=>panel.querySelector(focus==='editor'?(target.parentId?'.derived-editor':'.feedback-form select'):focus==='confirm'?'#confirm-feedback':'#edit-feedback')?.focus({preventScroll:true}));};
  if(!draft.open) {restoreFocus();return;}
  const form=el('form','feedback-form'); form.addEventListener('submit',e=>{e.preventDefault();saveAction('correct');});
  const row=el('div','feedback-field-row'); const classLabel=el('label','field-label','修改分类');
  const select=el('select'); select.setAttribute('aria-label','修改当前条目分类');
  for(const [code,label] of Object.entries(CLASS_LABELS)) {const option=el('option','',label); option.value=code; option.selected=code===draft.class; select.append(option);}
  const updateGuards=()=>{pendingMutation=null;const changed=draftIsDirty(draft);confirm.disabled=saving||changed||conflicts.has(target.id)||bundleStale;const undo=panel.querySelector('.undo-button');if(undo)undo.disabled=confirm.disabled;panel.querySelector('.unsaved-note').hidden=!changed;};
  select.disabled=saving; select.addEventListener('change',()=>{draft.class=select.value;updateGuards();}); classLabel.append(select);row.append(classLabel);
  row.append(el('p','small-muted','修改另存为人工修订；客户原文与当前分析候选都会保留。')); form.append(row);
  if(target.parentId) {
    const label=el('label','field-label','修改派生内容'); const area=el('textarea','derived-editor'); area.value=draft.text; area.rows=4; area.maxLength=12000; area.disabled=saving;
    area.addEventListener('input',()=>{draft.text=area.value;updateGuards();}); label.append(area); form.append(label);
  }
  const label=el('label','field-label','问题 / 修改原因'); const note=el('textarea','note-editor'); note.value=draft.note; note.rows=2; note.maxLength=8000; note.placeholder='说明分类、拆分、表格边界或证据哪里需要调整…'; note.disabled=saving;
  note.addEventListener('input',()=>{draft.note=note.value;updateGuards();}); label.append(note); form.append(label);
  const actions=el('div','feedback-save-actions');
  const problem=button('仅记录问题','quiet-button',()=>saveAction('comment')); problem.disabled=saving||conflict||bundleStale;
  const save=button('保存修改','primary-button',()=>saveAction('correct')); save.disabled=saving||conflict||bundleStale;
  actions.append(el('span','small-muted','切换条目不会丢失本次页面中的未保存草稿。'),problem,save); form.append(actions);
  panel.append(form); restoreFocus();
}

async function saveAction(action) {
  if(saving||!detailValid||bundleStale) return;
  const target=currentTarget(); if(!target) return;
  const draft=draftFor(target);
  if(conflicts.has(target.id)) {toast('请先核对已载入的最新内容，再继续操作。');return;}
  if((action==='confirm'||action==='undo')&&draftIsDirty(draft)) {toast('本条有未保存编辑；请先保存或放弃草稿。');return;}
  if((action==='comment'||action==='correct')&&!draft.note.trim()) {toast('请填写问题或修改原因；草稿仍保留。');return;}
  const effective=effectiveTarget(target,review);
  if(action==='correct'&&draft.class===effective.class&&(!target.parentId||draft.text.trim()===effective.text)) {toast('尚未改动分类或派生内容。只提问题请用“仅记录问题”。');return;}
  const signature=JSON.stringify({id:target.id,action,draft});
  if(!pendingMutation||pendingMutation.signature!==signature) pendingMutation={signature,payload:targetPayload(target,review,action,{...draft,note:action==='confirm'||action==='undo'?'':draft.note},crypto.randomUUID())};
  saving=true; renderReview();
  try {
    const result=await api('/api/review',{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':review.writeToken},body:JSON.stringify(pendingMutation.payload)});
    review=result.state; pendingMutation=null;
    if(action==='comment'&&(draft.class!==draft.baseClass||draft.text!==draft.baseText)) draft.note='';
    else drafts.delete(target.id);
    conflicts.delete(target.id);
    toast(action==='undo'?'已撤销本条最近反馈；历史记录仍保留。':action==='confirm'?(review.reviewerKind==='qa'?'已记录本条交互验证；不计入人工验收。':'已记录本条核对；其它条目和派生需求未自动确认。'):'反馈已保存到本机，并绑定当前来源与分析版本。');
    renderTree(); renderDetail();
  } catch(error) {
    toast(error.message);
    if(/其它窗口|版本已变化/.test(error.message)) {
      try {
        const latestState=await api('/api/review-state');pendingMutation=null;
        if(latestState.sourceRevision!==bundle.revision) {
          bundleStale=true;
          $('#global-error').hidden=false;
          $('#global-error').textContent='分析结果版本已经变化。当前页面保留旧版及未保存草稿，不能继续提交；请复制需要保留的草稿，再刷新载入新结果。';
          return;
        }
        review=latestState;
        const latest=effectiveTarget(target,review);
        if(draft.class===draft.baseClass)draft.class=latest.class;
        if(draft.text===draft.baseText)draft.text=latest.text;
        draft.baseClass=latest.class;draft.baseText=latest.text;
        conflicts.add(target.id);renderTree();renderDetail();
      } catch {conflicts.add(target.id);}
    }
  } finally {saving=false;renderReview(currentTarget()?.id===target.id?(action==='confirm'?'confirm':'edit'):null);}
}

function compactView(evidence) {
  compactEvidence=evidence; $('.workbench').classList.toggle('show-evidence',evidence);
  $('#show-evidence').setAttribute('aria-pressed',String(evidence)); $('#show-content').setAttribute('aria-pressed',String(!evidence));
  if(evidence) requestAnimationFrame(focusEvidence);
}

async function start() {
  try {
    [bundle,review]=await Promise.all([api('/api/bundle'),api('/api/review-state')]);
    if(bundle.revision!==review.sourceRevision) throw new Error('来源与反馈版本不一致。请停止核对并重新载入。');
    const pageImage=el('img');pageImage.id='page-image';pageImage.alt='原PDF页面';pageImage.draggable=false;$('#page-paper').prepend(pageImage);
    itemMap=new Map(bundle.items.map(i=>[i.id,i])); sectionMap=new Map(bundle.outline.map(s=>[s.id,s])); tableMap=new Map(bundle.tables.map(t=>[t.id,t]));
    buildNodes();
    $('#document-title').textContent=bundle.source.name.replace(/\.pdf$/i,'');
    const proxyCount=bundle.proxyReview?.summary?.reviewedItems;
    $('#document-meta').textContent=`${bundle.source.pageCount} 页原文 · 原文 ${bundle.items.length} · 派生 ${bundle.coverage.derivedDrafts}${proxyCount===undefined?'':` · 代理复核 ${proxyCount}`} · 仅本机可见`;
    $('#run-status').textContent=review.reviewerKind==='qa'?'交互验证沙盒 · 不记作人工验收':bundle.proxyReview?'代理复核候选 · 等待你最终评判':'Agent 初稿 · 等待人工核对';
    $('#run-status').classList.toggle('qa',review.reviewerKind==='qa');
    $('#run-status').classList.toggle('proxy',review.reviewerKind!=='qa'&&Boolean(bundle.proxyReview));
    const explanation=$('#run-explanation');
    explanation.append(el('p','',bundle.method),el('p','',bundle.coverage.warning),el('p','',`本次捕获${bundle.coverage.nativeLines}行，未归属${bundle.coverage.unassignedLines}行。附件图标与附件本体分开记录。`));
    if(bundle.proxyReview) explanation.append(el('p','proxy-explanation',`“代理复核”表示 Agent 已对 ${proxyCount} 条原文给出候选处置，不等于人工验收。首版与代理候选均保留，可逐条查看差异。`));
    explanation.append(el('p','',review.reviewerKind==='qa'?'当前为交互验证沙盒；所有保存、确认和撤销只验证界面行为，不计入人工验收。':'只有点击“标记本条已核对”才记录当前条目的人工核对；不会确认整个文档，也不会自动确认子需求。'));
    $('#page-input').max=String(bundle.pages.length);$('#page-total').textContent=`/ ${bundle.pages.length}`;
    for(const spot of bundle.spotlights??[]) $('#spotlights').append(button(spot.label,'spotlight-link',()=>goSection(spot.sectionId)));
    $('#search').addEventListener('input',()=>renderTree(true)); $('#filter').addEventListener('change',()=>renderTree(true));
    $('#collapse-all').addEventListener('click',()=>{expanded.clear();renderTree();});
    $('#locate-selection').addEventListener('click',()=>{$('#search').value='';$('#filter').value='all';expandPath(selectedNode);renderTree();const selected=$('#tree .selected');selected?.scrollIntoView({block:'center'});selected?.focus();});
    $('#previous-page').addEventListener('click',()=>showPage(currentPage-1)); $('#next-page').addEventListener('click',()=>showPage(currentPage+1));
    $('#page-input').addEventListener('change',()=>showPage($('#page-input').value)); $('#zoom').addEventListener('change',applyZoom);
    $('#page-image').addEventListener('load',()=>{imageReady=true;$('#page-paper').hidden=false;drawOverlay();focusEvidence();});
    $('#page-image').addEventListener('error',()=>{imageReady=false;$('#page-paper').hidden=true;$('#page-error').hidden=false;$('#page-error').textContent='此页原始渲染缺失，未使用替代内容。可打开原PDF检查，或恢复本地页图。';});
    $('#page-paper').addEventListener('click',pageClick);
    $('#show-content').addEventListener('click',()=>compactView(false)); $('#show-evidence').addEventListener('click',()=>compactView(true));
    window.addEventListener('beforeunload',event=>{if([...drafts.values()].some(draftIsDirty)||saving){event.preventDefault();event.returnValue='';}});
    expanded=new Set(); selectNode(sectionNode(bundle.spotlights?.[0]?.sectionId) ?? nodes.values().next().value);
    $('#app').setAttribute('aria-busy','false');
  } catch(error) {
    $('#global-error').hidden=false; $('#global-error').textContent=`无法打开核对结果：${error.message}。没有写入或覆盖任何反馈。`;
    $('#run-status').textContent='结果未就绪'; $('#app').setAttribute('aria-busy','false');
  }
}
start();
