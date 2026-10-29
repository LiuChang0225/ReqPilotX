/** Memory-only page cache. GETs existing render artifacts; never invokes a parser. */
import { useEffect, useState } from "react";
import { artifactUrl, type Run } from "./contracts";
interface Entry { promise: Promise<string>; src?: string; pins: number; used: number }
const entries = new Map<string, Entry>();
let clock = 0;
function trim() {
  for (const [key, entry] of [...entries].sort((a,b) => a[1].used-b[1].used)) {
    if (entries.size <= 16) break;
    if (!entry.pins && entry.src) { URL.revokeObjectURL(entry.src); entries.delete(key); }
  }
}
function resource(key: string) {
  const found = entries.get(key);
  if (found) { found.used = ++clock; return found; }
  const entry: Entry = { pins: 0, used: ++clock, promise: Promise.resolve("") };
  entry.promise = fetch(key).then(async response => {
    if (!response.ok) throw new Error(`页图读取失败 HTTP ${response.status}`);
    const src = URL.createObjectURL(await response.blob());
    entry.src = src; trim(); return src;
  }).catch(error => { entries.delete(key); throw error; });
  entries.set(key, entry);
  return entry;
}
export function pageKey(run: Run, page: number) {
  const geometry = run.pages.find(p => p.page_number === page);
  return geometry ? artifactUrl(run, geometry.render_artifact) : "";
}
export function useCachedPage(run: Run, page: number, prefetch = true) {
  const key = pageKey(run, page);
  const [state, setState] = useState({ key: "", src: "", error: "" });
  useEffect(() => {
    if (!key) return;
    let active = true;
    const entry = resource(key); entry.pins++;
    entry.promise.then(src => { if (active) setState({ key, src, error: "" }); })
      .catch(error => { if (active) setState({ key, src: "", error: String(error) }); });
    if (prefetch) for (const number of [page-1, page+1]) {
      const neighbour = pageKey(run, number);
      if (neighbour) void resource(neighbour).promise.catch(() => {});
    }
    return () => { active = false; entry.pins--; trim(); };
  }, [key, run.run_id, page, prefetch]);
  // Never expose a previous page/profile while the new request is pending.
  return state.key === key ? state : { key, src: "", error: "" };
}
export function resetPageCache() {
  for (const entry of entries.values()) if (entry.src) URL.revokeObjectURL(entry.src);
  entries.clear();
}
