export interface PdfCanvasOutput {
  width: number;
  height: number;
  cssWidth: number;
  cssHeight: number;
  transform?: number[];
}

export function pdfCanvasOutput(
  viewportWidth: number,
  viewportHeight: number,
  devicePixelRatio: number,
): PdfCanvasOutput {
  const safePixelRatio = Number.isFinite(devicePixelRatio)
    ? Math.max(1, Math.min(devicePixelRatio, 2))
    : 1;

  return {
    width: Math.max(1, Math.floor(viewportWidth * safePixelRatio)),
    height: Math.max(1, Math.floor(viewportHeight * safePixelRatio)),
    cssWidth: viewportWidth,
    cssHeight: viewportHeight,
    transform:
      safePixelRatio === 1
        ? undefined
        : [safePixelRatio, 0, 0, safePixelRatio, 0, 0],
  };
}
