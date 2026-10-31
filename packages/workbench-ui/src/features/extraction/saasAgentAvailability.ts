import { useEffect, useState } from "react";
import { request } from "../../shared/api/client";
import { getSaasAccountIdentity } from "../../shared/api/saasSession";

export const SAAS_KIT_UNAVAILABLE_MESSAGE = "此兼容模式所需的 Agent Kit 或执行环境尚未提供，当前 SaaS 试点不可用。请使用文档的 Agent 协作任务。";
export const SAAS_LEGACY_STRUCTURE_UNAVAILABLE_MESSAGE = "旧结构指引与建议确认尚未在当前 SaaS 试点开放，请使用文档的 Agent 协作任务。";
type Availability = { ready: boolean; message: string };
const asRecord = (value: unknown): Record<string, unknown> => value && typeof value === "object" ? value as Record<string, unknown> : {};

/** Only the audited native provider loop is enabled in the SaaS pilot.
 * This is an offline configuration preflight, never a paid model/auth probe.
 * Local/Pair keeps its existing runtime contracts and makes no new request.
 */
export function useSaasNativeAgentAvailability() {
  const account = getSaasAccountIdentity();
  const [retry, setRetry] = useState(0);
  const pending: Availability = { ready: false, message: "正在读取 Agent 运行配置…" };
  const [state, setState] = useState<Availability & { account: string | null | undefined; retry: number }>({ ...pending, account: undefined, retry: -1 });
  useEffect(() => {
    if (account === undefined) return;
    if (!account) { setState({ ready: false, message: "请先登录后运行 Agent。", account, retry }); return; }
    const controller = new AbortController();
    setState({ ...pending, account, retry });
    void Promise.all([
      request<unknown>("/api/runtime/agent-readiness", { signal: controller.signal }),
      request<unknown>("/api/runtime/agent-workflow-readiness", { signal: controller.signal }),
    ]).then(([value, workflowValue]) => {
      if (controller.signal.aborted || getSaasAccountIdentity() !== account) return;
      const record = asRecord(value), workflow = asRecord(workflowValue);
      const configured = record.schema_version === "agent-runtime-readiness/v1" && record.runtime_id === "provider-tool-loop"
        && (record.provider_id === "azure" || record.provider_id === "aliyun")
        && typeof record.model_name === "string" && Boolean(record.model_name.trim()) && record.ready === true && record.configured === true
        && record.readiness_scope === "configuration" && record.authentication_verified === false;
      const ready = configured && workflow.schema_version === "agent-workflow-readiness/v1" && workflow.ready === true
        && workflow.runtime_id === "provider-tool-loop" && workflow.readiness_scope === "configuration_and_core"
        && workflow.provider_id === record.provider_id && workflow.model_name === record.model_name && workflow.authentication_verified === false
        && ["postgresql", "object_store", "durable_worker", "provider_configuration", "customer_data_authorization", "source_reader"]
          .every(name => asRecord(asRecord(workflow.checks)[name]).ready === true);
      setState({ account, retry, ready, message: ready
        ? "模型配置就绪；在线认证、权限与可用性尚未核验。"
        : configured ? "模型配置已就绪，但文档处理授权或后台运行环境未就绪，请联系管理员。"
        : "模型尚未配置或当前 Agent 运行时不可用，请联系管理员完成配置后刷新。" });
    }).catch(() => {
      if (!controller.signal.aborted && getSaasAccountIdentity() === account) {
        setState({ ready: false, message: "Agent 配置状态暂时无法读取，刷新成功前不能启动任务。", account, retry });
      }
    });
    return () => controller.abort();
  }, [account, retry]);
  return { ...(account === undefined ? { ready: true, message: "" } : state.account === account && state.retry === retry ? state : pending), refresh: () => setRetry(value => value + 1) };
}
