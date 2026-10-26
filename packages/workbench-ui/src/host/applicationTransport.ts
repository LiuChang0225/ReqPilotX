/** One transport generation per embedded application bundle, separate from the host's global fetch. */
export interface ApplicationTransport {
  /** Stable application identity, independent of its current loopback port. */
  id: string;
  /** Resolve business resources through the authenticated host broker. */
  resourceUrl(path: string): string;
  /** Execute a business request; the host owns routing and authorization. */
  fetch: typeof fetch;
}

let generation: { transport: ApplicationTransport; lifetime: AbortController } | undefined;
let hosted = false;

/** Install before rendering the application; disposal aborts outstanding requests and prevents browser fallback. */
export function installApplicationTransport(transport: ApplicationTransport): () => void {
  if (generation) throw new Error("An embedded ReqPilotX application transport is already installed.");
  const current = { transport, lifetime: new AbortController() };
  generation = current; hosted = true;
  return () => {
    current.lifetime.abort();
    if (generation === current) generation = undefined;
  };
}

function currentGeneration() {
  if (hosted && !generation) throw new Error("ReqPilotX 连接已关闭，请重新打开工作台。");
  return generation;
}

/** Map upload/download paths without changing standalone Web URLs. */
export function applicationResourceUrl(path: string): string {
  return currentGeneration()?.transport.resourceUrl(path) ?? path;
}

/** Namespace pending business commands by their managed application, not by a service port. */
export function applicationNamespace(): string | undefined {
  return currentGeneration()?.transport.id;
}

/** Capture the transport generation before awaiting a request. */
export const applicationFetch: typeof fetch = (input, init) => {
  const current = currentGeneration();
  if (!current) return fetch(input, init);
  const signal = init?.signal ? AbortSignal.any([init.signal, current.lifetime.signal]) : current.lifetime.signal;
  return current.transport.fetch(input, { ...init, signal });
};
