import type { JsonRecord, Project, SorDocument } from './contracts';
import type { UploadTransportOptions } from './uploadTransport';
import { entryCommandStore, fileSha256, type EntryCommandOptions, type EntryIntent } from './entryCommands';

export type ProjectDocumentRequest = <T>(path: string, init?: RequestInit) => Promise<T>;
export type ProjectDocumentUpload = <T>(path: string, form: FormData, options?: UploadTransportOptions) => Promise<T>;

/** Canonical routes shared by the standalone app and embedded host adapters. */
export const projectDocumentPaths = {
  projects: '/api/projects',
  projectCommand: (commandId: string) => `/api/projects/creation-commands/${encodeURIComponent(commandId)}`,
  documents: (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/sor-documents`,
  importCommand: (projectId: string, commandId: string) => `${projectDocumentPaths.documents(projectId)}/import-commands/${encodeURIComponent(commandId)}`,
  document: (projectId: string, documentId: string) => `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}`,
  source: (projectId: string, documentId: string) => `${projectDocumentPaths.document(projectId, documentId)}/file`,
  works: (projectId: string, documentId: string) => `${projectDocumentPaths.document(projectId, documentId)}/agent-structure/delivery/plugin-workbench/sessions`,
};

/** Business requests have one owner; hosts supply only JSON and upload transports. */
export function createProjectDocumentClient(request: ProjectDocumentRequest, upload?: ProjectDocumentUpload, commandOptions: EntryCommandOptions = {}) {
  const store = entryCommandStore(commandOptions);
  const inFlight = new Map<string, Promise<Project | SorDocument>>();
  const pathFor = (intent: EntryIntent) => intent.kind === 'project.create'
    ? projectDocumentPaths.projectCommand(intent.commandId) : projectDocumentPaths.importCommand(intent.scope, intent.commandId);
  const matches = (intent: EntryIntent, result: Project | SorDocument) => {
    if (!result?.id) return false;
    if (intent.kind === 'project.create') {
      const project = result as Project;
      return project.name === intent.payload.name && project.customer_name === intent.payload.customer_name;
    }
    const document = result as SorDocument;
    return document.project_id === intent.scope && document.file_name === intent.payload.file_name
      && document.source_sha256 === intent.payload.source_sha256 && document.source_size === intent.payload.source_size;
  };
  const recover = async (intent: EntryIntent) => {
    let result: Project | SorDocument;
    try { result = await request<Project | SorDocument>(pathFor(intent)); }
    catch (cause) {
      const error = cause as { status?: number; payload?: { detail?: unknown } };
      // A route missing on an older server is not an absent command receipt.
      if (error.status === 404 && error.payload?.detail === 'entry_command_not_found') return null;
      throw cause;
    }
    if (!matches(intent, result)) throw new Error('提交回执与原请求不一致，请核对原提交。');
    store.complete(intent);
    return result;
  };
  const submit = <T extends Project | SorDocument>(kind: EntryIntent['kind'], scope: string, payload: EntryIntent['payload'], send: (id: string) => Promise<T>): Promise<T> => {
    const fingerprint = JSON.stringify([kind, scope, payload]);
    const previous = inFlight.get(fingerprint);
    if (previous) return previous as Promise<T>;
    const { intent, retry } = store.acquire(kind, scope, payload);
    const operation = (async () => {
      if (retry) {
        const saved = await recover(intent);
        if (saved) return saved as T;
      }
      try {
        const result = await send(intent.commandId);
        if (!matches(intent, result)) throw new Error('提交响应与原请求不一致，请核对原提交。');
        store.complete(intent);
        return result;
      } catch (cause) {
        const status = (cause as { status?: number }).status;
        if (status === undefined || status === 0 || status === 408 || status >= 500) {
          try { const saved = await recover(intent); if (saved) return saved as T; }
          catch { /* Preserve the intention when querying is also uncertain. */ }
          const error = new Error(`${cause instanceof Error ? cause.message : String(cause)} 保存结果尚未确认，请使用原请求重试或重新连接后核对。`, { cause });
          if ((cause as { name?: string }).name === 'AbortError') error.name = 'AbortError';
          throw error;
        }
        throw cause;
      }
    })().finally(() => inFlight.delete(fingerprint));
    inFlight.set(fingerprint, operation);
    return operation;
  };
  return {
    pendingEntryCommands: store.pending,
    async recoverPendingEntryCommands(projectId?: string) {
      const recovered: { intent: EntryIntent; result: Project | SorDocument }[] = [];
      const failures: { intent: EntryIntent; error: unknown }[] = [];
      for (const intent of store.pending()) {
        if (intent.kind === 'document.import' && intent.scope !== projectId) continue;
        try {
          const result = await recover(intent);
          if (result) recovered.push({ intent, result });
        } catch (error) { failures.push({ intent, error }); }
      }
      return { recovered, failures };
    },
    projectCreationCommand: (commandId: string) => request<Project>(projectDocumentPaths.projectCommand(commandId)),
    documentImportCommand: (projectId: string, commandId: string) => request<SorDocument>(projectDocumentPaths.importCommand(projectId, commandId)),
    projects: () => request<Project[]>(projectDocumentPaths.projects),
    createProject: (name: string, customerName: string) => {
      const payload = { name: name.trim(), customer_name: customerName.trim() };
      return submit('project.create', '', payload, commandId => request<Project>(projectDocumentPaths.projects, {
        method: 'POST', body: JSON.stringify({ ...payload, command_id: commandId }),
      }));
    },
    sorDocuments: (projectId: string) => request<SorDocument[]>(projectDocumentPaths.documents(projectId)),
    uploadSorDocument: (projectId: string, file: File, options: UploadTransportOptions = {}) => {
      if (!upload && (options.onProgress || options.onTransferred)) throw new Error('This host has not supplied an upload-progress transport.');
      if (options.signal?.aborted) return Promise.reject(new DOMException('上传已取消', 'AbortError'));
      return fileSha256(file).then(sourceSha => {
        if (options.signal?.aborted) throw new DOMException('上传已取消', 'AbortError');
        return submit('document.import', projectId,
        { file_name: file.name, source_sha256: sourceSha, source_size: file.size, media_type: file.type || 'application/octet-stream' }, commandId => {
          const form = new FormData(); form.append('file', file); form.append('command_id', commandId);
          if (upload) return upload<SorDocument>(projectDocumentPaths.documents(projectId), form, options);
          return request<SorDocument>(projectDocumentPaths.documents(projectId), { method: 'POST', body: form, signal: options.signal });
        });
      });
    },
    deleteSorDocument: (projectId: string, documentId: string) => request<JsonRecord>(projectDocumentPaths.document(projectId, documentId), { method: 'DELETE' }),
  };
}
