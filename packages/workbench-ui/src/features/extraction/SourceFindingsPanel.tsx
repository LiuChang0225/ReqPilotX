import type { SourceFindingV1, SourceFindingsV1 } from "../../shared/api/contracts";

const labels: Record<string,string> = { NUMBER_SEQUENCE_GAP:"编号间断", NUMBER_SEQUENCE_DUPLICATE:"重复编号", NUMBER_SEQUENCE_REVERSED:"编号回跳" };
function record(value: unknown): Record<string, unknown> { return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string,unknown> : {}; }
function endpoints(finding: SourceFindingV1) {
  const evidence=record(finding.evidence);
  return [record(evidence.before),record(evidence.after)].filter(point => typeof point.node_id === "string" || Array.isArray(point.pages));
}
export function SourceFindingsPanel({ value, sourceSha256, onLocate }: { value?: SourceFindingsV1; sourceSha256: string; onLocate: (nodeId: string, page?: number) => void }) {
  if (!value) return null;
  if (value.source_sha256 && value.source_sha256 !== sourceSha256) return <span className="source-findings-state">检查结果属于另一来源，待同步。</span>;
  const findings=value.findings || [];
  const done=value.scan_state === "COMPLETED";
  return <details className="source-findings-panel"><summary>{findings.length ? `来源提示 ${findings.length}` : done ? "当前检查范围无发现" : "来源检查未完成"}</summary>
    <div className="source-findings-content"><p>仅检查已保存的同级正文编号范围；不代表整份原件完整，也不自动补号或新增需求。</p>
      {!findings.length && <p>{done ? "本次范围内未产生提示；未检查的来源范围仍需核对。" : "尚无已完成的来源检查结果。"}</p>}
      {findings.map((finding,index) => <article key={finding.finding_id || index}>
        <strong>{labels[finding.kind] || "来源待核对"}{typeof finding.observed === "string" ? ` · ${finding.observed}` : ""}</strong>
        <span className="finding-review-state">{finding.source_review_state === "NOT_REVIEWED" || finding.status === "PENDING_SOURCE_REVIEW" ? "待回查原件" : String(finding.status || "待核对")}</span>
        <p>{typeof finding.message === "string" ? finding.message : finding.summary || "核对该范围的原件与已保存成果。"}</p>
        <div className="source-item-delivery-actions">{endpoints(finding).map((point,i) => <button className="button secondary compact" type="button" key={`${point.node_id}:${i}`} onClick={() => onLocate(String(point.node_id || ""), Array.isArray(point.pages) && typeof point.pages[0] === "number" ? point.pages[0] : undefined)}>查看 {String(point.source_number || (i ? "后一条款" : "前一条款"))} 原页</button>)}</div>
      </article>)}
    </div>
  </details>;
}
