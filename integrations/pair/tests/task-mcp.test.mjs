import test from 'node:test';
import assert from 'node:assert/strict';
import { installTaskMcp } from '../host/src/task-mcp.js';
const effect = execute => {
 let disposal;let disposed=false;
 return ()=>{if(disposed)return disposal;disposed=true;return disposal=Promise.resolve().then(execute);};
};
const host = onCreated => ({
 on(name,fn){assert.equal(name,'agent/created');onCreated(fn);return effect(()=>onCreated(undefined));},
 effect(run){return effect(run());},
});
const waitFor = async predicate => {
 for(let i=0;i<50;i++){if(predicate())return;await new Promise(resolve=>setImmediate(resolve));}
 assert.fail('condition did not become true');
};
test('MCP mounts once per owning Agent and the first request is assembled with its tools',async()=>{
 let created;const mounts=[];
 installTaskMcp(host(fn=>created=fn),{async toolBinding(id){return id==='other'?null:{command:'/python',args:['/fixed/mcp.py','--working-root','/'+id],cwd:'/'+id};}},async()=>({name:'official-mcp-bridge'}));
 function agent(id){
  let hook,tools=[]; const ctx={on(name,fn){assert.equal(name,'system-prompt/assemble');hook=fn;return effect(()=>{hook=undefined;});},effect(run){return effect(run());},plugin(bridge,config){mounts.push({id,bridge,config});const handle=Promise.resolve().then(()=>{tools=['owned-'+id];});handle.dispose=effect(()=>{tools=[];});return handle;},get(){return {assemble};}};
  async function assemble(context){const snapshot={tools:[...tools]};return hook(snapshot,context,async()=>snapshot);}
  created({agent:{id,ctx}});return assemble;
 }
 const a=agent('a'),b=agent('b'),other=agent('other');const context={signal:new AbortController().signal};
 assert.deepEqual((await a(context)).tools,['owned-a']);assert.deepEqual((await b(context)).tools,['owned-b']);
 await a(context);assert.deepEqual((await other(context)).tools,[]);
 assert.equal(mounts.length,2);assert.equal(mounts[0].config.cwd,'/a');assert.equal(mounts[1].config.cwd,'/b');
});
test('startup failure disposes its scoped bridge and prevents request assembly',async()=>{
 let created,hook,disposed=0,next=0;
 installTaskMcp(host(fn=>created=fn),{async toolBinding(){return {command:'/python',args:[],cwd:'/task'};}},async()=>({}));
 const agent={id:'a',ctx:{on(_name,fn){hook=fn;return effect(()=>{hook=undefined;});},effect(run){return effect(run());},plugin(){const handle=Promise.reject(Error('mcp not ready'));handle.dispose=effect(()=>{disposed++;});return handle;}}};created({agent});
 await assert.rejects(hook({}, {signal:new AbortController().signal},()=>next++),/mcp not ready/);
 assert.equal(disposed,1);assert.equal(next,0);
});
test('Host disposal removes two Agent bindings without disposing either Agent or unrelated tools',async()=>{
 let created;const mounts=[];const hostDispose=installTaskMcp(host(fn=>created=fn),{
  async toolBinding(id){return {command:'/python',args:['/mcp.py','--working-root','/'+id],cwd:'/'+id};},
 },async()=>({}));
 function makeAgent(id){
  let hook;let agentAlive=true;let tools=['unrelated-'+id];let bridgeDisposals=0;
  const ctx={
   on(_name,fn){hook=fn;return effect(()=>{hook=undefined;});},effect(run){return effect(run());},
   plugin(){tools.push('mcp-'+id);const handle=Promise.resolve();handle.dispose=effect(()=>{bridgeDisposals++;tools=tools.filter(name=>name!=='mcp-'+id);});mounts.push(id);return handle;},
   get(){return {assemble};},
  };
  async function assemble(context){const snapshot={tools:[...tools]};return hook?hook(snapshot,context,async()=>snapshot):snapshot;}
  created({agent:{id,ctx}});
  return {assemble,alive:()=>agentAlive,stop:()=>{agentAlive=false;},tools:()=>tools,bridgeDisposals:()=>bridgeDisposals,hook:()=>hook};
 }
 const a=makeAgent('a'),b=makeAgent('b');const context={signal:new AbortController().signal};
 assert.deepEqual((await a.assemble(context)).tools,['unrelated-a','mcp-a']);
 assert.deepEqual((await b.assemble(context)).tools,['unrelated-b','mcp-b']);
 await hostDispose();await hostDispose();
 assert.equal(a.alive(),true);assert.equal(b.alive(),true);assert.equal(a.hook(),undefined);assert.equal(b.hook(),undefined);
 assert.deepEqual(a.tools(),['unrelated-a']);assert.deepEqual(b.tools(),['unrelated-b']);
 assert.equal(a.bridgeDisposals(),1);assert.equal(b.bridgeDisposals(),1);assert.deepEqual(mounts,['a','b']);
});
test('Host disposal during bridge import prevents a late mount and lets assembly continue without MCP',async()=>{
 let created,resolveImport,mounts=0;const imported=new Promise(resolve=>resolveImport=resolve);
 const hostDispose=installTaskMcp(host(fn=>created=fn),{async toolBinding(){return {command:'/python',args:[],cwd:'/task'};}},()=>imported);
 let hook;const ctx={on(_name,fn){hook=fn;return effect(()=>{hook=undefined;});},effect(run){return effect(run());},plugin(){mounts++;throw Error('late mount');},get(){return {assemble};}};
 const assemble=async context=>hook({},context,async()=>({tools:['unrelated']}));created({agent:{id:'a',ctx}});
 const pending=assemble({signal:new AbortController().signal});await waitFor(()=>typeof resolveImport==='function');
 const disposing=hostDispose();resolveImport({});
 assert.deepEqual(await pending,{tools:['unrelated']});await disposing;assert.equal(mounts,0);assert.equal(hook,undefined);
});
test('Host disposal during bridge startup awaits one idempotent bridge teardown',async()=>{
 let created,hook,resolveStartup,started=false,disposed=0;
 const hostDispose=installTaskMcp(host(fn=>created=fn),{async toolBinding(){return {command:'/python',args:[],cwd:'/task'};}},async()=>({}));
 const ctx={on(_name,fn){hook=fn;return effect(()=>{hook=undefined;});},effect(run){return effect(run());},plugin(){started=true;const handle=new Promise(resolve=>resolveStartup=resolve);handle.dispose=effect(()=>{disposed++;resolveStartup();});return handle;},get(){return {assemble};}};
 const assemble=async context=>hook({},context,async()=>({tools:['unrelated']}));created({agent:{id:'a',ctx}});
 const pending=assemble({signal:new AbortController().signal});await waitFor(()=>started);
 await Promise.all([hostDispose(),hostDispose()]);assert.deepEqual(await pending,{tools:['unrelated']});
 assert.equal(disposed,1);assert.equal(hook,undefined);
});
test('Agent and Host disposal racing share the same binding teardown',async()=>{
 let created,hook,agentDispose,disposed=0;
 const hostDispose=installTaskMcp(host(fn=>created=fn),{async toolBinding(){return {command:'/python',args:[],cwd:'/task'};}},async()=>({}));
 const ctx={on(_name,fn){hook=fn;return effect(()=>{hook=undefined;});},effect(run){agentDispose=effect(run());return agentDispose;},plugin(){const handle=Promise.resolve();handle.dispose=effect(()=>{disposed++;});return handle;},get(){return {assemble};}};
 const assemble=async context=>hook({},context,async()=>({tools:[]}));created({agent:{id:'a',ctx}});await assemble({signal:new AbortController().signal});
 await Promise.all([agentDispose(),hostDispose()]);assert.equal(disposed,1);assert.equal(hook,undefined);
});
