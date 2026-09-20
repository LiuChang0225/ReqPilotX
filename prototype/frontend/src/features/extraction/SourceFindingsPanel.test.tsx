import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SourceFindingsPanel } from "./SourceFindingsPanel";
afterEach(cleanup);
it("presents a scoped numbering observation as pending source review and locates both endpoints",()=>{
 const locate=vi.fn();render(<SourceFindingsPanel sourceSha256="source" onLocate={locate} value={{source_sha256:"source",scan_state:"COMPLETED",complete_or_unknown:"UNKNOWN",findings:[{finding_id:"gap",kind:"NUMBER_SEQUENCE_GAP",status:"PENDING_SOURCE_REVIEW",source_review_state:"NOT_REVIEWED",observed:"2.6 → 2.10",message:"当前成果出现编号间断，待回查原件。",evidence:{before:{node_id:"n6",source_number:"2.6",pages:[3]},after:{node_id:"n10",source_number:"2.10",pages:[3]}}}]}}/>);
 fireEvent.click(screen.getByText("来源提示 1"));expect(screen.getByText("待回查原件")).toBeVisible();expect(screen.getByText(/不代表整份原件完整/)).toBeVisible();
 fireEvent.click(screen.getByRole("button",{name:"查看 2.10 原页"}));expect(locate).toHaveBeenCalledWith("n10",3);
});
it("does not call an absent or mismatched scan a clean document",()=>{
 const page=render(<SourceFindingsPanel sourceSha256="current" onLocate={vi.fn()} value={{source_sha256:"other",scan_state:"COMPLETED",findings:[]}}/>);
 expect(screen.getByText("检查结果属于另一来源，待同步。")).toBeVisible();page.rerender(<SourceFindingsPanel sourceSha256="current" onLocate={vi.fn()} value={{scan_state:"NOT_RUN",findings:[]}}/>);
 expect(screen.getByText("来源检查未完成")).toBeVisible();
});
