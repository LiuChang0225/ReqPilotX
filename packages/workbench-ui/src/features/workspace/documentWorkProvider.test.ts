import { expect, it } from "vitest";
import { externalExecutionLabel, pluginCapability, pluginFeedbackLabel, type PluginState } from "./documentWorkProvider";
import { readViewedWork, rememberViewedWork } from "./documentWorkSelection";
it("never equates saved or revision-reported feedback with external retrieval or execution",()=>{
 const state={execution:{status:"UNREPORTED"},versions:[{revision_id:"v1",number:1},{revision_id:"v2",number:2}],feedback_receipts:[{feedback_id:"f",state:"REVISION_REPORTED",retrieval_state:"UNREPORTED",retrieved_at:null}],feedback_resolutions:{f:{revision_id:"v2",reason:"Changed"}}} as unknown as PluginState;
 const feedback={feedback_id:"f",expected_revision_id:"v1",text:"请修改"};
 expect(externalExecutionLabel(state)).toBe("外部运行状态未报告");
 expect(pluginFeedbackLabel(state,feedback,"v1")).toBe("意见已保存 · 待外部任务取回");expect(pluginFeedbackLabel(state,feedback,"v2")).toBe("插件已提交修订 · 待核对");
 expect(pluginCapability(state,"resume").enabled).toBe(false);
});
it("remembers the explicit producer and version only within the same original source",()=>{
 const choice={producer:"EXTERNAL_PLUGIN" as const,sessionId:"plugin-"+"a".repeat(32),revisionId:"b".repeat(64)};
 rememberViewedWork("p","d","sha1",choice);expect(readViewedWork("p","d","sha1")).toEqual(choice);expect(readViewedWork("p","d","sha2")).toBeNull();localStorage.clear();
});
