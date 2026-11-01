import { useEffect, useRef, useState } from "react";
import type { SourceRequirementOutlineRecordV1, SourceRequirementRecordDetailV1 } from "../../shared/api/contracts";

export type SourcePreviewReader = {
  scope: string; sourceSha256: string; baselineSha256: string; mapSha256: string;
  loadRecord: (id: string, signal?: AbortSignal) => Promise<SourceRequirementRecordDetailV1>;
  cache: Map<string, string>;
};

export function needsSourcePreview(label: string, record: SourceRequirementOutlineRecordV1) {
  if (record.source_form?.startsWith("TABLE")) return Boolean(record.has_owned_text);
  if (record.source_form === "IMAGE") return false;
  const numbers = [record.source_number, ...(record.source_numbers || []).map(value => typeof value === "string" ? value : value.value)].filter(Boolean);
  return Boolean(label.trim() && numbers.some(value => value?.trim() === label.trim()));
}

export function sourcePreviewText(value: SourceRequirementRecordDetailV1, id: string, reader: SourcePreviewReader, numberLabel: string) {
  if (value.source_sha256 !== reader.sourceSha256 || value.baseline_artifact_sha256 !== reader.baselineSha256
    || value.document_map_artifact_sha256 !== reader.mapSha256 || value.record.source_record_id !== id) throw new Error("原文版本不一致");
  return value.record.text_bindings.find(binding => binding.text.trim() && binding.text.trim() !== numberLabel.trim())?.text.trim() || "";
}

/** An excerpt of the bound original, never a replacement source title or stored label. */
export function SourceRecordPreview({ label, record, reader }: { label: string; record: SourceRequirementOutlineRecordV1; reader: SourcePreviewReader }) {
  const element = useRef<HTMLSpanElement>(null);
  const key = `${reader.scope}:${record.source_record_id}`;
  const [visible, setVisible] = useState(false);
  const [entry, setEntry] = useState<{ key: string; text: string } | null>(null);
  const text = entry?.key === key ? entry.text : reader.cache.get(record.source_record_id) || "";
  useEffect(() => {
    const target = element.current;
    if (!target) return;
    if (typeof IntersectionObserver === "undefined") { setVisible(true); return; }
    const observer = new IntersectionObserver(entries => { if (entries.some(item => item.isIntersecting)) { setVisible(true); observer.disconnect(); } },
      { root: target.closest(".native-hierarchy-tree-pane"), rootMargin: "100px" });
    observer.observe(target);
    return () => observer.disconnect();
  }, [key]);
  useEffect(() => {
    if (!visible || reader.cache.has(record.source_record_id)) return;
    const controller = new AbortController();
    void reader.loadRecord(record.source_record_id, controller.signal).then(value => {
      if (controller.signal.aborted) return;
      const text = sourcePreviewText(value, record.source_record_id, reader, label);
      reader.cache.set(record.source_record_id, text);
      setEntry({ key, text });
    }).catch(() => { /* The original label and full-detail action remain available. */ });
    return () => controller.abort();
  }, [key, record.source_record_id, reader, visible, label]);
  return <span ref={element} className="ledger-record-label">{text ? <>{record.source_form.startsWith("TABLE") && <strong>{label} · </strong>}<span className="ledger-source-excerpt">{text}</span><span className="ledger-excerpt-origin">原文摘录</span></> : <strong>{label}</strong>}</span>;
}
