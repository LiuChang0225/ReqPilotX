import type { PropsWithChildren, ReactNode } from "react";
import { Icon } from "./Icon";
import { cx } from "../lib/format";
import { useUiCopy } from "../../host/HostLocaleContext";

export function StatusBadge({
  tone = "neutral",
  children,
}: PropsWithChildren<{
  tone?: "neutral" | "success" | "warning" | "danger" | "info";
}>) {
  return <span className={cx("status-badge", `is-${tone}`)}>{children}</span>;
}

export function LoadingBlock({ label }: { label?: string }) {
  const l = useUiCopy();
  return (
    <div className="loading-block" role="status" aria-live="polite">
      <span className="loading-mark" aria-hidden="true" />
      <span>{label ?? l("common.loading", "正在加载数据")}</span>
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
  const l = useUiCopy();
  return (
    <div className="error-block" role="alert">
      <div>
        <strong>{l("common.requestFailed", "暂时无法完成请求")}</strong>
        <p>{error.message}</p>
      </div>
      {onRetry && (
        <button className="button secondary" type="button" onClick={onRetry}>
          <Icon name="refresh" />
          {l("common.retry", "重试")}
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
