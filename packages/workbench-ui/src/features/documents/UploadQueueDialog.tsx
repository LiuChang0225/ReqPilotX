import { Dialog } from "../../shared/ui/Dialog";
import { FileName } from "../../shared/ui/FileName";
import { Icon } from "../../shared/ui/Icon";
import { formatBytes, cx } from "../../shared/lib/format";
import type { UploadQueueItem } from "./uploadQueue";
import { useUiCopy } from "../../host/HostLocaleContext";

const statusCopy = {
  queued: ["upload.queued", "等待上传"],
  uploading: ["upload.uploading", "正在上传"],
  verifying: ["upload.verifying", "服务端校验中"],
  succeeded: ["upload.succeeded", "已保存"],
  failed: ["upload.failed", "上传失败"],
  cancelled: ["upload.cancelled", "已取消"],
} as const;

export function UploadQueueDialog({
  open,
  items,
  currentProjectId,
  onClose,
  onCancel,
  onRetry,
  onRemove,
  onClearFinished,
}: {
  open: boolean;
  items: UploadQueueItem[];
  currentProjectId: string;
  onClose: () => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onRemove: (id: string) => void;
  onClearFinished: () => void;
}) {
  const l = useUiCopy();
  const active = items.filter((item) =>
    ["queued", "uploading", "verifying"].includes(item.status),
  ).length;
  const completed = items.filter((item) => item.status === "succeeded").length;

  return (
    <Dialog
      open={open}
      title={l("upload.title", "文件上传")}
      className="upload-queue-dialog"
      onClose={onClose}
    >
      <header>
        <div>
          <p className="section-label">{l("upload.title", "文件上传")}</p>
          <h2>{l(items.length === 1 ? "upload.batchOne" : "upload.batchMany", items.length === 1 ? "本批次 1 份文件" : "本批次 {count} 份文件", { count: items.length })}</h2>
          <p>
            {active
              ? l("upload.processing", "正在处理 {count} 份；单项失败不会中断其余文件。", { count: active })
              : l(completed === 1 ? "upload.savedOne" : "upload.savedMany", completed === 1 ? "已保存 1 份，可关闭窗口继续工作。" : "已保存 {count} 份，可关闭窗口继续工作。", { count: completed })}
          </p>
        </div>
        <button
          className="icon-button"
          type="button"
          aria-label={l("upload.closeQueue", "关闭上传窗口")}
          onClick={onClose}
        >
          <Icon name="close" />
        </button>
      </header>

      <div className="upload-queue-list" aria-live="polite">
        {items.map((item) => (
          <article
            key={item.id}
            className={cx("upload-queue-row", `is-${item.status}`)}
          >
            <span className="file-tile">
              {item.kind === "sor" ? "SOR" : "KB"}
            </span>
            <div className="upload-file-copy">
              <FileName value={item.file.name} />
              <small>
                {item.projectId !== currentProjectId && l("upload.otherProject", "其他项目 · ")}
                {formatBytes(item.file.size)} · {l(statusCopy[item.status][0], statusCopy[item.status][1])}
              </small>
              {["uploading", "verifying"].includes(item.status) && (
                <div
                  className={cx(
                    "upload-progress",
                    item.status === "verifying" && "is-indeterminate",
                  )}
                  role="progressbar"
                  aria-label={l("upload.progressAria", "{name} 上传进度", { name: item.file.name })}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={
                    item.status === "uploading" ? item.progress : undefined
                  }
                >
                  <span style={{ width: `${item.progress}%` }} />
                </div>
              )}
              {item.error && <em>{item.error}</em>}
            </div>
            <div className="upload-row-actions">
              {["queued", "uploading"].includes(item.status) && (
                <button
                  className="text-button"
                  type="button"
                  onClick={() => onCancel(item.id)}
                >
                  {l("upload.cancel", "取消")}
                </button>
              )}
              {item.status === "verifying" && (
                <span className="upload-locked-state">{l("upload.transferred", "已传输")}</span>
              )}
              {["failed", "cancelled"].includes(item.status) && (
                <button
                  className="text-button"
                  type="button"
                  onClick={() => onRetry(item.id)}
                >
                  {l("upload.retry", "重试")}
                </button>
              )}
              {["failed", "cancelled", "succeeded"].includes(item.status) && (
                <button
                  className="icon-button"
                  type="button"
                  aria-label={l("upload.removeAria", "移除 {name}", { name: item.file.name })}
                  onClick={() => onRemove(item.id)}
                >
                  <Icon name="close" size={15} />
                </button>
              )}
            </div>
          </article>
        ))}
      </div>

      <footer>
        <span>{l("upload.footer", "文件传输完成后，解析与知识索引会在后台继续。")}</span>
        <div>
          <button
            className="button ghost"
            type="button"
            onClick={onClearFinished}
          >
            {l("upload.clearFinished", "清理已完成")}
          </button>
          <button className="button primary" type="button" onClick={onClose}>
            {active ? l("upload.continueBackground", "在后台继续") : l("upload.done", "完成")}
          </button>
        </div>
      </footer>
    </Dialog>
  );
}
