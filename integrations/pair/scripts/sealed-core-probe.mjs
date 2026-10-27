/** Observe the installed Host's default bridge import inside a packaged DSH profile. */
import { registerHooks } from 'node:module';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readFile, writeFile } from 'node:fs/promises';

export const name = 'reqpilot-sealed-core-probe';
export const inject = ['appExit'];
export function apply(ctx) {
  const output = process.env.REQPILOT_PEER_OUTPUT;
  void run(ctx).then(() => ctx.get('appExit')(0)).catch(async error => {
    await writeFile(join(output, 'failure.json'), JSON.stringify({ status: 'FAIL', message: error.message, stack: error.stack }, null, 2));
    ctx.get('appExit')(1);
  });
}

async function run(ctx) {
  const output = process.env.REQPILOT_PEER_OUTPUT, version = process.env.REQPILOT_PEER_VERSION;
  const resources = resolve(process.env.REQPILOT_SEALED_RESOURCES);
  const core = '@deepseek-ai/dsh-mcp-client', resolutions = [];
  let observingHostDefaultImport = false;
  const hooks = registerHooks({ resolve(specifier, context, nextResolve) {
    const result = nextResolve(specifier, context);
    if (specifier === core && observingHostDefaultImport) resolutions.push({ resolverParent: context.parentURL, url: result.url });
    return result;
  } });
  ctx.effect(() => () => hooks.deregister(), 'sealed peer resolution observation');
  const entry = import.meta.resolve('@reqpilotx/dsh-pair-host');
  const installedHost = fileURLToPath(entry);
  const profileRoot = join(resolve(process.env.DSH_HOME), 'profiles', 'reqpilot-peer-check') + sep;
  if (!installedHost.startsWith(profileRoot)) throw Error('Host must be installed in the private profile');
  const { installTaskMcp } = await import(new URL('./task-mcp.js', entry));
  const imported = Error('PEER_IMPORT_COMPLETE');
  let created, assemble, bridgeExports;
  const effect = run => {
    const dispose = run(); let pending;
    return () => pending ??= Promise.resolve().then(dispose);
  };
  // Only the lifecycle trigger is synthetic. The default import runs unchanged;
  // stop at plugin registration so this check opens no transport or model session.
  const disposeHost = installTaskMcp({
    on(event, listener) {
      if (event !== 'agent/created') throw Error('Unexpected Host event: ' + event);
      created = listener; return () => { created = undefined; };
    }, effect,
  }, { async toolBinding() { return { command: 'unused-import-probe', args: [], cwd: output }; } });
  try {
    created({ agent: { id: 'sealed-peer-import', ctx: {
      on(event, listener) {
        if (event !== 'system-prompt/assemble') throw Error('Unexpected Agent event: ' + event);
        assemble = listener; return () => { assemble = undefined; };
      }, effect,
      plugin(bridge) { bridgeExports = Object.keys(bridge); throw imported; },
    } } });
    observingHostDefaultImport = true;
    try { await assemble({}, { signal: new AbortController().signal }, () => {}); }
    catch (error) { if (error !== imported) throw error; }
  } finally { observingHostDefaultImport = false; await disposeHost(); }
  if (!bridgeExports?.length) throw Error('Host did not load the actual bridge module');
  const selected = resolutions;
  if (!selected.length) throw Error('No actual Host bridge resolution was observed');
  const packages = [];
  for (const value of selected) {
    const path = fileURLToPath(value.url);
    if (![join(resources, 'app.asar', 'dsh') + sep, join(resources, 'app.asar.unpacked', 'dsh') + sep].some(prefix => path.startsWith(prefix))) {
      throw Error('Host selected a core outside the sealed application: ' + path);
    }
    let directory = dirname(path), metadata;
    for (;;) {
      try {
        const candidate = JSON.parse(await readFile(join(directory, 'package.json'), 'utf8'));
        if (candidate.name === core) { metadata = candidate; break; }
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
      const parent = dirname(directory); if (parent === directory) break; directory = parent;
    }
    if (metadata?.version !== version) throw Error('Unexpected sealed core version: ' + metadata?.version);
    packages.push({ ...value, packageDirectory: directory, version: metadata.version });
  }
  await writeFile(join(output, 'receipt.json'), JSON.stringify({ status: 'PASS', scope: 'installed-host-default-import-only',
    expectedCoreVersion: version, adapterEntry: entry, defaultImportConsumer: new URL('./task-mcp.js', entry).href,
    actualAdapterBridgeImports: packages, bridgeExports,
    realDshProfile: true, resolverResultModified: false, realMcpRoundtrip: false, modelSessions: 0,
    customerDataUsed: false }, null, 2) + '\n');
}
