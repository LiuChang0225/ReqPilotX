import { createContext, useContext } from "react";

export type WorkbenchPresentation = {
  readOnly: boolean;
  resourceUrl: (path: string) => string;
  pdfViewerUrl: (sourceUrl: string, page: number, embedded: boolean) => string;
};
const standalone: WorkbenchPresentation = {
  readOnly: false,
  resourceUrl: path => path,
  pdfViewerUrl: (sourceUrl, page, embedded) => `/pdf-viewer?${embedded ? "embedded=1&" : ""}fit=page&pdf=${encodeURIComponent(sourceUrl)}&page=${page}`,
};
export const WorkbenchHostContext = createContext<WorkbenchPresentation>(standalone);
export const useWorkbenchHost = () => useContext(WorkbenchHostContext);
