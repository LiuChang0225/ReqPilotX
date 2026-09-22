import type { JsonRecord, Project, SorDocument } from './contracts';
import type { UploadTransportOptions } from './uploadTransport';

export type ProjectDocumentRequest = <T>(path: string, init?: RequestInit) => Promise<T>;
export type ProjectDocumentUpload = <T>(path: string, form: FormData, options?: UploadTransportOptions) => Promise<T>;

/** Canonical routes shared by the standalone app and embedded host adapters. */
export const projectDocumentPaths = {
  projects: '/api/projects',
  documents: (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/sor-documents`,
  document: (projectId: string, documentId: string) => `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}`,
  source: (projectId: string, documentId: string) => `${projectDocumentPaths.document(projectId, documentId)}/file`,
  works: (projectId: string, documentId: string) => `${projectDocumentPaths.document(projectId, documentId)}/agent-structure/delivery/plugin-workbench/sessions`,
};

/** Business requests have one owner; hosts supply only JSON and upload transports. */
export function createProjectDocumentClient(request: ProjectDocumentRequest, upload?: ProjectDocumentUpload) {
  return {
    projects: () => request<Project[]>(projectDocumentPaths.projects),
    createProject: (name: string, customerName: string) => request<Project>(projectDocumentPaths.projects, {
      method: 'POST', body: JSON.stringify({ name, customer_name: customerName }),
    }),
    sorDocuments: (projectId: string) => request<SorDocument[]>(projectDocumentPaths.documents(projectId)),
    uploadSorDocument: (projectId: string, file: File, options: UploadTransportOptions = {}) => {
      const form = new FormData(); form.append('file', file);
      if (upload) return upload<SorDocument>(projectDocumentPaths.documents(projectId), form, options);
      if (options.onProgress || options.onTransferred) throw new Error('This host has not supplied an upload-progress transport.');
      return request<SorDocument>(projectDocumentPaths.documents(projectId), { method: 'POST', body: form, signal: options.signal });
    },
    deleteSorDocument: (projectId: string, documentId: string) => request<JsonRecord>(projectDocumentPaths.document(projectId, documentId), { method: 'DELETE' }),
  };
}
