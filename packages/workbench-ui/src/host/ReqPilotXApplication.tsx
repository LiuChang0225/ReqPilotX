import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ErrorBoundary } from "../app/ErrorBoundary";
import { PreferencesProvider, usePreferences, type Theme } from "../app/preferences";
import { HomePage } from "../features/home/HomePage";
import { WorkspacePage } from "../features/workspace/WorkspacePage";
import { WorkbenchHostContext } from "../features/workspace/WorkbenchHostContext";
import { ApplicationNavigationContext, type ApplicationNavigation } from "../shared/navigation";
import { ApplicationHostContext, ApplicationWidthContext, type WorkbenchApplicationHost } from "./ApplicationHost";

/** One panel-local route store; it never writes the parent window's URL. */
export function createApplicationNavigation(initial = "/", changed?: (path: string) => void): ApplicationNavigation {
  let current = initial;
  const listeners = new Set<() => void>();
  return {
    snapshot: () => current,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    navigate(to) {
      const next = new URL(to, window.location.origin);
      if (next.origin !== window.location.origin || !["/", "/home", "/workspace"].includes(next.pathname)) throw new Error("此链接不属于当前 ReqPilotX 工作台。");
      const path = next.pathname + next.search + next.hash;
      if (path === current) return;
      current = path; changed?.(path); listeners.forEach(listener => listener());
    },
  };
}

function ApplicationSurface({ navigation }: { navigation: ApplicationNavigation }) {
  const route = useSyncExternalStore(navigation.subscribe, navigation.snapshot, navigation.snapshot);
  const { theme } = usePreferences();
  const url = new URL(route, window.location.origin);
  return url.pathname === "/" || url.pathname === "/home"
    ? <section className="reqpilotx-panel-root is-hosted-application" data-theme={theme} style={{ display: "block", width: "100%", height: "100%", overflow: "auto" }}><HomePage /></section>
    : <WorkspacePage />;
}

/** Embed the same HomePage and WorkspacePage used by the Web application. */
export function ReqPilotXApplication({ host, theme = "light" }: { host: WorkbenchApplicationHost; theme?: Theme }) {
  const root = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number | undefined>(undefined);
  useEffect(() => {
    const element = root.current;
    if (!element) return;
    const resize = () => setWidth(element.clientWidth);
    resize(); const observer = new ResizeObserver(resize); observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const [navigation] = useState(() => {
    const key = `reqpilotx:application-route:v1:${host.id}`;
    let initial = "/";
    try { const stored = localStorage.getItem(key); if (stored && /^\/(?:home|workspace)?(?:\?|$)/.test(stored)) initial = stored; } catch { /* Persistence is optional. */ }
    return createApplicationNavigation(initial, path => { try { localStorage.setItem(key, path); } catch { /* The active route remains usable. */ } });
  });
  const presentation = useMemo(() => ({ readOnly: false, nativeEvidence: false, resourceUrl: host.resourceUrl, pdfViewerUrl: host.pdfViewerUrl }), [host]);
  return <div ref={root} className="reqpilotx-application" lang="zh-CN" style={{ flex: "1 1 auto", width: "100%", height: "100%", minWidth: 0, minHeight: 0, overflow: "hidden" }}>
    <ErrorBoundary embedded><ApplicationHostContext.Provider value={host}><ApplicationWidthContext.Provider value={width}><ApplicationNavigationContext.Provider value={navigation}>
      <WorkbenchHostContext.Provider value={presentation}><PreferencesProvider embedded initialHostTheme={theme}>
        <ApplicationSurface navigation={navigation} />
      </PreferencesProvider></WorkbenchHostContext.Provider>
    </ApplicationNavigationContext.Provider></ApplicationWidthContext.Provider></ApplicationHostContext.Provider></ErrorBoundary>
  </div>;
}
