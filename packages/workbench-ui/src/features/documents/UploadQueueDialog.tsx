import { Dialog } from "../../shared/ui/Dialog";
import { FileName } from "../../shared/ui/FileName";
import { Icon } from "../../shared/ui/Icon";
import { formatBytes, cx } from "../../shared/lib/format";
import type { UploadQueueItem } from "./uploadQueue";

const statusCopy = {
  queued: "等待上传",
  uploading: "正在上传",
  verifying: "服务端校验中",
  succeeded: "已保存",
  failed: "上传失败",
  cancelled: "已取消",
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
  const active = items.filter((item) =>
    ["queued", "uploading", "verifying"].includes(item.status),
  ).length;
  const completed = items.filter((item) => item.status === "succeeded").length;

  return (
    <Dialog
      open={open}
      title="文件上传"
      className="upload-queue-dialog"
      onClose={onClose}
    >
      <header>
        <div>
          <p className="section-label">文件上传</p>
          <h2>本批次 {items.length} 份文件</h2>
          <p>
            {active
              ? `正在处理 ${active} 份；单项失败不会中断其余文件。`
              : `已保存 ${completed} 份，可关闭窗口继续工作。`}
          </p>
        </div>
        <button
          className="icon-button"
          type="button"
          aria-label="关闭上传窗口"
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
                {item.projectId !== currentProjectId && "其他项目 · "}
                {formatBytes(item.file.size)} · {statusCopy[item.status]}
              </small>
              {["uploading", "verifying"].includes(item.status) && (
                <div
                  className={cx(
                    "upload-progress",
                    item.status === "verifying" && "is-indeterminate",
                  )}
                  role="progressbar"
                  aria-label={`${item.file.name} 上传进度`}
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
                  取消
                </button>
              )}
              {item.status === "verifying" && (
                <span className="upload-locked-state">已传输</span>
              )}
              {["failed", "cancelled"].includes(item.status) && (
                <button
                  className="text-button"
                  type="button"
                  onClick={() => onRetry(item.id)}
                >
                  重试
                </button>
              )}
              {["failed", "cancelled", "succeeded"].includes(item.status) && (
                <button
                  className="icon-button"
                  type="button"
                  aria-label={`移除 ${item.file.name}`}
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
        <span>文件传输完成后，解析与知识索引会在后台继续。</span>
        <div>
          <button
            className="button ghost"
            type="button"
            onClick={onClearFinished}
          >
            清理已完成
          </button>
          <button className="button primary" type="button" onClick={onClose}>
            {active ? "在后台继续" : "完成"}
          </button>
        </div>
      </footer>
    </Dialog>
  );
}
