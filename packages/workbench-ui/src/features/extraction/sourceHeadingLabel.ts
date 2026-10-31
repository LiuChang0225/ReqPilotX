const PREFIX_SEPARATORS = new Set([..."、,，;；:：()（）[]【】-—–"]);
const LEADING_SEPARATORS = new Set([..."、,，;；:：.．()（）[]【】-—–"]);

/** Compose one visible source heading without duplicating its exact number prefix. */
export function sourceHeadingLabel(label?: string, title?: string): string {
  const sourceLabel = String(label || "").trim();
  const sourceTitle = String(title || "").trim();
  if (!sourceLabel) return sourceTitle;
  if (![...sourceLabel].some(character => /[\p{L}\p{N}]/u.test(character))) return sourceTitle || sourceLabel;
  if (!sourceTitle || sourceTitle === sourceLabel) return sourceLabel;
  if (sourceTitle.startsWith(sourceLabel)) {
    const remainder = sourceTitle.slice(sourceLabel.length);
    const first = remainder.slice(0, 1);
    if (/\s/u.test(first) || PREFIX_SEPARATORS.has(first)) return sourceTitle;
    if ([".", "．"].includes(first) && (remainder.length === 1 || !/\d/u.test(remainder[1]))) return sourceTitle;
  }
  const first = sourceTitle.slice(0, 1);
  if (LEADING_SEPARATORS.has(first)) {
    if (LEADING_SEPARATORS.has(sourceLabel.slice(-1))) return sourceLabel + sourceTitle.slice(1);
    return sourceLabel + sourceTitle;
  }
  return `${sourceLabel} ${sourceTitle}`;
}
