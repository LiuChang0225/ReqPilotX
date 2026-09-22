import { expect, it } from "vitest";
import { originalNumber, scopeCountLabel, scopeCountDetail } from "./sourcePresentation";
it("uses explicit source-number projections without parsing display IDs or inventing numbers",()=>{
 expect(originalNumber({source_number:"2.10"})).toBe("2.10");
 expect(originalNumber({source_number:"",source_numbers:[],source_number_role:"UNNUMBERED"})).toBe("无独立原编号");
 expect(originalNumber({source_numbers:[{value:"1"},{value:"3"}]})).toBe("1、3");
 expect(originalNumber({})).toBe("未提供");
});
it("distinguishes a saved chapter subtree from direct content and analysis units",()=>{
 const summary={direct_source_item_count:1,subtree_source_item_count:8,subtree_requirement_count:8,subtree_non_requirement_count:0,derived_count:2,analysis_unit_count:9};
 expect(scopeCountLabel(summary)).toBe("原文 8 · 需求 8");expect(scopeCountDetail(summary)).toContain("本层直属正文 1");expect(scopeCountDetail(summary)).toContain("分析单元 9");
 expect(scopeCountLabel(undefined,1)).toBe("直属原文 1");
});
