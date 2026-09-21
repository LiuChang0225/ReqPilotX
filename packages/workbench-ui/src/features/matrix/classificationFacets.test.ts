import { describe, expect, it } from "vitest";
import type { RequirementMatrixNode, RequirementMatrixSnapshot } from "../../shared/api/contracts";
import { matchesClassificationFacets } from "./classificationFacets";
import { flattenRequirementMatrix, matchingCandidateExportIds, requirementMatrixMatchCounts } from "./requirementMatrixModel";

function node(id: string, parent: string | null, classes: string[], tags: string[]): RequirementMatrixNode {
  return { node_id:id, parent_id:parent, child_ids:[], depth:0, source_order:0, display_role:"CLAUSE",
    label:id, summary:id, content_blocks:[], table_refs:[], figure_refs:[], source_refs:[], page_start:1, page_end:1,
    structure_node_id:null, requirement_entry_id:id, requirement_id:null, requirement_code:id, requirement_text:id,
    table_record_id:null, logical_table_ids:[], matrix_visibility:"VISIBLE", disposition:"RETAIN", disposition_reason:"",
    analysis_eligibility:"NOT_ELIGIBLE", analysis_status:"NOT_APPLICABLE", ai_assessment:null, manual_decision:null,
    compliance_status:"", save_revision:0, updated_at:"", classification_paths:classes, topic_tags:tags };
}
describe("classification and topic facets", () => {
  it("keeps a regulatory project deliverable distinct from a product constraint", () => {
    const delivery = node("delivery",null,["PROJECT"],["REGULATORY"]);
    expect(matchesClassificationFacets(delivery,{category:"PROJECT",topic:"REGULATORY"})).toBe(true);
    expect(matchesClassificationFacets(delivery,{category:"PRODUCT_SYSTEM.CONSTRAINT",topic:"REGULATORY"})).toBe(false);
    expect(matchesClassificationFacets(delivery,{category:"PRODUCT_SYSTEM",topic:""})).toBe(false);
  });
  it("matches combined dimensions on each child and retains the original parent only as navigation", () => {
    const parent=node("source",null,[],[]);
    const functional=node("d1","source",["PRODUCT_SYSTEM.FUNCTIONAL","PRODUCT_SYSTEM.CONSTRAINT"],["VERIFICATION"]);
    const delivery=node("d2","source",["PROJECT"],["REGULATORY"]);
    parent.child_ids=["d1","d2"];
    const matrix={root_node_ids:["source"],nodes:[parent,functional,delivery]} as RequirementMatrixSnapshot;
    const facets={category:"PRODUCT_SYSTEM.FUNCTIONAL",topic:"VERIFICATION"};
    expect(flattenRequirementMatrix(matrix,new Set(["source"]),"","all",facets).map(row=>row.node.node_id)).toEqual(["source","d1"]);
    expect(requirementMatrixMatchCounts(matrix,"","all",facets)).toEqual({matched:1,retainedAncestors:1});
    expect(flattenRequirementMatrix(matrix,new Set(["source"]),"","all",{...facets,topic:"REGULATORY"})).toEqual([]);
  });
  it("does not hide retained references when only filtering a topic", () => {
    const reference=node("list",null,[],["STANDARDS","VERIFICATION"]);
    expect(matchesClassificationFacets(reference,{category:"",topic:"VERIFICATION"})).toBe(true);
    expect(matchesClassificationFacets(reference,{category:"PRODUCT_SYSTEM.QUALITY",topic:"VERIFICATION"})).toBe(false);
  });
  it("exports in frozen source-tree order rather than node storage order", () => {
    const early=node("early",null,["PROJECT"],[]), later=node("later",null,["PROJECT"],[]);
    const child=node("early/d1","early",["PROJECT"],[]);
    early.child_ids=[child.node_id];
    [early,later,child].forEach(item => { item.candidate_review={}; });
    const matrix={root_node_ids:["early","later"],nodes:[later,child,early]} as RequirementMatrixSnapshot;
    expect(matchingCandidateExportIds(matrix,"","all",{category:"",topic:""})).toEqual(["early","early/d1","later"]);
  });
});
