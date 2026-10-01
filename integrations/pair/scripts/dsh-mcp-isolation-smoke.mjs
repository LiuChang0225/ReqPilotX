/** Launch the real DSH Web profile in a private home and verify MCP teardown after exit. */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { spawn, execFileSync } from 'node:child_process';

const [pairArg, pythonArg, bundleArg, outArg] = process.argv.slice(2);
if (!outArg) {
  throw new Error('Usage: dsh-mcp-isolation-smoke.mjs <Pair checkout> <python> <frozen bundle> <new private output>');
}
const pair = resolve(pairArg);
const python = resolve(pythonArg);
const bundle = resolve(bundleArg);
const output = resolve(outArg);
await mkdir(output, { recursive: false, mode: 0o700 });
const home = join(output, 'harness-home');
await mkdir(home);
execFileSync(python, ['-c', [
  'from pypdf import PdfWriter',
  'import sys',
  'for path,title in [(sys.argv[1],"isolation-a"),(sys.argv[2],"isolation-b")]:',
  ' w=PdfWriter();w.add_blank_page(200,200);w.add_metadata({"/Title":title});w.write(path)',
].join('\n'), join(output, 'synthetic-a.pdf'), join(output, 'synthetic-b.pdf')]);

const blockedNetwork = join(output, 'blocked-network.jsonl');
const guard = join(output, 'network-guard.mjs');
await writeFile(guard, [
  "import {appendFileSync} from 'node:fs';",
  'const original=globalThis.fetch;',
  `globalThis.fetch=(input,...args)=>{const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);if(!['127.0.0.1','localhost','[::1]'].includes(url.hostname)){appendFileSync(${JSON.stringify(blockedNetwork)},JSON.stringify({hostname:url.hostname})+'\\n');throw Error('NONLOCAL_NETWORK_BLOCKED_BY_PROBE');}return original(input,...args);};`,
  '',
].join('\n'));
const patch = join(output, 'probe.patch.json');
await writeFile(patch, JSON.stringify([
  { id: 'session-title-llm', disabled: true },
  { insert: [{
    id: 'reqpilot-mcp-isolation-probe',
    name: fileURLToPath(new URL('./dsh-mcp-isolation-probe.mjs', import.meta.url)),
  }] },
], null, 2));

const require = createRequire(join(pair, 'package.json'));
const tsx = pathToFileURL(require.resolve('tsx/esm')).href;
const env = Object.fromEntries(['PATH', 'HOME', 'LANG', 'TMPDIR']
  .filter(key => process.env[key]).map(key => [key, process.env[key]]));
Object.assign(env, {
  DSH_HOME: home,
  DSH_TELEMETRY_DISABLED: '1',
  TSX_TSCONFIG_PATH: join(pair, 'tsconfig.json'),
  REQPILOT_PROBE_OUTPUT: output,
  REQPILOT_PROBE_PYTHON: python,
  REQPILOT_PROBE_CAPABILITY: bundle,
  REQPILOT_PROBE_PAIR: pair,
});
const child = spawn(process.execPath, [
  '--import', guard,
  '--import', tsx,
  join(pair, 'apps/cli/src/bin.ts'),
  '--profile', 'web',
  '--patch', patch,
  '--no-open',
  '--port', '0',
], { cwd: output, env, stdio: ['ignore', 'pipe', 'pipe'] });
let stdout = '';
let stderr = '';
child.stdout.on('data', chunk => { stdout += chunk; });
child.stderr.on('data', chunk => { stderr += chunk; });
const timeout = setTimeout(() => child.kill('SIGTERM'), 120000);
const exitCode = await new Promise((resolvePromise, reject) => {
  child.once('error', reject);
  child.once('close', resolvePromise);
});
clearTimeout(timeout);
await writeFile(join(output, 'launcher.log'), (stdout + '\n' + stderr).replace(/token=[^\s]+/g, 'token=[redacted]'));
if (exitCode !== 0) {
  let failure;
  try { failure = JSON.parse(await readFile(join(output, 'failure.json'))).message; }
  catch { failure = stderr.slice(-2400); }
  throw new Error(`DSH MCP isolation probe failed: ${failure}`);
}

const receipt = JSON.parse(await readFile(join(output, 'receipt-pre-disposal.json')));
const disposalLines = (await readFile(join(output, 'agent-context-disposals.jsonl'), 'utf8'))
  .trim().split('\n').filter(Boolean).map(line => JSON.parse(line));
const disposed = new Set(disposalLines.map(row => row.session_id));
const missingDisposals = receipt.expected_agent_context_disposals.filter(sessionId => !disposed.has(sessionId));
if (missingDisposals.length) {
  throw new Error(`DSH exited without observed Agent context disposal: ${JSON.stringify(missingDisposals)}`);
}
let remaining = [];
for (let attempt = 0; attempt < 20; attempt++) {
  remaining = execFileSync('ps', ['-axo', 'pid=,ppid=,command='], { encoding: 'utf8' }).split('\n')
    .filter(line => line.includes(output) && line.includes('mcp_server.py'));
  if (remaining.length === 0) break;
  await new Promise(resolvePromise => setTimeout(resolvePromise, 50));
}
if (remaining.length) throw new Error(`MCP stdio processes survived DSH disposal: ${remaining.length}`);
const blocked = await readFile(blockedNetwork, 'utf8').catch(error => {
  if (error.code === 'ENOENT') return '';
  throw error;
});
if (blocked.trim()) throw new Error('Unexpected nonlocal fetch attempted; inspect private probe evidence');

Object.assign(receipt, {
  status: 'PASS',
  dsh_home: home,
  dsh_home_isolated: true,
  launcher_exit_code: exitCode,
  agent_contexts_disposed: receipt.expected_agent_context_disposals,
  mcp_stdio_processes_after_exit: [],
  nonlocal_fetch_attempts: 0,
});
delete receipt.expected_agent_context_disposals;
await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify(receipt));
