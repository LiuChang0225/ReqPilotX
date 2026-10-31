/** Keyless real-DSH Session snapshot against one explicitly approved loopback OpenAI-compatible route. */
import { mkdir, readFile, writeFile, copyFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { spawn, execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const [pairArg, pythonArg, bundleArg, outArg, dshCliArg] = process.argv.slice(2);
if (!outArg) throw Error('Usage: dsh-native-snapshot.mjs <pair-source-root> <python> <frozen-bundle> <new-output> [dsh-cli]');
const pair = resolve(pairArg), python = resolve(pythonArg), bundle = resolve(bundleArg), output = resolve(outArg);
await mkdir(output, { recursive: false, mode: 0o700 });
const home = join(output, 'harness-home'), dataRoot = join(output, 'owned');
const commandId = randomUUID();
await mkdir(home); await mkdir(dataRoot);
execFileSync(python, ['-c', 'from pypdf import PdfWriter;import sys;w=PdfWriter();w.add_blank_page(200,200);w.write(sys.argv[1])', join(output, 'synthetic.pdf')]);

const requests = [];
let mockPort;
const mockRequests = [];
const mock = createServer(async (req, res) => {
  if (req.method !== 'POST' || !req.url?.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
  let body = ''; for await (const part of req) body += part;
  let request; try { request = JSON.parse(body); } catch { request = null; }
  let taskId;
  try { taskId = JSON.parse(await readFile(join(dataRoot, 'task-launches', commandId + '.json'), 'utf8')).prepared?.task_id; } catch { /* not prepared yet */ }
  const attempt = mockRequests.length + 1;
  mockRequests.push({ attempt, path: req.url, authorization: req.headers.authorization ? 'SYNTHETIC_BEARER' : null,
    body_model: request?.model, stream: request?.stream, task_id: taskId || null });
  if (!taskId) { res.writeHead(500, { 'content-type': 'application/json' }); res.end('{"error":"task not prepared"}'); return; }
  res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' });
  const chunk = extra => ({ id: 'chatcmpl-reqpilot-probe', object: 'chat.completion.chunk', created: 1, model: 'noop', ...extra });
  const send = value => res.write('data: ' + JSON.stringify(value) + '\n\n');
  if (attempt === 1) {
    send(chunk({ choices: [{ index: 0, delta: { role: 'assistant', tool_calls: [{
      index: 0, id: 'call_reqpilot_task_open', type: 'function',
      function: { name: 'mcp__reqpilot__task_open', arguments: JSON.stringify({ mode: 'resume', task_id: taskId }) } }] },
      finish_reason: null }] }));
    send(chunk({ choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] }));
  } else {
    send(chunk({ choices: [{ index: 0, delta: { role: 'assistant', content: 'PROBE_ONLY: tool result received; no business artifacts were saved by this message.' }, finish_reason: null }] }));
    send(chunk({ choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }));
    send(chunk({ choices: [], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }));
  }
  res.write('data: [DONE]\n\n'); res.end();
});
mock.listen(0, '127.0.0.1'); await once(mock, 'listening'); mockPort = mock.address().port;

const policy = { revision: 'reqpilot-rx02-scripted-v1', routes: [{
  provider: 'reqpilot-scripted', models: ['noop'], protocols: ['openai-completions'],
  endpoints: [{ origin: 'http://127.0.0.1:' + mockPort, pathPrefix: '/v1' }],
  authKinds: ['api-key'], allowLogin: false }] };
const directory = { providers: { 'reqpilot-scripted': {
  displayName: 'ReqPilot scripted loopback', api: 'openai-completions',
  baseURL: 'http://127.0.0.1:' + mockPort + '/v1', apiKeyEnv: 'REQPILOT_SCRIPTED_API_KEY',
  models: [{ id: 'noop', name: 'Noop', contextWindow: 65536, maxTokens: 4096, reasoningEfforts: { max: 'max' } }] } },
  defaultModel: { provider: 'reqpilot-scripted', model: 'noop', reasoningEffort: 'max' } };
const policyFile = join(output, 'pair-model-policy.json'), directoryFile = join(output, 'pair-model-directory.json');
await writeFile(policyFile, JSON.stringify(policy, null, 2) + '\n', { mode: 0o600 });
await writeFile(directoryFile, JSON.stringify(directory, null, 2) + '\n', { mode: 0o600 });

const patch = join(output, 'probe.patch.json');
await writeFile(patch, JSON.stringify([
  { id: 'session-title-llm', disabled: true },
  { insert: [{ id: 'reqpilot-native-snapshot-probe',
    name: fileURLToPath(new URL('./dsh-native-snapshot-probe.mjs', import.meta.url)) }] },
], null, 2));
const guard = join(output, 'network-guard.mjs');
await writeFile(guard, `import {appendFileSync} from 'node:fs';\nconst original=globalThis.fetch;globalThis.fetch=(input,...args)=>{const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);if(!['127.0.0.1','localhost','[::1]'].includes(url.hostname)){appendFileSync(${JSON.stringify(join(output,'blocked-network.jsonl'))},JSON.stringify({hostname:url.hostname})+'\\n');throw Error('NONLOCAL_NETWORK_BLOCKED_BY_PROBE');}return original(input,...args);};\n`);
const require = createRequire(join(pair, 'package.json'));
const tsx = dshCliArg ? null : pathToFileURL(require.resolve('tsx/esm')).href;
let nativeSystem = null;
if (!dshCliArg) {
  const sourcePackage = join(pair, 'native/system/packages/darwin-arm64/package.json');
  const packagedBinary = process.env.REQPILOT_NATIVE_SYSTEM_NODE
    || '/Users/liuchang/Applications/PAIR.app/Contents/Resources/app.asar.unpacked/dsh/node_modules/@deepseek-ai/node-addon-system-darwin-arm64/bin/system.node';
  if (!existsSync(packagedBinary)) throw Error('Source DSH requires the packaged native system addon; set REQPILOT_NATIVE_SYSTEM_NODE to an existing system.node. Missing: ' + packagedBinary);
  const nativeRoot = join(output, 'native-system-package');
  await mkdir(join(nativeRoot, 'bin'), { recursive: true, mode: 0o700 });
  await copyFile(sourcePackage, join(nativeRoot, 'package.json'));
  await copyFile(packagedBinary, join(nativeRoot, 'bin/system.node'));
  const shim = join(output, 'native-system-shim.mjs');
  await writeFile(shim, `import Module from 'node:module';
const target='@deepseek-ai/node-addon-system-darwin-arm64/package.json';
const replacement=${JSON.stringify(join(nativeRoot, 'package.json'))};
const original=Module._resolveFilename;
Module._resolveFilename=function(request,...args){if(request===target||request==='@deepseek-ai/node-addon-system-darwin-arm64')return replacement;return original.call(this,request,...args);};
`);
  nativeSystem = { source: 'PACKAGED_PAIR_PREBUILT', package_json: join(nativeRoot, 'package.json'),
    binary: join(nativeRoot, 'bin/system.node'), shim };
}
const env = Object.fromEntries(['PATH', 'HOME', 'LANG', 'TMPDIR'].filter(key => process.env[key]).map(key => [key, process.env[key]]));
Object.assign(env, {
  DSH_HOME: home, TSX_TSCONFIG_PATH: join(pair, 'tsconfig.json'),
  NODE_OPTIONS: '--import ' + pathToFileURL(guard).href,
  DSH_PAIR_MODEL_POLICY_FILE: policyFile, DSH_PAIR_MODEL_DIRECTORY_FILE: directoryFile,
  REQPILOT_SCRIPTED_API_KEY: 'reqpilot-synthetic-not-a-secret',
  REQPILOT_NATIVE_OUTPUT: output, REQPILOT_NATIVE_PYTHON: python,
  REQPILOT_NATIVE_CAPABILITY: bundle, REQPILOT_NATIVE_COMMAND: commandId,
});
const argv = dshCliArg
  ? [resolve(dshCliArg), '--profile', 'pair', '--patch', patch, '--no-open', '--port', '0']
  : [process.execPath, ...(nativeSystem ? ['--import', nativeSystem.shim] : []), '--import', tsx,
    join(pair, 'apps/cli/src/bin.ts'), '--profile', 'pair', '--patch', patch, '--no-open', '--port', '0'];
const child = spawn(argv[0], argv.slice(1), { cwd: output, env, stdio: ['ignore', 'pipe', 'pipe'] });
let stdout = '', stderr = '';
child.stdout.on('data', chunk => stdout += chunk); child.stderr.on('data', chunk => stderr += chunk);
const timeout = setTimeout(() => child.kill('SIGTERM'), 120000);
const exitCode = await new Promise((ok, fail) => { child.once('error', fail); child.once('close', ok); });
clearTimeout(timeout);
await new Promise(ok => mock.close(ok));
await writeFile(join(output, 'launcher.log'), (stdout + '\n' + stderr).replace(/token=[^\s]+/g, 'token=[redacted]'));
await writeFile(join(output, 'mock-openai-requests.json'), JSON.stringify(mockRequests, null, 2) + '\n');
const blocked = await readFile(join(output, 'blocked-network.jsonl'), 'utf8').catch(error => { if (error.code === 'ENOENT') return ''; throw error; });
if (blocked.trim()) throw Error('Unexpected nonlocal fetch attempted; inspect private probe evidence.');
if (exitCode !== 0) {
  let failure; try { failure = JSON.parse(await readFile(join(output, 'failure.json'))).message; }
  catch { failure = stderr.slice(-2000); }
  throw Error('DSH native snapshot failed: ' + failure);
}
const receipt = JSON.parse(await readFile(join(output, 'receipt.json')));
Object.assign(receipt, {
  pair_source_root: pair, launcher: dshCliArg ? resolve(dshCliArg) : 'SOURCE_TSX',
  native_system_addon: nativeSystem,
  profile: 'pair', dsh_home: home, isolated_profile: true, user_profile_modified: false,
  policy_file: policyFile, policy_revision: policy.revision, policy_route: policy.routes[0],
  directory_file: directoryFile, mock_openai_attempts: mockRequests.length,
  mock_requests: mockRequests, nonlocal_fetch_attempts: 0,
  session_projection: 'PUBLIC_SESSION_INSPECT_EVENTS',
  launcher_exit_code: exitCode,
});
await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify(receipt));
