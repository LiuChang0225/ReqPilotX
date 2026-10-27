/** Entry recovery belongs to the shared workbench client; native UI coverage lives with ProjectManagementPanel. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { webcrypto } from 'node:crypto';
import vm from 'node:vm';
import { rolldown } from 'rolldown';

test('shared entry client recovers project and PDF identities without resubmission after a lost acknowledgement', async () => {
  const values = new Map(), requests = [], projects = new Map(), documents = new Map();
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key), key: index => [...values.keys()][index] ?? null, get length() { return values.size; } };
  const bundle = await rolldown({ input: fileURLToPath(new URL('../../../packages/workbench-ui/src/shared/api/projectDocuments.ts', import.meta.url)) });
  const { output } = await bundle.generate({ format: 'cjs' }); await bundle.close();
  const module = { exports: {} };
  vm.runInNewContext(output.find(item => item.type === 'chunk').code, { module, exports: module.exports,
    crypto: webcrypto, localStorage: storage, FormData, File, DOMException, TextEncoder, Uint8Array,
    window: { location: { origin: 'http://pair.test' } } });
  let reachable = false;
  const request = async (path, init = {}) => {
    requests.push({ path, init });
    if (path.includes('/creation-commands/')) {
      if (!reachable) throw Error('query unavailable');
      return projects.get(path.split('/').at(-1));
    }
    if (path.includes('/import-commands/')) {
      if (!reachable) throw Error('query unavailable');
      return documents.get(path.split('/').at(-1));
    }
    if (path === '/api/projects' && init.method === 'POST') {
      const input = JSON.parse(init.body); projects.set(input.command_id, { id: 'p', name: input.name, customer_name: input.customer_name });
      throw Error('creation reply lost');
    }
    if (path === '/api/projects/p/sor-documents' && init.method === 'POST') {
      const file = init.body.get('file'), command = init.body.get('command_id');
      const hash = Buffer.from(await webcrypto.subtle.digest('SHA-256', await file.arrayBuffer())).toString('hex');
      documents.set(command, { id: 'd', project_id: 'p', file_name: file.name, source_size: file.size, source_sha256: hash });
      throw Error('import reply lost');
    }
    throw Error('Unexpected entry request: ' + path);
  };
  const create = () => module.exports.createProjectDocumentClient(request, undefined, { namespace: 'managed-app', storage });
  let client = create();
  await assert.rejects(client.createProject('Synthetic', 'Customer'), /creation reply lost/);
  assert.equal(projects.size, 1);
  reachable = true; client = create();
  const recoveredProject = await client.recoverPendingEntryCommands();
  assert.equal(recoveredProject.recovered[0].result.id, 'p');
  assert.equal(requests.filter(item => item.init.method === 'POST').length, 1);
  reachable = false;
  await assert.rejects(client.uploadSorDocument('p', new File(['%PDF-synthetic'], 'source.pdf', { type: 'application/pdf' })), /import reply lost/);
  assert.ok(!JSON.stringify([...values.values()]).includes('%PDF-synthetic'));
  reachable = true; client = create();
  const recoveredDocument = await client.recoverPendingEntryCommands('p');
  assert.equal(recoveredDocument.recovered[0].result.id, 'd');
  assert.equal(documents.size, 1); assert.equal(values.size, 0);
  assert.equal(requests.filter(item => item.init.method === 'POST').length, 2);
});
