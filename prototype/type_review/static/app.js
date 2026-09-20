import {RECOMMENDATION_LABELS, STATUS_LABELS, targetState, openProblemCount, matchesTarget, decisionPayload} from './review-core.js';

const $=selector=>document.querySelector(selector);
const el=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;};
const button=(text,className,action)=>{const node=el('button',className,text);node.type='button';node.addEventListener('click',action);return node;};
let campaign,state,targets=[],selectedId=null,visibleTargets=[],saving=false,toastTimer=null,drafts=new Map();

async function api(path,options={}) {
  const response=await fetch(path,{cache:'no-store',...options});
  const data=await response.json();
  if(!response.ok)throw new Error(data.error??`请求失败（${response.status}）`);
  return data;
}
function toast(message){clearTimeout(toastTimer);$('#toast').textContent=message;$('#toast').hidden=false;toastTimer=setTimeout(()=>{$('#toast').hidden=true;},5500);}
function current(){return targets.find(target=>target.id===selectedId)??null;}
function isDecision(target){return target.id.startsWith('D');}
function statusText(target){
  const effective=targetState(target,state);
  if(state.reviewerKind==='qa'&&effective.status!=='pending'){
    const qaLabels={accepted:'模拟接受','adjustment-needed':'模拟要求调整',deferred:'模拟暂缓',commented:'模拟记录问题'};
    return `交互验证 · ${qaLabels[effective.status]}`;
  }
  return STATUS_LABELS[effective.status];
}
function recommendation(target){return RECOMMENDATION_LABELS[target.recommendation]??target.recommendation;}
function badge(text,kind=''){return el('span',`badge${kind?` ${kind}`:''}`,text);}

function renderHeader(){
  const summary=campaign.summary;
  $('#campaign-meta').textContent=`${summary.formatCount}种格式 · 7类入口 · ${summary.registeredDocuments}份登记文档 · ${summary.registeredCases}个标准案例 · ${summary.openProblems}个开放问题`;
  $('#run-status').textContent=state.reviewerKind==='qa'?'交互验证沙盒 · 不记作负责人决定':'17类代理候选 · 等待最终决策';
  $('#run-status').classList.toggle('qa',state.reviewerKind==='qa');
}

function rowState(target){
  const status=targetState(target,state).status;
  if(drafts.has(target.id))return {text:'草稿',className:'attention'};
  if(status==='accepted')return {text:'已收',className:'accepted'};
  if(status==='adjustment-needed')return {text:'调整',className:'attention'};
  if(status==='deferred')return {text:'暂缓',className:'attention'};
  if(status==='commented')return {text:'有问',className:'attention'};
  const count=openProblemCount(target);return {text:count?`${count}问`:'待定',className:count?'attention':''};
}
function renderDocket(focusId=null){
  const query=$('#search').value,filter=$('#filter').value;
  visibleTargets=targets.filter(target=>matchesTarget(target,query,filter,state));
  if(!visibleTargets.some(target=>target.id===selectedId)&&visibleTargets.length)selectedId=visibleTargets[0].id;
  const docket=$('#docket');docket.replaceChildren();
  const appendGroup=(label,group)=>{
    if(!group.length)return;
    docket.append(el('div','docket-group',label));
    for(const target of group){
      const row=button('','docket-row',()=>selectTarget(target.id));row.dataset.target=target.id;row.setAttribute('role','option');row.setAttribute('aria-selected',String(target.id===selectedId));row.tabIndex=target.id===selectedId?0:-1;
      if(target.id===selectedId)row.classList.add('selected');
      const copy=el('span','docket-copy');copy.append(el('span','docket-title',target.title),el('span','docket-meta',isDecision(target)?recommendation(target):`${target.format} · ${target.role}`));
      const marker=rowState(target);const stateNode=el('span',`docket-state ${marker.className}`,marker.text);stateNode.title=statusText(target);
      row.append(el('span','docket-id',target.id),copy,stateNode);row.addEventListener('keydown',docketKey);docket.append(row);
    }
  };
  appendGroup('跨类型总决策',visibleTargets.filter(isDecision));
  appendGroup('文档类型',visibleTargets.filter(target=>!isDecision(target)));
  if(!visibleTargets.length)docket.append(el('p','empty-docket','没有匹配卷宗。请清除搜索或切换范围。'));
  $('#match-count').textContent=`${visibleTargets.length} / ${targets.length}`;
  const decided=targets.filter(target=>targetState(target,state).status!=='pending').length;
  const accepted=targets.filter(target=>targetState(target,state).status==='accepted').length;
  $('#docket-progress').textContent=`${decided} / ${targets.length}`;
  $('#decision-count').textContent=state.reviewerKind==='qa'?`交互验证 ${decided}项 · 已接受模拟 ${accepted}项`:`人工已决定 ${decided}项 · 接受 ${accepted}项`;
  if(focusId)requestAnimationFrame(()=>docket.querySelector(`[data-target="${CSS.escape(focusId)}"]`)?.focus({preventScroll:true}));
}
function docketKey(event){
  const target=targets.find(item=>item.id===event.currentTarget.dataset.target);const index=visibleTargets.indexOf(target);let next=null;
  if(event.key==='ArrowDown')next=visibleTargets[Math.min(index+1,visibleTargets.length-1)];
  else if(event.key==='ArrowUp')next=visibleTargets[Math.max(index-1,0)];
  else if(event.key==='Home')next=visibleTargets[0];
  else if(event.key==='End')next=visibleTargets.at(-1);
  else if(event.key==='Enter'||event.key===' ')next=target;
  else return;
  event.preventDefault();if(next)selectTarget(next.id,true);
}
function selectTarget(id,focus=false){
  if(!targets.some(target=>target.id===id))return;
  selectedId=id;history.replaceState(null,'',`#${id}`);renderDocket(focus?id:null);renderDetail();renderPreview();renderDecisionPanel();$('#detail').scrollTop=0;
}

function renderDetail(){
  const target=current(),detail=$('#detail');detail.replaceChildren();if(!target)return;
  const effective=targetState(target,state);
  const header=el('header','detail-header');header.append(el('span','detail-id',isDecision(target)?`总决策 ${target.id}`:`类型卷宗 ${target.id}`),el('h2','detail-title',target.title));
  const meta=el('div','detail-meta');
  if(isDecision(target))meta.append(badge('跨类型规则'),badge(recommendation(target),'proxy'));
  else meta.append(badge(target.format),badge(target.role),badge(target.route,'proxy'),badge(`${openProblemCount(target)}个开放问题`,openProblemCount(target)?'warn':''));
  meta.append(badge(statusText(target),effective.status==='accepted'?'human':effective.status==='pending'?'proxy':'warn'));header.append(meta);detail.append(header);
  if(isDecision(target)){
    const proxy=el('section','section proxy-block');const head=el('div','proxy-head');head.append(el('h3','','代理建议'),el('span','recommendation',recommendation(target)));proxy.append(head,el('p','',target.detail));detail.append(proxy);
    const affected=target.affectedTypeIds.map(id=>campaign.types.find(item=>item.id===id)).filter(Boolean);
    const scope=el('section','section');scope.append(el('h3','','影响范围'),el('p','',`该决定影响 ${affected.length} 类卷宗：${affected.map(item=>item.id).join('、')}。`));detail.append(scope);
    const question=el('section','section decision-question');question.append(el('h3','','你需要决定'),el('p','',`是否${target.recommendation==='ACCEPT'?'接受':'采纳'}上述代理建议？如需改变边界，请在下方写明。`));detail.append(question);
  } else {
    const result=el('section','section');result.append(el('h3','','本轮得到的结果'),el('p','',target.outcome));detail.append(result);
    const proxy=el('section','section proxy-block');const head=el('div','proxy-head');head.append(el('h3','','代理判断'),el('span','recommendation',recommendation(target)));proxy.append(head,el('p','',target.judgment));detail.append(proxy);
    const problems=el('section','section');problems.append(el('h3','',`典型问题与处理 · ${target.problems.length}`));const list=el('div','problem-list');
    for(const problem of target.problems){const open=String(problem[3]).startsWith('OPEN');const row=el('div',`problem${open?'':' resolved'}`);const title=el('div','');title.append(el('span','problem-label',open?'仍需处理':problem[3]==='MITIGATED'?'已有缓解':'已解决/已确认边界'),el('strong','',problem[0]));const evidence=el('p','');evidence.append(el('span','problem-label','证据'),document.createTextNode(problem[1]));const treatment=el('p','');treatment.append(el('span','problem-label','当前处理'),document.createTextNode(problem[2]));row.append(title,evidence,treatment);list.append(row);}problems.append(list);detail.append(problems);
    const question=el('section','section decision-question');question.append(el('h3','','你需要决定'),el('p','',target.decision));detail.append(question);
    const evidence=el('section','section');evidence.append(el('h3','','证据回执'));const tokens=el('div','evidence-list');for(const ref of target.evidence)tokens.append(el('span','evidence-token',ref));evidence.append(tokens);detail.append(evidence);
  }
  const technical=el('details','technical');technical.append(el('summary','','工件身份'),el('p','',`候选revision：${campaign.revision}`),el('p','',`当前对象：${target.id}；人工状态：${effective.status}`));detail.append(technical);
}

function renderPreview(){
  const target=current(),image=$('#preview-image'),empty=$('#preview-empty'),open=$('#open-preview');
  if(target&&!isDecision(target)&&target.preview){const url=`/preview/${target.id}`;image.src=url;image.alt=`${target.title}的真实渲染证据`;image.hidden=false;empty.hidden=true;open.href=url;open.hidden=false;$('#preview-caption').textContent=`${target.format} · ${target.source}`;$('#preview-scroller').scrollTop=0;}
  else {image.removeAttribute('src');image.alt='';image.hidden=true;empty.hidden=false;open.hidden=true;$('#preview-caption').textContent='选择具体类型查看页面、工作表或幻灯片证据';}
  applyPreviewZoom();
}
function applyPreviewZoom(){const value=$('#preview-zoom').value;$('#preview-image').style.width=value==='fit'?'100%':`${Number(value)*100}%`;}
function compactPreview(show){$('.workbench').classList.toggle('show-preview',show);$('#show-preview').setAttribute('aria-pressed',String(show));$('#show-detail').setAttribute('aria-pressed',String(!show));}

function renderDecisionPanel(){
  const panel=$('#decision-panel'),target=current();panel.replaceChildren();if(!target)return;
  const effective=targetState(target,state);const bar=el('div','decision-bar');const identity=el('div','decision-identity');identity.append(el('strong','',`当前：${target.id} ${target.title}`),el('span','',statusText(target)));bar.append(identity);
  const accept=button(state.reviewerKind==='qa'?'验证“接受建议”':'接受代理建议','primary-button',()=>saveDecision('accept'));
  const adjust=button('需要调整','warning-button',()=>saveDecision('adjust'));
  const defer=button('暂缓决定','quiet-button',()=>saveDecision('defer'));
  accept.id='accept-decision';adjust.id='adjust-decision';defer.id='defer-decision';
  for(const control of [accept,adjust,defer])control.disabled=saving;
  bar.append(defer,adjust,accept);panel.append(bar);
  const noteRow=el('div','note-row');const label=el('label','note-label','决定说明（调整、暂缓或记录问题时必填）');const note=el('textarea','');note.id='decision-note';note.rows=2;note.maxLength=8000;note.placeholder='写明要调整的边界、原因或补充条件…';note.disabled=saving;note.value=drafts.get(target.id)??'';note.addEventListener('input',()=>{const value=note.value;value.trim()?drafts.set(target.id,value):drafts.delete(target.id);renderDocket();});label.append(note);const comment=button('仅记录问题','quiet-button',()=>saveDecision('comment'));comment.id='comment-decision';comment.disabled=saving;noteRow.append(label,comment);panel.append(noteRow,el('p','draft-help','未保存说明按卷宗保留；保存当前决定后才会清除。'));
  if(effective.notes.length)panel.append(el('p','saved-note',`最近记录：${effective.notes.at(-1).text}`));
  if(effective.lastEventId){const undo=button('撤销当前对象最近决定','text-button',()=>saveDecision('undo'));undo.id='undo-decision';undo.disabled=saving;panel.append(undo);}
}
async function saveDecision(action){
  if(saving)return;const target=current();if(!target)return;const note=drafts.get(target.id)??$('#decision-note')?.value??'';
  if(['adjust','defer','comment'].includes(action)&&!note.trim()){toast('请先写明需要调整、暂缓或记录的问题。');$('#decision-note')?.focus();return;}
  const payload=decisionPayload(target,state,action,action==='undo'?'':note,crypto.randomUUID());const focusSelector={accept:'#accept-decision',adjust:'#adjust-decision',defer:'#defer-decision',comment:'#comment-decision',undo:'#accept-decision'}[action];saving=true;$('#decision-panel').setAttribute('aria-busy','true');$('#decision-panel').querySelectorAll('button,textarea').forEach(control=>{control.disabled=true;});
  try{const result=await api('/api/decision',{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':state.writeToken},body:JSON.stringify(payload)});state=result.state;if(action!=='undo')drafts.delete(target.id);toast(action==='undo'?'已撤销当前对象最近决定。':state.reviewerKind==='qa'?'已记录交互验证，不计作负责人决定。':'已保存当前对象的决定；其它卷宗未自动确认。');renderHeader();renderDocket();renderDetail();}
  catch(error){toast(error.message);if(/其它窗口|版本已经变化/.test(error.message)){try{state=await api('/api/state');renderHeader();renderDocket();renderDetail();}catch{}}}
  finally{saving=false;renderDecisionPanel();$('#decision-panel').setAttribute('aria-busy','false');requestAnimationFrame(()=>$('#decision-panel').querySelector(focusSelector)?.focus({preventScroll:true}));}
}

async function start(){
  try{
    [campaign,state]=await Promise.all([api('/api/campaign'),api('/api/state')]);
    if(campaign.revision!==state.sourceRevision)throw new Error('代理候选与决策日志版本不一致；请停止决定并重启服务。');
    const previewImage=el('img');previewImage.id='preview-image';previewImage.hidden=true;$('#preview-scroller').prepend(previewImage);
    targets=[...campaign.decisions,...campaign.types];renderHeader();
    const requested=location.hash.slice(1);selectedId=targets.some(target=>target.id===requested)?requested:'D01';
    $('#search').addEventListener('input',()=>{renderDocket();renderDetail();renderPreview();renderDecisionPanel();});
    $('#filter').addEventListener('change',()=>{renderDocket();renderDetail();renderPreview();renderDecisionPanel();});
    $('#preview-zoom').addEventListener('change',applyPreviewZoom);$('#show-detail').addEventListener('click',()=>compactPreview(false));$('#show-preview').addEventListener('click',()=>compactPreview(true));
    $('#preview-image').addEventListener('error',()=>{const target=current();$('#preview-image').hidden=true;$('#preview-empty').hidden=false;$('#preview-empty').textContent=`${target?.id??'当前类型'}的预览未能载入；请查看问题和证据回执，不使用替代图片。`;});
    window.addEventListener('beforeunload',event=>{if([...drafts.values()].some(value=>value.trim())){event.preventDefault();event.returnValue='';}});
    renderDocket();renderDetail();renderPreview();renderDecisionPanel();$('#app').setAttribute('aria-busy','false');
  }catch(error){$('#global-error').hidden=false;$('#global-error').textContent=`全类型候选未能安全载入：${error.message}`;$('#run-status').textContent='结果未就绪';$('#app').setAttribute('aria-busy','false');}
}
start();
