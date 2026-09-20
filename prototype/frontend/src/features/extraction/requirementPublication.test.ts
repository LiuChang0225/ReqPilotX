import { describe, expect, it } from "vitest";
import {
  draftRequirementPublicationSetId,
  requirementPublicationNotice,
  requirementPublicationSetId,
} from "./requirementPublication";

describe("requirement publication feedback", () => {
  it("states when an unchanged lineage reused an existing version", () => {
    expect(
      requirementPublicationNotice({
        requirement_set_created: false,
        publication: {
          requirement_set_id: "set-2",
          version: 2,
        },
      }),
    ).toBe("已复用 v2，没有创建新版本");
  });

  it("keeps compatibility with nested creation state", () => {
    expect(
      requirementPublicationNotice({
        publication: {
          requirement_set_created: false,
          requirement_set_id: "set-1",
        },
      }),
    ).toBe("已复用现有审阅版本，没有创建新版本");
  });

  it("returns local and enrichment DRAFT identities for activation", () => {
    expect(
      draftRequirementPublicationSetId({
        publication: {
          requirement_set_id: "local-set",
          status: "DRAFT",
        },
      }),
    ).toBe("local-set");
    expect(
      draftRequirementPublicationSetId({
        publication: {
          requirement_set_id: "llm-set",
          requirement_set_status: "DRAFT",
        },
      }),
    ).toBe("llm-set");
  });

  it("never activates a locked publication as if it were a DRAFT", () => {
    const result = {
      publication: {
        requirement_set_id: "locked-set",
        status: "LOCKED",
      },
    };

    expect(requirementPublicationSetId(result)).toBe("locked-set");
    expect(draftRequirementPublicationSetId(result)).toBe("");
  });
});
