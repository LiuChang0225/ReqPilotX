import { useEffect, useRef, useState } from "react";
import { evidenceFileName, openKbEvidenceApi, type EvidenceUploadMetadata, type EvidenceUploadResult, type OpenKbEvidenceStatus } from "./openKbEvidenceApi";

const defaults: EvidenceUploadMetadata = { doc_type: "REFERENCE", authority: "UNKNOWN", claim_role: "UNKNOWN", applicability: "UNKNOWN" };
const authorityOptions = [["UNKNOWN", "未标记"], ["INTERNAL_CONTROLLED", "内部受控"], ["CUSTOMER_APPROVED", "客户认可"], ["REGULATORY_AUTHORITY", "监管机构"], ["CERTIFICATION_BODY", "认证机构"], ["TEST_LAB", "试验机构"]];
const roleOptions = [["UNKNOWN", "未标记"], ["NORMATIVE_REQUIREMENT", "规范要求"], ["DESIGN_DECLARATION", "设计声明"], ["VALIDATION_RESULT", "验证结果"], ["TEST_RESULT", "试验结果"], ["CERTIFICATION", "认证记录"], ["COMPLIANCE_RECORD", "符合性记录"], ["RELEASED_DESIGN_EVIDENCE", "已发布设计依据"], ["REFERENCE_CONTEXT", "参考资料"]];

/** Keyed by project by its caller so picked files and late results never cross project boundaries. */
export function NativeEvidenceRegistration({ projectId, onRegistered, disabled = false }: { projectId: string; onRegistered: () => void; disabled?: boolean }) {
  const [status, setStatus] = useState<OpenKbEvidenceStatus | null>(null), [statusError, setStatusError] = useState("");
  const [statusRevision, setStatusRevision] = useState(0), [files, setFiles] = useState<File[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [metadata, setMetadata] = useState(defaults), [pending, setPending] = useState(false);
  const [result, setResult] = useState<EvidenceUploadResult | null>(null), [error, setError] = useState("");
  const upload = useRef<AbortController | null>(null), mounted = useRef(true);
  useEffect(() => {
    if (!projectId || !expanded) return;
    const controller = new AbortController(); setStatusError("");
    void openKbEvidenceApi.status(projectId, controller.signal).then(value => { if (!controller.signal.aborted) setStatus(value); })
      .catch(cause => { if (!controller.signal.aborted) { setStatus(null); setStatusError(cause instanceof Error ? cause.message : "资料登记入口暂不可用。"); } });
    return () => controller.abort();
  }, [projectId, statusRevision, expanded]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; upload.current?.abort(); }; }, []);
  const select = (chosen: FileList | null) => {
    if (!chosen?.length || upload.current || disabled) return;
    setFiles(Array.from(chosen)); setResult(null); setError("");
  };
  const register = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!projectId || !status?.available || !files.length || upload.current || disabled) return;
    const controller = new AbortController(); upload.current = controller;
    setPending(true); setError(""); setResult(null);
    try {
      const receipt = await openKbEvidenceApi.upload(projectId, files, metadata, controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      setResult(receipt);
      // Keep only failed inputs, so a partial-success retry never reuploads successful originals.
      const failedNames = receipt.documents.filter(item => item.status === "FAILED").map(item => item.file_name);
      setFiles(files.filter(file => failedNames.includes(evidenceFileName(file))));
      if (receipt.registered_count) {
        try { onRegistered(); } catch { setError("原件已登记，资料目录暂未更新。请同步资料后核对。"); }
        setStatusRevision(value => value + 1);
      }
    } catch (cause) {
      if (!mounted.current) return;
      setError(controller.signal.aborted ? "已停止等待。服务端可能已登记部分原件，请同步资料核对后再重试。"
        : `${cause instanceof Error ? cause.message : "原件登记失败。"} 已选文件仍保留；连接中断时请先同步资料，核对是否已登记。`);
    } finally { if (upload.current === controller) upload.current = null; if (mounted.current) setPending(false); }
  };
  return <details className="native-evidence-registration" onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary>登记项目证据原件</summary>
    <p>上传实际选择的文件，文件夹会保留相对名称。仅登记来源，不运行索引、OpenKB 检索或付费模型；登记成功不表示已证明符合性。</p>
    {!projectId ? <p>先选择项目，再登记原件。</p> : <>
      {status && <p className="muted">项目资料版本 {status.knowledge_base_version} · OpenKB 实时检索未运行{status.runtime_available === false ? " · 检索运行环境不可用" : ""}</p>}
      {status && !status.available && <p role="alert">本项目的来源登记入口当前不可用。已有资料仍可核对。</p>}
      {statusError && <p role="alert">{statusError} <button type="button" className="button ghost compact" disabled={pending} onClick={() => setStatusRevision(value => value + 1)}>重试资料状态</button></p>}
      {!status && !statusError && <p role="status">正在读取来源登记状态…</p>}
      <form onSubmit={event => void register(event)}>
        <div className="native-evidence-file-pickers">
          <label>选择证据文件<input type="file" multiple disabled={pending || disabled || !status?.available} onChange={event => { select(event.target.files); event.target.value = ""; }} /></label>
          <label>选择证据文件夹<input type="file" multiple ref={input => { input?.setAttribute("webkitdirectory", ""); }} disabled={pending || disabled || !status?.available} onChange={event => { select(event.target.files); event.target.value = ""; }} /></label>
        </div>
        <small>文件夹选择取决于浏览器支持；不会读取或提交任意服务器路径。</small>
        <div className="native-evidence-metadata">
          <label>资料类型<select value={metadata.doc_type} disabled={pending || disabled} onChange={event => setMetadata(value => ({ ...value, doc_type: event.target.value }))}>{[["REFERENCE", "参考资料"], ["STANDARD", "标准"], ["SPEC", "规格"], ["REGULATION", "法规"], ["CASE", "案例"]].map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></label>
          <label>来源权威<select value={metadata.authority} disabled={pending || disabled} onChange={event => setMetadata(value => ({ ...value, authority: event.target.value }))}>{authorityOptions.map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></label>
          <label>证据用途<select value={metadata.claim_role} disabled={pending || disabled} onChange={event => setMetadata(value => ({ ...value, claim_role: event.target.value }))}>{roleOptions.map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></label>
          <label>适用性<input type="text" maxLength={500} value={metadata.applicability} disabled={pending || disabled} onChange={event => setMetadata(value => ({ ...value, applicability: event.target.value }))} /></label>
        </div>
        <p className="muted">权威、用途与适用性需按真实登记信息填写；不确定时保留 UNKNOWN。</p>
        {files.length > 0 && <ul aria-label="待登记证据文件">{files.map((file, index) => <li key={`${evidenceFileName(file)}:${index}`}>{evidenceFileName(file)}</li>)}</ul>}
        <div className="native-evidence-actions"><button type="submit" className="button secondary compact" disabled={pending || disabled || !status?.available || !files.length}>{pending ? "正在登记原件…" : result?.failed_count ? "重试未登记文件" : "登记所选原件"}</button>
          {pending ? <button type="button" className="button ghost compact" onClick={() => { upload.current?.abort(); setError("已停止等待。服务端可能已登记部分原件，请同步资料核对后再重试。"); }}>停止等待</button> : files.length > 0 && <button type="button" className="button ghost compact" onClick={() => { setFiles([]); setResult(null); setError(""); }}>清除所选文件</button>}</div>
      </form>
      {error && <p role="alert">{error}</p>}
      {result && <div className="native-evidence-upload-result" role={result.registered_count ? "status" : "alert"}>
        <strong>{result.failed_count ? result.registered_count ? "部分原件已登记" : "本次原件未登记" : "原件登记完成"}：{result.registered_count} 成功，{result.failed_count} 失败 · 资料版本 {result.knowledge_base_version}</strong>
        <ul>{result.documents.map((item, index) => <li key={`${item.file_name}:${index}`}>{item.file_name}：{item.status === "REGISTERED" ? "已登记" : "未登记"}{item.message ? ` · ${item.message}` : ""}</li>)}</ul>
      </div>}
    </>}
  </details>;
}
