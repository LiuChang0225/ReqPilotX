import { expect, it } from "vitest";
import { analysisUnits, inAnalysisQueue, matchesAnalysisSearch } from "./analysisQueue";
import type { SourceRequirementOutlineRecordV1 } from "../../shared/api/contracts";
import type { SourceItemDeliveryState } from "./SourceItemReasoning";
it("counts derived units once, keeps source order and gives current human opinions priority", () => {
  const records = [{ source_record_id:"parent", requirementness:"REQUIREMENT" }, { source_record_id:"info", requirementness:"NON_REQUIREMENT" }, { source_record_id:"last",requirementness:"REQUIREMENT" }] as SourceRequirementOutlineRecordV1[];
  const state = { decomposition:{records:{parent:[{singular_requirement_id:"s1"},{singular_requirement_id:"s2"}]}},
    assessments:{records:{s1:{decision:"COMPLIANT"},s2:{decision:"PENDING_EVIDENCE"}}},
    engineer_responses:{records:{s1:{state:"CURRENT",decision:"NON_COMPLIANT"},last:{state:"STALE",decision:"COMPLIANT"}}} } as unknown as SourceItemDeliveryState;
  const units=analysisUnits(records,state);
  expect(units.map(unit=>unit.id)).toEqual(["s1","s2","last"]);
  expect(units.filter(unit=>inAnalysisQueue(unit,"ACTION")).map(unit=>unit.id)).toEqual(["s2","last"]);
  expect(units.filter(unit=>inAnalysisQueue(unit,"DEVIATION")).map(unit=>unit.id)).toEqual(["s1"]);
});

it("searches each derived unit by its own number and content", () => {
  const records=[{source_record_id:"src",source_display_id:"SRC-0001",requirement_display_id:"REQ-0001",requirementness:"REQUIREMENT",requirement_kinds:["FUNCTIONAL"],ancestor_path:["Scope"]}] as SourceRequirementOutlineRecordV1[];
  const state={decomposition:{records:{src:[{singular_requirement_id:"u1",requirement_display_id:"REQ-0001-S01",text:"Supply voltage"},{singular_requirement_id:"u2",requirement_display_id:"REQ-0001-S02",text:"Warmup TBD"}]}}} as unknown as SourceItemDeliveryState;
  const units=analysisUnits(records,state);
  expect(units.filter(unit=>matchesAnalysisSearch(unit,"req-0001-s02")).map(unit=>unit.id)).toEqual(["u2"]);
  expect(units.filter(unit=>matchesAnalysisSearch(unit,"TBD")).map(unit=>unit.id)).toEqual(["u2"]);
  expect(units.filter(unit=>matchesAnalysisSearch(unit,"SRC-0001"))).toHaveLength(2);
});
