import test from 'node:test';
import assert from 'node:assert/strict';
import { executionObservation } from '../host/src/execution-observation.js';
const record = {requestId:'owned-request'};
const event = (type,data) => ({type,data,time:1790070000000});
const begin = [event('turn/start',{turn:1}),event('user/message',{source:{kind:'user',rpcId:record.requestId}}),event('step/start',{turn:1,step:1})];
test('exact request scopes metadata and tool settlement without leaking content',()=>{
 const es=[event('tool/call',{callId:'older',arguments:'secret'}),...begin,event('assistant/message',{message:'secret',stream:['secret']}),event('tool/call',{callId:'one',name:'bash',arguments:'secret'}),event('tool/call',{callId:'two',arguments:'secret'}),event('tool/result',{message:{role:'tool',content:[{type:'tool-result',toolCallId:'one',isError:true,content:[{type:'text',text:'secret'}]}]}})];
 const a=executionObservation(record,es,'running'); assert.equal(a.activity,'TOOLS');assert.equal(a.toolCalls,2);assert.equal(a.toolFailures,1);assert.equal(a.status,'RUNNING');assert.equal(a.lastEventAt,'2026-09-22T09:40:00.000Z');assert.ok(!JSON.stringify(a).includes('secret'));
 const b=executionObservation(record,[...es,event('tool/result',{message:{role:'tool',content:[{type:'tool-result',toolCallId:'two',content:[]}]}})],'running');assert.equal(b.activity,'MODEL');
 assert.equal(executionObservation(record,[event('user/message',{source:{kind:'agent-instructions',rpcId:record.requestId}})],'running'),null);
});
test('retry wait, resumed attempt, and terminal failure remain distinct',()=>{
 const es=[...begin,event('llm/retry',{retry:1,delayMs:2500,failure:{message:'private'}})];
 const a=executionObservation(record,es,'running');assert.equal(a.status,'WAITING');assert.equal(a.retries,1);assert.equal(a.retryDelayMs,2500);
 const b=executionObservation(record,[...es,event('llm/retry-started',{})],'running');assert.equal(b.status,'RUNNING');assert.equal(b.retryDelayMs,undefined);
 const c=executionObservation(record,[...es,event('turn/end',{reason:{kind:'error',error:{message:'private'}}})],'idle');assert.equal(c.status,'FAILED');assert.ok(!JSON.stringify(c).includes('private'));
});
test('completed turn is only a runtime fact; unclosed cold log does not claim a running agent',()=>{
 const a=executionObservation(record,begin,undefined);assert.equal(a.connected,false);assert.equal(a.activity,'UNOBSERVED');
 for(const [kind,status] of [['completed','SUCCEEDED'],['aborted','INTERRUPTED'],['blocked','WAITING'],['max-tokens','FAILED']]) {
  const b=executionObservation(record,[...begin,event('turn/end',{reason:{kind}})],'idle');assert.equal(b.status,status);assert.equal(b.semanticAcceptance,undefined);
 }
});
