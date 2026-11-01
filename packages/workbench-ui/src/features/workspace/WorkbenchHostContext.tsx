import { createContext, useContext } from "react";

export type WorkbenchPresentation = {
  readOnly: boolean;
  /** Standalone Web source evidence flow. Hosts that omit this flag keep their existing contract. */
  nativeEvidence?: boolean;
  resourceUrl: (path: string) => string;
  pdfViewerUrl: (sourceUrl: string, page: number, embedded: boolean) => string;
};
const standalone: WorkbenchPresentation = {
  readOnly: false,
  nativeEvidence: true,
  resourceUrl: path => path,
  pdfViewerUrl: (sourceUrl, page, embedded) => `/pdf-viewer?${embedded ? "embedded=1&" : ""}fit=page&pdf=${encodeURIComponent(sourceUrl)}&page=${page}`,
};
export const WorkbenchHostContext = createContext<WorkbenchPresentation>(standalone);
export const useWorkbenchHost = () => useContext(WorkbenchHostContext);
