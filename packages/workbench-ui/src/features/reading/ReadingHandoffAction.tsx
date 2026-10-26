import { useState } from "react";
import { storeReadingHandoffIntent, type ReadingFinding, type ReadingHandoff } from "../workspace/readingHandoff";
import { useNavigate } from "../../shared/navigation";

export function ReadingHandoffAction({ finding, handoff }: { finding: ReadingFinding; handoff: ReadingHandoff }) {
  const navigate = useNavigate();
  const [result, setResult] = useState(""), [error, setError] = useState("");
  const stale = finding.value.status !== "ACTIVE";
  return <div>
    <button type="button" disabled={Boolean(handoff.reason) || stale} title={stale ? "旧捕获内容不能转绑当前工作" : handoff.reason || `准备到原先查看的 v${handoff.number} 草稿，不会提交任务`} onClick={() => {
      setError(""); try { setResult(handoff.prepare(finding)); } catch (cause) { setError(cause instanceof Error ? cause.message : "未能准备草稿。"); }
    }}>带到工作台草稿</button>
    {error && <p role="alert">{error}</p>}
    {result && <div role="status"><p>已准备核对版 v{handoff.number} 的工作台草稿，尚未发送。{handoff.historical ? "这是历史查看版，保持只读；未改绑最新版本。" : ""}{!handoff.durable ? "本机存储不可用，草稿仅暂留当前页面会话。" : ""}</p>
      <button type="button" onClick={() => { try { if (handoff.target) { storeReadingHandoffIntent(handoff.target); navigate(handoff.target.url); } } catch (cause) { setError(cause instanceof Error ? cause.message : "请重试打开草稿。"); } }}>打开已准备草稿</button>
      <details><summary>查看已准备内容</summary><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{result}</pre></details></div>}
  </div>;
}
