export type UploadKind = "sor" | "knowledge";
export type UploadStatus =
  | "queued"
  | "uploading"
  | "verifying"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface UploadQueueItem {
  id: string;
  projectId: string;
  kind: UploadKind;
  file: File;
  status: UploadStatus;
  progress: number;
  error: string;
}

export type UploadQueueAction =
  | { type: "append"; items: UploadQueueItem[] }
  | { type: "start"; id: string }
  | { type: "progress"; id: string; progress: number }
  | { type: "verify"; id: string }
  | { type: "succeed"; id: string }
  | { type: "fail"; id: string; error: string }
  | { type: "cancel"; id: string }
  | { type: "retry"; id: string }
  | { type: "remove"; id: string }
  | { type: "clear-finished" };

export const ACCEPTED_UPLOAD_EXTENSIONS = [".pdf", ".docx", ".xlsx", ".txt"];
export const MAX_UPLOAD_FILES = 10;
export const MAX_UPLOAD_BYTES = 128 * 1024 * 1024;

function fileIdentity(kind: UploadKind, file: File): string {
  return `${kind}:${file.name}:${file.size}:${file.lastModified}`;
}

export function createUploadItems(
  projectId: string,
  kind: UploadKind,
  files: File[],
  existing: UploadQueueItem[],
  makeId: () => string = () => crypto.randomUUID(),
): { items: UploadQueueItem[]; rejected: string[] } {
  const rejected: string[] = [];
  const seen = new Set(existing.map((item) => fileIdentity(item.kind, item.file)));
  const items: UploadQueueItem[] = [];
  for (const file of files.slice(0, MAX_UPLOAD_FILES)) {
    const extension = file.name.includes(".")
      ? `.${file.name.split(".").pop()?.toLocaleLowerCase()}`
      : "";
    if (!ACCEPTED_UPLOAD_EXTENSIONS.includes(extension)) {
      rejected.push(`${file.name}：不支持该格式`);
      continue;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      rejected.push(`${file.name}：超过 128 MiB`);
      continue;
    }
    const identity = fileIdentity(kind, file);
    if (seen.has(identity)) {
      rejected.push(`${file.name}：已在队列中`);
      continue;
    }
    seen.add(identity);
    items.push({
      id: makeId(),
      projectId,
      kind,
      file,
      status: "queued",
      progress: 0,
      error: "",
    });
  }
  if (files.length > MAX_UPLOAD_FILES) {
    rejected.push(`一次最多添加 ${MAX_UPLOAD_FILES} 份文件`);
  }
  return { items, rejected };
}

export function uploadQueueReducer(
  state: UploadQueueItem[],
  action: UploadQueueAction,
): UploadQueueItem[] {
  if (action.type === "append") return [...state, ...action.items];
  if (action.type === "remove") {
    return state.filter(
      (item) =>
        item.id !== action.id ||
        ["uploading", "verifying"].includes(item.status),
    );
  }
  if (action.type === "clear-finished") {
    return state.filter((item) =>
      ["queued", "uploading", "verifying"].includes(item.status),
    );
  }
  return state.map((item) => {
    if (item.id !== action.id) return item;
    if (action.type === "start") {
      return { ...item, status: "uploading", progress: 0, error: "" };
    }
    if (action.type === "progress") {
      return { ...item, progress: Math.max(0, Math.min(100, action.progress)) };
    }
    if (action.type === "verify") {
      return { ...item, status: "verifying", progress: 100 };
    }
    if (action.type === "succeed") {
      return { ...item, status: "succeeded", progress: 100, error: "" };
    }
    if (action.type === "fail") {
      return { ...item, status: "failed", error: action.error };
    }
    if (action.type === "cancel") {
      return { ...item, status: "cancelled", error: "" };
    }
    if (action.type === "retry") {
      return { ...item, status: "queued", progress: 0, error: "" };
    }
    return item;
  });
}
