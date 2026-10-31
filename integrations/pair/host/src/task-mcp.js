/** Reuse DSH's bridge in the owning Agent scope; assemble again after its first tool registration. */
export function installTaskMcp(ctx, launcher, loadBridge = () => import('@deepseek-ai/dsh-mcp-client')) {
  const states = new Set();
  let closing = false;

  const settle = async results => {
    const errors = results.filter(result => result.status === 'rejected').map(result => result.reason);
    if (errors.length) throw new AggregateError(errors, 'Task MCP disposal failed');
  };
  const closeState = state => {
    if (state.disposal) return state.disposal;
    state.closed = true;
    state.disposal = (async () => {
      const cleanup = [Promise.resolve().then(() => state.disposeAssembly())];
      for (const handle of state.handles || []) cleanup.push(Promise.resolve().then(() => handle.dispose()));
      if (state.readiness) cleanup.push(Promise.resolve(state.readiness).then(() => undefined, () => undefined));
      const results = await Promise.allSettled(cleanup);
      results.push(...await Promise.allSettled([...state.assemblies]));
      await settle(results);
    })().finally(() => states.delete(state));
    return state.disposal;
  };
  const disposeCreated = ctx.on('agent/created', ({ agent }) => {
    if (closing) return;
    const state = { agent, closed: false, readiness: undefined, handles: [],
      disposal: undefined, assemblies: new Set(), disposeAssembly: () => {}, lifecycle: undefined };
    const mount = async context => {
      const discovered = typeof launcher.toolBindings === 'function'
        ? await launcher.toolBindings(agent.id)
        : [await launcher.toolBinding(agent.id)].filter(Boolean);
      const bindings = (Array.isArray(discovered) ? discovered : [discovered]).filter(Boolean);
      if (!bindings.length || closing || state.closed) return false;
      context.signal?.throwIfAborted();
      const bridge = await loadBridge();
      if (closing || state.closed) return false;
      context.signal?.throwIfAborted();
      const mounted = [];
      try {
        for (const binding of bindings) {
          const handle = agent.ctx.plugin(bridge, { serverName: binding.serverName || 'reqpilot', transport: 'stdio',
            command: binding.command, args: binding.args, cwd: binding.cwd,
            toolCallTimeoutMs: 120000, failOnStartupError: true });
          mounted.push(handle);
          state.handles = mounted;
          await handle;
          if (closing || state.closed) { await handle.dispose(); return false; }
          context.signal?.throwIfAborted();
        }
        return true;
      } catch (error) {
        for (const handle of mounted) await handle.dispose().catch(() => {});
        state.handles = [];
        if (closing || state.closed) return false;
        throw error;
      }
    };
    const assemble = async (_assembly, context, next) => {
      if (closing || state.closed) return next();
      const first = !state.readiness;
      if (!state.readiness) {
        const readiness = mount(context);
        state.readiness = readiness;
        readiness.catch(() => {
          if (!closing && !state.closed && state.readiness === readiness) state.readiness = undefined;
        });
      }
      const mounted = await state.readiness;
      if (closing || state.closed) return next();
      const assembled = await next();
      if (closing || state.closed) return assembled;
      // The original assembly collected its schemas before this waterfall. Re-read the
      // public assembler once so the very first request also sees the newly mounted tools.
      return first && mounted ? agent.ctx.get('systemPrompt').assemble(context) : assembled;
    };
    state.disposeAssembly = agent.ctx.on('system-prompt/assemble', (...args) => {
      const pending = assemble(...args);
      state.assemblies.add(pending);
      void pending.finally(() => state.assemblies.delete(pending)).catch(() => {});
      return pending;
    });
    state.lifecycle = agent.ctx.effect(() => () => closeState(state), 'reqpilot task MCP binding');
    states.add(state);
  });
  return ctx.effect(() => async () => {
    closing = true;
    disposeCreated();
    await settle(await Promise.allSettled([...states].map(state => Promise.resolve().then(() => state.lifecycle()))));
  }, 'reqpilot task MCP host ownership');
}
