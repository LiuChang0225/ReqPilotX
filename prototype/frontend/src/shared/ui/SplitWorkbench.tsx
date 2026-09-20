import {
  Children,
  type CSSProperties,
  type HTMLAttributes,
  type KeyboardEvent,
  type PointerEvent,
  useMemo,
  useRef,
  useState,
} from "react";
import { cx } from "../lib/format";

interface SplitWorkbenchProps extends HTMLAttributes<HTMLDivElement> {
  storageKey: string;
  defaultLeft?: number;
  defaultRight?: number;
}

type Layout = { left: number; right: number };

function loadLayout(
  key: string,
  fallback: Layout,
): Layout {
  try {
    const parsed = JSON.parse(localStorage.getItem(key) || "");
    if (Number.isFinite(parsed.left) && Number.isFinite(parsed.right)) {
      return parsed;
    }
  } catch {
    // Corrupt layout preferences are safely ignored.
  }
  return fallback;
}

export function SplitWorkbench({
  storageKey,
  defaultLeft = 300,
  defaultRight = 340,
  className,
  children,
  ...props
}: SplitWorkbenchProps) {
  const fallback = useMemo(
    () => ({ left: defaultLeft, right: defaultRight }),
    [defaultLeft, defaultRight],
  );
  const [layout, setLayout] = useState<Layout>(() =>
    loadLayout(storageKey, fallback),
  );
  const rootRef = useRef<HTMLDivElement>(null);
  const panes = Children.toArray(children);

  const commit = (next: Layout) => {
    setLayout(next);
    localStorage.setItem(storageKey, JSON.stringify(next));
  };

  const resize = (side: "left" | "right", requested: number) => {
    const total = rootRef.current?.clientWidth || 1200;
    const other = side === "left" ? layout.right : layout.left;
    const minimum = side === "left" ? 240 : 280;
    const maximum = Math.max(minimum, total - other - 390);
    commit({
      ...layout,
      [side]: Math.round(Math.max(minimum, Math.min(maximum, requested))),
    });
  };

  const pointerResize = (
    side: "left" | "right",
    event: PointerEvent<HTMLDivElement>,
  ) => {
    event.preventDefault();
    const separator = event.currentTarget;
    const start = event.clientX;
    const initial = layout[side];
    separator.setPointerCapture(event.pointerId);
    const move = (moveEvent: globalThis.PointerEvent) => {
      const delta = moveEvent.clientX - start;
      resize(side, initial + (side === "left" ? delta : -delta));
    };
    const finish = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish, { once: true });
    window.addEventListener("pointercancel", finish, { once: true });
  };

  const keyboardResize = (
    side: "left" | "right",
    event: KeyboardEvent<HTMLDivElement>,
  ) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const amount = event.shiftKey ? 48 : 16;
    const direction = event.key === "ArrowRight" ? 1 : -1;
    resize(
      side,
      layout[side] + direction * amount * (side === "left" ? 1 : -1),
    );
  };

  const separator = (side: "left" | "right") => (
    <div
      className="split-separator"
      role="separator"
      aria-label={side === "left" ? "调整列表宽度" : "调整检查器宽度"}
      aria-orientation="vertical"
      aria-valuemin={side === "left" ? 240 : 280}
      aria-valuenow={layout[side]}
      tabIndex={0}
      onPointerDown={(event) => pointerResize(side, event)}
      onKeyDown={(event) => keyboardResize(side, event)}
      onDoubleClick={() => commit(fallback)}
    />
  );

  const style = {
    ...props.style,
    "--workbench-left": `${layout.left}px`,
    "--workbench-right": `${layout.right}px`,
  } as CSSProperties;

  return (
    <div
      {...props}
      ref={rootRef}
      className={cx(className, "is-resizable-workbench")}
      style={style}
    >
      {panes[0]}
      {separator("left")}
      {panes[1]}
      {separator("right")}
      {panes[2]}
    </div>
  );
}
