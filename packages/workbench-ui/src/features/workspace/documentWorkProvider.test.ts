import { expect, it } from "vitest";
import { externalExecutionLabel, pluginCapability, workbenchCapability, pluginFeedbackLabel, type PluginState } from "./documentWorkProvider";
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

it("intersects managed-host and service capabilities without widening either",()=>{
  const state={capabilities:{save_feedback:{supported:true,enabled:true},save_engineer_response:{supported:true,enabled:true},save_delivery_version:{supported:true,enabled:true},export_draft:{supported:true,enabled:true}}} as PluginState;
  const flags={save_feedback:true,save_engineer_response:false};
  expect(workbenchCapability(state,"save_feedback",true,false,flags).enabled).toBe(true);
  expect(workbenchCapability(state,"save_engineer_response",true,false,flags).enabled).toBe(false);
  expect(workbenchCapability(state,"save_delivery_version",true,false,flags).enabled).toBe(false);
  expect(workbenchCapability(state,"save_feedback",false,false,flags).reason).toContain("历史版本");
  expect(workbenchCapability(state,"save_feedback",true,true,flags).enabled).toBe(false);
  expect(workbenchCapability(state,"export_draft",false,true,flags).enabled).toBe(true);
  expect(workbenchCapability({...state,capabilities:{save_feedback:{supported:false,enabled:true}}},"save_feedback",true,false,flags).enabled).toBe(false);
});
