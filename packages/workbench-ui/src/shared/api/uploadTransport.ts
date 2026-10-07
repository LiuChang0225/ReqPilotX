export interface UploadTransportOptions {
  signal?: AbortSignal;
  onProgress?: (progress: number) => void;
  onTransferred?: () => void;
}

export function uploadWithProgress<T>(
  path: string,
  form: FormData,
  options: UploadTransportOptions = {},
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    xhr.open("POST", path);
    xhr.responseType = "json";
    xhr.setRequestHeader("Accept", "application/json");
    xhr.withCredentials = true;

    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      options.onProgress?.(
        Math.min(100, Math.round((event.loaded / event.total) * 100)),
      );
    });
    xhr.upload.addEventListener("load", () => {
      options.onProgress?.(100);
      options.onTransferred?.();
    });
    xhr.addEventListener("load", () => {
      options.signal?.removeEventListener("abort", abort);
      const payload =
        xhr.response ??
        (() => {
          try {
            return JSON.parse(xhr.responseText);
          } catch {
            return xhr.responseText;
          }
        })();
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload as T);
        return;
      }
      const detail =
        payload && typeof payload === "object"
          ? payload.detail ?? payload.message ?? payload.error
          : payload;
      reject(
        Object.assign(new Error(
          typeof detail === "string" && detail
            ? detail
            : `上传失败（HTTP ${xhr.status}）`,
        ), { status: xhr.status }),
      );
    });
    xhr.addEventListener("error", () => {
      options.signal?.removeEventListener("abort", abort);
      reject(new Error("网络连接中断，请检查本地服务后重试。"));
    });
    xhr.addEventListener("abort", () => {
      options.signal?.removeEventListener("abort", abort);
      reject(new DOMException("上传已取消", "AbortError"));
    });

    if (options.signal?.aborted) {
      reject(new DOMException("上传已取消", "AbortError"));
      return;
    }
    options.signal?.addEventListener("abort", abort, { once: true });
    xhr.send(form);
  });
}
