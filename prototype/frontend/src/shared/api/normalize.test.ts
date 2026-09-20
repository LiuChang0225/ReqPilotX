import { describe, expect, it } from "vitest";
import {
  normalizeAiProfiles,
  normalizeRuntimeChecks,
} from "./normalize";

describe("runtime response normalization", () => {
  it("accepts profile arrays and keyed profile maps", () => {
    expect(normalizeAiProfiles([{ id: "aliyun", ready: true }])).toEqual([
      { id: "aliyun", ready: true },
    ]);
    expect(
      normalizeAiProfiles({
        azure: { label: "Azure", ready: false },
      }),
    ).toEqual([
      {
        check_id: "azure",
        id: "azure",
        label: "Azure",
        ready: false,
      },
    ]);
  });

  it("turns keyed readiness checks into a searchable list", () => {
    expect(
      normalizeRuntimeChecks({
        postgresql: { status: "ok", detail: "reachable" },
        aliyun: {
          status: "error",
          profile_id: "aliyun",
          detail: "Qdrant is optional",
        },
      }),
    ).toEqual([
      {
        check_id: "postgresql",
        detail: "reachable",
        id: "postgresql",
        status: "ok",
      },
      {
        check_id: "aliyun",
        detail: "Qdrant is optional",
        id: "aliyun",
        profile_id: "aliyun",
        status: "error",
      },
    ]);
  });

  it("fails closed for scalar and malformed collections", () => {
    expect(normalizeAiProfiles("aliyun")).toEqual([]);
    expect(normalizeRuntimeChecks({ broken: "not-a-check" })).toEqual([]);
  });
});
