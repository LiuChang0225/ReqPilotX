import { useRef, type CSSProperties } from "react";
import type { NativeStructureTreeNodeV1, SourceItemLedgerRowV1, SourceRequirementOutlineRecordV1, SourceScopeSummaryV1 } from "../../shared/api/contracts";
import { originalNumber, scopeCountLabel, scopeCountDetail } from "./sourcePresentation";
import { sourcePageLabel } from "./sourcePageLabel";
import { Icon } from "../../shared/ui/Icon";
import type { SourceItemDeliveryState } from "./SourceItemReasoning";
import { ENGINEER_DECISION_LABELS, type EngineerDecision } from "./EngineerOpinion";
import type { SourceItemReviewContext } from "./sourceItemReviewContext";
import { needsSourcePreview, SourceRecordPreview, type SourcePreviewReader } from "./SourceRecordPreview";
import { useUiCopy } from "../../host/HostLocaleContext";
import { engineerDecisionLabel } from "./EngineerOpinion";

type SourceRow = SourceItemLedgerRowV1 & { depth?: number };
type DerivedItem = NonNullable<SourceItemDeliveryState["decomposition"]>["records"][string][number];
type TableRow = { id: string; source: SourceRow; child?: DerivedItem; unitId?: string };
export type SourceLedgerLens = "source" | "requirements" | "analysis";
export const derivedTableRowId = (unitId: string) => `derived:${unitId}`;

/** Source order and engineering derivation stay distinct, on one table surface. */
export function projectLedgerTableRows(rows: SourceRow[], reasoning: SourceItemDeliveryState | null,
  collapsedDerivedIds: ReadonlySet<string>, visibleDerivedIds?: ReadonlySet<string>): TableRow[] {
  return rows.flatMap(source => {
    const children = source.source_item_id && !collapsedDerivedIds.has(source.source_item_id)
      ? reasoning?.decomposition?.records[source.source_item_id] || [] : [];
    return [{ id: source.row_id, source }, ...children.flatMap((child, index) => {
      const unitId = child.singular_requirement_id || child.node_id;
      if (visibleDerivedIds && (!unitId || !visibleDerivedIds.has(unitId))) return [];
      return [{ id: derivedTableRowId(unitId || `${source.row_id}:unbound:${index}`), source, child, unitId }];
    })];
  });
}

/** Stage fields are a projection: stable source identity is always kept, analysis is never implied. */
export function SourceLedgerTable({ rows, nodes, records, selectedId, selectedDerivedId, expandedIds,
  collapsedDerivedIds = new Set(), visibleDerivedIds, onSelect, onSelectDerived, onToggle, onToggleDerived,
  titleFor, kindFor, reasoning, review, scopeSummaries, previewReader,
  lens = "analysis", requirementsOnly = false, ancestorIds, ancestorPathFor }: {
  rows: SourceRow[];
  scopeSummaries?: Record<string, SourceScopeSummaryV1>;
  previewReader?: SourcePreviewReader;
  nodes: ReadonlyMap<string, NativeStructureTreeNodeV1>;
  records: ReadonlyMap<string, SourceRequirementOutlineRecordV1>;
  selectedId: string; selectedDerivedId?: string; expandedIds: ReadonlySet<string>;
  collapsedDerivedIds?: ReadonlySet<string>; visibleDerivedIds?: ReadonlySet<string>;
  onSelect: (row: SourceItemLedgerRowV1) => void; onToggle: (id: string) => void;
  onSelectDerived?: (row: SourceItemLedgerRowV1, unitId: string) => void;
  onToggleDerived?: (sourceId: string) => void;
  titleFor: (row: SourceItemLedgerRowV1) => string;
  kindFor: (item: SourceRequirementOutlineRecordV1) => string;
  reasoning: SourceItemDeliveryState | null; review?: SourceItemReviewContext | null;
  lens?: SourceLedgerLens; requirementsOnly?: boolean;
  ancestorIds?: ReadonlySet<string>;
  ancestorPathFor?: (record: SourceRequirementOutlineRecordV1) => string;
}) {
  const l = useUiCopy();
  const table = useRef<HTMLTableElement>(null);
  const activeLens: SourceLedgerLens = requirementsOnly ? "requirements" : lens;
  const contextAncestorIds = (() => {
    const ids = new Set(ancestorIds || []);
    const rowsById = new Map(rows.map(row => [row.row_id, row]));
    for (const row of rows) {
      const record = row.source_item_id ? records.get(row.source_item_id) : undefined;
      if (record?.requirementness !== "REQUIREMENT") continue;
      let parent = row.parent_row_id;
      while (parent && !ids.has(parent)) {
        ids.add(parent);
        parent = rowsById.get(parent)?.parent_row_id || null;
      }
    }
    return ids;
  })();
  const tableRows = projectLedgerTableRows(rows, reasoning, collapsedDerivedIds, visibleDerivedIds)
    .filter(entry => {
      if (!requirementsOnly || entry.child) return true;
      if (contextAncestorIds.has(entry.source.row_id)) return true;
      const record = entry.source.source_item_id ? records.get(entry.source.source_item_id) : undefined;
      // A missing read is not proof of a non-requirement; omit unverified leaves instead of relabelling them.
      return record?.requirementness === "REQUIREMENT";
    });
  const lineageRows = new Map(rows.map(row => [row.row_id, row]));
  const sourceDepth = (row: SourceRow) => {
    let depth = 0; let parent = row.parent_row_id; const visited = new Set([row.row_id]);
    while (parent && lineageRows.has(parent) && !visited.has(parent)) { visited.add(parent); depth += 1; parent = lineageRows.get(parent)!.parent_row_id; }
    return depth;
  };
  const activeId = selectedDerivedId ? derivedTableRowId(selectedDerivedId) : selectedId;
  const select = (entry: TableRow) => {
    if (entry.child) { if (entry.unitId) onSelectDerived?.(entry.source, entry.unitId); }
    else onSelect(entry.source);
  };
  const headers = activeLens === "analysis"
    ? [["col-original-number", l("ledger.originalNumber", "客户原编号")], ["col-content", l("ledger.hierarchy", "原文与派生层级")],
      ["col-identity", l("ledger.identity", "系统标识 / 类别")], ["col-response", l("ledger.response", "响应 / 需处理")]]
    : activeLens === "requirements"
      ? [["col-original-number", l("ledger.originalNumber", "客户原编号")], ["col-content", l("ledger.requirementContent", "需求内容与层级")],
        ["col-identity", l("ledger.requirementIdentity", "REQ / 类别")]]
      : [["col-original-number", l("ledger.originalNumber", "客户原编号")], ["col-content", l("ledger.sourceContent", "提取原文与层级")],
        ["col-identity", l("ledger.sourceIdentity", "来源 / SRC")]];
  return <table ref={table} className={`source-ledger-table is-${activeLens}-lens`} data-lens={activeLens}
    aria-label={activeLens === "requirements" ? l("ledger.requirementsAria", "需求清单表格") : l("ledger.aria", "原文条目表格")}
    onKeyDown={event => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const rowId = (event.target as HTMLElement).closest<HTMLTableRowElement>("tr[data-ledger-row]")?.dataset.ledgerRow;
    const index = tableRows.findIndex(row => row.id === rowId);
    if (index < 0) return;
    event.preventDefault();
    const next = tableRows[event.key === "Home" ? 0 : event.key === "End" ? tableRows.length - 1 : Math.max(0, Math.min(tableRows.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)))];
    if (!next) return;
    select(next);
    table.current?.querySelector<HTMLButtonElement>(`[data-ledger-row="${CSS.escape(next.id)}"] .ledger-select`)?.focus();
  }}><thead><tr>{headers.map(([className, label]) => <th key={className} className={className} aria-label={label}>{className === "col-original-number" ? l("ledger.originalNumberShort", "原编号") : label}</th>)}</tr></thead><tbody>
    {tableRows.map(entry => {
      const row = entry.source, child = entry.child;
      const record = row.source_item_id ? records.get(row.source_item_id) : undefined;
      const node = nodes.get(row.structure_node_id);
      const context = record && !child ? review?.records[record.source_record_id] : undefined;
      const unitId = child ? entry.unitId : record?.source_record_id;
      const manual = unitId ? reasoning?.engineer_responses?.records[unitId] : null;
      const decision = unitId ? manual?.state === "CURRENT" ? manual : reasoning?.assessments?.records[unitId] : null;
      const children = record ? reasoning?.decomposition?.records[record.source_record_id] : null;
      const isParent = !child && Boolean(children?.length);
      const stale = manual?.state === "STALE" || !decision && unitId && reasoning?.assessments?.carry_forward?.stale_unit_ids?.includes(unitId);
      const skipped = (reasoning as SourceItemDeliveryState & { analysis_policy?: { status: string; reason: string } } | null)?.analysis_policy?.status === "SKIPPED";
      const response = !record ? "" : isParent ? l(children!.length === 1 ? "ledger.derivedCountOne" : "ledger.derivedCount", "{count} 个派生项", { count: children!.length }) : record.requirementness !== "REQUIREMENT" ? l("ledger.retainSource", "保留原文") : decision ? engineerDecisionLabel(decision.decision as EngineerDecision, l) || l("ledger.pendingJudgment", "待判断") : skipped ? l("reasoning.complianceSkipped", "符合性分析未执行").replace(/^ · /, "") : stale ? l("ledger.reanalyze", "待重新分析") : reasoning ? l("ledger.notAnalyzed", "尚未分析") : l("ledger.reading", "正在读取…");
      const title = child ? child.text : titleFor(row);
      const ownPages = child?.source_excerpts?.flatMap(excerpt => excerpt.page ? [excerpt.page] : []) || [];
      const pages = ownPages.length ? [...new Set(ownPages)] : record?.pages || node?.pages || [];
      const expanded = isParent ? !collapsedDerivedIds.has(record!.source_record_id) : expandedIds.has(row.row_id);
      const canExpand = !child && (isParent || row.child_row_ids.length > 0);
      const ancestorPath = !child && record && activeLens === "requirements"
        ? (ancestorPathFor?.(record) || record.ancestor_path.filter(Boolean).join(" / "))
        : "";
      const evidenceCount = decision && "evidence" in decision ? decision.evidence?.length || 0 : 0;
      return <tr key={entry.id} data-ledger-row={entry.id} data-depth={sourceDepth(row) + (child ? 1 : 0)} data-parent-ledger-row={child ? row.row_id : undefined}
        className={`${activeId === entry.id ? "is-selected " : ""}${child ? "is-derived" : !record ? "is-structure" : isParent ? "is-derived-parent" : ""}`}>
        <td className="col-original-number ledger-original-number"><span title={child ? l("ledger.derivedOriginalNumber", "派生项的来源原编号：{value}", { value: String(originalNumber(child, l) ?? "") }) : record ? l("ledger.sourceOriginalNumber", "客户原编号：{value}", { value: String(originalNumber(record, l) ?? "") }) : l("ledger.sourceSectionNumber", "原文章节编号")}>{child ? originalNumber(child, l) : record ? originalNumber(record, l) : node?.label || "—"}</span>{child && <small>{l("ledger.sourceOriginalNumberShort", "来源原编号")}</small>}</td>
        <td className="col-content"><div className="ledger-title-cell" style={{ paddingLeft: `${8 + (sourceDepth(row) + (child ? 1 : 0)) * 18}px`, "--ledger-depth": sourceDepth(row) + (child ? 1 : 0) } as CSSProperties}>
          {canExpand ? <button type="button" className="ledger-expand" aria-label={expanded ? l("ledger.collapseAria", "收起 {label}", { label: isParent ? l("ledger.derivedItemsOf", "{id} 的派生项", { id: record!.requirement_display_id || "" }) : titleFor(row) }) : l("ledger.expandAria", "展开 {label}", { label: isParent ? l("ledger.derivedItemsOf", "{id} 的派生项", { id: record!.requirement_display_id || "" }) : titleFor(row) })} aria-expanded={expanded} onClick={() => {
            if (row.child_row_ids.length && expandedIds.has(row.row_id) === expanded) onToggle(row.row_id);
            if (isParent) onToggleDerived?.(record!.source_record_id);
          }}><Icon name={expanded ? "chevron-down" : "chevron"} size={13} /></button> : child ? <span className="ledger-derived-mark"><Icon name="split" size={13} /></span> : <span className="ledger-expand-spacer" />}
          <button type="button" className="ledger-select" aria-label={child ? `${child.requirement_display_id} ${child.text}` : undefined}
            aria-current={activeId === entry.id ? "true" : undefined} disabled={Boolean(child && !entry.unitId)} onClick={() => select(entry)}>
            {!child && record && previewReader && needsSourcePreview(title, record) ? <SourceRecordPreview key={`${previewReader.scope}:${record.source_record_id}`} label={title} record={record} reader={previewReader} /> : <strong>{title}</strong>}<small>{child ? l("ledger.derivedFrom", "派生自 {id} · ", { id: record?.requirement_display_id || record?.source_display_id || "" }) : isParent ? l("ledger.completeParent", "完整父条款 · ") : ""}{sourcePageLabel(pages, l)}{!child && row.is_table_structure ? l("ledger.originalTable", " · 原表") : !child && record?.source_form?.startsWith("TABLE") ? l("ledger.tableItem", " · 表格条目") : ""}</small>
            {ancestorPath && <small className="ledger-ancestor-path" title={l("ledger.ancestorPathTitle", "完整祖先路径")}>{ancestorPath}</small>}
          </button>
        </div></td>
        {activeLens === "analysis" ? <td className="col-identity">{record ? <><span className="ledger-source-id">{child?.requirement_display_id || record.requirement_display_id || record.source_display_id}</span><small>{kindFor(child ? { ...record, requirement_kinds: child.requirement_kinds || record.requirement_kinds } : record)}</small></> : entry.source.source_item_id ? <><span>{l("ledger.unread", "尚未读取")}</span><small>{l("ledger.unreadContext", "尚未读取判定工件")}</small></> : <><span>{l("ledger.structure", "结构")}</span><small title={scopeCountDetail(scopeSummaries?.[row.structure_node_id], l)}>{scopeCountLabel(scopeSummaries?.[row.structure_node_id], row.direct_source_item_count, l)}</small></>}</td> : activeLens === "requirements" ? <td className="col-identity">{record ? (record.requirementness === "REQUIREMENT" ? <><span className="ledger-source-id">{child?.requirement_display_id || record.requirement_display_id || l("ledger.notSaved", "尚未保存")}</span><small>{kindFor(child ? { ...record, requirement_kinds: child.requirement_kinds || record.requirement_kinds } : record)}</small><small className="ledger-source-ref">{l("ledger.sourceRef", "来源 {id}", { id: record.source_display_id })}</small></> : <><span className="ledger-source-id">{l("ledger.sourceContext", "来源上下文")}</span><small>{l("ledger.sourceContextNote", "保留原文 · 非需求")}</small><small className="ledger-source-ref">{l("ledger.sourceRef", "来源 {id}", { id: record.source_display_id })}</small></>) : entry.source.source_item_id ? <><span className="ledger-source-id">{l("ledger.sourceContext", "来源上下文")}</span><small>{l("ledger.unreadContext", "尚未读取判定工件")}</small></> : <><span>{l("ledger.structure", "结构")}</span><small>{l("ledger.ancestorScope", "祖先层级 · 不单独计为需求")}</small></>}</td> : <td className="col-identity">{record ? <><span className="ledger-source-id">{record.source_display_id}</span><small>{kindFor(record)}</small></> : entry.source.source_item_id ? <><span>{l("ledger.unread", "尚未读取")}</span><small>{l("ledger.unreadContext", "尚未读取判定工件")}</small></> : <><span>{l("ledger.structure", "结构")}</span><small title={scopeCountDetail(scopeSummaries?.[row.structure_node_id], l)}>{scopeCountLabel(scopeSummaries?.[row.structure_node_id], row.direct_source_item_count, l)}</small></>}</td>}
        {activeLens === "analysis" && <td className="col-response"><span className={decision?.decision === "PENDING_EVIDENCE" || decision?.decision === "NEEDS_CLARIFICATION" ? "ledger-response-pending" : ""}>{response}</span>
          {decision && !isParent && <small>{manual?.state === "CURRENT" ? l("ledger.engineerOpinion", "工程师意见") : l("ledger.agentSuggestion", "Agent 建议")}</small>}
          {decision && !isParent && <small className="ledger-evidence-state">{evidenceCount ? l("ledger.evidenceLinked", "引用依据 {count} 条", { count: evidenceCount }) : l("ledger.noEvidenceLinked", "无引用依据")}</small>}
          {manual?.state === "STALE" && !isParent && <small>{l("ledger.previousNeedsReview", "前版人工意见待重评")}</small>}
          {!record && row.source_item_id && !isParent && <small>{l("ledger.unreadContext", "尚未读取判定工件")}</small>}
          {context?.needs_attention && <small className="ledger-attention">{l("ledger.needsAttention", "需关注")}</small>}
          {context?.changed_from_previous && <small>{l("ledger.changed", "本次变化")}</small>}
        </td>}
      </tr>;
    })}
  </tbody></table>;
}
