const PREFIX_SEPARATORS = new Set([..."、,，;；:：()（）[]【】-—–"]);
const NUMBER_BOUNDARY_SEPARATORS = new Set([..."、,，;；:：.．-—–"]);
const OPENING_DELIMITERS = new Set([..."([{（［｛【"]);

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
  if (OPENING_DELIMITERS.has(first)) return `${sourceLabel} ${sourceTitle}`;
  if (NUMBER_BOUNDARY_SEPARATORS.has(first)) {
    if ((first === "." || first === "．") && sourceTitle.length > 1 && !/\s/u.test(sourceTitle[1])) {
      return `${sourceLabel} ${sourceTitle}`;
    }
    if (sourceLabel.slice(-1) === first) return sourceLabel + sourceTitle.slice(1);
    return sourceLabel + sourceTitle;
  }
  return `${sourceLabel} ${sourceTitle}`;
}
