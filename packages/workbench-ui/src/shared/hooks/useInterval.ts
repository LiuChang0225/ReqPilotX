import { useEffect, useRef } from "react";

export function useInterval(
  callback: () => void | Promise<void>,
  delay: number | null,
): void {
  const latest = useRef(callback);
  useEffect(() => {
    latest.current = callback;
  }, [callback]);

  useEffect(() => {
    if (delay === null) return;
    const id = window.setInterval(() => {
      void latest.current();
    }, delay);
    return () => window.clearInterval(id);
  }, [delay]);
}
