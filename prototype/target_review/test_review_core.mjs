import test from 'node:test';
import assert from 'node:assert/strict';
import {CLASS_LABELS, TABLE_SELECTION_EVIDENCE_LABEL, effectiveTarget, targetNeedsAttention, needsAttention, derivedTreeLabel, sourceParentId, pageEvidenceCaption, resolvedTreeOrder, normalizedCellVisualRefs, draftIsDirty, matchesItem, proxyState, targetReviewLabel, tableSlice, cellContext, sourceRowRects, sourceBlocks, targetPayload} from './static/review-core.js';

const fragment = {tableId:'t1', page:2, bbox:[10,10,70,70], cellsFlat:[
  {id:'h1',bbox:[10,10,30,30],text:'项目'}, {id:'h2',bbox:[30,10,70,30],text:'值'},
  {id:'a',bbox:[10,30,30,70],text:'真实跨行'}, {id:'v1',bbox:[30,30,70,50],text:'80'},
  {id:'v2',bbox:[30,50,70,70],text:''},
]};
const item={id:'s1',sectionId:'9.16.2',label:'阻尼力公差',class:'Q',text:'±50N',reason:'共享表注',issues:['x'],derived:[{id:'s1/d1',parentId:'s1',class:'Q',text:'起点电流IStart'}]};
const state={revision:0,sourceRevision:'r1',targets:{}};

test('a real vertically merged cell is inherited by its continuation row',()=>{
  const slice=tableSlice(fragment,[2]);
  assert.equal(slice.rows[0].cells[0].text,'真实跨行');
  assert.equal(slice.rows[0].cells[0].inherited,true);
  assert.equal(slice.rows[0].cells[0].rowSpan,1);
});
test('empty source cells stay empty, never borrow a value',()=>{
  assert.equal(tableSlice(fragment,[2]).rows[0].cells[1].text,'');
});
test('full table renders one rowspan owner only',()=>{
  const slice=tableSlice(fragment);
  assert.equal(slice.rows[1].cells[0].rowSpan,2);
  assert.deepEqual(slice.rows[2].cells.map(c=>c.id),['v2']);
});
test('header plus non-contiguous selection does not pull in a neighbour row',()=>{
  const slice=tableSlice(fragment,[0,2]);
  assert.deepEqual(slice.rows.map(r=>r.row),[0,2]);
  assert.equal(slice.rows.flatMap(r=>r.cells).some(c=>c.text==='80'),false);
});
test('row evidence is clipped to physical row, not the entire merged group',()=>{
  const rowItem={...item,table:{logicalId:'logical',selected:[{tableId:'t1',rows:[2]}]}};
  const refs=sourceRowRects(rowItem,[{id:'logical',fragments:[fragment]}]);
  assert.deepEqual(refs[0].rects,[[10,50,70,70]]);
});
test('human edits project separately without mutating AI output',()=>{
  const edited=effectiveTarget(item,{targets:{s1:{status:'changed',patch:{class:'C'},notes:[]}}});
  assert.equal(edited.class,'C');assert.equal(item.class,'Q');
});
test('parent confirmation never auto-confirms a derived child',()=>{
  const s={targets:{s1:{status:'reviewed',patch:{},notes:[]}}};
  assert.equal(effectiveTarget(item.derived[0],s).reviewStatus,'unreviewed');
});
test('search covers original text, section numbers, and derived text',()=>{
  for(const query of ['50n','9.16.2','istart']) assert.equal(matchesItem(item,query,'all',state),true);
  assert.equal(matchesItem(item,'absent','all',state),false);
});
test('search uses human-corrected derived text',()=>{
  const s={targets:{'s1/d1':{patch:{text:'人工修正内容'},status:'changed'}}};
  assert.equal(matchesItem(item,'人工修正','all',s),true);
});
test('derived requirements receive ordered tree labels and use the effective human text',()=>{
  assert.equal(derivedTreeLabel(item,item.derived[0],state),'要求 1 · 起点电流IStart');
  const s={targets:{'s1/d1':{patch:{text:'人工修正后的派生要求'},status:'changed'}}};
  assert.equal(derivedTreeLabel(item,item.derived[0],s),'要求 1 · 人工修正后的派生要求');
});
test('a derived tree row exposes its own attention state',()=>{
  assert.equal(targetNeedsAttention(item.derived[0],state),false);
  assert.equal(targetNeedsAttention(item.derived[0],{targets:{'s1/d1':{status:'needs-review'}}}),true);
});
test('content roles include revision history and examples without making them requirements',()=>{
  assert.equal(CLASS_LABELS.REVISION_HISTORY,'修订记录');
  assert.equal(CLASS_LABELS.EXAMPLE,'示例');
  for(const code of ['REVISION_HISTORY','EXAMPLE'])assert.equal(matchesItem({...item,class:code},'','requirements',state),false);
});
test('printed page labels come only from the bundle page metadata',()=>{
  assert.equal(pageEvidenceCaption({printedLabel:'20'},28,60),'PDF 28 / 60 · 文内页码 20 · 原文像素');
  assert.equal(pageEvidenceCaption({},28,60),'PDF 28 / 60 · 原文像素');
});
test('cell visual references keep valid page crops and reject invalid geometry',()=>{
  const refs=normalizedCellVisualRefs({visualRefs:[{page:3,bbox:[10,20,30,40],label:'图A'},{bbox:[1,2,1,5]},{rects:[[4,5,8,9]]}]},2);
  assert.deepEqual(refs,[{page:3,bbox:[10,20,30,40],label:'图A'},{page:2,bbox:[4,5,8,9],label:'单元格视觉 3.1'}]);
});
test('table-selection evidence language is generic across document types',()=>{
  assert.equal(TABLE_SELECTION_EVIDENCE_LABEL,'派生需求 · 当前表格行与共享内容');
  assert.doesNotMatch(TABLE_SELECTION_EVIDENCE_LABEL,/电流|BOM|F06/);
});
test('a coordinate-less container inherits its earliest real descendant order',()=>{
  assert.equal(resolvedTreeOrder(null,[50320,30180,40200],50000),30180);
  assert.equal(resolvedTreeOrder(50120,[30180],50000),50120);
  assert.equal(resolvedTreeOrder(null,[],50000),50000);
});
test('returning from a derived requirement resolves the source parent identity',()=>{
  assert.equal(sourceParentId(item.derived[0]),item.id);
  assert.equal(sourceParentId(item),item.id);
});
test('requirement filtering does not turn missing evidence into requirements',()=>{
  assert.equal(matchesItem({...item,class:'UNRESOLVED'},'','requirements',state),false);
  assert.equal(matchesItem({...item,class:'UNRESOLVED',issues:[]},'','issues',state),true);
});
test('reviewed filter reflects only this source item',()=>{
  assert.equal(matchesItem(item,'','reviewed',state),false);
  assert.equal(matchesItem(item,'','reviewed',{targets:{s1:{status:'reviewed'}}}),true);
});
test('proxy filter selects only items touched by the proxy loop',()=>{
  const proxyItem={...item,proxyReview:{action:'SPLIT',reason:'按独立验证目标拆分',readiness:'candidate-for-owner-review'}};
  assert.equal(matchesItem(proxyItem,'','proxy',state),true);
  assert.equal(matchesItem(item,'','proxy',state),false);
  assert.equal(matchesItem(proxyItem,'独立验证目标','proxy',state),true);
});
test('proxy state explains action and unresolved readiness without claiming acceptance',()=>{
  const proxyItem={...item,proxyReview:{action:'KEEP_WITH_GAP',readiness:'clarification-needed'}};
  const display=proxyState(proxyItem);
  assert.equal(display.actionLabel,'保留并标出待澄清信息');
  assert.equal(display.needsClarification,true);
  assert.match(display.statusLabel,/等待最终评判/);
});
test('human state takes precedence over proxy origin labels',()=>{
  const proxyChild={...item.derived[0],origin:'agent-proxy'};
  assert.equal(targetReviewLabel(proxyChild,state,item),'代理派生 · 待最终评判');
  assert.equal(targetReviewLabel(proxyChild,{targets:{'s1/d1':{status:'reviewed'}}},item),'人工已核对');
  assert.equal(targetReviewLabel(item,{targets:{s1:{status:'changed'}}}),'人工已修改 · 待复核');
});
test('correction payload cannot overwrite original source text',()=>{
  const p=targetPayload(item,state,'correct',{class:'C',text:'DO NOT WRITE',note:'原因'},'synthetic-request');
  assert.deepEqual(p.patch,{class:'C'});assert.equal(p.sourceRevision,'r1');
});
test('derived correction keeps the target and revision identities',()=>{
  const p=targetPayload(item.derived[0],state,'correct',{class:'F',text:'新内容',note:'原因'},'synthetic-request');
  assert.equal(p.targetId,'s1/d1');assert.deepEqual(p.patch,{class:'F',text:'新内容'});
});
test('undo references only current target latest event',()=>{
  const p=targetPayload(item,{...state,targets:{s1:{lastEventId:'event-1'}}},'undo',{note:''},'synthetic-request');
  assert.equal(p.undoOf,'event-1');
});
test('native table text is rendered as a table, not flattened alongside it',()=>{
  const it={...item,lineIds:['l1','l2','l3'],matrixTables:['m']};
  const b={tables:[{id:'m',label:'表1',fragments:[fragment]}],lines:{l1:{id:'l1',text:'上文',page:2,bbox:[10,0,30,5],block:1},l2:{id:'l2',text:'80',page:2,bbox:[31,33,50,40],block:2},l3:{id:'l3',text:'下文',page:2,bbox:[10,80,30,90],block:3}}};
  const blocks=sourceBlocks(it,b);assert.deepEqual(blocks.map(b=>b.type),['text','table','text']);
  assert.equal(blocks.flatMap(b=>b.lines??[]).some(l=>l.text==='80'),false);
});

test('a derived problem keeps the source item in the attention filter',()=>{
  const cleanItem={...item,issues:[]};
  const s={targets:{'s1/d1':{status:'needs-review'}}};
  assert.equal(needsAttention(cleanItem,s),true);
  assert.equal(matchesItem(cleanItem,'','issues',s),true);
});
test('manual edits and effective missing-evidence classification stay visible',()=>{
  const cleanItem={...item,issues:[]};
  for(const targetState of [{status:'changed'},{status:'reviewed',patch:{class:'UNRESOLVED'}}]) assert.equal(needsAttention(cleanItem,{targets:{s1:targetState}}),true);
});
test('unsaved classification and content are dirty even without a note',()=>{
  assert.equal(draftIsDirty({class:'C',baseClass:'Q',text:'x',baseText:'x',note:''}),true);
  assert.equal(draftIsDirty({class:'Q',baseClass:'Q',text:'changed',baseText:'x',note:''}),true);
  assert.equal(draftIsDirty({class:'Q',baseClass:'Q',text:'x',baseText:'x',note:''}),false);
});
test('a stale unchanged classification is not submitted as an intended edit',()=>{
  const s={...state,targets:{'s1/d1':{status:'changed',patch:{class:'C'}}}};
  const payload=targetPayload(item.derived[0],s,'correct',{baseClass:'Q',class:'Q',baseText:'起点电流IStart',text:'new content',note:'reason'},'synthetic-request');
  assert.deepEqual(payload.patch,{text:'new content'});
});
test('accessible cell context associates actual row and column headers',()=>{
  const f={...fragment,roles:{columnHeaderRows:[0],rowHeaderColumns:[0]}};
  const cell=tableSlice(f,[2]).rows[0].cells[1];
  const context=cellContext(f,cell);
  assert.deepEqual(context.headerIds.sort(),['a','h2']);
  assert.match(context.label,/真实跨行/);assert.match(context.label,/原文空白单元格/);
});
