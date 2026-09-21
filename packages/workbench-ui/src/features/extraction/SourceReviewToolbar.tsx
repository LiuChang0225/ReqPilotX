import type { Ref } from "react";
import type { ReviewFilter } from "./sourceItemReviewContext";

export function SourceReviewToolbar({ query, onQuery, view, onView, category, onCategory, topic, onTopic,
  more, onMore, total, matched, focus, attention, changed, ready, focusStatus, historyKnown, error, onRetry,
  onExpand, onCollapse, onLocate, onChooseFirst, inputRef, analysisMode = false }: {
  analysisMode?: boolean;
  query: string; onQuery: (value: string) => void; view: ReviewFilter; onView: (value: ReviewFilter) => void;
  category: string; onCategory: (value: string) => void; topic: string; onTopic: (value: string) => void;
  more: boolean; onMore: (value: boolean) => void; total: number; matched: number;
  focus: number; attention: number; changed: number; ready: boolean; historyKnown: boolean;
  focusStatus: "BOUND" | "PARTIAL" | "UNAVAILABLE";
  error: string; onRetry: () => void; onExpand: () => void; onCollapse: () => void; onLocate: () => void;
  onChooseFirst: () => void;
  inputRef: Ref<HTMLInputElement>;
}) {
  const filtered = Boolean(query || view !== "ALL" || category || topic);
  const focusAvailable = ready && focusStatus !== "UNAVAILABLE";
  const focusHint = error ? "审阅提示暂不可读取，请重试。" : !ready ? "正在读取审阅提示。" : focusStatus === "PARTIAL" ? "重点提示仅覆盖部分原文；未标记内容仍需审阅。"
    : focusStatus === "UNAVAILABLE" ? "当前版本尚无重点标记，不能据此判断没有技术重点。" : "重点提示用于优先分析，不代表存在不符合。";
  return <header className="source-review-tree-toolbar" aria-label="条目搜索与筛选">
    <div className="source-review-toolbar-main">
    <input ref={inputRef} type="search" aria-label="搜索原文条目" value={query} onChange={event => onQuery(event.target.value)}
      title="按 / 搜索，回车定位首个匹配项" onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); onChooseFirst(); } }}
      placeholder={ready ? "搜索编号、标题或原文 /" : "搜索编号或标题 /"} />
      <div className="native-hierarchy-tree-actions">
        <button type="button" aria-label="全部展开" onClick={onExpand}>展开</button>
        <button type="button" aria-label="全部收起" onClick={onCollapse}>收起</button>
        <button type="button" aria-label="定位当前" onClick={onLocate}>定位</button>
      </div>
    </div>
    <div className="source-review-toolbar-filters">
    <div className="source-review-quick-filters" aria-label="审阅范围">
      {!analysisMode && <><button type="button" aria-pressed={view === "ALL"} onClick={() => onView("ALL")}>全部 {total}</button>
      <button type="button" disabled={!focusAvailable} title={focusHint} aria-pressed={view === "FOCUS"} onClick={() => onView("FOCUS")}>重点 {focusAvailable ? focus : ready || error ? "—" : "…"}</button>
      <button type="button" disabled={!ready} aria-pressed={view === "ATTENTION"} onClick={() => onView("ATTENTION")}>需关注 {ready ? attention : error ? "—" : "…"}</button></>}
      <button type="button" className={category || topic || !["ALL", "FOCUS", "ATTENTION"].includes(view) ? "has-active-filters" : undefined} aria-expanded={more} onClick={() => onMore(!more)}>筛选</button>
    </div>
    {(filtered || analysisMode) && <div className="source-review-filter-result" role="status" title="匹配条目及其完整原文路径已保留">
      <span>{analysisMode && !filtered ? `${matched} 条原文` : `匹配 ${matched} / ${total}`}</span>
      {filtered && <button type="button" aria-label="清除搜索与筛选" onClick={() => { onQuery(""); onView("ALL"); onCategory(""); onTopic(""); }}>清除</button>}
    </div>}
    </div>
    {more && <div className="source-review-more-filters">
      <small>{focusHint}</small>
      <label>范围<select aria-label="筛选审阅状态" value={view} onChange={event => onView(event.target.value as ReviewFilter)}>
        <option value="ALL">全部原文</option><option value="REQUIREMENTS">仅需求</option>
        <option value="FOCUS" disabled={!focusAvailable}>重点分析</option><option value="PARAMETERS" disabled={!focusAvailable}>技术参数</option>
        <option value="VERIFICATION" disabled={!focusAvailable}>试验验证</option><option value="ATTENTION" disabled={!ready}>需关注</option>
        <option value="CHANGED" disabled={!ready || !historyKnown}>本次变化 {historyKnown ? changed : "（历史未知）"}</option>
        <option value="DERIVED" disabled={!ready}>有派生项</option><option value="IMAGES" disabled={!ready}>有图片释读</option>
      </select></label>
      <label>类别<select aria-label="筛选需求类别" value={category} onChange={event => onCategory(event.target.value)}>
        <option value="">全部类别</option><option value="FUNCTIONAL">功能</option><option value="QUALITY">质量</option>
        <option value="CONSTRAINT">约束</option><option value="PROJECT">项目</option><option value="PROCESS">过程</option>
      </select></label>
      <label>主题<select aria-label="筛选需求主题" value={topic} disabled={!ready} onChange={event => onTopic(event.target.value)}>
        <option value="">全部主题</option><option value="VERIFICATION">试验验证</option><option value="REGULATORY">法规</option>
        <option value="LOGISTICS_PACKAGING">物流包装</option><option value="STANDARDS">标准</option>
      </select></label>
    </div>}
    {error && <small className="source-review-index-error">正文检索与审阅提示暂不可用，原文仍可查看。<button type="button" onClick={onRetry}>重试</button></small>}
  </header>;
}
