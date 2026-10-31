import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PdfViewerPage } from "./PdfViewerPage";
import { withPdfFit, withPdfLocale, pdfViewerHref } from "./pdfViewerUrl";

vi.mock("pdfjs-dist", () => ({
  GlobalWorkerOptions: {},
  TextLayer: class { cancel() {} async render() {} },
  getDocument: () => ({ promise: Promise.reject(new Error("fixture: no pdf")), destroy: async () => {} }),
}));
vi.mock("pdfjs-dist/build/pdf.worker.min.mjs?url", () => ({ default: "worker.js" }));

afterEach(() => { cleanup(); window.history.replaceState({}, "", "/"); });

beforeEach(() => { vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} }); });

it("renders the separate PDF viewer in explicit English without touching source origin", () => {
  window.history.replaceState({}, "", "/pdf-viewer?locale=en");
  render(<PdfViewerPage />);
  expect(screen.getByRole("navigation", { name: "PDF controls" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Next page" })).toBeInTheDocument();
  expect(screen.getByText("The PDF source URL is missing.")).toBeVisible();
  expect(screen.queryByText("PDF 控制")).not.toBeInTheDocument();
});

it("adds only a supported locale to viewer links and preserves absolute host origins", () => {
  expect(withPdfLocale("/pdf-viewer?pdf=%2Fapi%2Fsource&page=2", "en")).toBe("/pdf-viewer?pdf=%2Fapi%2Fsource&page=2&locale=en");
  expect(withPdfFit("/pdf-viewer?fit=page&pdf=%2Fapi%2Fsource&page=2", "width")).toBe("/pdf-viewer?fit=width&pdf=%2Fapi%2Fsource&page=2");
  expect(withPdfLocale("https://service.test/pdf-viewer?pdf=https%3A%2F%2Fpair.test%2Fapi", "zh")).toBe("https://service.test/pdf-viewer?pdf=https%3A%2F%2Fpair.test%2Fapi&locale=zh");
  expect(pdfViewerHref({ sourceUrl: "/api/source", page: 3, locale: "en", embedded: true, fit: "page" })).toBe("/pdf-viewer?embedded=1&fit=page&pdf=%2Fapi%2Fsource&page=3&locale=en");
});
