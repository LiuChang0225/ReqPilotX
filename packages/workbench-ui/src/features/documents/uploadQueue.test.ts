import { describe, expect, it } from "vitest";
import {
  createUploadItems,
  uploadQueueReducer,
  type UploadQueueItem,
} from "./uploadQueue";

function file(name: string, size = 10) {
  return new File(["x".repeat(size)], name, { lastModified: 7 });
}

describe("upload queue", () => {
  it("filters unsupported and duplicate files without rejecting the batch", () => {
    let id = 0;
    const result = createUploadItems(
      "project",
      "sor",
      [file("a.pdf"), file("a.pdf"), file("b.exe")],
      [],
      () => `id-${++id}`,
    );
    expect(result.items).toHaveLength(1);
    expect(result.rejected).toEqual([
      "a.pdf：已在队列中",
      "b.exe：不支持该格式",
    ]);
  });

  it("keeps later files runnable when one item fails", () => {
    const base = [
      {
        id: "a",
        projectId: "p",
        kind: "sor",
        file: file("a.pdf"),
        status: "uploading",
        progress: 30,
        error: "",
      },
      {
        id: "b",
        projectId: "p",
        kind: "sor",
        file: file("b.pdf"),
        status: "queued",
        progress: 0,
        error: "",
      },
    ] satisfies UploadQueueItem[];
    const failed = uploadQueueReducer(base, {
      type: "fail",
      id: "a",
      error: "bad",
    });
    expect(failed[0].status).toBe("failed");
    expect(failed[1].status).toBe("queued");
  });
});
