/** Development-only host harness. It opens saved results and never creates a model task or fake Pair session. */
import React, { useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
// @ts-expect-error Generated library is the subject of this harness.
import { ReqPilotXReadonlyPanel, createReadonlyHttpClient } from "../../dist-panel/reqpilotx-readonly.js";
import "../../dist-panel/reqpilotx-readonly.css";
import type { ReadonlyWorkbenchHost, ReadonlyWorkSelection } from "./readonlyHost";

const params = new URLSearchParams(location.search);
const initial: ReadonlyWorkSelection = { projectId: params.get("projectId") || "", documentId: params.get("documentId") || "", sourceSha256: params.get("sourceSha256") || "",
  workRef: params.get("workRef") || "", revisionId: params.get("revisionId") || undefined, view: "items" };
function Preview() {
  const [narrow,setNarrow]=useState(false), [theme,setTheme]=useState<"light"|"dark">("light");
  const [selection,setSelection]=useState(initial);
  const host = useMemo<ReadonlyWorkbenchHost>(()=>({id:"react-host-harness",label:"宿主",client:createReadonlyHttpClient("local-proxy",(path:string)=>path),
    resourceUrl:path=>new URL(path,params.get("resourceOrigin") || location.origin).href,
    pdfViewerUrl:(source,page,embedded)=>{
      const url = new URL("/pdf-viewer",params.get("viewerOrigin") || location.origin);
      url.searchParams.set("pdf",source);url.searchParams.set("page",String(page));url.searchParams.set("fit","page");
      if(embedded){url.searchParams.set("embedded","1");url.searchParams.set("parentOrigin",location.origin);}
      return url.href;
    },onSelectionChange:setSelection}),[]);
  return <><header className="preview-header"><strong>宿主容器验证 · React {React.version}</strong><span>既有成果 · 无模型运行</span><button id="outside-control" onClick={()=>setNarrow(value=>!value)}>{narrow?"恢复宽面板":"窄面板"}</button><button onClick={()=>setTheme(value=>value==="light"?"dark":"light")}>切换面板主题</button></header>
    <div className={`preview-container${narrow?" is-narrow":""}`}><ReqPilotXReadonlyPanel host={host} selection={selection} documentName={params.get("documentName") || "已保存原件"} theme={theme}/></div></>;
}
createRoot(document.getElementById("preview-root")!).render(<Preview/>);
