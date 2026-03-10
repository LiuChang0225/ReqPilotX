// Make sure to register ScrollTrigger with GSAP
gsap.registerPlugin(ScrollTrigger);

const el = {
  metricHealth: document.querySelector("#metric-health"),
  metricHealthFoot: document.querySelector("#metric-health-foot"),
  metricSplit: document.querySelector("#metric-split"),
  metricSplitFoot: document.querySelector("#metric-split-foot"),
  metricGate: document.querySelector("#metric-gate"),
  metricGateFoot: document.querySelector("#metric-gate-foot"),
  metricRounds: document.querySelector("#metric-rounds"),
  metricRoundsFoot: document.querySelector("#metric-rounds-foot"),
  latestSummary: document.querySelector("#latest-summary"),
};

document.addEventListener('DOMContentLoaded', () => {
    initAnimations();
    initMetrics().catch((error) => {
        console.error(error);
        if (el.latestSummary) {
        el.latestSummary.textContent = "指标加载失败，请检查后端服务状态。";
        }
    });
});

// --- GSAP Animations (Apple Style) --- //
function initAnimations() {
    // 1. Hero Section Entrance Sequence
    const heroTl = gsap.timeline({ defaults: { ease: "power3.out" } });

    heroTl.to(".hero-kicker", { opacity: 1, y: 0, duration: 1, delay: 0.2 })
          .to(".hero-title", { opacity: 1, y: 0, duration: 1.2 }, "-=0.8")
          .to(".hero-subtitle", { opacity: 1, y: 0, duration: 1 }, "-=0.9")
          .to(".hero-actions", { opacity: 1, y: 0, duration: 0.8 }, "-=0.8")
          .to(".hero-image-wrapper", { opacity: 1, y: 0, scale: 1, duration: 1.5, ease: "power4.out" }, "-=0.6");

    // 2. Parallax effect for the Hero Image Mockup
    gsap.to(".hero-image-wrapper", {
        y: 100, // Move down slightly as we scroll down
        ease: "none",
        scrollTrigger: {
            trigger: ".hero-section",
            start: "top top",
            end: "bottom top",
            scrub: true
        }
    });

    // 3. Bento Box Grid Reveal
    ScrollTrigger.batch(".bento-card", {
        interval: 0.1, // time window (in seconds) for batching assigns to the same timeline
        batchMax: 4,   // maximum batch size (targets)
        onEnter: batch => gsap.to(batch, {opacity: 1, y: 0, stagger: {each: 0.15, grid: [1, 4]}, overwrite: true, duration: 1, ease: "power3.out"}),
        onLeave: batch => gsap.set(batch, {opacity: 1, y: 0, overwrite: true}), // Set to 1 because we don't want it to vanish
        onEnterBack: batch => gsap.to(batch, {opacity: 1, y: 0, stagger: 0.15, overwrite: true}),
        onLeaveBack: batch => gsap.set(batch, {opacity: 0, y: 40, overwrite: true}),
        start: "top 85%",
    });
    // Initial state for bento cards (hidden)
    gsap.set(".bento-card", {opacity: 0, y: 40});
    gsap.set(".sec-title", {opacity: 0, y: 30});

    // Reveal section headers
    gsap.utils.toArray('.sec-title').forEach(title => {
        gsap.to(title, {
            scrollTrigger: {
                trigger: title,
                start: "top 85%",
            },
            opacity: 1,
            y: 0,
            duration: 1,
            ease: "power3.out"
        });
    });

    // 4. Z-Pattern Highlights Reveal
    gsap.utils.toArray('.z-row').forEach(row => {
        const text = row.querySelector('.z-text');
        const img = row.querySelector('.z-img');
        
        // Initial State
        gsap.set(text, { opacity: 0, x: row.querySelector('.order-1') === text ? -50 : 50 });
        gsap.set(img, { opacity: 0, scale: 0.95 });

        // Animation Timeline triggered on scroll
        const tl = gsap.timeline({
            scrollTrigger: {
                trigger: row,
                start: "top 75%", // Trigger when top of row hits 75% down the viewport
            }
        });

        tl.to(img, { opacity: 1, scale: 1, duration: 1.2, ease: "power4.out" })
          .to(text, { opacity: 1, x: 0, duration: 1, ease: "power3.out" }, "-=0.8");
    });

    // 5. Tools Cards Stagger Reveal
    gsap.set(".tools-grid > a", {opacity: 0, y: 40});
    ScrollTrigger.batch(".tools-grid > a", {
        onEnter: batch => gsap.to(batch, {opacity: 1, y: 0, stagger: 0.15, duration: 1, ease: "power3.out"}),
        start: "top 85%",
    });
}

// --- Metrics Fetching (Original Logic Preservation) --- //
async function initMetrics() {
  await refreshOverview();
  setInterval(() => {
    refreshOverview().catch((error) => console.error(error));
  }, 30000);
}

async function refreshOverview() {
  const [health, projects, progress] = await Promise.all([
    api("/api/health").catch(() => ({ status: "down" })),
    api("/api/projects").catch(() => []),
    api("/api/progress/snapshot?limit_rounds=120&limit_events=120").catch(() => null),
  ]);

  renderHealth(health);
  renderProgress(progress, projects);
}

function renderHealth(health) {
  const ok = health?.status === "ok";
  if(el.metricHealth) {
      el.metricHealth.textContent = ok ? "ONLINE" : "OFFLINE";
      // Removed direct style manipulation to let Tailwind handle normal coloring, but we can override the text color if needed.
      el.metricHealth.className = `text-4xl font-bold tracking-tight mb-2 relative z-10 ${ok ? 'text-green-600' : 'text-red-500'}`;
  }
  if(el.metricHealthFoot) {
      el.metricHealthFoot.textContent = ok ? "FastAPI backend is healthy" : "Backend is unavailable";
  }
}

function renderProgress(progress, projects) {
  if (!progress) {
    if(el.metricSplit) el.metricSplit.textContent = "-";
    if(el.metricGate) el.metricGate.textContent = "-";
    if(el.metricRounds) el.metricRounds.textContent = "-";
    if(el.metricSplitFoot) el.metricSplitFoot.textContent = "latest round";
    if(el.metricGateFoot) el.metricGateFoot.textContent = "quality gate";
    if(el.metricRoundsFoot) el.metricRoundsFoot.textContent = "iteration board";
    if(el.latestSummary) el.latestSummary.textContent = "暂无进度快照数据。";
    return;
  }

  const kpi = progress.kpis || {};
  const latestSplit = Number(kpi.latest_split_accuracy_pct || 0);
  const latestGate = Number(kpi.latest_quality_gate_score_pct || 0);
  const totalRounds = Number(kpi.total_rounds || 0);

  if(el.metricSplit) el.metricSplit.textContent = `${latestSplit.toFixed(2)}%`;
  if(el.metricSplitFoot) el.metricSplitFoot.textContent = `latest: ${kpi.latest_round_id || "-"}`;

  if(el.metricGate) el.metricGate.textContent = `${latestGate.toFixed(2)}%`;
  if(el.metricGateFoot) el.metricGateFoot.textContent = `checks quality`;

  if(el.metricRounds) el.metricRounds.textContent = `${totalRounds}`;
  
  const projectCount = Array.isArray(projects) ? projects.length : 0;
  if(el.metricRoundsFoot) el.metricRoundsFoot.textContent = `projects: ${projectCount}`;

  const trend = progress.charts?.split_trend || [];
  const latest = trend.length ? trend[trend.length - 1] : null;
  const latestLine = latest
    ? `当前最新轮次 ${latest.round_id}：split ${Number(latest.split_accuracy_pct || 0).toFixed(2)}%，parse ${Number(
        latest.parse_coverage_pct || 0
      ).toFixed(2)}%，pages ${latest.pages_with_items || "-"}。`
    : "暂无轮次详情。";
    
  if(el.latestSummary) el.latestSummary.textContent = latestLine;
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}
