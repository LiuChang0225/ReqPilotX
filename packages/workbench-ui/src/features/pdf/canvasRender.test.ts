import { describe, expect, it } from "vitest";
import { pdfCanvasOutput } from "./canvasRender";

describe("pdfCanvasOutput", () => {
  it("uses the PDF.js render transform for high-density canvases", () => {
    expect(pdfCanvasOutput(595, 842, 2)).toEqual({
      width: 1190,
      height: 1684,
      cssWidth: 595,
      cssHeight: 842,
      transform: [2, 0, 0, 2, 0, 0],
    });
  });

  it("keeps a one-to-one canvas when the pixel ratio is unavailable", () => {
    expect(pdfCanvasOutput(595, 842, Number.NaN)).toEqual({
      width: 595,
      height: 842,
      cssWidth: 595,
      cssHeight: 842,
      transform: undefined,
    });
  });

  it("caps excessive density to keep large PDF pages bounded", () => {
    expect(pdfCanvasOutput(300, 400, 4)).toMatchObject({
      width: 600,
      height: 800,
      transform: [2, 0, 0, 2, 0, 0],
    });
  });
});
