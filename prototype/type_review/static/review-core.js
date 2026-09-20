export const RECOMMENDATION_LABELS = Object.freeze({
  ACCEPT: '建议接受',
  ACCEPT_WITH_GUARDRAILS: '建议有条件接受',
  ACCEPT_WITH_POLICY: '建议在明确策略后接受',
  ACCEPT_PDF_DOCX_XLSX_CORE: '建议核心支持 PDF / DOCX / XLSX',
  REJECT_AS_REQUIREMENT_SOURCE: '建议仅作知识，不作为需求源',
  REJECT_AS_CUSTOMER_SOURCE: '建议保留为派生工件，不作为客户源',
  REJECT_AS_ORIGINAL_REQUIREMENT_SOURCE: '建议作为验证规范，不作为原始需求源',
});

export const STATUS_LABELS = Object.freeze({
  pending: '等待你的决定',
  accepted: '你已接受',
  'adjustment-needed': '你要求调整',
  deferred: '你已暂缓',
  commented: '已记录问题',
});

export function normalized(value) {
  return String(value ?? '').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g,' ').trim();
}

export function targetState(target, state) {
  const saved=state.targets?.[target.id];
  return {status:saved?.status??'pending', notes:saved?.notes??[], lastEventId:saved?.lastEventId, actor:saved?.actor};
}

export function isOpenProblem(problem) {
  return String(problem?.[3]??'').startsWith('OPEN');
}

export function openProblemCount(target) {
  return (target.problems??[]).filter(isOpenProblem).length;
}

export function targetSearchText(target) {
  return normalized([
    target.id,target.title,target.format,target.role,target.source,target.route,
    target.outcome,target.judgment,target.decision,target.detail,target.recommendation,
    ...(target.problems??[]).flat(),
  ].join(' '));
}

export function matchesTarget(target, query, filter, state) {
  if(query && !targetSearchText(target).includes(normalized(query))) return false;
  const effective=targetState(target,state);
  if(filter==='pending'&&effective.status!=='pending') return false;
  if(filter==='open'&&openProblemCount(target)===0) return false;
  if(filter==='requirements'&&!String(target.route??'').startsWith('requirement_source')) return false;
  if(filter==='nonrequirements'&&(String(target.route??'').startsWith('requirement_source'))) return false;
  if(filter==='decisions'&&!target.id.startsWith('D')) return false;
  return true;
}

export function decisionPayload(target, state, action, note, mutationId) {
  return {
    mutationId,
    expectedRevision:state.revision,
    sourceRevision:state.sourceRevision,
    targetId:target.id,
    action,
    note:String(note??'').trim(),
    ...(action==='undo'?{undoOf:state.targets?.[target.id]?.lastEventId}:{}),
  };
}
