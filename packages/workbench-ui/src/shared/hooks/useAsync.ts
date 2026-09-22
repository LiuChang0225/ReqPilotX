import { useCallback, useEffect, useRef, useState } from "react";

export interface AsyncState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  refresh: () => Promise<T | null>;
}

export function useAsync<T>(
  loader: () => Promise<T>,
  dependencies: readonly unknown[],
  options: { enabled?: boolean; retain?: boolean } = {},
): AsyncState<T> {
  const { enabled = true, retain = true } = options;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(enabled);
  const requestRef = useRef(0);

  const refresh = useCallback(async () => {
    if (!enabled) return null;
    const requestId = ++requestRef.current;
    setLoading(true);
    setError(null);
    if (!retain) setData(null);
    try {
      const result = await loader();
      if (requestId === requestRef.current) setData(result);
      return result;
    } catch (cause) {
      const normalized =
        cause instanceof Error ? cause : new Error(String(cause));
      if (requestId === requestRef.current) setError(normalized);
      return null;
    } finally {
      if (requestId === requestRef.current) setLoading(false);
    }
  }, [enabled, loader, retain]);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    void refresh();
    return () => {
      requestRef.current += 1;
    };
    // dependencies are intentionally owned by the caller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, refresh, ...dependencies]);

  return { data, error, loading, refresh };
}
