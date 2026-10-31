import { useCallback } from "react";
import { useUiLocale } from "../../host/HostLocaleContext";
import { supportedUiLocales, type UiLocale } from "../../shared/i18n/messages";
import { useWorkbenchHost } from "../workspace/WorkbenchHostContext";

/** Keep the viewer's own origin and query security intact; add only the explicit UI locale. */
export function withPdfLocale(url: string, locale: UiLocale): string {
  try {
    const relative = url.startsWith("/") && !url.startsWith("//");
    const parsed = new URL(url, window.location.origin);
    parsed.searchParams.set("locale", supportedUiLocales.includes(locale) ? locale : "zh");
    return relative ? `${parsed.pathname}${parsed.search}${parsed.hash}` : parsed.href;
  } catch {
    return url;
  }
}

export function pdfViewerHref(input: {
  sourceUrl: string;
  page: number;
  locale: UiLocale;
  embedded?: boolean;
  fit?: "page" | "width";
  selection?: "reading";
  keyboard?: "parent";
}): string {
  const params = new URLSearchParams();
  if (input.embedded) params.set("embedded", "1");
  if (input.selection) params.set("selection", input.selection);
  if (input.keyboard) params.set("keyboard", input.keyboard);
  params.set("fit", input.fit || "page");
  params.set("pdf", input.sourceUrl);
  params.set("page", String(input.page));
  params.set("locale", supportedUiLocales.includes(input.locale) ? input.locale : "zh");
  return `/pdf-viewer?${params.toString()}`;
}

/** Wrapper around a host-owned viewer URL that carries the current explicit locale. */
export function useHostPdfViewerUrl() {
  const host = useWorkbenchHost();
  const locale = useUiLocale();
  return useCallback(
    (sourceUrl: string, page: number, embedded: boolean) => withPdfLocale(host.pdfViewerUrl(sourceUrl, page, embedded), locale),
    [host, locale],
  );
}
