import { useCallback, useEffect, useState } from "react";
import { AppLink } from "../../shared/navigation";
import { api } from "../../shared/api/client";
import { Brand } from "../../shared/ui/Brand";
import { Icon, type IconName } from "../../shared/ui/Icon";
import { StatusBadge } from "../../shared/ui/Feedback";
import { useAsync } from "../../shared/hooks/useAsync";
import { cx } from "../../shared/lib/format";

const workflow = [
  {
    index: "01",
    title: "项目管理",
    body: "在一个工作面选择项目、客户文件与知识依据；离线时也能先安全保存。",
  },
  {
    index: "02",
    title: "需求提取",
    body: "按章节、条款与需求条目查看同一棵结构树，只处理真正需要人工关注的异常。",
  },
  {
    index: "03",
    title: "需求分析",
    body: "在同一工作面查看原文、知识证据与 AI 建议，最终结论始终由人确认。",
  },
  {
    index: "04",
    title: "矩阵导出",
    body: "选择需求版本、比较差异并下载矩阵；只有正式导出才启用严格检查。",
  },
];

const capabilities: Array<{
  icon: IconName;
  code: string;
  title: string;
  body: string;
}> = [
  {
    icon: "document",
    code: "SOURCE",
    title: "来源事实不变",
    body: "客户原文、表格和位置保持不变；任何整理都不会悄悄改写来源事实。",
  },
  {
    icon: "split",
    code: "STRUCTURE",
    title: "默认自动，异常局部",
    body: "正常内容自动向前流转；失败只影响具体文件、条款或任务，不锁住整份文档。",
  },
  {
    icon: "evidence",
    code: "EVIDENCE",
    title: "证据就在判断旁边",
    body: "从条目直接回到原文件或知识依据，AI 建议不会脱离证据成为黑盒。",
  },
  {
    icon: "matrix",
    code: "DELIVERY",
    title: "一个人工结论",
    body: "在在线 Matrix 的分析面板中写入人工结论，Excel 导出复用同一份层级、状态与判定。",
  },
];

export function HomePage() {
  const loadReadiness = useCallback(() => api.readiness(), []);
  const readiness = useAsync(loadReadiness, []);
  const [headerSolid, setHeaderSolid] = useState(false);

  useEffect(() => {
    document.title = "ReqPilotX · 可追溯需求智能工作台";
    const onScroll = () => setHeaderSolid(window.scrollY > 24);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const runtimeReady = readiness.data?.runtime_ready === true;
  const aiReady = readiness.data?.ready === true;

  return (
    <div className="home-page">
      <header className={cx("home-header", headerSolid && "is-solid")}>
        <Brand />
        <nav aria-label="首页导航">
          <a href="#workflow">工作流</a>
          <a href="#capabilities">产品原则</a>
          <a href="#delivery">交付边界</a>
        </nav>
        <AppLink className="button primary compact" to="/workspace">
          进入工作台
          <Icon name="arrow" />
        </AppLink>
      </header>

      <main>
        <section className="home-hero">
          <div className="hero-copy">
            <p className="eyebrow">ReqPilotX · 需求工程工作台</p>
            <h1>
              从客户 SOR，
              <span>到有来源、有判断、能交付的工程结论。</span>
            </h1>
            <p className="hero-lede">
              默认自动完成，异常只局部打断。工程师始终看得到来源，草稿在工作面切换时不会消失，AI
              只提供证据与建议。
            </p>
            <div className="hero-actions">
              <AppLink className="button primary large" to="/workspace">
                开始处理 SOR
                <Icon name="arrow" />
              </AppLink>
              <a className="button ghost large" href="#workflow">
                查看工作流
              </a>
            </div>
            <div className="hero-runtime" aria-live="polite">
              <span className={cx("runtime-dot", runtimeReady && "is-ready")} />
              {readiness.loading && "正在检查本地服务"}
              {readiness.error && "服务状态暂不可用"}
              {runtimeReady && (
                <>
                  基础服务就绪
                  <StatusBadge tone={aiReady ? "success" : "warning"}>
                    {aiReady ? "AI 就绪" : "AI 可稍后配置"}
                  </StatusBadge>
                </>
              )}
              {readiness.data && !runtimeReady && "基础服务需要处理"}
            </div>
          </div>

          <div className="hero-instrument" aria-label="ReqPilotX 分析链路示意">
            <div className="instrument-top">
              <span>当前需求</span>
              <strong>来源与分析</strong>
              <i />
            </div>
            <div className="instrument-source">
              <span className="instrument-tag">SOR · PDF</span>
              <strong>5.3.9 盐雾试验</strong>
              <p>客户原文 · 第 47 页</p>
              <div className="source-lines">
                <i />
                <i />
                <i />
              </div>
            </div>
            <div className="instrument-flow" aria-hidden="true">
              <span />
              <span />
              <span />
            </div>
            <div className="instrument-decision">
              <div>
                <small>需求提取</small>
                <strong>边界已核对</strong>
                <em>已完成</em>
              </div>
              <div>
                <small>证据</small>
                <strong>ISO 16750-5</strong>
                <em>已关联</em>
              </div>
              <div className="is-accent">
                <small>人工结论</small>
                <strong>需澄清</strong>
                <em>等待工程师确认</em>
              </div>
            </div>
            <div className="instrument-footer">
              <span>来源</span>
              <span>证据</span>
              <span>人工结论</span>
            </div>
          </div>
        </section>

        <section id="workflow" className="home-section workflow-section">
          <header className="section-heading">
            <p className="eyebrow">一条工作路径</p>
            <h2>四个用户任务，一根证据主线。</h2>
            <p>
              可选步骤只在确有价值时出现；基础链路不会因为 Qdrant 或远端 AI
              暂不可用而停止本地整理与审阅。
            </p>
          </header>
          <ol className="workflow-grid">
            {workflow.map((step) => (
              <li key={step.index}>
                <span>{step.index}</span>
                <h3>{step.title}</h3>
                <p>{step.body}</p>
              </li>
            ))}
          </ol>
        </section>

        <section id="capabilities" className="home-section capability-section">
          <header className="section-heading is-inverse">
            <p className="eyebrow">产品原则</p>
            <h2>复杂性留在系统里，不转嫁给工程师。</h2>
          </header>
          <div className="capability-grid">
            {capabilities.map((capability) => (
              <article key={capability.code}>
                <div className="capability-code">
                  <Icon name={capability.icon} />
                  {capability.code}
                </div>
                <h3>{capability.title}</h3>
                <p>{capability.body}</p>
              </article>
            ))}
          </div>
        </section>

        <section id="delivery" className="home-section delivery-section">
          <div>
            <p className="eyebrow">Local now · cloud when ready</p>
            <h2>本地使用与阿里云上线，共用一套产品入口。</h2>
          </div>
          <div className="delivery-comparison">
            <article>
              <span>当前</span>
              <h3>本地运行</h3>
              <p>PostgreSQL 与本地对象存储承接核心数据；Qdrant 可按平台独立启动。</p>
            </article>
            <Icon name="arrow" size={28} />
            <article>
              <span>下一步</span>
              <h3>阿里云部署</h3>
              <p>替换为 RDS、OSS 与云端进程托管，不需要再改页面或业务领域合同。</p>
            </article>
          </div>
          <AppLink className="button primary large" to="/workspace">
            进入正式工作台
            <Icon name="arrow" />
          </AppLink>
        </section>
      </main>

      <footer className="home-footer">
        <Brand compact />
        <p>项目管理 → 需求提取 → 需求分析 → 矩阵导出</p>
        <span>ReqPilotX</span>
      </footer>
    </div>
  );
}
