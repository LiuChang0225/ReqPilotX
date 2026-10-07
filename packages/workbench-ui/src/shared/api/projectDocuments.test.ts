import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { webcrypto } from 'node:crypto';
import { api, requestWithTransport } from './client';
import { createProjectDocumentClient, projectDocumentPaths } from './projectDocuments';

beforeEach(() => { localStorage.clear(); vi.stubGlobal('crypto', webcrypto); });
afterEach(() => { localStorage.clear(); vi.unstubAllGlobals(); });
describe('one project/document client across hosts', () => {
  it('standalone API and embedded transport emit the same project payload', async () => {
    const fetcher = vi.fn().mockImplementation(async (_path, init) => new Response(JSON.stringify({ ...JSON.parse(init.body), id: 'created' }), { headers: { 'content-type': 'application/json' } }));
    vi.stubGlobal('fetch', fetcher);
    const embedded = createProjectDocumentClient((path, init) => requestWithTransport(fetch, '/pair' + path, init));
    await api.createProject('Project A', 'Customer B'); await embedded.createProject('Project A', 'Customer B');
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/projects', '/pair/api/projects']);
    const [standalone, embeddedBody] = fetcher.mock.calls.map(call => JSON.parse(call[1].body));
    expect(standalone).toEqual({ name: 'Project A', customer_name: 'Customer B', command_id: expect.any(String) });
    expect(embeddedBody).toEqual({ ...standalone, command_id: expect.any(String) });
    expect(standalone.command_id).not.toEqual(embeddedBody.command_id);
  });
  it('preserves multipart file and cancellation while host controls routing', async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ id: 'doc', project_id: 'project/one', file_name: 'source.pdf', source_size: 8, source_sha256: '0682c5f2076f099c34cfdd15a9e063849ed437a49677e6fcc5b4198c76575be5' }), { headers: { 'content-type': 'application/json' } })); vi.stubGlobal('fetch', fetcher);
    const client = createProjectDocumentClient((path, init) => requestWithTransport(fetch, '/pair' + path, init));
    const file = new File(['original'], 'source.pdf', { type: 'application/pdf' }); const signal = new AbortController().signal;
    await client.uploadSorDocument('project/one', file, { signal });
    const [path, init] = fetcher.mock.calls[0]; expect(path).toBe('/pair/api/projects/project%2Fone/sor-documents');
    expect(init.body.get('file')).toBe(file); expect(init.body.get('command_id')).toEqual(expect.any(String)); expect(init.signal).toBe(signal); expect(init.headers.has('content-type')).toBe(false);
    expect(() => client.uploadSorDocument('p', file, { onProgress: () => {} })).toThrow('upload-progress');
  });
  it('forwards native upload-progress transport without changing its contract', async () => {
    const upload = vi.fn().mockResolvedValue({ id: 'doc', project_id: 'p', file_name: 'x.pdf', source_size: 1, source_sha256: '2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881' }); const request = vi.fn();
    const client = createProjectDocumentClient(request, upload); const onProgress = vi.fn(); const file = new File(['x'], 'x.pdf');
    await client.uploadSorDocument('p', file, { onProgress }); expect(upload.mock.calls[0][2]).toEqual({ onProgress }); expect(upload.mock.calls[0][1].get('file')).toBe(file); expect(request).not.toHaveBeenCalled();
  });
  it('persists command before submission and recovers a lost commit reply', async () => {
    let payload: Record<string, string>;
    const request = vi.fn().mockImplementation(async (_path, init) => {
      if (init?.method === 'POST') { payload = JSON.parse(init.body); throw Error('lost reply'); }
      return { id: 'saved', name: payload.name, customer_name: payload.customer_name };
    });
    const client = createProjectDocumentClient(request);
    expect(await client.createProject(' Project ', 'Customer')).toEqual({ id: 'saved', name: 'Project', customer_name: 'Customer' });
    expect(request.mock.calls[1][0]).toBe(projectDocumentPaths.projectCommand(payload!.command_id));
    expect(client.pendingEntryCommands()).toEqual([]);
  });
  it('refresh retains unknown command and retry queries then replays the original ID', async () => {
    const submitted: string[] = [];
    let online = false;
    const request = vi.fn().mockImplementation(async (_path, init) => {
      if (!init) throw Object.assign(Error('not found'), { status: online ? 404 : 503, payload: { detail: 'entry_command_not_found' } });
      const body = JSON.parse(init.body); submitted.push(body.command_id);
      if (!online) throw Error('disconnected');
      return { id: 'saved', ...body };
    });
    await expect(createProjectDocumentClient(request).createProject('P', 'C')).rejects.toThrow('disconnected');
    const refreshed = createProjectDocumentClient(request);
    expect(refreshed.pendingEntryCommands()).toHaveLength(1);
    online = true;
    await refreshed.createProject('P', 'C');
    expect(submitted).toHaveLength(2); expect(submitted[0]).toBe(submitted[1]);
  });
  it('coalesces double submits and allows a new intentional creation after success', async () => {
    let resolve!: (value: unknown) => void;
    const request = vi.fn().mockImplementation((_path, init) => new Promise(done => { resolve = () => done({ id: 'saved', ...JSON.parse(init.body) }); }));
    const client = createProjectDocumentClient(request);
    const a = client.createProject('P', 'C'), b = client.createProject('P', 'C');
    expect(a).toBe(b); expect(request).toHaveBeenCalledTimes(1);
    resolve(undefined); await a;
    const c = client.createProject('P', 'C'); resolve(undefined); await c;
    expect(JSON.parse(request.mock.calls[0][1].body).command_id).not.toBe(JSON.parse(request.mock.calls[1][1].body).command_id);
  });
  it('retains a newer same-payload command when an older recovery returns late', async () => {
    const offline = vi.fn().mockRejectedValue(Object.assign(Error('offline'), { status: 503 }));
    const first = createProjectDocumentClient(offline);
    await expect(first.createProject('P', 'C')).rejects.toThrow();
    const old = first.pendingEntryCommands()[0];
    let finish!: (result: unknown) => void;
    const slow = createProjectDocumentClient(vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve; })));
    const late = slow.recoverPendingEntryCommands();
    const result = { id: 'saved-old', name: 'P', customer_name: 'C' };
    await createProjectDocumentClient(vi.fn().mockResolvedValue(result)).recoverPendingEntryCommands();
    const next = createProjectDocumentClient(offline);
    await expect(next.createProject('P', 'C')).rejects.toThrow();
    const newer = next.pendingEntryCommands()[0];
    expect(newer.commandId).not.toBe(old.commandId);
    finish(result); await late;
    expect(next.pendingEntryCommands()).toEqual([newer]);
  });
  it('does not submit or persist an upload cancelled while hashing bytes', async () => {
    const controller = new AbortController(), upload = vi.fn(), request = vi.fn();
    const file = new File(['x'], 'x.pdf');
    let finish!: (bytes: ArrayBuffer) => void;
    Object.defineProperty(file, 'arrayBuffer', { value: () => new Promise<ArrayBuffer>(resolve => { finish = resolve; }) });
    const client = createProjectDocumentClient(request, upload);
    const operation = client.uploadSorDocument('p', file, { signal: controller.signal });
    controller.abort(); finish(new Uint8Array([120]).buffer);
    await expect(operation).rejects.toMatchObject({ name: 'AbortError' });
    expect(upload).not.toHaveBeenCalled(); expect(request).not.toHaveBeenCalled();
    expect(client.pendingEntryCommands()).toEqual([]);
  });
  it('preserves cancellation and the command when a transferred upload outcome is unknown', async () => {
    const request = vi.fn().mockRejectedValue(Object.assign(Error('offline'), { status: 503 }));
    const upload = vi.fn().mockRejectedValue(new DOMException('cancelled', 'AbortError'));
    const client = createProjectDocumentClient(request, upload);
    await expect(client.uploadSorDocument('p', new File(['x'], 'x.pdf'))).rejects.toMatchObject({ name: 'AbortError', message: expect.stringContaining('保存结果尚未确认') });
    expect(client.pendingEntryCommands()).toHaveLength(1);
    expect(request).toHaveBeenCalledTimes(1);
  });
  it('keeps conflict and deleted commands pending without treating them as transport failures', async () => {
    for (const status of [409, 410]) {
      localStorage.clear();
      const request = vi.fn().mockRejectedValue(Object.assign(Error('conflict'), { status }));
      const client = createProjectDocumentClient(request);
      await expect(client.createProject('P', 'C')).rejects.toThrow('conflict');
      expect(request).toHaveBeenCalledTimes(1); expect(client.pendingEntryCommands()).toHaveLength(1);
    }
  });
  it('fails before sending if durable storage is denied', async () => {
    const request = vi.fn(), storage = { getItem: () => null, setItem: () => { throw Error('storage denied'); }, removeItem: vi.fn(), key: () => null, length: 0 };
    const client = createProjectDocumentClient(request, undefined, { storage });
    expect(() => client.createProject('P', 'C')).toThrow('storage denied'); expect(request).not.toHaveBeenCalled();
  });
  it('refuses a damaged pending command rather than sending a legacy write without its ID', async () => {
    const offline = createProjectDocumentClient(vi.fn().mockRejectedValue(Error('offline')));
    await expect(offline.createProject('P', 'C')).rejects.toThrow();
    const key = localStorage.key(0)!;
    const damaged = JSON.parse(localStorage.getItem(key)!);
    delete damaged.commandId; localStorage.setItem(key, JSON.stringify(damaged));
    const request = vi.fn().mockImplementation(async (_path, init) => {
      if (!init) throw Object.assign(Error('absent'), { status: 404, payload: { detail: 'entry_command_not_found' } });
      return { id: 'legacy', name: 'P', customer_name: 'C' };
    });
    const client = createProjectDocumentClient(request);
    await expect(Promise.resolve().then(() => client.createProject('P', 'C'))).rejects.toThrow('提交记录无效');
    expect(request).not.toHaveBeenCalled();
  });
  it('isolates pending intentions by stable application identity across endpoint changes', async () => {
    const request = vi.fn().mockRejectedValue(Error('offline'));
    await expect(createProjectDocumentClient(request, undefined, { namespace: 'owner-one' }).createProject('P', 'C')).rejects.toThrow();
    expect(createProjectDocumentClient(request, undefined, { namespace: 'owner-two' }).pendingEntryCommands()).toEqual([]);
    expect(createProjectDocumentClient(request, undefined, { namespace: 'owner-one' }).pendingEntryCommands()).toHaveLength(1);
  });
  it('hashes bytes so different files with identical metadata use distinct commands', async () => {
    const upload = vi.fn().mockRejectedValue(Object.assign(Error('rejected'), { status: 422 }));
    const client = createProjectDocumentClient(vi.fn(), upload);
    for (const text of ['a', 'b']) await expect(client.uploadSorDocument('p', new File([text], 'same.pdf', { lastModified: 1 }))).rejects.toThrow('rejected');
    expect(upload.mock.calls[0][1].get('command_id')).not.toBe(upload.mock.calls[1][1].get('command_id'));
  });
  it('reselected identical bytes reuse the import command after an unknown result', async () => {
    let online = false;
    const request = vi.fn().mockRejectedValue(Object.assign(Error('not found'), { status: 404, payload: { detail: 'entry_command_not_found' } }));
    const upload = vi.fn().mockImplementation(async () => {
      if (!online) throw Error('lost reply');
      return { id: 'doc', project_id: 'p', file_name: 'x.pdf', source_size: 1, source_sha256: '2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881' };
    });
    await expect(createProjectDocumentClient(request, upload).uploadSorDocument('p', new File(['x'], 'x.pdf'))).rejects.toThrow('保存结果尚未确认');
    online = true;
    await createProjectDocumentClient(request, upload).uploadSorDocument('p', new File(['x'], 'x.pdf'));
    expect(upload.mock.calls[0][1].get('command_id')).toBe(upload.mock.calls[1][1].get('command_id'));
  });
  it('does not replay against an older server whose query route is missing', async () => {
    const request = vi.fn().mockImplementation(async (_path, init) => {
      if (init) throw Error('lost reply');
      throw Object.assign(Error('Not Found'), { status: 404, payload: { detail: 'Not Found' } });
    });
    const first = createProjectDocumentClient(request);
    await expect(first.createProject('P', 'C')).rejects.toThrow('尚未确认');
    await expect(createProjectDocumentClient(request).createProject('P', 'C')).rejects.toThrow('Not Found');
    expect(request.mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(1);
  });
  it('refuses a receipt for a different payload and retains the original intention', async () => {
    const request = vi.fn().mockImplementation(async (_path, init) => {
      if (init) throw Error('lost reply');
      return { id: 'unrelated', name: 'Other', customer_name: 'Customer' };
    });
    await expect(createProjectDocumentClient(request).createProject('P', 'C')).rejects.toThrow('尚未确认');
    const recovery = await createProjectDocumentClient(request).recoverPendingEntryCommands();
    expect(recovery.failures[0].error).toEqual(expect.objectContaining({ message: expect.stringContaining('回执与原请求不一致') }));
    expect(createProjectDocumentClient(request).pendingEntryCommands()).toHaveLength(1);
  });
  it('recovers independent saved commands even if another pending result is deleted', async () => {
    const client = createProjectDocumentClient(vi.fn().mockRejectedValue(Object.assign(Error('offline'), { status: 503 })));
    await expect(client.createProject('Deleted', 'C')).rejects.toThrow();
    await expect(client.createProject('Saved', 'C')).rejects.toThrow();
    const pending = client.pendingEntryCommands();
    const request = vi.fn().mockImplementation(async path => {
      if (path.endsWith(pending[0].commandId)) throw Object.assign(Error('deleted'), { status: 410 });
      return { id: 'saved', name: 'Saved', customer_name: 'C' };
    });
    const recovery = await createProjectDocumentClient(request).recoverPendingEntryCommands();
    expect(recovery.recovered[0].result.id).toBe('saved'); expect(recovery.failures).toHaveLength(1);
    expect(createProjectDocumentClient(request).pendingEntryCommands()).toHaveLength(1);
  });
  it('shares escaped document/source/work paths without losing identity', () => {
    expect(projectDocumentPaths.source('p/a', 'd b')).toBe('/api/projects/p%2Fa/sor-documents/d%20b/file');
    expect(projectDocumentPaths.works('p/a', 'd b')).toBe('/api/projects/p%2Fa/sor-documents/d%20b/agent-structure/delivery/plugin-workbench/sessions');
  });
});
