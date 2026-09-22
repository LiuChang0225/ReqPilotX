/** Compress consecutive physical pages without inventing coverage in gaps. */
export function sourcePageLabel(pages: readonly number[]) {
  const values = [...new Set(pages)].sort((a, b) => a - b);
  if (!values.length) return "未绑定页面";
  const ranges: string[] = [];
  let start = values[0], previous = values[0];
  for (const page of values.slice(1)) {
    if (page === previous + 1) { previous = page; continue; }
    ranges.push(start === previous ? `P${start}` : `P${start}–P${previous}`);
    start = previous = page;
  }
  ranges.push(start === previous ? `P${start}` : `P${start}–P${previous}`);
  return ranges.join("、");
}
