import type { Ref } from "react";
import type { ReviewFilter } from "./sourceItemReviewContext";
import { useUiCopy } from "../../host/HostLocaleContext";

export function SourceReviewToolbar({ query, onQuery, view, onView, category, onCategory, topic, onTopic,
  more, onMore, total, matched, focus, attention, changed, ready, focusStatus, historyKnown, error, onRetry,
  onExpand, onCollapse, onLocate, onChooseFirst, inputRef, analysisMode = false, requirementsOnly = false }: {
  analysisMode?: boolean;
  requirementsOnly?: boolean;
  query: string; onQuery: (value: string) => void; view: ReviewFilter; onView: (value: ReviewFilter) => void;
  category: string; onCategory: (value: string) => void; topic: string; onTopic: (value: string) => void;
  more: boolean; onMore: (value: boolean) => void; total: number; matched: number;
  focus: number; attention: number; changed: number; ready: boolean; historyKnown: boolean;
  focusStatus: "BOUND" | "PARTIAL" | "UNAVAILABLE";
  error: string; onRetry: () => void; onExpand: () => void; onCollapse: () => void; onLocate: () => void;
  onChooseFirst: () => void;
  inputRef: Ref<HTMLInputElement>;
}) {
  const l = useUiCopy();
  const filtered = Boolean(query || view !== "ALL" || category || topic);
  const focusAvailable = ready && focusStatus !== "UNAVAILABLE";
  const focusHint = error ? l("reviewToolbar.hintError", "审阅提示暂不可读取，请重试。") : !ready ? l("reviewToolbar.hintLoading", "正在读取审阅提示。") : focusStatus === "PARTIAL" ? l("reviewToolbar.hintPartial", "重点提示仅覆盖部分原文；未标记内容仍需审阅。")
    : focusStatus === "UNAVAILABLE" ? l("reviewToolbar.hintUnavailable", "当前版本尚无重点标记，不能据此判断没有技术重点。") : l("reviewToolbar.hintReady", "重点提示用于优先分析，不代表存在不符合。");
  return <header className="source-review-tree-toolbar" aria-label={l("reviewToolbar.aria", "条目搜索与筛选")}>
    <div className="source-review-toolbar-main">
    <input ref={inputRef} type="search" aria-label={l("reviewToolbar.searchAria", "搜索原文条目")} value={query} onChange={event => onQuery(event.target.value)}
      title={l("reviewToolbar.searchTitle", "按 / 搜索，回车定位首个匹配项")} onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); onChooseFirst(); } }}
      placeholder={ready ? l("reviewToolbar.searchReady", "搜索编号、标题或原文 /") : l("reviewToolbar.search", "搜索编号或标题 /")} />
      <div className="native-hierarchy-tree-actions">
        <button type="button" aria-label={l("reviewToolbar.expandAllAria", "全部展开")} onClick={onExpand}>{l("reviewToolbar.expand", "展开")}</button>
        <button type="button" aria-label={l("reviewToolbar.collapseAllAria", "全部收起")} onClick={onCollapse}>{l("reviewToolbar.collapse", "收起")}</button>
        <button type="button" aria-label={l("reviewToolbar.locateAria", "定位当前")} onClick={onLocate}>{l("reviewToolbar.locate", "定位")}</button>
      </div>
    </div>
    <div className="source-review-toolbar-filters">
    <div className="source-review-quick-filters" aria-label={l("reviewToolbar.scope", "审阅范围")}>
      {requirementsOnly ? <span className="source-review-scope-note" role="status">{l("reviewToolbar.requirementsOnly", "仅需求 {count} · 保留祖先路径", { count: total })}</span>
        : !analysisMode && <><button type="button" aria-pressed={view === "ALL"} onClick={() => onView("ALL")}>{l("reviewToolbar.all", "全部 {count}", { count: total })}</button>
      <button type="button" disabled={!focusAvailable} title={focusHint} aria-pressed={view === "FOCUS"} onClick={() => onView("FOCUS")}>{l("reviewToolbar.focus", "重点 {count}", { count: focusAvailable ? focus : ready || error ? "—" : "…" })}</button>
      <button type="button" disabled={!ready} aria-pressed={view === "ATTENTION"} onClick={() => onView("ATTENTION")}>{l("reviewToolbar.attention", "需关注 {count}", { count: ready ? attention : error ? "—" : "…" })}</button></>}
      <button type="button" className={category || topic || !["ALL", "REQUIREMENTS", "FOCUS", "ATTENTION"].includes(view) ? "has-active-filters" : undefined} aria-expanded={more} onClick={() => onMore(!more)}>{l("reviewToolbar.filters", "筛选")}</button>
    </div>
    {(filtered || analysisMode) && <div className="source-review-filter-result" role="status" title={l("reviewToolbar.filterResultTitle", "匹配条目及其完整原文路径已保留")}>
      <span>{analysisMode && !filtered ? l("reviewToolbar.sourceCount", "{count} 条原文", { count: matched }) : l("reviewToolbar.matchCount", "匹配 {matched} / {total}", { matched, total })}</span>
      {filtered && <button type="button" aria-label={l("reviewToolbar.clearAria", "清除搜索与筛选")} onClick={() => { onQuery(""); onView("ALL"); onCategory(""); onTopic(""); }}>{l("reviewToolbar.clear", "清除")}</button>}
    </div>}
    </div>
    {more && <div className="source-review-more-filters">
      <small>{focusHint}</small>
      {requirementsOnly ? <small>{l("reviewToolbar.requirementsOnlyHint", "当前为需求清单视图；非需求内容仍在原文条目视图中查看。")}</small>
        : <label>{l("reviewToolbar.scopeLabel", "范围")}<select aria-label={l("reviewToolbar.scopeFilterAria", "筛选审阅状态")} value={view} onChange={event => onView(event.target.value as ReviewFilter)}>
        <option value="ALL">{l("reviewToolbar.scopeAll", "全部原文")}</option><option value="REQUIREMENTS">{l("reviewToolbar.scopeRequirements", "仅需求")}</option>
        <option value="FOCUS" disabled={!focusAvailable}>{l("reviewToolbar.scopeFocus", "重点分析")}</option><option value="PARAMETERS" disabled={!focusAvailable}>{l("reviewToolbar.scopeParameters", "技术参数")}</option>
        <option value="VERIFICATION" disabled={!focusAvailable}>{l("reviewToolbar.scopeVerification", "试验验证")}</option><option value="ATTENTION" disabled={!ready}>{l("reviewToolbar.scopeAttention", "需关注")}</option>
        <option value="CHANGED" disabled={!ready || !historyKnown}>{l("reviewToolbar.scopeChanged", "本次变化 {count}", { count: historyKnown ? changed : l("reviewToolbar.historyUnknown", "（历史未知）") })}</option>
        <option value="DERIVED" disabled={!ready}>{l("reviewToolbar.scopeDerived", "有派生项")}</option><option value="IMAGES" disabled={!ready}>{l("reviewToolbar.scopeImages", "有图片释读")}</option>
      </select></label>}
      <label>{l("reviewToolbar.category", "类别")}<select aria-label={l("reviewToolbar.categoryFilterAria", "筛选需求类别")} value={category} onChange={event => onCategory(event.target.value)}>
        <option value="">{l("reviewToolbar.categoryAll", "全部类别")}</option><option value="FUNCTIONAL">{l("stage.kind.functional", "功能")}</option><option value="QUALITY">{l("stage.kind.quality", "质量")}</option>
        <option value="CONSTRAINT">{l("stage.kind.constraint", "约束")}</option><option value="PROJECT">{l("stage.kind.project", "项目")}</option><option value="PROCESS">{l("stage.kind.process", "过程")}</option>
      </select></label>
      <label>{l("reviewToolbar.topic", "主题")}<select aria-label={l("reviewToolbar.topicFilterAria", "筛选需求主题")} value={topic} disabled={!ready} onChange={event => onTopic(event.target.value)}>
        <option value="">{l("reviewToolbar.topicAll", "全部主题")}</option><option value="VERIFICATION">{l("reviewToolbar.topicVerification", "试验验证")}</option><option value="REGULATORY">{l("reviewToolbar.topicRegulatory", "法规")}</option>
        <option value="LOGISTICS_PACKAGING">{l("reviewToolbar.topicLogistics", "物流包装")}</option><option value="STANDARDS">{l("reviewToolbar.topicStandards", "标准")}</option>
      </select></label>
    </div>}
    {error && <small className="source-review-index-error">{l("reviewToolbar.indexError", "正文检索与审阅提示暂不可用，原文仍可查看。")}<button type="button" onClick={onRetry}>{l("common.retry", "重试")}</button></small>}
  </header>;
}
