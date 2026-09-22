import { describe, expect, it } from "vitest";
import {
  overlayLabelPlacement,
  overlayStyleFor,
  overlayFocusView,
} from "./overlayGeometry";

describe("PDF overlay geometry", () => {
  it("fits a source region and preserves its center rather than centering the whole page", () => {
    const result = overlayFocusView({ bbox: [25,10,75,30], sourceBounds: [0,0,100,100] }, 600,800,400,300);
    expect(result?.centerX).toBe(.5); expect(result?.centerY).toBe(.2);
    expect(result?.scale).toBeCloseTo(368/300);
    expect(overlayFocusView({ bbox: [1,1,1,1] }, 600,800,400,300)).toBeNull();
  });
  it("projects PDF point coordinates through the exact page bounds", () => {
    expect(
      overlayStyleFor({
        bbox: [59.5, 84.2, 297.5, 421],
        sourceBounds: [0, 0, 595, 842],
      }),
    ).toEqual({
      left: "10%",
      top: "10%",
      width: "40%",
      height: "40%",
    });
  });

  it("keeps normalized overlays compatible", () => {
    expect(overlayStyleFor({ bbox: [0.2, 0.1, 0.7, 0.3] })).toEqual({
      left: "20%",
      top: "10%",
      width: "50%",
      height: "20%",
    });
  });

  it("clips malformed out-of-page overlays to the visible canvas", () => {
    expect(
      overlayStyleFor({
        bbox: [-10, 100, 610, 900],
        sourceBounds: [0, 0, 595, 842],
      }),
    ).toEqual({
      left: "0%",
      top: "11.876485%",
      width: "100%",
      height: "88.123515%",
    });
  });

  it("never lets a partially visible box extend beyond the right or bottom edge", () => {
    expect(
      overlayStyleFor({
        bbox: [500, 700, 680, 940],
        sourceBounds: [0, 0, 595, 842],
      }),
    ).toEqual({
      left: "84.033613%",
      top: "83.135392%",
      width: "15.966387%",
      height: "16.864608%",
    });
  });

  it("places labels inside boxes that touch the top edge", () => {
    expect(
      overlayLabelPlacement({
        bbox: [40, 0, 200, 30],
        sourceBounds: [0, 0, 595, 842],
      }),
    ).toBe("inside");
    expect(
      overlayLabelPlacement({
        bbox: [40, 100, 200, 130],
        sourceBounds: [0, 0, 595, 842],
      }),
    ).toBe("outside");
  });
});
