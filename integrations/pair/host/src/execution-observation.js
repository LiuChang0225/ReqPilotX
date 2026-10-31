/** Project public DSH lifecycle facts; never expose messages, arguments, streams or raw failures. */
export function executionObservation(record, events, liveStatus, now = new Date().toISOString()) {
  const accepted = events.findIndex(e => e.type === 'user/message' && e.data?.source?.kind === 'user' && e.data.source.rpcId === record.requestId);
  if (accepted < 0) return null;
  let start = accepted;
  while (start > 0 && events[start].type !== 'turn/start') start--;
  const result = { runId: record.requestId, status: 'IDLE', connected: true, observedAt: now,
    activity: 'IDLE', step: 0, toolCalls: 0, toolFailures: 0, retries: 0, lastEventAt: null };
  const pending = new Set(); let open = false;
  for (const event of events.slice(start)) {
    const d = event.data || {};
    switch (event.type) {
      case 'turn/start': open = true; result.runId = record.requestId + ':turn-' + d.turn; result.status = 'RUNNING'; result.activity = 'MODEL'; pending.clear(); break;
      case 'step/start': result.step = d.step; result.status = 'RUNNING'; result.activity = 'MODEL'; break;
      case 'tool/call': pending.add(d.callId); result.toolCalls++; result.activity = 'TOOLS'; break;
      case 'tool/result':
        for (const block of d.message?.content || []) if (block.type === 'tool-result') {
          pending.delete(block.toolCallId); if (block.isError) result.toolFailures++;
        }
        result.activity = pending.size ? 'TOOLS' : 'MODEL'; break;
      case 'llm/retry': result.retries++; result.status = 'WAITING'; result.activity = 'RETRY'; result.retryDelayMs = d.delayMs; break;
      case 'llm/retry-started': result.status = 'RUNNING'; result.activity = 'MODEL'; delete result.retryDelayMs; break;
      case 'turn/end': {
        open = false; pending.clear(); delete result.retryDelayMs;
        const status = { completed: 'SUCCEEDED', error: 'FAILED', aborted: 'INTERRUPTED', interrupted: 'INTERRUPTED', blocked: 'WAITING', 'max-tokens': 'FAILED' }[d.reason?.kind];
        result.status = status || 'IDLE'; result.activity = status === 'SUCCEEDED' ? 'TURN_ENDED' : status === 'FAILED' ? 'FAILED' : status === 'WAITING' ? 'BLOCKED' : status === 'INTERRUPTED' ? 'INTERRUPTED' : 'IDLE';
        break;
      }
      default: continue;
    }
    if (typeof event.time === 'number' && Number.isFinite(event.time)) result.lastEventAt = new Date(event.time).toISOString();
  }
  // An unclosed stored turn alone does not prove that an Agent is still running.
  if (open && liveStatus !== 'running') { result.status = 'IDLE'; result.activity = 'UNOBSERVED'; result.connected = false; }
  return result;
}
