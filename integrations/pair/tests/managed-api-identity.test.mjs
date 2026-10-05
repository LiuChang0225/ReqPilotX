import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createHmac } from 'node:crypto';

test('managed ASGI listener proves this owner/instance/origin/challenge and delegates other routes', () => {
  const owner = '12345678-1234-4234-8234-123456789abc', instance = '87654321-4321-4321-8321-cba987654321';
  const origin = 'http://127.0.0.1:54321', challenge = 'a'.repeat(64), key = 'b'.repeat(64);
  const script = `import asyncio,importlib.util,json,os,sys
spec=importlib.util.spec_from_file_location('managed_api',sys.argv[1]);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
os.environ.update(REQPILOT_MANAGED_OWNER=sys.argv[2],REQPILOT_MANAGED_INSTANCE=sys.argv[3],REQPILOT_MANAGED_PROOF_KEY=sys.argv[6])
async def run():
    calls=[]
    async def app(scope,receive,send): calls.append(scope['path'])
    wrapped=module.managed_application(app,sys.argv[4]);out=[]
    async def send(value): out.append(value)
    await wrapped({'type':'http','path':'/api/pair-managed-identity','method':'GET','query_string':('challenge='+sys.argv[5]).encode()},None,send)
    proof=json.loads(out[-1]['body']);out=[]
    await wrapped({'type':'http','path':'/api/pair-managed-identity','method':'POST','query_string':b'challenge=bad'},None,send)
    invalid=out[0]['status']
    await wrapped({'type':'http','path':'/api/projects'},None,send)
    print(json.dumps({'proof':proof,'invalid':invalid,'calls':calls}))
asyncio.run(run())`;
  const output = JSON.parse(execFileSync(process.env.PAIR_RECONNECT_PYTHON || 'python3', ['-I', '-c', script,
    fileURLToPath(new URL('../host/src/managed_api.py', import.meta.url)), owner, instance, origin, challenge, key], { encoding: 'utf8' }));
  const proof = createHmac('sha256', Buffer.from(key, 'hex')).update(JSON.stringify([owner, instance, origin, challenge])).digest('hex');
  assert.deepEqual(output.proof, { ownerId: owner, instanceId: instance, challenge, proof });
  assert.equal(output.invalid, 422); assert.deepEqual(output.calls, ['/api/projects']);
});
