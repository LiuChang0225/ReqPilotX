/** Install an isolated profile through the sealed CLI, then exercise the adapter's default MCP import. */
import { readFile, writeFile, mkdir, cp, realpath, readdir } from 'node:fs/promises';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
const [cliArg, outputArg] = process.argv.slice(2);
if (!outputArg) throw Error('Usage: sealed-core-smoke.mjs <packaged-dsh-cli> <new-private-output>');
const cli = await realpath(resolve(cliArg)), output = resolve(outputArg);
await mkdir(output, { recursive: false, mode: 0o700 });
const resources = resolve(dirname(cli), '../../..'), home = join(output, 'dsh-home'), osHome = join(output, 'os-home');
await mkdir(osHome); await mkdir(home);
const host = join(output, 'host'), probe = join(output, 'probe');
await cp(fileURLToPath(new URL('../host', import.meta.url)), host, { recursive: true,
  filter: source => !source.split('/').some(part => ['node_modules', '__pycache__'].includes(part)) });
await mkdir(probe);
const manifest = JSON.parse(await readFile(join(host, 'package.json'), 'utf8'));
const version = manifest.peerDependencies?.['@deepseek-ai/dsh-mcp-client'];
if (manifest.dependencies?.['@deepseek-ai/dsh-mcp-client'] || version !== '0.2.1-alpha.2') throw Error('Host must declare the verified MCP core as an exact peer');
await cp(new URL('./sealed-core-probe.mjs', import.meta.url), join(probe, 'probe.mjs'));
await writeFile(join(probe, 'package.json'), JSON.stringify({ name: '@reqpilotx/sealed-core-probe', version: '0.0.0', private: true, type: 'module', main: './probe.mjs',
  peerDependencies: { '@reqpilotx/dsh-pair-host': manifest.version } }, null, 2));
const patch = join(output, 'probe.patch.json');
await writeFile(patch, JSON.stringify([{ id: 'session-title-llm', disabled: true }, { insert: [{ id: 'reqpilot-sealed-peer-probe', name: '@reqpilotx/sealed-core-probe' }] }], null, 2));
const env = Object.fromEntries(['PATH', 'LANG', 'TMPDIR'].filter(key => process.env[key]).map(key => [key, process.env[key]]));
Object.assign(env, { HOME: osHome, DSH_HOME: home, DSH_AGENTS_HOME: join(output, 'agents'), DSH_PRIMARY_RUNTIME: join(resources, 'runtime/primary-runtime'),
  REQPILOT_PEER_OUTPUT: output, REQPILOT_PEER_VERSION: version, REQPILOT_SEALED_RESOURCES: resources });
async function command(args, log, timeoutMs = 120000) {
  const child = spawn(cli, args, { cwd: output, env, stdio: ['ignore', 'pipe', 'pipe'] });
  let text = ''; child.stdout.on('data', value => text += value); child.stderr.on('data', value => text += value);
  let forced, timedOut = false;
  const timer = setTimeout(() => { timedOut = true; child.kill('SIGTERM'); forced = setTimeout(() => child.kill('SIGKILL'), 10000); }, timeoutMs);
  try {
    const status = await new Promise((ok, fail) => { child.once('error', fail); child.once('close', ok); });
    await writeFile(join(output, log), text.replace(/token=[^\s]+/g, 'token=[redacted]'));
    if (timedOut) throw Error('Sealed CLI timed out after ' + timeoutMs + ' ms; inspect ' + join(output, log));
    if (status !== 0) throw Error('Sealed CLI failed (' + status + '); inspect ' + join(output, log));
  } finally { clearTimeout(timer); clearTimeout(forced); }
}
await command(['--profile', 'reqpilot-peer-check', '--from-default-profile', 'web', '--dump-config'], 'initialize.log');
const workspace = await readFile(join(home, 'profiles/reqpilot-peer-check/pnpm-workspace.yaml'), 'utf8');
if (!/autoInstallPeers:\s*false/.test(workspace)) throw Error('Profile must not auto-install framework peers');
await command(['plugin', '--profile', 'reqpilot-peer-check', 'add', '--offline', 'file:' + host, 'file:' + probe], 'install.log');
const pending = [join(home, 'profiles/reqpilot-peer-check/node_modules')];
while (pending.length) {
  const directory = pending.pop();
  for (const item of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, item.name);
    if (item.isDirectory()) pending.push(path);
    if (item.isFile() && item.name === 'package.json') {
      const installed = JSON.parse(await readFile(path, 'utf8'));
      if (installed.name === '@deepseek-ai/dsh-mcp-client') throw Error('Profile installed a private MCP framework copy: ' + path);
    }
  }
}
await command(['--profile', 'reqpilot-peer-check', '--patch', patch, '--no-open', '--port', '0'], 'profile.log');
const receipt = JSON.parse(await readFile(join(output, 'receipt.json'), 'utf8'));
if (receipt.status !== 'PASS') throw Error('Missing passing profile receipt');
receipt.cli = cli; receipt.dshHome = home; receipt.autoInstallPeers = false; receipt.mainProfileTouched = false;
receipt.profilePhysicalMcpCopies = 0;
await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify(receipt));
