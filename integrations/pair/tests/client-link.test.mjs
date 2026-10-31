import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const code = await readFile(new URL('../client/lib/client.js', import.meta.url), 'utf8');
let entry;
vm.runInNewContext(code, {
  window: { __ModuleLoader__: { load(value) { entry = value; } } },
  document: { createElement() { return { dataset: {}, remove() {} }; }, head: { append() {} } },
  URL, console,
});
const plugin = entry.factory(id => {
  if (id === 'react' || id === 'react/jsx-runtime' || id === 'react-dom') return require(id);
  throw Error(`unexpected external ${id}`);
});

test('reopening a Pair workbench link restores each document view from its route', () => {
  const base = 'https://pair.example/workspace?projectId=p&documentId=d&pluginSessionId=plugin-' + 'a'.repeat(32);
  for (const [query, view] of [
    ['', 'structure'],
    ['&checkpoint=list', 'items'],
    ['&checkpoint=requirements', 'requirements'],
    ['&task=analysis', 'analysis'],
    ['&task=export', 'export'],
  ]) {
    const { selection } = plugin.workspaceSelectionFromUrl(base + query);
    assert.equal(JSON.parse(JSON.stringify(selection)).view, view);
  }
});

test('reopened requirements links retain version and stable work identity', () => {
  const revision = 'b'.repeat(64);
  const { selection } = plugin.workspaceSelectionFromUrl(
    'https://pair.example/workspace?task=extraction&mode=organize&checkpoint=requirements&projectId=p&documentId=d&pluginSessionId=plugin-' + 'c'.repeat(32) + '&resultRevision=' + revision,
  );
  assert.deepEqual(JSON.parse(JSON.stringify(selection)), {
    projectId: 'p', documentId: 'd', workRef: 'plugin-' + 'c'.repeat(32), revisionId: revision, view: 'requirements',
  });
});
