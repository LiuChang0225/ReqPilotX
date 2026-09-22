import type { ReactNode } from "react";

type LinkRenderer = (target: string, label: ReactNode, key: string) => ReactNode;

/** A small text-only inline grammar. Unknown syntax stays visible; HTML is never interpreted. */
export function renderKnowledgeInline(text: string, renderLink: LinkRenderer, keyPrefix = "inline"): ReactNode[] {
  const parts: ReactNode[] = [];
  const tokens = /\\([\\`*\[\]|])|(`+)([^\n]*?)\2(?!`)|\*\*([^\n]+?)\*\*|\[\[([^\]\n]+)\]\]/g;
  let start = 0;
  for (const match of text.matchAll(tokens)) {
    const index = match.index!;
    if (index > start) parts.push(text.slice(start, index));
    const key = `${keyPrefix}:${index}`;
    if (match[1] !== undefined) parts.push(match[1]);
    else if (match[2] !== undefined) parts.push(<code key={key}>{match[3]}</code>);
    else if (match[4] !== undefined) parts.push(<strong key={key}>{renderKnowledgeInline(match[4], renderLink, key)}</strong>);
    else {
      const [target, ...alias] = match[5].split("|");
      const label = alias.join("|") || target;
      parts.push(renderLink(target.trim(), renderKnowledgeInline(label, (_target, nestedLabel) => nestedLabel, key), key));
    }
    start = index + match[0].length;
  }
  if (start < text.length) parts.push(text.slice(start));
  return parts;
}

/** Do not treat pipes inside code, wikilinks or escaped text as table separators. */
export function splitKnowledgeTableRow(row: string): string[] {
  const cells = [""];
  const tokens = /\\.|(`+)([^\n]*?)\1(?!`)|\[\[[^\]\n]+\]\]|\||[^\\`[|]+|./g;
  for (const match of row.trim().slice(1, -1).matchAll(tokens)) {
    if (match[0] === "|") cells.push("");
    else cells[cells.length - 1] += match[0];
  }
  return cells.map(cell => cell.trim());
}
