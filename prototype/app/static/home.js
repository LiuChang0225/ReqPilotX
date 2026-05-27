const header = document.querySelector("[data-header]");
const statusEl = document.querySelector("#service-status");
const revealItems = Array.from(document.querySelectorAll(".reveal-in"));
const navLinks = Array.from(document.querySelectorAll('.site-nav a[href^="#"], .guide-nav a[href^="#"]'));
const sections = navLinks
  .map((link) => document.querySelector(link.getAttribute("href")))
  .filter(Boolean);

document.addEventListener("DOMContentLoaded", () => {
  initHeader();
  initReveal();
  initActiveNav();
  refreshHealth().catch(() => setServiceStatus("状态未知", "is-down"));
});

function initHeader() {
  const update = () => {
    if (!header) return;
    header.classList.toggle("is-scrolled", window.scrollY > 24);
  };
  update();
  window.addEventListener("scroll", update, { passive: true });
}

function initReveal() {
  if (!revealItems.length) return;
  if (!("IntersectionObserver" in window)) {
    revealItems.forEach((item) => item.classList.add("is-visible"));
    return;
  }
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    },
    { rootMargin: "0px 0px -10% 0px", threshold: 0.12 }
  );
  revealItems.forEach((item, index) => {
    item.style.transitionDelay = `${Math.min(index * 35, 220)}ms`;
    observer.observe(item);
  });
}

function initActiveNav() {
  if (!sections.length || !("IntersectionObserver" in window)) return;
  const observer = new IntersectionObserver(
    (entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (!visible) return;
      const target = `#${visible.target.id}`;
      navLinks.forEach((link) => link.classList.toggle("is-active", link.getAttribute("href") === target));
    },
    { rootMargin: "-30% 0px -55% 0px", threshold: [0.1, 0.35, 0.6] }
  );
  sections.forEach((section) => observer.observe(section));
}

async function refreshHealth() {
  const response = await fetch("/api/health", { credentials: "same-origin", cache: "no-store" });
  if (!response.ok) {
    setServiceStatus("离线", "is-down");
    return;
  }
  const payload = await response.json();
  const ok = payload?.status === "ok" || payload?.ready === true;
  setServiceStatus(ok ? "在线" : "需检查", ok ? "is-ok" : "is-down");
}

function setServiceStatus(text, className) {
  if (!statusEl) return;
  statusEl.textContent = text;
  statusEl.classList.remove("is-ok", "is-down");
  statusEl.classList.add(className);
}