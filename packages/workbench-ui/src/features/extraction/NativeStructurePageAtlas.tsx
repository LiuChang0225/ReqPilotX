import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import type {
  NativeStructureLogicalTableV1,
  NativeStructurePageUnitV1,
  NativeStructureProposalV1,
  NativeStructureReferencePublicV1,
  NativeStructureUnresolvedV1,
} from "../../shared/api/contracts";
import { cx } from "../../shared/lib/format";
import { Icon } from "../../shared/ui/Icon";

interface NativeStructurePageAtlasProps {
  units: readonly NativeStructurePageUnitV1[];
  pageCount: number;
  receipts: NativeStructureReferencePublicV1["visual_summary"]["receipts"];
  logicalTables: readonly NativeStructureLogicalTableV1[];
  unresolved: readonly NativeStructureUnresolvedV1[];
  proposals: readonly NativeStructureProposalV1[];
  activePage: number;
  agentFocusPages: readonly number[];
  onSelectPage: (page: number) => void;
  onClose: () => void;
}

interface AtlasUnit {
  unitId: string;
  title: string;
  pages: number[];
  titleAuthority: string;
}

interface PageReceipt {
  state: string;
  purpose: string;
}

type AtlasFilter = "ALL" | "TABLE" | "ATTENTION" | "AGENT";

function integer(value: unknown): number {
  const result = Math.trunc(Number(value || 0));
  return Number.isFinite(result) ? result : 0;
}

function text(value: unknown): string {
  return String(value || "").trim();
}

function publicPagePurpose(receipt: PageReceipt | undefined): string {
  if (!receipt) return "点击查看原 PDF";
  if (receipt.state !== "OPENED") return "视觉检查尚未完成，可打开原 PDF";
  if (/[㐀-鿿]/u.test(receipt.purpose)) return receipt.purpose;
  return "页面证据已检查，可打开原文核对";
}

function pageEvidenceLabel(receipt: PageReceipt | undefined): string {
  if (!receipt) return "已建立页位";
  return receipt.state === "OPENED" ? "已视觉检查" : "视觉检查待完成";
}

function pageRange(pages: readonly number[]): string {
  if (pages.length === 0) return "未绑定页面";
  const ranges: string[] = [];
  let start = pages[0];
  let previous = pages[0];
  for (const page of pages.slice(1)) {
    if (page === previous + 1) {
      previous = page;
      continue;
    }
    ranges.push(start === previous ? `P${start}` : `P${start}–P${previous}`);
    start = page;
    previous = page;
  }
  ranges.push(start === previous ? `P${start}` : `P${start}–P${previous}`);
  return ranges.join("、");
}

function normalizedPages(
  pages: readonly number[],
  pageCount: number,
): number[] {
  return Array.from(
    new Set(
      pages
        .map((page) => integer(page))
        .filter((page) => page >= 1 && page <= pageCount),
    ),
  ).sort((left, right) => left - right);
}

function atlasUnits(
  units: readonly NativeStructurePageUnitV1[],
  pageCount: number,
): AtlasUnit[] {
  const assigned = new Set<number>();
  const result = units
    .map((unit) => {
      const pages = normalizedPages(unit.pages, pageCount);
      pages.forEach((page) => assigned.add(page));
      return {
        unitId: unit.unit_id,
        title: unit.title || "未命名文档范围",
        pages,
        titleAuthority: unit.title_authority,
      };
    })
    .filter((unit) => unit.pages.length > 0)
    .sort((left, right) => left.pages[0] - right.pages[0]);

  const remaining = Array.from({ length: pageCount }, (_, index) => index + 1)
    .filter((page) => !assigned.has(page));
  if (remaining.length > 0) {
    result.push({
      unitId: "unassigned-pages",
      title: "其他页面",
      pages: remaining,
      titleAuthority: "UNCLASSIFIED",
    });
  }
  if (result.length === 0 && pageCount > 0) {
    result.push({
      unitId: "whole-document",
      title: "整份文档",
      pages: Array.from({ length: pageCount }, (_, index) => index + 1),
      titleAuthority: "UNCLASSIFIED",
    });
  }
  return result;
}

function receiptMap(
  receipts: NativeStructurePageAtlasProps["receipts"],
): Map<number, PageReceipt> {
  const result = new Map<number, PageReceipt>();
  for (const raw of receipts) {
    const page = integer(raw.page);
    if (page < 1) continue;
    result.set(page, {
      state: text(raw.state).toUpperCase(),
      purpose: text(raw.purpose),
    });
  }
  return result;
}

function countByPage(
  pages: readonly number[],
  result: Map<number, number>,
): void {
  for (const page of new Set(pages.map((item) => integer(item)))) {
    if (page < 1) continue;
    result.set(page, (result.get(page) || 0) + 1);
  }
}

export function NativeStructurePageAtlas({
  units,
  pageCount,
  receipts,
  logicalTables,
  unresolved,
  proposals,
  activePage,
  agentFocusPages,
  onSelectPage,
  onClose,
}: NativeStructurePageAtlasProps) {
  const groupedUnits = useMemo(
    () => atlasUnits(units, pageCount),
    [pageCount, units],
  );
  const receiptsByPage = useMemo(() => receiptMap(receipts), [receipts]);
  const tableCounts = useMemo(() => {
    const result = new Map<number, number>();
    logicalTables.forEach((table) => countByPage(table.pages, result));
    return result;
  }, [logicalTables]);
  const crossPageTableCounts = useMemo(() => {
    const result = new Map<number, number>();
    logicalTables
      .filter((table) => new Set(table.pages).size > 1)
      .forEach((table) => countByPage(table.pages, result));
    return result;
  }, [logicalTables]);
  const attentionScopes = useMemo(
    () => [
      ...unresolved.map((finding) => normalizedPages(finding.pages, pageCount)),
      ...proposals
        .filter((proposal) => !["CONFIRMED", "UNDONE"].includes(proposal.status))
        .map((proposal) => normalizedPages([proposal.page], pageCount)),
    ].filter((pages) => pages.length > 0),
    [pageCount, proposals, unresolved],
  );
  const attentionCounts = useMemo(() => {
    const result = new Map<number, number>();
    attentionScopes.forEach((pages) => countByPage(pages, result));
    return result;
  }, [attentionScopes]);
  const focusPages = useMemo(
    () => new Set(normalizedPages(agentFocusPages, pageCount)),
    [agentFocusPages, pageCount],
  );
  const [filter, setFilter] = useState<AtlasFilter>("ALL");
  const visibleUnits = useMemo(
    () =>
      groupedUnits
        .map((unit) => ({
          unit,
          visiblePages: unit.pages.filter((page) => {
            if (filter === "TABLE") return (tableCounts.get(page) || 0) > 0;
            if (filter === "ATTENTION") {
              return (attentionCounts.get(page) || 0) > 0;
            }
            if (filter === "AGENT") return focusPages.has(page);
            return true;
          }),
        }))
        .filter((item) => item.visiblePages.length > 0),
    [attentionCounts, filter, focusPages, groupedUnits, tableCounts],
  );
  const orderedPages = useMemo(
    () => visibleUnits.flatMap((item) => item.visiblePages),
    [visibleUnits],
  );
  const rovingPage = orderedPages.includes(activePage)
    ? activePage
    : orderedPages[0] || 0;
  const [expandedUnitIds, setExpandedUnitIds] = useState<Set<string>>(
    new Set(),
  );
  const pageButtonRefs = useRef(new Map<number, HTMLButtonElement>());

  useEffect(() => {
    setExpandedUnitIds((current) => {
      const availableIds = new Set(groupedUnits.map((unit) => unit.unitId));
      const next = new Set(
        [...current].filter((unitId) => availableIds.has(unitId)),
      );
      if (pageCount <= 60) {
        groupedUnits.forEach((unit) => next.add(unit.unitId));
      } else {
        groupedUnits
          .filter(
            (unit) =>
              unit.pages.includes(activePage) ||
              unit.pages.some((page) => focusPages.has(page)),
          )
          .forEach((unit) => next.add(unit.unitId));
        if (next.size === 0 && groupedUnits[0]) next.add(groupedUnits[0].unitId);
      }
      return next;
    });
  }, [activePage, focusPages, groupedUnits, pageCount]);

  const moveFocus = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    page: number,
  ) => {
    const index = orderedPages.indexOf(page);
    if (index < 0) return;
    let nextIndex = index;
    if (event.key === "ArrowRight") nextIndex = Math.min(orderedPages.length - 1, index + 1);
    else if (event.key === "ArrowLeft") nextIndex = Math.max(0, index - 1);
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = orderedPages.length - 1;
    else return;
    event.preventDefault();
    pageButtonRefs.current.get(orderedPages[nextIndex])?.focus();
  };

  const openedPageCount = Array.from(receiptsByPage.values()).filter(
    (receipt) => receipt.state === "OPENED",
  ).length;

  return (
    <section className="native-page-atlas" aria-label="全文页面图谱">
      <header className="native-page-atlas-header">
        <div>
          <strong>全文页面图谱</strong>
          <span>
            {groupedUnits.length} 个文档范围 · {pageCount} 页 · {openedPageCount} 页有视觉检查回执
          </span>
          <small>页面块是证据摘要；点击后以原 PDF 为准</small>
        </div>
        <div className="native-page-atlas-legend" aria-label="页面图谱图例">
          <span><i className="is-opened" />视觉检查</span>
          <span><i className="has-table" />包含逻辑表</span>
          <span><i className="has-attention" />需要关注</span>
          <span><i className="is-agent-focus" />Agent 当前范围</span>
          <div className="native-page-atlas-filters" aria-label="页面图谱筛选">
            {([
              ["ALL", "全部"],
              ["TABLE", "表格"],
              ["ATTENTION", "需关注"],
              ["AGENT", "当前范围"],
            ] as const).map(([value, label]) => (
              <button
                type="button"
                aria-pressed={filter === value}
                className={filter === value ? "is-active" : ""}
                key={value}
                onClick={() => setFilter(value)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <button type="button" className="button ghost compact" onClick={onClose}>
          <Icon name="close" />
          关闭概览
        </button>
      </header>

      <div className="native-page-atlas-units">
        {visibleUnits.length === 0 ? (
          <div className="native-page-atlas-empty" role="status">
            <Icon name={filter === "AGENT" ? "spark" : "search"} />
            <strong>
              {filter === "AGENT"
                ? "当前没有运行中的页面范围"
                : "当前筛选没有匹配页面"}
            </strong>
            <span>
              {filter === "AGENT"
                ? "Agent 开始处理具体范围后，这里会同步显示实际页码。"
                : "可以切回“全部”继续浏览整份文档。"}
            </span>
          </div>
        ) : visibleUnits.map(({ unit, visiblePages }) => {
          const current = unit.pages.includes(activePage);
          const expanded = expandedUnitIds.has(unit.unitId);
          const opened = visiblePages.filter(
            (page) => receiptsByPage.get(page)?.state === "OPENED",
          ).length;
          const attention = attentionScopes.filter((scopePages) =>
            scopePages.some((page) => visiblePages.includes(page)),
          ).length;
          return (
            <section
              className={cx(
                "native-page-atlas-unit",
                current && "is-current",
                !expanded && "is-collapsed",
              )}
              key={unit.unitId}
              aria-label={`${unit.title} ${pageRange(unit.pages)}`}
            >
              <header>
                <button
                  type="button"
                  onClick={() => {
                    setExpandedUnitIds((currentIds) =>
                      new Set(currentIds).add(unit.unitId),
                    );
                    onSelectPage(visiblePages[0]);
                  }}
                >
                  <strong>{unit.title}</strong>
                  <span>{pageRange(unit.pages)}</span>
                </button>
                <span>
                  {opened}/{visiblePages.length} 页已检查
                  {visiblePages.length !== unit.pages.length
                    ? ` · 当前显示 ${visiblePages.length}/${unit.pages.length} 页`
                    : ""}
                  {attention > 0 ? ` · ${attention} 项待关注` : ""}
                </span>
                {unit.titleAuthority === "DERIVED_FROM_VISIBLE_TABLE_ROLE" ? (
                  <em>Agent 命名</em>
                ) : null}
                <button
                  type="button"
                  className="native-page-atlas-unit-toggle"
                  aria-expanded={expanded}
                  onClick={() =>
                    setExpandedUnitIds((currentIds) => {
                      const next = new Set(currentIds);
                      if (next.has(unit.unitId)) next.delete(unit.unitId);
                      else next.add(unit.unitId);
                      return next;
                    })
                  }
                >
                  <Icon name={expanded ? "chevron-down" : "chevron"} size={12} />
                  {expanded ? "收起" : "展开"}
                </button>
              </header>
              {expanded ? <div className="native-page-atlas-pages">
                {visiblePages.map((page) => {
                  const receipt = receiptsByPage.get(page);
                  const tableCount = tableCounts.get(page) || 0;
                  const crossPageTableCount = crossPageTableCounts.get(page) || 0;
                  const attentionCount = attentionCounts.get(page) || 0;
                  const selected = page === activePage;
                  const agentFocused = focusPages.has(page);
                  const visualOpened = receipt?.state === "OPENED";
                  const status = pageEvidenceLabel(receipt);
                  const purpose = publicPagePurpose(receipt);
                  return (
                    <button
                      type="button"
                      key={page}
                      className={cx(
                        "native-page-atlas-page",
                        visualOpened && "is-opened",
                        tableCount > 0 && "has-table",
                        attentionCount > 0 && "has-attention",
                        agentFocused && "is-agent-focus",
                        selected && "is-selected",
                      )}
                      aria-label={`P${page} · ${status}${
                        tableCount ? ` · ${tableCount} 张逻辑表` : ""
                      }${crossPageTableCount ? " · 包含跨页表" : ""}${
                        attentionCount ? ` · ${attentionCount} 项待关注` : ""
                      }`}
                      aria-pressed={selected}
                      title={purpose}
                      tabIndex={page === rovingPage ? 0 : -1}
                      ref={(element) => {
                        if (element) pageButtonRefs.current.set(page, element);
                        else pageButtonRefs.current.delete(page);
                      }}
                      onKeyDown={(event) => moveFocus(event, page)}
                      onClick={() => onSelectPage(page)}
                    >
                      <span className="native-page-atlas-page-heading">
                        <strong>P{page}</strong>
                        <small>{status}</small>
                      </span>
                      <span className="native-page-atlas-page-sheet" aria-hidden="true">
                        <i />
                        <i />
                        <i />
                        {tableCount > 0 ? <b /> : null}
                      </span>
                      <span className="native-page-atlas-page-signals">
                        {tableCount > 0 ? (
                          <small className="is-table">表 {tableCount}</small>
                        ) : null}
                        {crossPageTableCount > 0 ? (
                          <small className="is-continuation">续表</small>
                        ) : null}
                        {attentionCount > 0 ? (
                          <small className="is-attention">关注 {attentionCount}</small>
                        ) : null}
                        {agentFocused ? (
                          <small className="is-agent-focus">当前范围</small>
                        ) : null}
                      </span>
                      <span className="native-page-atlas-page-purpose">
                        {purpose}
                      </span>
                    </button>
                  );
                })}
              </div> : null}
            </section>
          );
        })}
      </div>
    </section>
  );
}
