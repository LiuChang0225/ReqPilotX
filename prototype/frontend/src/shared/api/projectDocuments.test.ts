import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, requestWithTransport } from './client';
import { createProjectDocumentClient, projectDocumentPaths } from './projectDocuments';

afterEach(() => vi.unstubAllGlobals());
describe('one project/document client across hosts', () => {
  it('standalone API and embedded transport emit the same project payload', async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response('{"id":"created"}', { headers: { 'content-type': 'application/json' } }));
    vi.stubGlobal('fetch', fetcher);
    const embedded = createProjectDocumentClient((path, init) => requestWithTransport(fetch, '/pair' + path, init));
    await api.createProject('Project A', 'Customer B'); await embedded.createProject('Project A', 'Customer B');
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/projects', '/pair/api/projects']);
    expect(fetcher.mock.calls[0][1].body).toEqual(fetcher.mock.calls[1][1].body);
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ name: 'Project A', customer_name: 'Customer B' });
  });
  it('preserves multipart file and cancellation while host controls routing', async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response('{"id":"doc"}', { headers: { 'content-type': 'application/json' } })); vi.stubGlobal('fetch', fetcher);
    const client = createProjectDocumentClient((path, init) => requestWithTransport(fetch, '/pair' + path, init));
    const file = new File(['original'], 'source.pdf', { type: 'application/pdf' }); const signal = new AbortController().signal;
    await client.uploadSorDocument('project/one', file, { signal });
    const [path, init] = fetcher.mock.calls[0]; expect(path).toBe('/pair/api/projects/project%2Fone/sor-documents');
    expect(init.body.get('file')).toBe(file); expect(init.signal).toBe(signal); expect(init.headers.has('content-type')).toBe(false);
    expect(() => client.uploadSorDocument('p', file, { onProgress: () => {} })).toThrow('upload-progress');
  });
  it('forwards native upload-progress transport without changing its contract', async () => {
    const upload = vi.fn().mockResolvedValue({ id: 'doc' }); const request = vi.fn();
    const client = createProjectDocumentClient(request, upload); const onProgress = vi.fn(); const file = new File(['x'], 'x.pdf');
    await client.uploadSorDocument('p', file, { onProgress }); expect(upload.mock.calls[0][2]).toEqual({ onProgress }); expect(upload.mock.calls[0][1].get('file')).toBe(file); expect(request).not.toHaveBeenCalled();
  });
  it('shares escaped document/source/work paths without losing identity', () => {
    expect(projectDocumentPaths.source('p/a', 'd b')).toBe('/api/projects/p%2Fa/sor-documents/d%20b/file');
    expect(projectDocumentPaths.works('p/a', 'd b')).toBe('/api/projects/p%2Fa/sor-documents/d%20b/agent-structure/delivery/plugin-workbench/sessions');
  });
});
