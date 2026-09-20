export interface OverlayGeometry {
  bbox: number[];
  sourceBounds?: number[];
}

function isValidBounds(value: number[] | undefined): value is number[] {
  return Boolean(
    value &&
      value.length === 4 &&
      value.every(Number.isFinite) &&
      value[2] > value[0] &&
      value[3] > value[1],
  );
}

function percentage(value: number): string {
  return `${Number(value.toFixed(6))}%`;
}

export function overlayStyleFor({
  bbox,
  sourceBounds,
}: OverlayGeometry): Record<"left" | "top" | "width" | "height", string> {
  const normalized =
    Math.min(...bbox) >= 0 && Math.max(...bbox) <= 1.001;
  const [pageX0, pageY0, pageX1, pageY1] = isValidBounds(sourceBounds)
    ? sourceBounds
    : normalized
      ? [0, 0, 1, 1]
      : [0, 0, 1000, 1000];
  const [x0, y0, x1, y1] = bbox;
  const pageWidth = pageX1 - pageX0;
  const pageHeight = pageY1 - pageY0;
  const left = Math.max(0, Math.min(100, ((x0 - pageX0) / pageWidth) * 100));
  const top = Math.max(0, Math.min(100, ((y0 - pageY0) / pageHeight) * 100));
  const right = Math.max(0, Math.min(100, ((x1 - pageX0) / pageWidth) * 100));
  const bottom = Math.max(
    0,
    Math.min(100, ((y1 - pageY0) / pageHeight) * 100),
  );

  return {
    left: percentage(left),
    top: percentage(top),
    width: percentage(Math.max(0, right - left)),
    height: percentage(Math.max(0, bottom - top)),
  };
}

export function overlayLabelPlacement(
  geometry: OverlayGeometry,
): "inside" | "outside" {
  const top = Number.parseFloat(overlayStyleFor(geometry).top);
  return top < 3 ? "inside" : "outside";
}

export function overlayFocusView(geometry: OverlayGeometry, pageWidth: number, pageHeight: number, viewportWidth: number, viewportHeight: number) {
  if (geometry.bbox.length !== 4 || !geometry.bbox.every(Number.isFinite) || pageWidth <= 0 || pageHeight <= 0) return null;
  const style = overlayStyleFor(geometry);
  const width = Number.parseFloat(style.width) / 100, height = Number.parseFloat(style.height) / 100;
  if (width <= 0 || height <= 0) return null;
  return {
    scale: Math.max(.25, Math.min(4, (viewportWidth - 32) / (pageWidth * width), (viewportHeight - 48) / (pageHeight * height))),
    centerX: Number.parseFloat(style.left) / 100 + width / 2,
    centerY: Number.parseFloat(style.top) / 100 + height / 2,
  };
}
