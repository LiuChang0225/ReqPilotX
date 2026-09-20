import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, cp, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { loadSharedArtifacts } from '../scripts/shared-artifacts.mjs';
const required = JSON.parse(await readFile(new URL('../client/adapter-contract.json', import.meta.url), 'utf8'));
const source = new URL('../../../prototype/frontend/dist-panel', import.meta.url);
test('built shared artifact relocates independently of checkout and retains identity', async () => {
  const temp = await mkdtemp(join(tmpdir(), 'reqpilot-ui-portable-'));
  try { await cp(source, temp, { recursive: true }); const original = await loadSharedArtifacts(source, required); const moved = await loadSharedArtifacts(temp, required); assert.equal(original.identity, moved.identity); assert.deepEqual(original.contract, moved.contract); }
  finally { await rm(temp, { recursive: true, force: true }); }
});
test('incompatible contract and missing shared capability fail before packing', async () => {
  const temp = await mkdtemp(join(tmpdir(), 'reqpilot-ui-contract-'));
  try {
    await cp(source, temp, { recursive: true }); const file = join(temp, 'host-contract.json'); const contract = JSON.parse(await readFile(file, 'utf8'));
    await writeFile(file, JSON.stringify({ ...contract, major: 99 })); await assert.rejects(loadSharedArtifacts(temp, required), /incompatible/);
    await writeFile(file, JSON.stringify({ ...contract, exports: contract.exports.filter(x => x !== 'createProjectDocumentClient') })); await assert.rejects(loadSharedArtifacts(temp, required), /missing required exports/);
  } finally { await rm(temp, { recursive: true, force: true }); }
});

test('adapter rebuild outside checkout consumes only relocated shared outputs', async () => {
  const temp = await mkdtemp(join(tmpdir(), 'reqpilot-adapter-portable-'));
  try {
    const adapter = join(temp, 'adapter'), ui = join(temp, 'shared-ui');
    await cp(new URL('../scripts', import.meta.url), join(adapter, 'scripts'), { recursive: true });
    await cp(new URL('../client/src', import.meta.url), join(adapter, 'client/src'), { recursive: true });
    await cp(new URL('../client/adapter-contract.json', import.meta.url), join(adapter, 'client/adapter-contract.json'));
    await cp(source, ui, { recursive: true });
    execFileSync(process.execPath, [join(adapter, 'scripts/build.mjs'), '--shared-ui', ui], { cwd: temp });
    const moved = JSON.parse(await readFile(join(adapter, 'client/lib/build-receipt.json'), 'utf8'));
    const original = JSON.parse(await readFile(new URL('../client/lib/build-receipt.json', import.meta.url), 'utf8'));
    assert.equal(moved.clientSha256, original.clientSha256);
    assert.equal(moved.sharedArtifactIdentity, original.sharedArtifactIdentity);
  } finally { await rm(temp, { recursive: true, force: true }); }
});
