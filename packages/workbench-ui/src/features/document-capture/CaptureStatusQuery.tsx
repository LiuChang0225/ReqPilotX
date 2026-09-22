import { useEffect, useState } from "react";
import { ApiError, asRecord } from "../../shared/api/client";
import { captureApi } from "./captureApi";
import { CaptureStatus } from "./CaptureStatus";
import {
  normalizeCaptureQualityManifest,
  type CaptureQualityManifestV1,
} from "./contracts";

interface CaptureStatusQueryProps {
  projectId: string;
  documentId: string;
  sourceSha256: string;
  currentPage: number;
  onPage?: (page: number) => void;
  className?: string;
}

function captureNotPrepared(cause: unknown): boolean {
  if (!(cause instanceof ApiError)) return false;
  if (cause.status === 404) return true;
  if (cause.status !== 409) return false;
  const payload = asRecord(cause.payload);
  const detail = asRecord(payload.detail);
  return String(detail.reason_code || payload.reason_code || payload.code || "")
    .toLowerCase() === "source_capture_not_prepared";
}

export function CaptureStatusQuery({
  projectId,
  documentId,
  sourceSha256,
  currentPage,
  onPage,
  className,
}: CaptureStatusQueryProps) {
  const [manifest, setManifest] = useState<CaptureQualityManifestV1 | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setManifest(null);
    setError("");
    if (!projectId || !documentId || !/^[0-9a-f]{64}$/i.test(sourceSha256)) {
      setLoading(false);
      setError("中立捕获尚未准备；当前只显示已有解析事实。");
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    void captureApi
      .quality(projectId, documentId, controller.signal)
      .then((payload) => {
        if (controller.signal.aborted) return;
        const normalized = normalizeCaptureQualityManifest(payload);
        if (
          !normalized ||
          normalized.project_id !== projectId ||
          normalized.sor_document_id !== documentId ||
          normalized.source_sha256 !== sourceSha256.toLowerCase()
        ) {
          throw new Error("中立捕获清单与当前文档身份不一致，已阻止显示。");
        }
        setManifest(normalized);
      })
      .catch((cause) => {
        if (controller.signal.aborted) return;
        setError(
          captureNotPrepared(cause)
            ? "中立捕获尚未准备；当前只显示已有解析事实。"
            : cause instanceof Error
              ? cause.message
              : "中立捕获状态暂时无法读取。",
        );
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [documentId, projectId, sourceSha256]);

  return (
    <CaptureStatus
      className={className}
      manifest={manifest}
      loading={loading}
      error={error}
      currentPage={currentPage}
      onPage={onPage}
      readOnly
    />
  );
}
