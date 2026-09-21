import { request } from "../../shared/api/client";
import type {
  CaptureQualityManifestV1,
  VisualPageCaptureV1,
} from "./contracts";

function captureRoot(projectId: string, documentId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/source-capture`;
}

export const captureApi = {
  quality: (
    projectId: string,
    documentId: string,
    signal?: AbortSignal,
  ) =>
    request<CaptureQualityManifestV1>(
      `${captureRoot(projectId, documentId)}/quality`,
      { signal },
    ),
  visualPage: (
    projectId: string,
    documentId: string,
    pageNumber: number,
    signal?: AbortSignal,
  ) =>
    request<VisualPageCaptureV1>(
      `${captureRoot(projectId, documentId)}/visual/pages/${encodeURIComponent(String(pageNumber))}`,
      { signal },
    ),
  resumeVisualPage: (
    projectId: string,
    documentId: string,
    pageNumber: number,
    signal?: AbortSignal,
  ) =>
    request<VisualPageCaptureV1>(
      `${captureRoot(projectId, documentId)}/visual/pages/${encodeURIComponent(String(pageNumber))}`,
      { method: "POST", signal },
    ),
};
