import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { setSaasAccountIdentity, setSaasCsrfToken } from "@reqpilotx/workbench-ui/web";
import { AccountRequestError, accountErrorMessage, requestAccountSession, type AccountSession } from "./client";
import "./account.css";

type AccountState = { kind: "loading" } | { kind: "failed" } | { kind: "ready"; session: AccountSession };

function AccountForm({ onSession }: { onSession: (session: AccountSession) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const formRef = useRef<HTMLFormElement>(null);
  const alive = useRef(true);
  const submitting = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    const form = event.currentTarget;
    const values = new FormData(form);
    const payload: Record<string, string> = {
      email: String(values.get("email") || "").trim(),
      password: String(values.get("password") || ""),
    };
    if (mode === "register") payload.invitation = String(values.get("invitation") || "").trim();
    setPending(true);
    setError("");
    try {
      const session = await requestAccountSession(mode, { payload });
      if (!session.enabled || !session.user) throw new Error("invalid_authenticated_session");
      if (alive.current) onSession(session);
    } catch (reason) {
      if (alive.current) setError(accountErrorMessage(reason));
    } finally {
      // Keep credentials only for this request; never put them in browser storage.
      const password = form.elements.namedItem("password");
      const invitation = form.elements.namedItem("invitation");
      if (password instanceof HTMLInputElement) password.value = "";
      if (invitation instanceof HTMLInputElement) invitation.value = "";
      payload.password = "";
      if (payload.invitation) payload.invitation = "";
      submitting.current = false;
      if (alive.current) setPending(false);
    }
  }

  function switchMode() {
    if (pending) return;
    formRef.current?.reset();
    setError("");
    setMode(mode === "login" ? "register" : "login");
  }

  return <main className="reqpilotx-account-screen">
    <section className="reqpilotx-account-card" aria-labelledby="account-title">
      <p className="reqpilotx-account-brand">ReqPilotX</p>
      <h1 id="account-title">{mode === "login" ? "登录私人工作区" : "创建私人工作区"}</h1>
      <p className="reqpilotx-account-intro">上传原文件，让 Agent 整理需求，保留可追溯的交付版本</p>
      <form ref={formRef} onSubmit={submit} aria-busy={pending}>
        <fieldset disabled={pending}>
          <label htmlFor="account-email">邮箱</label>
          <input id="account-email" name="email" type="email" autoComplete="username" required maxLength={254} />
          <label htmlFor="account-password">密码{mode === "register" ? "（至少 12 位）" : ""}</label>
          <input id="account-password" name="password" type="password"
            autoComplete={mode === "register" ? "new-password" : "current-password"}
            required minLength={mode === "register" ? 12 : undefined} maxLength={256} />
          {mode === "register" && <>
            <label htmlFor="account-invitation">邀请码</label>
            <input id="account-invitation" name="invitation" type="password" autoComplete="off" required minLength={32} maxLength={512} />
            <p className="reqpilotx-account-help">当前为邀请试用，请使用邀请人提供的邀请码</p>
          </>}
          <button className="reqpilotx-account-primary" type="submit">{pending ? "请稍候…" : mode === "login" ? "登录" : "创建账户"}</button>
        </fieldset>
      </form>
      {error && <p className="reqpilotx-account-error" role="alert">{error}</p>}
      <button className="reqpilotx-account-switch" type="button" onClick={switchMode} disabled={pending}>
        {mode === "login" ? "有邀请码？创建账户" : "已有账户？返回登录"}
      </button>
    </section>
  </main>;
}

export function AccountGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AccountState>({ kind: "loading" });
  const [logoutError, setLogoutError] = useState("");
  const [loggingOut, setLoggingOut] = useState(false);
  const sequence = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);

  const applySession = useCallback((session: AccountSession) => {
    sequence.current += 1;
    controller.current?.abort();
    setSaasAccountIdentity(session.enabled ? session.user?.id ?? null : undefined);
    setSaasCsrfToken(session.csrf_token);
    setLogoutError("");
    setState({ kind: "ready", session });
  }, []);

  const refreshSession = useCallback(async (silent = false) => {
    const current = ++sequence.current;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    if (!silent) setState({ kind: "loading" });
    try {
      const session = await requestAccountSession("session", { signal: abort.signal });
      if (mounted.current && current === sequence.current) applySession(session);
    } catch {
      if (!mounted.current || current !== sequence.current || abort.signal.aborted) return;
      setSaasAccountIdentity(null);
      setSaasCsrfToken(null);
      setState({ kind: "failed" });
    }
  }, [applySession]);

  useEffect(() => {
    mounted.current = true;
    void refreshSession();
    const expired = () => applySession({ enabled: true, user: null, csrf_token: null });
    const focused = () => { void refreshSession(true); };
    window.addEventListener("reqpilotx:session-expired", expired);
    window.addEventListener("focus", focused);
    return () => {
      mounted.current = false;
      sequence.current += 1;
      controller.current?.abort();
      window.removeEventListener("reqpilotx:session-expired", expired);
      window.removeEventListener("focus", focused);
      setSaasAccountIdentity(null);
      setSaasCsrfToken(null);
    };
  }, [applySession, refreshSession]);

  async function logout() {
    if (loggingOut || state.kind !== "ready") return;
    setLoggingOut(true);
    setLogoutError("");
    try {
      const session = await requestAccountSession("logout", { csrfToken: state.session.csrf_token });
      if (mounted.current) {
        window.history.replaceState(null, "", "/");
        window.dispatchEvent(new PopStateEvent("popstate"));
        applySession(session);
      }
    } catch (reason) {
      if (mounted.current && reason instanceof AccountRequestError && reason.status === 401) {
        applySession({ enabled: true, user: null, csrf_token: null });
      } else if (mounted.current) setLogoutError(accountErrorMessage(reason));
    } finally {
      if (mounted.current) setLoggingOut(false);
    }
  }

  if (state.kind === "loading") return <main className="reqpilotx-account-screen"><p role="status">正在检查账户…</p></main>;
  if (state.kind === "failed") return <main className="reqpilotx-account-screen"><section className="reqpilotx-account-card">
    <h1>暂时无法检查账户</h1><p role="alert">工作区尚未开放，请重试账户服务</p>
    <button className="reqpilotx-account-primary" type="button" onClick={() => { void refreshSession(); }}>重试</button>
  </section></main>;
  const { session } = state;
  if (!session.enabled) return <>{children}</>;
  if (!session.user) return <AccountForm onSession={applySession} />;
  return <div key={session.user.id} className="reqpilotx-authenticated-app">
    <div className="reqpilotx-account-bar" aria-label="当前账户">
      <span>私人工作区</span><span className="reqpilotx-account-email">{session.user.email}</span>
      <button type="button" onClick={() => { void logout(); }} disabled={loggingOut}>{loggingOut ? "正在退出…" : "退出登录"}</button>
      {logoutError && <span role="alert">{logoutError}</span>}
    </div>
    {children}
  </div>;
}
