import test from 'node:test';
import assert from 'node:assert/strict';
import {targetState,isOpenProblem,openProblemCount,matchesTarget,decisionPayload,RECOMMENDATION_LABELS} from './static/review-core.js';

const type={id:'T06',title:'双语客户ESOW DOCX',format:'DOCX',role:'客户需求源',source:'ESOW',route:'requirement_source',outcome:'54条候选',judgment:'双语合一',decision:'是否接受',recommendation:'ACCEPT_WITH_GUARDRAILS',problems:[['中英文差异','2.1.3','等待确认','OPEN'],['模板说明','两条','降为上下文','RESOLVED']]};
const state={revision:0,sourceRevision:'rev',targets:{}};

test('open problem status is explicit and counted',()=>{assert.equal(isOpenProblem(type.problems[0]),true);assert.equal(isOpenProblem(type.problems[1]),false);assert.equal(openProblemCount(type),1);});
test('search covers route, evidence language, and recommendation copy',()=>{for(const q of ['docx','双语','2.1.3','requirement_source'])assert.equal(matchesTarget(type,q,'all',state),true);assert.equal(matchesTarget(type,'不存在','all',state),false);});
test('filters keep requirement and open-problem semantics separate',()=>{assert.equal(matchesTarget(type,'','requirements',state),true);assert.equal(matchesTarget(type,'','nonrequirements',state),false);assert.equal(matchesTarget(type,'','open',state),true);});
test('pending filter reflects only saved owner state',()=>{assert.equal(matchesTarget(type,'','pending',state),true);assert.equal(matchesTarget(type,'','pending',{...state,targets:{T06:{status:'accepted'}}}),false);});
test('saved state does not mutate proxy recommendation',()=>{const effective=targetState(type,{targets:{T06:{status:'adjustment-needed',notes:[{text:'改'}]}}});assert.equal(effective.status,'adjustment-needed');assert.equal(type.recommendation,'ACCEPT_WITH_GUARDRAILS');});
test('decision payload binds target and both revisions',()=>{assert.deepEqual(decisionPayload(type,state,'adjust','原因','1234567890abcdef'),{mutationId:'1234567890abcdef',expectedRevision:0,sourceRevision:'rev',targetId:'T06',action:'adjust',note:'原因'});});
test('undo binds only the latest event on this target',()=>{const payload=decisionPayload(type,{...state,targets:{T06:{lastEventId:'event-1'}}},'undo','', '1234567890abcdef');assert.equal(payload.undoOf,'event-1');});
test('recommendation language never claims human acceptance',()=>{assert.match(RECOMMENDATION_LABELS.ACCEPT_WITH_GUARDRAILS,/建议/);assert.doesNotMatch(RECOMMENDATION_LABELS.ACCEPT_WITH_GUARDRAILS,/已接受/);});
