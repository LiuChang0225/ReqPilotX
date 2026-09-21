/** Real relocated adapter + installed Python + disposable PostgreSQL acceptance. */
import assert from 'node:assert/strict';
import { readFile, writeFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createHash, randomUUID } from 'node:crypto';
import { ManagedRuntime } from '../host/src/managed-runtime.js';
import { createHandler } from '../host/src/index.js';
import { nativeSubprocess } from '../tests/native-subprocess.mjs';

const [python, frontend, postgresBinDir, fixtureArg, reportArg] = process.argv.slice(2);
if (!reportArg) throw Error('Usage: installed-smoke.mjs <installed-python> <frontend> <postgres-bin> <synthetic-fixture> <report>');
const fixture=resolve(fixtureArg), report=resolve(reportArg);
const temporary=await mkdtemp(join(tmpdir(),'reqpilot-pair-installed-'));
const config={python:resolve(python),frontendRoot:resolve(frontend),postgresBinDir:resolve(postgresBinDir),dataRoot:join(temporary,'data')};
let runtime=new ManagedRuntime(nativeSubprocess,config);
const proxy=createServer(createHandler(null,()=>undefined,fetch,()=>runtime.snapshot));
proxy.listen(0,'127.0.0.1'); await once(proxy,'listening');
const base=`http://127.0.0.1:${proxy.address().port}/reqpilotx/service`;
const requests=[];
const sha=value=>createHash('sha256').update(value).digest('hex');
async function request(path,init={},direct=false) {
  const response=await fetch((direct?runtime.snapshot.serviceOrigin:base)+path,init);
  requests.push({path,status:response.status});
  assert.equal(response.status,200,`${path}: ${await (response.ok?Promise.resolve(''):response.text())}`);
  return response;
}
const json=async (path,init,direct)=>(await request(path,init,direct)).json();
let receipt={status:'FAIL',scope:'synthetic installed Pair adapter; external Pair UI not tested'};
try {
  for(const invalid of [{...config,python:undefined},{...config,frontendRoot:join(temporary,'missing')}]) {
    const candidate=new ManagedRuntime(nativeSubprocess,invalid);await candidate.start();assert.equal(candidate.snapshot.status,'ERROR');await candidate.stop();
  }
  await runtime.start();assert.equal(runtime.snapshot.status,'READY',runtime.snapshot.message);
  const competing=new ManagedRuntime(nativeSubprocess,config);await competing.start();assert.equal(competing.snapshot.status,'ERROR');assert.match(competing.snapshot.message,/already in use/);
  const source=await readFile(join(fixture,'example.pdf'));
  const project=await json('/api/projects',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name:'Synthetic installed Pair',customer_name:'Invented'})});
  const documents=`/api/projects/${project.id}/sor-documents`;
  const form=new FormData();form.append('file',new Blob([source],{type:'application/pdf'}),'example.pdf');
  const document=await json(documents,{method:'POST',body:form});
  const documentPath=`${documents}/${document.id}`;
  assert.equal(sha(Buffer.from(await (await request(documentPath+'/file')).arrayBuffer())),sha(source));
  const artifacts={};
  for(const [dir,names] of [['map',['document-map.json','table-previews.json']],['baseline',['source-requirement-baseline.json','validation-receipt.json']]])
    for(const name of names)artifacts[name]=JSON.parse(await readFile(join(fixture,dir,name),'utf8'));
  const api=documentPath+'/agent-structure/delivery/plugin-workbench';
  // Seed an already saved work through the shared API, never widen the Pair write allowlist.
  const session='plugin-'+randomUUID().replaceAll('-','');
  const state=await json(api,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({session_id:session,expected_source_sha256:sha(source),require_map_confirmation:false,goal:'Synthetic saved work',artifacts})},true);
  const work=api+'/'+session;
  const snapshotPath=work+'/snapshot?revision='+state.revision_id;
  const snapshot=await json(snapshotPath);
  const exported=await request(work+'/export?revision='+state.revision_id);
  assert.equal(exported.headers.get('x-reqpilot-revision'),state.revision_id);await exported.arrayBuffer();
  assert.equal((await fetch(base+api,{method:'POST'})).status,405);
  const first=JSON.parse(await readFile(join(config.dataRoot,'runtime-receipt.json'),'utf8'));
  await runtime.stop();assert.equal(runtime.snapshot.status,'STOPPED');
  runtime=new ManagedRuntime(nativeSubprocess,config);await runtime.start();assert.equal(runtime.snapshot.status,'READY',runtime.snapshot.message);
  assert.equal((await json('/api/projects'))[0].id,project.id);
  assert.equal((await json(documents))[0].id,document.id);
  assert.deepEqual(await json(snapshotPath),snapshot);
  assert.equal(sha(Buffer.from(await (await request(documentPath+'/file')).arrayBuffer())),sha(source));
  const second=JSON.parse(await readFile(join(config.dataRoot,'runtime-receipt.json'),'utf8'));
  assert.equal(first.ownerId,second.ownerId);assert.notEqual(first.instanceId,second.instanceId);
  const readiness=await json('/api/runtime-readiness');assert.equal(readiness.database_backend,'postgresql');
  const index=await fetch(runtime.snapshot.serviceOrigin+'/workspace');assert.equal(index.status,200);assert.match(await index.text(),/ReqPilotX/);
  receipt={status:'PASS',installation:first.installation,source_sha256:sha(source),project_id:project.id,document_id:document.id,revision_id:state.revision_id,
    snapshot_sha256:sha(JSON.stringify(snapshot)),restart_identity_preserved:true,frontend_served:true,worker_started:false,model_called:false,external_pair_ui:'NOT_RUN',requests};
} finally {
  await runtime.stop();await new Promise(ok=>proxy.close(ok));
  await writeFile(report,JSON.stringify(receipt,null,2)+'\n');
  await rm(temporary,{recursive:true,force:true});
}
console.log(JSON.stringify(receipt,null,2));
