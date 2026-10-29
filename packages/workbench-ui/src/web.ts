/** Web composition surface; business components remain shared with the host panel. */
export { ErrorBoundary } from "./app/ErrorBoundary";
export { PreferencesProvider } from "./app/preferences";
export { HomePage } from "./features/home/HomePage";
export { WorkspacePage } from "./features/workspace/WorkspacePage";
export { navigate, useBrowserLocation } from "./shared/navigation";
export { LoadingBlock } from "./shared/ui/Feedback";
export { setSaasCsrfToken, setSaasAccountIdentity } from "./shared/api/saasSession";
export { parseWorkspaceRoute, serializeWorkspaceRoute } from "./features/workspace/route";
export const loadCandidateReview = () => import("./features/extraction/CandidateReviewWorkspace");
export const loadPdfViewer = () => import("./features/pdf/PdfViewerPage");
export const loadParserLab = () => import("./features/parser-lab/ParserLabPage");
