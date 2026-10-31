import { createContext, useCallback, useContext, type ReactNode } from "react";

export type HostTranslate = (key: string, params?: Record<string, unknown>) => string;

const HostLocaleContext = createContext<HostTranslate | undefined>(undefined);

export function HostLocaleProvider({ t, children }: { t?: HostTranslate; children: ReactNode }) {
  return <HostLocaleContext.Provider value={t}>{children}</HostLocaleContext.Provider>;
}

/** Resolve a new host-facing label through the native slot `t`; standalone keeps the supplied default. */
export function useHostCopy() {
  const t = useContext(HostLocaleContext);
  return useCallback((key: string, fallback: string, params?: Record<string, string | number>) => {
    let value = t ? t(key, params) : fallback;
    if (value === key) value = fallback;
    if (params) value = value.replace(/\{(\w+)\}/g, (_match, name: string) => String(params[name] ?? `{${name}}`));
    return value;
  }, [t]);
}
