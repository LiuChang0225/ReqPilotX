import { describe, expect, it } from "vitest";
import {
  bindWorkspaceDataToScope,
  documentScopeKey,
  exactScopeValue,
  isExtractionPollingStatus,
  projectScopeKey,
  requirementSetScopeKey,
  type WorkspaceScopedData,
} from "./WorkspaceContext";

describe("workspace exact scope binding", () => {
  it("hides every project- and document-bound value during a route switch", () => {
    const oldProject = projectScopeKey("project-a");
    const newProject = projectScopeKey("project-b");
    const oldDocument = documentScopeKey("project-a", "document-a");
    const newDocument = documentScopeKey("project-b", "document-b");
    const oldRequirementSet = requirementSetScopeKey(
      "project-a",
      "document-a",
      "set-a",
    );
    const newRequirementSet = requirementSetScopeKey(
      "project-b",
      "document-b",
      "set-b",
    );
    const oldData: WorkspaceScopedData = {
      documents: [
        { id: "document-a", project_id: "project-a", file_name: "a.pdf" },
      ],
      knowledge: [{ id: "knowledge-a", file_name: "knowledge.pdf" }],
      processingProfile: {
        schema_version: "project-processing-profile/v1",
        project_id: "project-a",
        document: {
          primary_parser: "pymupdf",
          enhancement_chain: [],
          complex_vision: null,
          auto_escalation: false,
        },
        llm_provider: "none",
        knowledge_backend: "none",
        revision: 1,
        canonical_sha256: "a".repeat(64),
      },
      workspace: { id: "workspace-a" },
      requirementSets: { active_requirement_set_id: "set-a" },
      extraction: { document_id: "document-a" },
      structure: { id: "structure-a" },
      structureOptimization: { document_id: "document-a" },
      aiStatus: { id: "analysis-a" },
      exports: [{ id: "export-a" }],
    };

    expect(
      bindWorkspaceDataToScope(
        oldData,
        {
          documents: oldProject,
          knowledge: oldProject,
          processingProfile: oldProject,
          workspace: oldRequirementSet,
          requirementSets: oldDocument,
          extraction: oldDocument,
          structure: oldDocument,
          structureOptimization: oldDocument,
          aiStatus: oldDocument,
          exports: oldProject,
        },
        {
          project: newProject,
          document: newDocument,
          requirementSet: newRequirementSet,
        },
      ),
    ).toEqual({
      documents: [],
      knowledge: [],
      processingProfile: null,
      workspace: null,
      requirementSets: null,
      extraction: null,
      structure: null,
      structureOptimization: null,
      aiStatus: null,
      exports: [],
    });
  });

  it("returns the original value only for the exact current scope", () => {
    const value = { id: "current" };
    const scope = requirementSetScopeKey("project", "document", "set");

    expect(exactScopeValue(value, scope, scope, null)).toBe(value);
    expect(exactScopeValue(value, "", scope, null)).toBeNull();
    expect(exactScopeValue(value, scope, "", null)).toBeNull();
  });
});

describe("extraction polling status", () => {
  it.each([
    "PENDING",
    "pending",
    "QUEUED",
    "RUNNING",
    "STARTING",
    "CANCEL_REQUESTED",
  ])("polls while an extraction job is %s", (status) => {
    expect(isExtractionPollingStatus(status)).toBe(true);
  });

  it.each(["", "SUCCEEDED", "FAILED", "CANCELLED", undefined])(
    "does not poll after an extraction job is %s",
    (status) => {
      expect(isExtractionPollingStatus(status)).toBe(false);
    },
  );
});
