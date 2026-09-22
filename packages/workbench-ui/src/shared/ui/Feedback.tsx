import type { PropsWithChildren, ReactNode } from "react";
import { Icon } from "./Icon";
import { cx } from "../lib/format";

export function StatusBadge({
  tone = "neutral",
  children,
}: PropsWithChildren<{
  tone?: "neutral" | "success" | "warning" | "danger" | "info";
}>) {
  return <span className={cx("status-badge", `is-${tone}`)}>{children}</span>;
}

export function LoadingBlock({ label = "正在加载数据" }: { label?: string }) {
  return (
    <div className="loading-block" role="status" aria-live="polite">
      <span className="loading-mark" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function EmptyState({
  icon = "document",
  title,
  description,
  action,
}: {
  icon?: Parameters<typeof Icon>[0]["name"];
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-state-icon">
        <Icon name={icon} size={24} />
      </span>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}

export function ErrorBlock({
  error,
  onRetry,
}: {
  error: Error;
  onRetry?: () => void;
}) {
  return (
    <div className="error-block" role="alert">
      <div>
        <strong>暂时无法完成请求</strong>
        <p>{error.message}</p>
      </div>
      {onRetry && (
        <button className="button secondary" type="button" onClick={onRetry}>
          <Icon name="refresh" />
          重试
        </button>
      )}
    </div>
  );
}

export function InlineNotice({
  tone = "info",
  title,
  children,
}: PropsWithChildren<{
  tone?: "info" | "warning" | "danger" | "success";
  title?: string;
}>) {
  return (
    <div className={cx("inline-notice", `is-${tone}`)}>
      <span className="notice-pip" aria-hidden="true" />
      <div>
        {title && <strong>{title}</strong>}
        <div>{children}</div>
      </div>
    </div>
  );
}
