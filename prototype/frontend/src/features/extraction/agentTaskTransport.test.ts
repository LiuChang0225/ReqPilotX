import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, request } from "../../shared/api/client";
import { readAgentEventTail, retainedCommand, settleCommand } from "./agentTaskTransport";
vi.mock("../../shared/api/client", async original => ({ ...await original<object>(), request: vi.fn() }));
afterEach(() => { vi.resetAllMocks(); localStorage.clear(); });
describe("Agent event recovery", () => {
  it("drains terminal history through all available pages before calling it recent", async () => {
    const events = (start: number, count: number) => Array.from({ length: count }, (_, index) => ({ type: "tool_completed", message: `event-${start+index}` }));
    vi.mocked(request).mockResolvedValueOnce({ events: events(0, 100), next_cursor: 1000 })
      .mockResolvedValueOnce({ events: events(100, 100), next_cursor: 2000 })
      .mockResolvedValueOnce({ events: events(200, 18), next_cursor: 2200 });
    const result = await readAgentEventTail("/loop", "task", 0, new AbortController().signal);
    expect(result.hasMore).toBe(false); expect(result.cursor).toBe(2200);
    expect(result.events).toHaveLength(40);
    expect(result.events[0].message).toBe("event-178"); expect(result.events.at(-1)?.message).toBe("event-217");
    expect(request).toHaveBeenNthCalledWith(2, "/loop/task/events?after=1000", expect.anything());
  });
  it("does not commit a page from an abandoned source request", async () => {
    const controller = new AbortController();
    vi.mocked(request).mockImplementation(async () => { controller.abort(); return { events: [{ message: "old" }], next_cursor: 400 } as never; });
    expect(await readAgentEventTail("/loop", "old", 0, controller.signal)).toEqual({ events: [], cursor: 0, hasMore: false });
  });
  it("keeps an uncertain command across retry and refresh, but starts a new command after a receipt", () => {
    const first = retainedCommand("command-key", "same source and payload");
    settleCommand("command-key", first, new Error("connection lost"));
    expect(retainedCommand("command-key", "same source and payload")).toBe(first);
    settleCommand("command-key", first);
    expect(retainedCommand("command-key", "same source and payload")).not.toBe(first);
  });
});

import { reducePublicMessages } from "./agentTaskTransport";
it("reconnects without duplicating Unicode deltas and uses authoritative completion", () => {
  const identity = { message_id:"m", attempt_id:"a",thread_id:"t",turn_id:"r",phase:"commentary" };
  let state = reducePublicMessages([], [{...identity,type:"agent_message_start",version:0,text:""},{...identity,type:"agent_message_delta",version:1,delta:"正在核对表格。"}]);
  const snapshot = state;
  state = reducePublicMessages(state, [{...identity,type:"agent_message_delta",version:1,delta:"正在核对表格。"},{...identity,type:"agent_message_delta",version:2,delta:" 已读 2 页。"}], snapshot);
  expect(state[0].text).toBe("正在核对表格。 已读 2 页。");
  state = reducePublicMessages(state,[{...identity,type:"agent_message",version:4,text:"已完成表格核对。"}]);
  expect(state[0].text).toBe("已完成表格核对。"); expect(state[0].status).toBe("complete");
  expect(reducePublicMessages(state,[{...identity,type:"agent_message_delta",version:5,delta:"duplicate"}])[0].text).toBe("已完成表格核对。");
});
it("does not merge another attempt or surface reasoning, and preserves unfinished replies", () => {
  const event = {message_id:"m",attempt_id:"one",thread_id:"t",turn_id:"r",phase:"commentary",type:"agent_message_start",text:"已收到",version:0};
  const state=reducePublicMessages([], [event,{...event,attempt_id:"two",text:"第二轮"},{...event,phase:"analysis",text:"private"},{type:"turn/completed",turn_id:"r"}]);
  expect(state).toHaveLength(2); expect(state.every(message=>message.status==="interrupted")).toBe(true);
  expect(JSON.stringify(state)).not.toContain("private");
});
