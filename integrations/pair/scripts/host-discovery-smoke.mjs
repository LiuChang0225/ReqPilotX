/** Real Cordis fiber/service test for the Host-side Pair application contribution. */
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { applicationMetadata, registerPairApplication } from '../host/src/index.js';

const [pairArg, outArg] = process.argv.slice(2);
if (!outArg) throw Error('Usage: host-discovery-smoke.mjs <pair-source-root> <new-output>');
const pair = resolve(pairArg), output = resolve(outArg);
await mkdir(output, { recursive: false, mode: 0o700 });
const require = createRequire(join(pair, 'package.json'));
let cordis;
try { cordis = require.resolve('@deepseek-ai/cordis'); }
catch { cordis = join(pair, 'vendor/cordis/lib/index.js'); }
const { Context } = await import(pathToFileURL(cordis).href);

const runtime = { snapshot: { status: 'STARTING' } };
const registrations = [], disposals = [];
const center = { registerApplication(metadata, readStatus) {
  registrations.push({ metadata, readStatus });
  return () => disposals.push('center');
} };
const ctx = new Context();
ctx.provide('pairSkillCenter', center);
const fiber = ctx.inject(['pairSkillCenter'], child => { registerPairApplication(child, runtime); });
await fiber;
if (registrations.length !== 1) throw Error('Host contribution did not register once');
const registration = registrations[0];
if (JSON.stringify(registration.metadata) !== JSON.stringify(applicationMetadata)) throw Error('Metadata differs');
if (registration.readStatus() !== 'starting') throw Error('Unexpected initial status');
runtime.snapshot.status = 'READY';
if (registration.readStatus() !== 'ready') throw Error('Ready status did not follow runtime snapshot');
await fiber.dispose();
if (JSON.stringify(disposals) !== JSON.stringify(['center'])) throw Error('Fiber disposal did not dispose the registration');

const noService = new Context();
let rawApplied = false;
const rawFiber = noService.inject(['pairSkillCenter'], () => { rawApplied = true; throw Error('raw DSH without the service must not apply'); });
await new Promise(ok => setTimeout(ok, 20));
await rawFiber.dispose();
if (rawApplied) throw Error('Raw DSH applied a missing discovery service');

const receipt = { status: 'PASS', schema: 'reqpilot-pair-host-discovery-smoke/v1',
  pair_source_root: pair, metadata: applicationMetadata, registered_once: true,
  status_before: 'starting', status_ready: 'ready', disposed_with_fiber: true,
  raw_dsh_without_service: 'PENDING_WITHOUT_APPLY',
  limitation: 'Real Cordis service/fiber from the PAIR source checkout; no PAIR UI, model or business acceptance.' };
await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify(receipt));
