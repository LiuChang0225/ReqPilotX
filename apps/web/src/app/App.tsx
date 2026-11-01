import { lazy, Suspense, useEffect } from "react";
import { AccountGate } from "../auth/AccountGate";
import {
  ErrorBoundary, PreferencesProvider, HomePage, WorkspacePage,
  navigate, useBrowserLocation, LoadingBlock, loadCandidateReview, loadPdfViewer,
  parseWorkspaceRoute,
  serializeWorkspaceRoute,
} from "@reqpilotx/workbench-ui/web";

const CandidateReviewWorkspace = lazy(async () => ({ default: (await loadCandidateReview()).CandidateReviewWorkspace }));

const PdfViewerPage = lazy(async () => ({
  default: (await loadPdfViewer()).PdfViewerPage,
}));

function LegacyWorkspaceRedirect({ search }: { search: string }) {
  useEffect(() => {
    const parsed = parseWorkspaceRoute(search);
    navigate(
      serializeWorkspaceRoute({
        ...parsed,
        task: parsed.documentId ? "extraction" : "projects",
        mode: "organize",
      }),
      { replace: true },
    );
  }, [search]);
  return <LoadingBlock label="正在进入需求结构树" />;
}

export function App() {
  const location = useBrowserLocation();
  const workspaceRoute = parseWorkspaceRoute(location.search);
  const page = (() => {
    if (["/", "/workspace"].includes(location.pathname) && workspaceRoute.reviewCase) return <CandidateReviewWorkspace />;
    if (location.pathname === "/") return <WorkspacePage />;
    if (location.pathname === "/home") return <HomePage />;
    if (location.pathname === "/workspace") return <WorkspacePage />;
    if (location.pathname === "/extraction-review") {
      return <LegacyWorkspaceRedirect search={location.search} />;
    }
    if (location.pathname === "/pdf-viewer") return <PdfViewerPage />;
    return <WorkspacePage />;
  })();
  return (
    <ErrorBoundary>
      <PreferencesProvider>
        <AccountGate>
          <Suspense fallback={<LoadingBlock label="正在装载前端模块" />}>
            {page}
          </Suspense>
        </AccountGate>
      </PreferencesProvider>
    </ErrorBoundary>
  );
}
