import {
  type Dispatch,
  type SetStateAction,
  useCallback,
  useEffect,
  useState,
} from "react";
import { saasStorageKey } from "../api/saasSession";

const cache = new Map<string, unknown>();
const MAX_SCOPED_STATES = 320;

/** A completed save may outlive its editor; never erase a newer draft. */
export function clearScopedUiStateIfUnchanged<T>(key: string, expected: T): boolean {
  key = saasStorageKey(key);
  if (cache.has(key) && cache.get(key) !== expected) return false;
  cache.delete(key);
  return true;
}

function remember<T>(key: string, value: T) {
  if (!cache.has(key) && cache.size >= MAX_SCOPED_STATES) {
    const oldest = cache.keys().next().value;
    if (typeof oldest === "string") cache.delete(oldest);
  }
  cache.set(key, value);
}

export function useScopedUiState<T>(
  key: string,
  initialValue: T | (() => T),
): [T, Dispatch<SetStateAction<T>>] {
  key = saasStorageKey(key);
  const resolveInitial = useCallback(
    () =>
      cache.has(key)
        ? (cache.get(key) as T)
        : typeof initialValue === "function"
          ? (initialValue as () => T)()
          : initialValue,
    [initialValue, key],
  );
  const [value, setValue] = useState<T>(resolveInitial);

  useEffect(() => {
    setValue(resolveInitial());
  }, [key, resolveInitial]);

  const update: Dispatch<SetStateAction<T>> = useCallback(
    (next) => {
      setValue((current) => {
        const resolved =
          typeof next === "function"
            ? (next as (previous: T) => T)(current)
            : next;
        remember(key, resolved);
        return resolved;
      });
    },
    [key],
  );

  return [value, update];
}
