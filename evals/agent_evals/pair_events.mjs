// Public event adapter, mounted inside the ordinary dsh headless profile.
// Does not serialize request headers, provider streams, or reasoning blocks.
export const name = 'reqpilot-public-events';
export const inject = ['sessions'];

// Only expose bounded machine metadata. Provider error prose can contain
// request content, so it must not be copied into the public trace.
export function failureMetadata(error) {
  const code = typeof error?.code === 'string' && /^[A-Z0-9_]{1,64}$/.test(error.code)
    ? error.code : undefined;
  const idle = code === 'TIMEOUT' && /^pi-ai stream idle timeout after ([0-9]{1,9})ms$/.exec(error?.message ?? '');
  return {
    ...(code && { host_error_code: code }),
    ...(idle && { error_code: 'HOST_LLM_STREAM_IDLE_TIMEOUT', idle_timeout_ms: Number(idle[1]) }),
  };
}

export function apply(ctx) {
  const seen = new Set();
  const calls = new Map();
  const usages = new Map();
  const emit = event => process.stdout.write(JSON.stringify(event) + '\n');
  ctx.on('session/event', (session, event) => {
    if (!seen.has(session.id)) {
      seen.add(session.id);
      emit({ type: 'thread.started', thread_id: session.id });
    }
    const d = event.data;
    if (event.type === 'step/start') emit({ type: 'host.model_step.started',
      step: d.step, host_time_ms: event.time, boundary: 'STEP_START_NOT_HTTP_SEND' });
    if (event.type === 'llm/retry') emit({ type: 'host.retry', step: d.step,
      retry: d.retry, max_retries: d.maxRetries, delay_ms: d.delayMs,
      host_time_ms: event.time, ...failureMetadata(d.failure) });
    if (event.type === 'assistant/attempt') emit({ type: 'host.attempt_failed',
      step: d.step, host_time_ms: event.time });
    if (event.type === 'turn/start') emit({ type: 'turn.started', turn_id: String(d.turn) });
    if (event.type === 'assistant/message') {
      for (const block of d.message.content) {
        if (block.type === 'text') emit({ type: 'item.completed', item: {
          id: `text-${event.seq}`, type: 'agent_message', text: block.text } });
        if (block.type === 'tool-call') {
          calls.set(block.id, block.name);
          emit({ type: 'item.started', item: { id: block.id, type: 'tool_call',
            name: block.name, arguments: block.arguments } });
        }
      }
      if (d.usage) {
        const total = usages.get(d.turn) ?? {};
        const mapping = { inputTokens: 'input_tokens', outputTokens: 'output_tokens',
          totalTokens: 'total_tokens', cacheReadTokens: 'cached_input_tokens',
          cacheWriteTokens: 'cache_write_input_tokens', reasoningTokens: 'reasoning_tokens' };
        for (const [key, value] of Object.entries(d.usage)) {
          if (mapping[key] && Number.isFinite(value)) total[mapping[key]] = (total[mapping[key]] ?? 0) + value;
        }
        usages.set(d.turn, total);
        emit({ type: 'host.usage', usage: total, usage_scope: 'turn_cumulative',
          input_tokens_semantics: 'uncached; cache read and write are separate' });
      }
    }
    if (event.type === 'tool/result') {
      for (const block of d.message.content) {
        if (block.type !== 'tool-result') continue;
        emit({ type: 'item.completed', item: { id: block.toolCallId, type: 'tool_call',
          name: calls.get(block.toolCallId), status: block.isError ? 'failed' : 'completed',
          result: block.content.filter(b => b.type === 'text').map(b => ({ type: 'text', text: b.text })),
          error: d.error && { code: d.error.code } } });
      }
    }
    if (event.type === 'turn/end') emit({
      type: d.reason.kind === 'completed' ? 'turn.completed' : 'turn.failed',
      turn_id: String(d.turn), host_reason: d.reason.kind, usage: usages.get(d.turn),
      ...(d.reason.kind === 'error' && failureMetadata(d.reason.error)) });
  });
}
