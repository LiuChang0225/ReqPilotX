import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";
import { supportedUiLocales, workbenchMessages, type UiLocale } from "../shared/i18n/messages";

export type HostTranslate = (key: string, params?: Record<string, unknown>) => string;
export type UiTranslate = (key: string, fallback?: string, params?: Record<string, string | number>) => string;

export const uiLocaleKey = "workbench.locale";

function format(value: string, params?: Record<string, string | number>): string {
  if (!params) return value;
  return value.replace(/\{(\w+)\}/g, (_match, name: string) => String(params[name] ?? `{${name}}`));
}

export function resolveUiLocale(t?: HostTranslate, explicit?: string): UiLocale {
  if (explicit && supportedUiLocales.includes(explicit as UiLocale)) return explicit as UiLocale;
  if (!t) return "zh";
  try {
    const value = t(uiLocaleKey);
    if (value && supportedUiLocales.includes(value as UiLocale)) return value as UiLocale;
  } catch {
    // A host translate slot without this key keeps the standalone default.
  }
  return "zh";
}

export function lookupMessage(locale: UiLocale, key: string, fallback?: string): string | undefined {
  return workbenchMessages[locale][key] ?? fallback;
}

/** Resolve a keyed owned string outside React, for the separate PDF viewer page. */
export function translateUi(locale: UiLocale, key: string, fallback?: string, params?: Record<string, string | number>): string {
  return format(lookupMessage(locale, key, fallback) ?? key, params);
}

type HostLocaleValue = { t?: HostTranslate; locale: UiLocale };
const defaultHostLocale: HostLocaleValue = { locale: "zh" };
const HostLocaleContext = createContext<HostLocaleValue>(defaultHostLocale);

export function HostLocaleProvider({ t, locale, children }: { t?: HostTranslate; locale?: string; children: ReactNode }) {
  const resolved = useMemo(() => resolveUiLocale(t, locale), [t, locale]);
  const value = useMemo(() => ({ t, locale: resolved }), [t, resolved]);
  return <HostLocaleContext.Provider value={value}>{children}</HostLocaleContext.Provider>;
}

export function useUiLocale(): UiLocale {
  return useContext(HostLocaleContext).locale;
}

/** Resolve workbench chrome through the native host slot, falling back to owned dictionaries. */
export function useUiCopy(): UiTranslate {
  const { t, locale } = useContext(HostLocaleContext);
  return useCallback<UiTranslate>((key, fallback, params) => {
    let value: string | undefined;
    if (t) {
      try {
        const translated = t(key, params as Record<string, unknown> | undefined);
        if (translated && translated !== key) value = translated;
      } catch {
        // Missing host dictionary entries fall through to the owned dictionary.
      }
    }
    if (value === undefined) value = lookupMessage(locale, key, fallback);
    if (value === undefined) value = key;
    return format(value, params);
  }, [t, locale]);
}

/** Resolve a new host-facing label through the native slot `t`; standalone keeps the supplied default. */
export function useHostCopy() {
  const t = useContext(HostLocaleContext).t;
  return useCallback((key: string, fallback: string, params?: Record<string, string | number>) => {
    let value = t ? t(key, params) : fallback;
    if (value === key) value = fallback;
    if (params) value = value.replace(/\{(\w+)\}/g, (_match, name: string) => String(params[name] ?? `{${name}}`));
    return value;
  }, [t]);
}
