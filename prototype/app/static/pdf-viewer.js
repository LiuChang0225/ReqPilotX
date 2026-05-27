const query = new URLSearchParams(window.location.search);
const pdfUrl = query.get("pdf") || "";
const initialPage = Math.max(1, Number(query.get("page") || 1));

const el = {
  canvas: document.querySelector("#pdf-canvas"),
  canvasWrap: document.querySelector("#canvas-wrap"),
  status: document.querySelector("#status-text"),
  page: document.querySelector("#page-text"),
  prevPage: document.querySelector("#prev-page"),
  nextPage: document.querySelector("#next-page"),
  zoomOut: document.querySelector("#zoom-out"),
  zoomIn: document.querySelector("#zoom-in"),
  errorBox: document.querySelector("#error-box"),
  errorText: document.querySelector("#error-text"),
  openLink: document.querySelector("#open-link"),
};

const state = {
  doc: null,
  pageNo: initialPage,
  scale: 1.12,
  rendering: false,
  pendingJump: null,
  highlightText: "",
};

boot().catch((error) => {
  showError(error?.message || "PDF 加载失败");
});

async function boot() {
  bindEvents();
  syncPagerButtons();
  if (!pdfUrl) {
    showError("缺少 PDF 地址参数");
    return;
  }

  if (typeof pdfjsLib === "undefined") {
    showError("PDF.js 初始化失败");
    return;
  }

  pdfjsLib.GlobalWorkerOptions.workerSrc = "/assets/vendor/pdfjs/pdf.worker.min.js";
  el.openLink.href = pdfUrl;

  setStatus("加载PDF中...");
  const task = pdfjsLib.getDocument({ url: pdfUrl });
  state.doc = await task.promise;
  state.pageNo = clampPage(state.pageNo);
  await renderPage(state.pageNo);
  postReady();
}

function bindEvents() {
  el.prevPage?.addEventListener("click", () => {
    void jumpToPage(state.pageNo - 1, state.highlightText);
  });

  el.nextPage?.addEventListener("click", () => {
    void jumpToPage(state.pageNo + 1, state.highlightText);
  });

  el.zoomOut?.addEventListener("click", () => {
    state.scale = Math.max(0.6, Number((state.scale - 0.12).toFixed(2)));
    void renderPage(state.pageNo);
  });

  el.zoomIn?.addEventListener("click", () => {
    state.scale = Math.min(2.2, Number((state.scale + 0.12).toFixed(2)));
    void renderPage(state.pageNo);
  });

  window.addEventListener("message", (event) => {
    const data = event.data || {};
    if (data.type !== "jump-page") {
      return;
    }
    const target = Math.max(1, Number(data.page || 1));
    const highlightText = normalizeHighlightInput(data.highlightText || "");
    void jumpToPage(target, highlightText);
  });

  window.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft" || event.key === "PageUp") {
      event.preventDefault();
      void jumpToPage(state.pageNo - 1, state.highlightText);
      return;
    }
    if (event.key === "ArrowRight" || event.key === "PageDown") {
      event.preventDefault();
      void jumpToPage(state.pageNo + 1, state.highlightText);
    }
  });
}

async function jumpToPage(pageNo, highlightText = "") {
  const target = clampPage(pageNo);
  const normalizedHighlight = normalizeHighlightInput(highlightText);
  const highlightChanged = normalizedHighlight !== state.highlightText;
  state.highlightText = normalizedHighlight;

  if (target === state.pageNo && !state.rendering && !highlightChanged) {
    return;
  }
  await renderPage(target);
}

async function renderPage(pageNo) {
  if (!state.doc || !el.canvas) {
    return;
  }

  if (state.rendering) {
    state.pendingJump = { pageNo, highlightText: state.highlightText };
    return;
  }

  state.rendering = true;
  state.pageNo = clampPage(pageNo);
  setStatus(`渲染第 ${state.pageNo} 页...`);

  try {
    const page = await state.doc.getPage(state.pageNo);
    const viewport = page.getViewport({ scale: state.scale });
    const canvas = el.canvas;
    const context = canvas.getContext("2d", { alpha: false });
    if (!context) {
      throw new Error("Canvas 上下文不可用");
    }

    canvas.width = Math.ceil(viewport.width);
    canvas.height = Math.ceil(viewport.height);
    canvas.style.width = `${Math.ceil(viewport.width)}px`;
    canvas.style.height = `${Math.ceil(viewport.height)}px`;

    await page.render({ canvasContext: context, viewport }).promise;
    const highlightRects = await paintHighlights(page, viewport, context, state.highlightText);
    const highlightCount = highlightRects.length;
    if (highlightCount > 0) {
      pinHighlightToTop(highlightRects);
      flashHighlightOverlays(highlightRects);
    } else {
      resetCanvasViewport();
    }
    setStatus(highlightCount > 0 ? `已加载 · 高亮 ${highlightCount} 处` : "已加载");
    el.page.textContent = `${state.pageNo} / ${state.doc.numPages}`;
    syncPagerButtons();
    notifyParentPageChanged();
    clearError();
  } catch (error) {
    showError(error?.message || "页面渲染失败");
  } finally {
    state.rendering = false;
    if (state.pendingJump) {
      const pending = state.pendingJump;
      state.pendingJump = null;
      state.highlightText = normalizeHighlightInput(pending.highlightText || "");
      void renderPage(pending.pageNo);
    }
  }
}

async function paintHighlights(page, viewport, context, highlightText) {
  const phrases = buildHighlightPhrases(highlightText);
  if (!phrases.length) {
    return [];
  }

  const textContent = await page.getTextContent({
    normalizeWhitespace: true,
    disableCombineTextItems: false,
  });
  const entries = [];
  let joined = "";

  (textContent.items || []).forEach((item) => {
    const raw = String(item?.str || "");
    const normalized = normalizeSearchText(raw);
    if (!normalized) {
      return;
    }
    const start = joined.length;
    joined += normalized;
    entries.push({
      item,
      normalized,
      start,
      end: joined.length,
    });
  });

  if (!entries.length || !joined) {
    return [];
  }

  const matched = findMatchedTextEntries(entries, joined, phrases, highlightText);
  if (!matched.size) {
    return [];
  }

  const rects = [];
  matched.forEach((index) => {
    const entry = entries[index];
    if (!entry) {
      return;
    }
    const rect = textItemToRect(entry.item, viewport);
    if (rect) {
      rects.push(rect);
    }
  });
  drawHighlightRects(context, rects);
  return rects;
}

function findMatchedTextEntries(entries, joinedText, phrases, highlightText) {
  const matched = new Set();
  const markRange = (start, end) => {
    for (let i = 0; i < entries.length; i += 1) {
      const entry = entries[i];
      if (entry.end > start && entry.start < end) {
        matched.add(i);
      }
    }
  };

  phrases.forEach((phrase) => {
    if (!phrase) {
      return;
    }
    let cursor = 0;
    while (cursor < joinedText.length) {
      const hit = joinedText.indexOf(phrase, cursor);
      if (hit < 0) {
        break;
      }
      markRange(hit, hit + phrase.length);
      cursor = hit + Math.max(1, phrase.length);
    }
  });

  if (matched.size > 0) {
    return matched;
  }

  const fallbackTokens = buildFallbackTokens(highlightText);
  if (!fallbackTokens.length) {
    return matched;
  }

  entries.forEach((entry, index) => {
    const hitTokens = fallbackTokens.filter((token) => entry.normalized.includes(token));
    const hasStrongToken = hitTokens.some((token) => isStrongToken(token));
    if (hasStrongToken || hitTokens.length >= 2) {
      matched.add(index);
    }
  });
  return matched;
}

function drawHighlightRects(context, rects) {
  if (!rects.length) {
    return;
  }
  context.save();
  context.fillStyle = "rgba(255, 216, 77, 0.36)";
  context.strokeStyle = "rgba(226, 163, 22, 0.52)";
  context.lineWidth = 0.9;
  rects.forEach((rect) => {
    context.fillRect(rect.x, rect.y, rect.width, rect.height);
    context.strokeRect(rect.x, rect.y, rect.width, rect.height);
  });
  context.restore();
}

function pinHighlightToTop(rects) {
  if (!el.canvasWrap || !rects.length) {
    return;
  }
  const first = rects.reduce((min, rect) => (rect.y < min.y ? rect : min), rects[0]);
  const topPadding = 18;
  const leftPadding = 24;
  const nextTop = Math.max(Math.floor(first.y - topPadding), 0);
  const nextLeft = Math.max(Math.floor(first.x - leftPadding), 0);
  el.canvasWrap.scrollTo({
    top: nextTop,
    left: nextLeft,
    behavior: "smooth"
  });
}

function resetCanvasViewport() {
  const container = document.querySelector("#canvas-container");
  if (container) {
    container.querySelectorAll(".pdf-flash-overlay").forEach(el => el.remove());
  }
  if (!el.canvasWrap) {
    return;
  }
  el.canvasWrap.scrollTop = 0;
  el.canvasWrap.scrollLeft = 0;
}

function flashHighlightOverlays(rects) {
  const container = document.querySelector("#canvas-container");
  if (!container || !rects.length) {
    return;
  }
  container.querySelectorAll(".pdf-flash-overlay").forEach(el => el.remove());

  rects.forEach((rect) => {
    const overlay = document.createElement("div");
    overlay.className = "pdf-flash-overlay";
    overlay.style.position = "absolute";
    overlay.style.left = `${rect.x}px`;
    overlay.style.top = `${rect.y}px`;
    overlay.style.width = `${rect.width}px`;
    overlay.style.height = `${rect.height}px`;
    container.appendChild(overlay);
  });

  setTimeout(() => {
    container.querySelectorAll(".pdf-flash-overlay").forEach(el => el.remove());
  }, 1500);
}

function textItemToRect(item, viewport) {
  try {
    const transformed = pdfjsLib.Util.transform(viewport.transform, item.transform);
    const x = Number(transformed[4] || 0);
    const baselineY = Number(transformed[5] || 0);
    const fontHeight = Math.max(
      Math.hypot(Number(transformed[2] || 0), Number(transformed[3] || 0)),
      Number(item.height || 0) * viewport.scale,
      8
    );
    const width = Math.max(Number(item.width || 0) * viewport.scale, fontHeight * 0.3);
    const y = baselineY - fontHeight * 0.88;
    return {
      x: Math.max(0, x - 1),
      y: Math.max(0, y - 1),
      width: width + 2,
      height: fontHeight * 1.2,
    };
  } catch (_error) {
    return null;
  }
}

function buildHighlightPhrases(highlightText) {
  const source = String(highlightText || "").trim();
  if (!source) {
    return [];
  }

  const phrases = [];
  const pushPhrase = (value) => {
    const normalized = normalizeSearchText(value);
    if (!isUsefulPhrase(normalized)) {
      return;
    }
    phrases.push(normalized);
  };

  source
    .split(/\r?\n/)
    .map((line) => line.replaceAll("|", " ").replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .slice(0, 8)
    .forEach(pushPhrase);

  source
    .split(/[，。,.;；:：!?！？、\s]+/)
    .map((token) => token.trim())
    .filter(Boolean)
    .forEach((token) => {
      const normalized = normalizeSearchText(token);
      if (!isUsefulToken(normalized)) {
        return;
      }
      phrases.push(normalized);
    });

  return dedupeByPriority(phrases, 18);
}

function buildFallbackTokens(text) {
  return dedupeByPriority(
    String(text || "")
      .split(/[，。,.;；:：!?！？、\s]+/)
      .map((token) => normalizeSearchText(token))
      .filter((token) => isUsefulToken(token)),
    24
  );
}

function dedupeByPriority(values, limit) {
  const set = new Set();
  values
    .sort((a, b) => b.length - a.length)
    .forEach((value) => {
      if (!value || set.has(value)) {
        return;
      }
      set.add(value);
    });
  return [...set].slice(0, limit);
}

function isUsefulPhrase(value) {
  if (!value) {
    return false;
  }
  const han = (value.match(/[\u4e00-\u9fff]/g) || []).length;
  if (han >= 4) {
    return true;
  }
  const compact = value.replace(/[^a-z0-9]/gi, "");
  return compact.length >= 6;
}

function isUsefulToken(value) {
  if (!value) {
    return false;
  }
  const han = (value.match(/[\u4e00-\u9fff]/g) || []).length;
  if (han >= 2) {
    return true;
  }
  const compact = value.replace(/[^a-z0-9]/gi, "");
  return compact.length >= 4;
}

function isStrongToken(value) {
  if (!value) {
    return false;
  }
  const han = (value.match(/[\u4e00-\u9fff]/g) || []).length;
  if (han >= 3) {
    return true;
  }
  const compact = value.replace(/[^a-z0-9]/gi, "");
  return compact.length >= 7;
}

function normalizeSearchText(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/\s+/g, "")
    .replace(/[|｜]/g, "")
    .replace(/[，。,.;；:：!?！？、'"“”‘’`~^$%&*_=+\\/()[\]{}<>《》【】\-]/g, "");
}

function normalizeHighlightInput(text) {
  return String(text || "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 2800);
}

function syncPagerButtons() {
  const hasDoc = Boolean(state.doc);
  if (el.prevPage) {
    el.prevPage.disabled = !hasDoc || state.pageNo <= 1;
  }
  if (el.nextPage) {
    el.nextPage.disabled = !hasDoc || state.pageNo >= (state.doc?.numPages || 1);
  }
}

function clampPage(pageNo) {
  if (!state.doc) {
    return Math.max(1, Number(pageNo || 1));
  }
  return Math.min(Math.max(1, Number(pageNo || 1)), state.doc.numPages);
}

function setStatus(text) {
  if (el.status) {
    el.status.textContent = text;
  }
}

function clearError() {
  if (el.errorBox) {
    el.errorBox.classList.add("hidden");
  }
}

function showError(message) {
  if (el.errorText) {
    el.errorText.textContent = message;
  }
  if (el.errorBox) {
    el.errorBox.classList.remove("hidden");
  }
  setStatus("加载失败");
}

function postReady() {
  if (!window.parent || window.parent === window) {
    return;
  }
  window.parent.postMessage({ type: "pdf-viewer-ready" }, "*");
}

function notifyParentPageChanged() {
  if (!window.parent || window.parent === window) {
    return;
  }
  window.parent.postMessage({ type: "pdf-page-changed", page: state.pageNo }, "*");
}
