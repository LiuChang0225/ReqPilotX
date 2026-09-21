import {
  useMemo,
  useSyncExternalStore,
  type AnchorHTMLAttributes,
  type MouseEvent,
} from "react";

const navigationEvent = "reqpilotx:navigation";
let navigationGuard: (() => boolean) | null = null;

export function registerNavigationGuard(guard: () => boolean): () => void {
  navigationGuard = guard;
  return () => {
    if (navigationGuard === guard) navigationGuard = null;
  };
}

function snapshot(): string {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`;
}

function subscribe(listener: () => void): () => void {
  window.addEventListener("popstate", listener);
  window.addEventListener(navigationEvent, listener);
  return () => {
    window.removeEventListener("popstate", listener);
    window.removeEventListener(navigationEvent, listener);
  };
}

export function useBrowserLocation(): URL {
  const current = useSyncExternalStore(subscribe, snapshot, snapshot);
  return useMemo(() => new URL(current, window.location.origin), [current]);
}

export function navigate(
  to: string,
  options: { replace?: boolean } = {},
): void {
  if (navigationGuard && !navigationGuard()) return;
  const next = new URL(to, window.location.origin);
  if (next.origin !== window.location.origin) {
    window.location.assign(next.href);
    return;
  }
  const method = options.replace ? "replaceState" : "pushState";
  window.history[method](null, "", `${next.pathname}${next.search}${next.hash}`);
  window.dispatchEvent(new Event(navigationEvent));
}

interface AppLinkProps extends AnchorHTMLAttributes<HTMLAnchorElement> {
  to: string;
}

export function AppLink({
  to,
  onClick,
  target,
  ...props
}: AppLinkProps) {
  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey ||
      target
    ) {
      return;
    }
    const destination = new URL(to, window.location.origin);
    if (destination.origin !== window.location.origin) return;
    event.preventDefault();
    navigate(to);
  };
  return <a href={to} target={target} onClick={handleClick} {...props} />;
}
