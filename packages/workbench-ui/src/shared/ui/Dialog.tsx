import {
  type PropsWithChildren,
  useEffect,
  useId,
  useRef,
} from "react";
import { cx } from "../lib/format";

interface DialogProps extends PropsWithChildren {
  open: boolean;
  title: string;
  onClose: () => void;
  className?: string;
  busy?: boolean;
}

export function Dialog({
  open,
  title,
  onClose,
  className,
  busy = false,
  children,
}: DialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      returnFocusRef.current =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      dialog.showModal();
      return;
    }
    if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(
    () => () => {
      returnFocusRef.current?.focus();
    },
    [],
  );

  return (
    <dialog
      ref={dialogRef}
      className={cx("app-dialog", className)}
      aria-labelledby={titleId}
      aria-busy={busy || undefined}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
      onClose={() => {
        if (!busy) onClose();
        window.requestAnimationFrame(() => returnFocusRef.current?.focus());
      }}
      onClick={(event) => {
        if (!busy && event.target === event.currentTarget) onClose();
      }}
    >
      <section className="modal-card">
        <h2 id={titleId} className="sr-only">
          {title}
        </h2>
        {children}
      </section>
    </dialog>
  );
}
