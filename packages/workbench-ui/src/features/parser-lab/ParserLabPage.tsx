import { useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import { artifactUrl, type ParserInfo, type Run, type Selection, type SourceNode } from "./contracts";
import { boxStyle, elementLabel, fittedFont, KINDS, presentation, type DisplayElement, type Layer } from "./presentation";
import { useCachedPage } from "./pageCache";
import "./parserLab.css";

type View = "pdf" | "content" | "split";
interface PaneProps {
  run: Run; elements: DisplayElement[]; page: number; zoom: number; layer: Layer; boxes: boolean;
  selection: Selection | null; onSelect: (s: Selection) => void;
}
async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/parser-lab${url}`, init);
  if (!response.ok) { const value = await response.json().catch(() => null); throw new Error(value?.detail || `HTTP ${response.status}`); }
  return response.json();
}
function activeKey(event: KeyboardEvent, action: () => void) {
  if (["Enter", " "].includes(event.key)) { event.preventDefault(); action(); }
}
function SelectedScroll({ selection, root }: { selection: Selection | null; root: React.RefObject<HTMLDivElement | null> }) {
  useEffect(() => {
    if (selection) Array.from(root.current?.querySelectorAll<HTMLElement>("[data-source-id]") || [])
      .find(e => e.dataset.sourceId === selection.id)?.scrollIntoView?.({ block: "nearest", behavior: "auto" });
  }, [selection, root]);
  return null;
}
function PdfPane({ run, elements, page, zoom, layer, boxes, selection, onSelect }: PaneProps) {
  const root = useRef<HTMLDivElement>(null);
  const image = useCachedPage(run, page);
  const geometry = run.pages.find(p => p.page_number === page);
  const locators = elements.filter(e => e.page === page && e.bbox).flatMap(e => [
    { ...e, label: `${e.order} · ${elementLabel(e, layer)}` },
    ...(e.node?.table_payload?.cells || []).filter(c => c.native_locator.bbox).map(c => ({
      ...e, id: c.cell_id, ownerId: e.id, bbox: c.native_locator.bbox, label: `单元格 ${c.row+1},${c.column+1}` })),
  ]);
  return <section className="pl-pane" aria-label={`${run.parser} PDF`} data-run-id={run.run_id} data-page={page}>
    <header><strong>{run.parser} · 原始 PDF · p.{page}</strong><span>页图缓存</span>
      <a href={artifactUrl(run, "source")} target="_blank" rel="noreferrer">原 PDF</a></header>
    <p className="pl-canvas-note">原 PDF 页图。框与标签只反映所选来源层；规则和噪声开关不会改动原件。页码与缩放在两侧同步。</p>
    <div className="pl-pdf-scroll" ref={root}>
      <SelectedScroll selection={selection} root={root} />
      {geometry && image.src ? <div className="pl-paper" style={{ width: `${zoom*100}%` }}>
        <img src={image.src} alt={`原 PDF 第 ${page} 页，旋转 ${geometry.rotation}°`} />
        {boxes && locators.map(box => <button key={box.id} type="button" data-source-id={box.id} data-kind={box.kind}
          className={`pl-bbox ${selection?.id === box.id ? "is-selected" : ""}`} aria-label={`定位 ${box.label}`}
          aria-pressed={selection?.id === box.id} title={`${box.origin}；${box.granularity}\n${box.reasons.join("\n")}`}
          style={{ ...boxStyle(box.bbox!, geometry.bbox), "--pl-kind": KINDS[box.kind].color } as CSSProperties}
          onClick={() => onSelect(box)}><span>{box.label}</span></button>)}
      </div> : <p className="pl-empty" role="status">{image.error || (geometry ? `正在读取第 ${page} 页缓存…` : "没有可显示的 PDF 页；请查看运行诊断。")}</p>}
    </div>
  </section>;
}
function Thumbnail({ run, number, page, onPage }: { run: Run; number: number; page: number; onPage: (n: number) => void }) {
  const image = useCachedPage(run, number, false);
  return <button type="button" className="pl-thumbnail" aria-label={`缩略图第 ${number} 页`} aria-pressed={number === page} onClick={() => onPage(number)}>
    {image.src ? <img src={image.src} alt={`第 ${number} 页缩略图`} /> : <span>p.{number}</span>}<small>{number}</small>
  </button>;
}
function TableContent({ node, onSelect, selected }: { node: SourceNode; onSelect: PaneProps["onSelect"]; selected?: string }) {
  const table = node.table_payload!;
  return <div className="pl-table-scroll"><table aria-label={`原表 ${node.source_order+1}`}><tbody>
    {Array.from({ length: table.row_count }, (_, row) => <tr key={row}>
      {table.cells.filter(c => c.row === row).sort((a,b) => a.column-b.column).map(cell => <td key={cell.cell_id}
        rowSpan={cell.row_span} colSpan={cell.column_span} data-source-id={cell.cell_id} className={selected === cell.cell_id ? "is-selected" : ""}>
        <button type="button" aria-label={`定位单元格 ${cell.row+1},${cell.column+1}`} onClick={() => onSelect({ id: cell.cell_id,
          ownerId: node.source_node_id, page: cell.native_locator.page_number, bbox: cell.native_locator.bbox })}>{cell.raw_text || "（空）"}</button>
      </td>)}
    </tr>)}
  </tbody></table></div>;
}
function ContentPane({ run, elements, selection, onSelect, layer }: PaneProps) {
  const root = useRef<HTMLDivElement>(null);
  return <section className="pl-pane" aria-label={`${run.parser} 完整提取内容`} data-run-id={run.run_id}>
    <header><strong>{run.parser} · 结构化全文列表</strong><span>SourceIR · {run.status}</span></header>
    <div className="pl-content-scroll" ref={root}><SelectedScroll selection={selection} root={root} />
      {!run.source_ir && <p className="pl-empty">没有正规化结果；请查看诊断与 raw。</p>}
      {elements.map(e => <article key={e.id} data-source-id={e.id} data-kind={e.kind}
        style={{ "--pl-kind": KINDS[e.kind].color } as CSSProperties} className={`pl-node ${selection?.id === e.id ? "is-selected" : ""}`}>
        <button type="button" className="pl-node-label" onClick={() => onSelect(e)}>{e.order} · {e.node?.kind || "OCR"} · p.{e.page} ↗</button>
        <span className="pl-kind-label">{elementLabel(e, layer)}</span>
        <small className="pl-origin">{e.origin} · {e.granularity}</small>
        {e.text && <p>{e.text}</p>}
        {e.node?.table_payload && <TableContent node={e.node} selected={selection?.id} onSelect={onSelect} />}
        {e.node?.asset_ref && (run.artifacts.some(a => a.id === e.node?.asset_ref)
          ? <img className="pl-asset" src={artifactUrl(run, e.node.asset_ref)} alt={`真实图片资产 ${e.node.asset_ref}`} />
          : <p className="pl-error">图片资产缺失：{e.node.asset_ref}</p>)}
        <details><summary>类别／顺序来源与理由</summary><p>{e.reasons.join("\n")}</p><small>SourceIR 原序 {e.sourceOrder}；当前展示序 {e.order}</small></details>
        {!e.bbox && <small>无原生 bbox；仅可定位页码。</small>}
      </article>)}
      {run.source_ir && !elements.length && <p className="pl-empty">当前无可显示对象；请检查噪声开关与诊断。不能据此认定页面无内容。</p>}
    </div>
  </section>;
}
function CanvasPane({ run, elements, page, zoom, layer, boxes, selection, onSelect }: PaneProps) {
  const root = useRef<HTMLDivElement>(null);
  const geometry = run.pages.find(p => p.page_number === page);
  const visible = elements.filter(e => e.page === page && e.bbox && !e.canvasOmitted);
  const duplicates = elements.filter(e => e.page === page && e.canvasOmitted).length;
  const missing = elements.filter(e => e.page === page && !e.bbox).length;
  return <section className="pl-pane" aria-label={`${run.parser} 原位还原`} data-run-id={run.run_id} data-page={page}>
    <header><strong>{run.parser} · 白底原位还原 · p.{page}</strong><span>仅提取内容</span></header>
    <p className="pl-canvas-note">按已有 bbox 放置。字号／换行采用拟合推算，并非原生字体恢复；只有块框时不声称字级定位。完整文字可切到列表核对。</p>
    <div className="pl-content-scroll pl-canvas-scroll" ref={root}>
      <SelectedScroll selection={selection} root={root} />
      {geometry ? <svg className="pl-reconstruction" role="group" aria-label={`第 ${page} 页白底提取画布`}
        style={{ width: `${zoom*100}%` }} viewBox={`${geometry.bbox[0]} ${geometry.bbox[1]} ${geometry.bbox[2]-geometry.bbox[0]} ${geometry.bbox[3]-geometry.bbox[1]}`}>
        <rect x={geometry.bbox[0]} y={geometry.bbox[1]} width={geometry.bbox[2]-geometry.bbox[0]} height={geometry.bbox[3]-geometry.bbox[1]} fill="white" />
        {visible.map(e => {
          const [x,y,r,b] = e.bbox!, width = r-x, height = b-y;
          const color = KINDS[e.kind].color;
          const table = e.node?.table_payload;
          const choose = () => onSelect(e);
          return <g key={e.id} data-source-id={e.id} data-kind={e.kind}>
            <title>{`${e.origin}；${e.granularity}\n${e.reasons.join("\n")}`}</title>
            <rect x={x} y={y} width={width} height={height} fill={selection?.id === e.id ? "#fff2c9" : "transparent"}
              stroke={boxes ? color : "transparent"} strokeWidth={selection?.id === e.id ? 2 : .7} strokeDasharray={e.noise ? "3 2" : undefined}
              role="button" tabIndex={0} aria-label={`还原定位 ${e.order} · ${elementLabel(e, layer)}`} aria-pressed={selection?.id === e.id}
              onClick={choose} onKeyDown={event => activeKey(event, choose)} />
            {e.node?.asset_ref && run.artifacts.some(a => a.id === e.node?.asset_ref)
              ? <image x={x} y={y} width={width} height={height} href={artifactUrl(run,e.node.asset_ref)} preserveAspectRatio="none" pointerEvents="none" />
              : table ? table.cells.map(cell => {
                const actual = cell.native_locator.bbox;
                const box = actual || [x+width*cell.column/table.column_count, y+height*cell.row/table.row_count,
                  x+width*(cell.column+cell.column_span)/table.column_count, y+height*(cell.row+cell.row_span)/table.row_count];
                const [cx,cy,cr,cb] = box;
                const select = () => onSelect({ id: cell.cell_id, ownerId: e.id, page: e.page, bbox: actual });
                return <g key={cell.cell_id} data-source-id={cell.cell_id} data-position-origin={actual ? "native-cell-bbox" : "topology-estimate"}>
                  <title>{actual ? "原生单元格 bbox" : "位置由表格拓扑均分推算，原生没有 cell bbox；仅回源至表格／页。"}</title>
                  <rect x={cx} y={cy} width={cr-cx} height={cb-cy} stroke={color} strokeWidth={.5}
                    fill={selection?.id === cell.cell_id ? "#ffe7a5" : "#ffffff"} tabIndex={0} role="button"
                    aria-label={`还原定位单元格 ${cell.row+1},${cell.column+1}`} aria-pressed={selection?.id === cell.cell_id}
                    onClick={select} onKeyDown={event => activeKey(event, select)} />
                  <foreignObject x={cx+1} y={cy} width={Math.max(0,cr-cx-2)} height={cb-cy} pointerEvents="none">
                    <div className="pl-fitted-text pl-cell-text" style={{ fontSize: fittedFont(cell.raw_text, [cx,cy,cr,cb]) }}>{cell.raw_text}</div>
                  </foreignObject>
                </g>;
              }) : e.text ? (e.node?.pdf_text_lines?.length ? e.node.pdf_text_lines.map((line,index) => {
                const text = Array.from(e.text).slice(line.start_offset,line.end_offset).join("");
                return <foreignObject key={index} x={line.bbox[0]} y={line.bbox[1]} width={line.bbox[2]-line.bbox[0]} height={line.bbox[3]-line.bbox[1]} pointerEvents="none">
                  <div className="pl-fitted-text" style={{fontSize:fittedFont(text,line.bbox)}}>{text}</div>
                </foreignObject>;
              }) : <foreignObject x={x} y={y} width={width} height={height} pointerEvents="none">
                <div className="pl-fitted-text" style={{ fontSize: fittedFont(e.text,e.bbox!) }}>{e.text}</div>
              </foreignObject>) : null}
            {boxes && <text className="pl-svg-tag" x={x} y={Math.max(geometry.bbox[1]+8,y-2)} fill={color} fontSize={8} pointerEvents="none">{e.order} · {elementLabel(e,layer)}</text>}
          </g>;
        })}
      </svg> : <p className="pl-empty">没有正规化结果或页面几何；未垫原图作为还原内容。</p>}
      {!!geometry && !visible.length && <p className="pl-empty">本页无可放置的提取对象，白底保留为空。</p>}
      {!!missing && <p>{missing} 个对象无 bbox，仅在全文列表显示。</p>}
      {!!duplicates && <p>{duplicates} 个与表格匹配的重复文字块仅在此画布折叠；规则来源 pl-table-text-overlap/v1，全文列表和 raw 保留。</p>}
      {visible.some(e => e.node?.table_payload?.cells.some(c => !c.native_locator.bbox)) && <p>部分单元格位置由表格拓扑均分推算，非原生 cell bbox。</p>}
    </div>
  </section>;
}

export function ParserLabPage() {
  const [parsers,setParsers] = useState<ParserInfo[]>([]), [runs,setRuns] = useState<Run[]>([]);
  const [run,setRun] = useState<Run|null>(null), [compare,setCompare] = useState<Run|null>(null);
  const [requested,setRequested] = useState(""), [compareRequested,setCompareRequested] = useState("");
  const primaryRequest=useRef(0), comparisonRequest=useRef(0);
  const [file,setFile] = useState<File|null>(null), [parser,setParser] = useState("pymupdf"), [language,setLanguage] = useState("eng");
  const [view,setView] = useState<View>("split"), [content,setContent] = useState<"canvas"|"list">("canvas"), [layer,setLayer] = useState<Layer>("native");
  const [navigation,setNavigation] = useState({ runId:"",page:1,zoom:1 });
  const [selection,setSelection] = useState<Selection|null>(null), [compareSelection,setCompareSelection] = useState<Selection|null>(null);
  const [showNoise,setShowNoise] = useState(true), [boxes,setBoxes] = useState(true), [busy,setBusy] = useState(false), [error,setError] = useState("");
  async function load(id:string, second=false) {
    const pending=second?comparisonRequest:primaryRequest, sequence=++pending.current;
    if(second) setCompareRequested(id); else {
      setRequested(id); comparisonRequest.current++; setCompareRequested(""); setCompare(null);
    }
    setError("");
    try { const value=await request<Run>(`/runs/${id}`); if(sequence!==pending.current)return;
      if(second){ setCompare(value);setCompareSelection(null); } else { setRun(value);setSelection(null);setNavigation({runId:id,page:1,zoom:1}); }
    } catch(e){ if(sequence===pending.current)setError(String(e)); }
  }
  useEffect(()=>{
    document.title="Parser Lab · ReqPilotX"; let active=true;
    Promise.all([request<ParserInfo[]>("/parsers"),request<Run[]>("/runs")]).then(([p,r])=>{
      if(!active)return;setParsers(p);setRuns(r);
      const explicit=new URLSearchParams(window.location.search).get("run");
      if(r.length)void load(r.find(item=>item.run_id===explicit)?.run_id||r[0].run_id);
    }).catch(e=>{if(active)setError(String(e));});
    return()=>{active=false;primaryRequest.current++;comparisonRequest.current++;};
  },[]);
  async function execute(){
    if(!file)return;setBusy(true);setError("");
    try { const form=new FormData();form.append("file",file);form.append("parser",parser);form.append("language",language);
      const result=await request<Run>("/runs",{method:"POST",body:form});primaryRequest.current++;setRun(result);setRequested(result.run_id);
      setSelection(null);setNavigation({runId:result.run_id,page:1,zoom:1});setRuns(await request<Run[]>("/runs"));
    }catch(e){setError(String(e));}finally{setBusy(false);}
  }
  const current=run && run.run_id===requested?run:null;
  const comparison=compare && compare.run_id===compareRequested && compare.source_sha256===current?.source_sha256 && compare.run_id!==current?.run_id?compare:null;
  const all=useMemo(()=>current?presentation(current,layer):[],[current,layer]);
  const comparisonAll=useMemo(()=>comparison?presentation(comparison,layer):[],[comparison,layer]);
  const elements=all.filter(e=>showNoise||!e.noise), comparisonElements=comparisonAll.filter(e=>showNoise||!e.noise);
  const page=current && navigation.runId===current.run_id?Math.min(Math.max(1,navigation.page),current.pages.length||1):1;
  const zoom=current && navigation.runId===current.run_id?navigation.zoom:1;
  function turn(number:number){if(current){setNavigation({runId:current.run_id,page:Math.max(1,Math.min(number,current.pages.length||1)),zoom});}}
  function select(value:Selection,second=false){if(second)setCompareSelection(value);else setSelection(value);turn(value.page);}
  function keyboard(event:KeyboardEvent){
    if(!current || event.altKey || event.metaKey || event.ctrlKey || (event.target as HTMLElement).closest("input,select,textarea,[contenteditable=true]"))return;
    const next=({PageUp:page-1,ArrowLeft:page-1,PageDown:page+1,ArrowRight:page+1,Home:1,End:current.pages.length} as Record<string,number>)[event.key];
    if(next!==undefined){event.preventDefault();turn(next);}
  }
  const props:PaneProps|null=current?{run:current,elements,page,zoom,layer,boxes,selection,onSelect:value=>select(value)}:null;
  const compareProps:PaneProps|null=comparison?{run:comparison,elements:comparisonElements,page:Math.min(page,comparison.pages.length||1),zoom,layer,boxes,
    selection:compareSelection,onSelect:value=>select(value,true)}:null;
  const contentPane=(p:PaneProps)=>(content==="canvas"?<CanvasPane {...p}/>:<ContentPane {...p}/>);
  return <main className="parser-lab">
    <header className="pl-top"><div><a href="/workspace">ReqPilotX ↗</a><h1>Parser Lab</h1><p>解析器原生能做什么 · 二次规则补出了什么</p></div><span className="pl-local">LOCAL / NO CLOUD</span></header>
    <div className="pl-layout"><aside className="pl-sidebar">
      <h2>解析运行</h2><label>本地 PDF<input type="file" accept="application/pdf,.pdf" disabled={busy} onChange={e=>setFile(e.target.files?.[0]||null)}/></label>
      <small>≤ 25 MiB / 100 页。仅发送到本地服务。</small>
      <label>下次运行解析器<select aria-label="下次运行解析器" value={parser} disabled={busy} onChange={e=>setParser(e.target.value)}>
        {parsers.map(p=><option key={p.id} value={p.id} disabled={p.availability!=="READY"}>{p.name} · {p.availability}</option>)}
      </select></label>
      {parser==="tesseract"&&<label>下次 OCR 语言<select aria-label="下次 OCR 语言" value={language} disabled={busy} onChange={e=>setLanguage(e.target.value)}><option value="eng">English</option><option value="chi_sim">简体中文</option><option value="eng+chi_sim">English + 简体中文</option></select></label>}
      <button className="pl-primary" type="button" disabled={!file||busy||!parsers.some(p=>p.id===parser&&p.availability==="READY")} onClick={()=>void execute()}>{busy?"正在本地解析…":"运行真实解析"}</button>
      <p>切页／显示规则只读缓存，不重跑解析或改写 raw。</p>
      <details><summary>依赖与支持范围</summary>{parsers.map(p=><p key={p.id}>{p.name} {p.version||"未安装"} · {p.availability}<br/>{p.reason}</p>)}</details>
      <h2>运行记录 <small>{runs.length}</small></h2><div className="pl-history">{runs.map(r=><button key={r.run_id} type="button" aria-pressed={r.run_id===requested} onClick={()=>void load(r.run_id)}>
        <strong>{r.parser} · {r.status}</strong><span>{r.filename}</span><small>{new Date(r.started_at).toLocaleString()}</small></button>)}{!runs.length&&<p>尚无运行。请提供已授权 PDF。</p>}</div>
    </aside><div className="pl-workspace" onKeyDown={keyboard} tabIndex={0} aria-label="解析核对工作面；PageUp/PageDown 或左右键翻页，Home/End 首末页">
      {error&&<p className="pl-error" role="alert">{error}</p>}
      <nav className="pl-view-tabs" aria-label="解析视图">{([["pdf","PDF 对照"],["content","内容对照"],["split","PDF＋内容"]]as const).map(([id,label])=><button key={id} type="button" aria-pressed={view===id} onClick={()=>setView(id)}>{label}</button>)}</nav>
      {!!runs.length&&<div className="pl-run-controls"><label>当前解析器运行<select aria-label="当前解析器运行" value={requested} onChange={e=>void load(e.target.value)}>
        {runs.map(r=><option key={r.run_id} value={r.run_id}>{r.parser} · {r.filename} · {r.status} · {r.run_id.slice(0,6)}</option>)}</select></label>
        {view!=="split"&&<label>同源对照<select aria-label="同源对照" value={compareRequested} onChange={e=>{if(e.target.value)void load(e.target.value,true);else{comparisonRequest.current++;setCompareRequested("");setCompare(null);}}}>
          <option value="">选择同一 PDF 的另一次运行</option>{runs.filter(r=>r.run_id!==requested&&r.source_sha256===current?.source_sha256).map(r=><option key={r.run_id} value={r.run_id}>{r.parser} · {r.status} · {r.run_id.slice(0,6)}</option>)}</select></label>}
      </div>}
      {current&&props?<>
        <div className="pl-run-header"><div><h2>{current.filename}</h2><p>{current.parser} {current.parser_version} · {current.status} · {current.elapsed_seconds??"—"} s</p><small>SHA-256 {current.source_sha256}</small></div>
          <details><summary>本次运行配置</summary><code>{JSON.stringify(current.config||{})}</code><p>修改左侧配置只影响下次运行。</p></details></div>
        <div className="pl-display-controls" aria-label="可视化选项">
          <div role="group" aria-label="来源层">{([["native","原生结果"],["rules","二次规则效果"]]as const).map(([id,label])=><button type="button" key={id} aria-pressed={layer===id} onClick={()=>{setLayer(id);setSelection(null);setCompareSelection(null);}}>{label}</button>)}</div>
          <div role="group" aria-label="内容呈现"><button type="button" aria-pressed={content==="canvas"} onClick={()=>setContent("canvas")}>原位还原画布</button><button type="button" aria-pressed={content==="list"} onClick={()=>setContent("list")}>结构化全文列表</button></div>
          <label><input type="checkbox" checked={boxes} onChange={e=>setBoxes(e.target.checked)}/>bbox / 类型</label>
          <label><input type="checkbox" checked={showNoise} onChange={e=>{setShowNoise(e.target.checked);setSelection(null);setCompareSelection(null);}}/>显示噪声候选</label>
        </div>
        <p className="pl-scope-note">{layer==="native"?"原生事实视图：展示实际提取文字、坐标、表格与资产。SourceIR 含既有 adapter 映射／加工；精确原生输出以 raw 工件为准，不能将映射标签或顺序冒称解析器原生能力。":"二次规则视图：类别、噪声与几何阅读顺序均为候选，来源和理由可在列表查看；不删除或改写 raw。位图水印／倾斜文字未可靠判定，未标噪声不代表没有噪声。"} {!showNoise&&`当前隐藏 ${all.filter(e=>e.noise).length} 个噪声候选。`}</p>
        <div className="pl-legend" aria-label="类型颜色图例">{Object.entries(KINDS).map(([kind,info])=><span key={kind} style={{"--pl-kind":info.color}as CSSProperties}><i/> {info.label}</span>)}</div>
        <nav className="pl-page-controls" aria-label="同步页码与缩放">
          <button type="button" disabled={page<=1} onClick={()=>turn(page-1)}>上一页</button>
          <label>页码<input aria-label="页码跳转" type="number" min={1} max={current.pages.length||1} value={page} onChange={e=>{const n=Number(e.target.value);if(Number.isFinite(n))turn(n);}}/></label><span>/ {current.pages.length}</span>
          <button type="button" disabled={page>=current.pages.length} onClick={()=>turn(page+1)}>下一页</button>
          <label>同步缩放<select aria-label="同步缩放" value={zoom} onChange={e=>setNavigation({runId:current.run_id,page,zoom:Number(e.target.value)})}>{[.5,.75,1,1.25,1.5,2].map(z=><option key={z} value={z}>{Math.round(z*100)}%</option>)}</select></label><small>PageUp / PageDown · ← / → · Home / End</small>
        </nav>
        <nav className="pl-thumbnails" aria-label="PDF 缩略图">{current.pages.filter(p=>Math.abs(p.page_number-page)<=2).map(p=><Thumbnail key={`${current.run_id}-${p.page_number}`} run={current} number={p.page_number} page={page} onPage={turn}/>)}</nav>
        <div className="pl-panes">
          {view==="content"?contentPane(props):<PdfPane {...props}/>}
          {view==="split"?contentPane(props):compareProps?(view==="pdf"?<PdfPane {...compareProps}/>:contentPane(compareProps)):<p className="pl-empty">{compareRequested?"正在读取同源运行或该运行与当前来源不同。":"选择同源运行以对照。PDF＋内容始终显示当前单解析器。"}</p>}
        </div>
        <details className="pl-evidence" open={current.status!=="COMPLETE"}><summary>诊断与支持边界 · {current.diagnostics.length}</summary>{current.diagnostics.map((d,i)=><p key={i}><code>{d.code}</code> {d.message}</p>)}{!current.diagnostics.length&&<p>无程序诊断；不代表来源语义已经人工验收。</p>}</details>
        <details className="pl-evidence"><summary>原始工件与版本 · {current.artifacts.length}</summary><p>Adapter: {current.adapter_id||"未执行"} / {current.adapter_version||"—"}。raw 与原始记录只读，规则效果仅在浏览器内计算。</p>
          {current.artifacts.map(a=><a key={a.id} href={artifactUrl(current,a.id)} target="_blank" rel="noreferrer">{a.path} · {a.byte_count} bytes</a>)}</details>
      </>:requested?<p className="pl-empty" role="status">正在读取所选运行；不展示旧 profile 的结果。</p>:<div className="pl-welcome"><h2>从真实 PDF 开始</h2><p>选择已授权本地文件和可用解析器。</p><p>尚无解析结果；未载入任何演示内容。</p></div>}
    </div></div>
  </main>;
}
