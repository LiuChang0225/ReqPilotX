let csrfToken: string | null = null;
let accountIdentity: string | null | undefined;

/** undefined is the unchanged local/Pair mode; null is an unauthenticated SaaS shell. */
export function setSaasAccountIdentity(account: string | null | undefined): void {
  accountIdentity = account;
}

export function getSaasAccountIdentity(): string | null | undefined {
  return accountIdentity;
}

export function isSaasAccountMode(): boolean {
  return accountIdentity !== undefined;
}

export function saasStorageKey(key: string): string {
  return accountIdentity === undefined ? key : `reqpilotx:account:${encodeURIComponent(accountIdentity || "guest")}:${key}`;
}

/** An in-memory session secret. Never persisted, and never sent to another origin. */
export function setSaasCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function saasCsrfFor(path: string): string | null {
  if (!csrfToken || typeof window === "undefined") return null;
  try {
    const url = new URL(path, window.location.href);
    return url.origin === window.location.origin && url.pathname.startsWith("/api/") ? csrfToken : null;
  } catch {
    return null;
  }
}

export function expireSaasSession(status: number, sentToken: string | null): void {
  if (status !== 401 || !sentToken || sentToken !== csrfToken) return;
  csrfToken = null;
  accountIdentity = null;
  if (typeof window !== "undefined") window.dispatchEvent(new Event("reqpilotx:session-expired"));
}
