import { lazy, Suspense, useEffect } from "react";
import { ErrorBoundary } from "./ErrorBoundary";
import { PreferencesProvider } from "./preferences";
import { HomePage } from "../features/home/HomePage";
import { WorkspacePage } from "../features/workspace/WorkspacePage";
import { navigate, useBrowserLocation } from "../shared/navigation";
import { LoadingBlock } from "../shared/ui/Feedback";
import {
  parseWorkspaceRoute,
  serializeWorkspaceRoute,
} from "../features/workspace/route";

const CandidateReviewWorkspace = lazy(async () => ({ default: (await import("../features/extraction/CandidateReviewWorkspace")).CandidateReviewWorkspace }));

const PdfViewerPage = lazy(async () => ({
  default: (await import("../features/pdf/PdfViewerPage")).PdfViewerPage,
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
        <Suspense fallback={<LoadingBlock label="正在装载前端模块" />}>
          {page}
        </Suspense>
      </PreferencesProvider>
    </ErrorBoundary>
  );
}
