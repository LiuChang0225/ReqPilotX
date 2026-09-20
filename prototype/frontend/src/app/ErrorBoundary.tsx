import { Component, type ErrorInfo, type ReactNode } from "react";
import reqPilotLogoUrl from "../assets/reqpilotx-logo.svg?url";

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<
  { children: ReactNode; embedded?: boolean },
  State
> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("ReqPilotX frontend boundary", error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;
    if (this.props.embedded) return <section className="readonly-panel-notice" role="alert">
      <p>当前工作面板未能显示：{this.state.error.message}</p>
      <button type="button" className="button secondary compact" onClick={() => this.setState({ error: null })}>重新读取面板</button>
    </section>;
    return (
      <main className="fatal-page">
        <img src={reqPilotLogoUrl} alt="" width={436} height={88} />
        <p>界面恢复</p>
        <h1>页面运行遇到异常</h1>
        <div>{this.state.error.message}</div>
        <button type="button" onClick={() => window.location.reload()}>
          重新加载
        </button>
      </main>
    );
  }
}
