const state = {
  rows: [],
  dirty: new Map(),
};

const DEFAULT_PHASE0_DIR = "eval/iterations/r27";

const el = {
  phase0Dir: document.querySelector("#phase0-dir"),
  topPages: document.querySelector("#top-pages"),
  annotator: document.querySelector("#annotator"),
  includeDone: document.querySelector("#include-done"),
  reloadBtn: document.querySelector("#reload-btn"),
  saveBtn: document.querySelector("#save-btn"),
  refreshReportBtn: document.querySelector("#refresh-report-btn"),
  statusText: document.querySelector("#status-text"),
  progressText: document.querySelector("#progress-text"),
  tableBody: document.querySelector("#review-table tbody"),
};

init().catch((error) => {
  console.error(error);
  alert(error.message || "初始化失败");
});

async function init() {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("phase0_dir");
  el.phase0Dir.value = (fromUrl || el.phase0Dir.value || DEFAULT_PHASE0_DIR).trim();
  bindEvents();
  await loadQueue();
}

function bindEvents() {
  el.reloadBtn.addEventListener("click", wrap(loadQueue));
  el.saveBtn.addEventListener("click", wrap(saveChanges));
  el.refreshReportBtn.addEventListener("click", wrap(refreshReport));
}

async function loadQueue() {
  const topPages = Math.max(1, Number(el.topPages.value || 30));
  const includeDone = el.includeDone.checked ? "1" : "0";
  const phase0Dir = currentPhase0Dir();
  setStatus("加载中...");
  const qs = new URLSearchParams({
    top_pages: String(topPages),
    include_done: includeDone,
    phase0_dir: phase0Dir,
  });
  const data = await api(`/api/phase0/review?${qs.toString()}`);
  state.dirty.clear();
  state.rows = flattenRows(data.pages || []);
  renderRows();
  renderProgress(data.meta || {});
  setStatus(`已加载 ${state.rows.length} 条标注项 | 目录: ${phase0Dir}`);
}

function flattenRows(pages) {
  const rows = [];
  pages.forEach((page) => {
    (page.items || []).forEach((item) => {
      if (item.source_type !== "predicted") {
        return;
      }
      rows.push({
        sample_id: page.sample_id,
        file_name: page.file_name,
        page_no: page.page_no,
        priority_score: page.priority_score,
        item_id: item.item_id,
        auto_requirement_text: item.auto_requirement_text,
        auto_requirement_type: item.auto_requirement_type,
        auto_priority: item.auto_priority,
        is_true_requirement: item.is_true_requirement || "",
        is_split_correct: item.is_split_correct || "",
        is_type_correct: item.is_type_correct || "",
        error_tags: item.error_tags || "",
        notes: item.notes || "",
        done: item.is_done,
      });
    });
  });
  return rows;
}

function renderProgress(meta) {
  const progress = Number(meta.progress_pct || 0).toFixed(2);
  el.progressText.textContent =
    `样本页: ${meta.selected_pages || 0} | 条目: ${meta.total_selected_items || 0} | ` +
    `已标: ${meta.labeled_selected_items || 0} | 未标: ${meta.remaining_selected_items || 0} | 进度: ${progress}%`;
}

function renderRows() {
  el.tableBody.innerHTML = "";
  if (!state.rows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="10">没有待标注条目，尝试勾选“显示已完成条目”或增大 Top Pages。</td>`;
    el.tableBody.appendChild(tr);
    return;
  }

  state.rows.forEach((row) => {
    const tr = document.createElement("tr");
    if (row.done) {
      tr.classList.add("done-row");
    }
    tr.dataset.itemId = row.item_id;
    tr.innerHTML = `
      <td>
        <strong>${escapeHtml(row.sample_id)}</strong><br/>
        <span class="hint">P:${escapeHtml(String(row.priority_score))}</span>
      </td>
      <td class="source">${escapeHtml(row.file_name)}<br/>page ${escapeHtml(String(row.page_no))}</td>
      <td class="item-text">${escapeHtml(row.auto_requirement_text || "")}</td>
      <td>${escapeHtml(row.auto_requirement_type || "")}<br/><span class="hint">${escapeHtml(
      row.auto_priority || ""
    )}</span></td>
      <td>${boolSelect("is_true_requirement", row.is_true_requirement)}</td>
      <td>${boolSelect("is_split_correct", row.is_split_correct)}</td>
      <td>${boolSelect("is_type_correct", row.is_type_correct)}</td>
      <td><input class="tag-input" data-field="error_tags" value="${escapeAttr(row.error_tags)}" /></td>
      <td><input class="note-input" data-field="notes" value="${escapeAttr(row.notes)}" /></td>
      <td>
        <div class="quick-buttons">
          <button type="button" data-quick="all_true">全对</button>
          <button type="button" data-quick="fp">误报</button>
        </div>
      </td>
    `;
    el.tableBody.appendChild(tr);
  });

  el.tableBody.querySelectorAll("select,input").forEach((node) => {
    node.addEventListener("change", onRowChanged);
    node.addEventListener("input", onRowChanged);
  });
  el.tableBody.querySelectorAll("[data-quick]").forEach((node) => {
    node.addEventListener("click", onQuickSet);
  });
}

function boolSelect(field, value) {
  const normalized = normalizeBool(value);
  const options = [
    ["", ""],
    ["1", "1"],
    ["0", "0"],
  ]
    .map(([val, label]) => `<option value="${val}" ${val === normalized ? "selected" : ""}>${label}</option>`)
    .join("");
  return `<select data-field="${field}">${options}</select>`;
}

function onQuickSet(event) {
  const tr = event.currentTarget.closest("tr");
  if (!tr) {
    return;
  }
  const type = event.currentTarget.getAttribute("data-quick");
  if (type === "all_true") {
    setField(tr, "is_true_requirement", "1");
    setField(tr, "is_split_correct", "1");
    setField(tr, "is_type_correct", "1");
    setField(tr, "error_tags", "");
  } else if (type === "fp") {
    setField(tr, "is_true_requirement", "0");
    setField(tr, "is_split_correct", "0");
    setField(tr, "is_type_correct", "0");
  }
  markDirty(tr);
}

function setField(tr, field, value) {
  const node = tr.querySelector(`[data-field="${field}"]`);
  if (!node) {
    return;
  }
  node.value = value;
}

function onRowChanged(event) {
  const tr = event.target.closest("tr");
  if (!tr) {
    return;
  }
  markDirty(tr);
}

function markDirty(tr) {
  const itemId = tr.dataset.itemId;
  if (!itemId) {
    return;
  }
  const update = {
    item_id: itemId,
    is_true_requirement: tr.querySelector('[data-field="is_true_requirement"]').value,
    is_split_correct: tr.querySelector('[data-field="is_split_correct"]').value,
    is_type_correct: tr.querySelector('[data-field="is_type_correct"]').value,
    error_tags: tr.querySelector('[data-field="error_tags"]').value.trim(),
    notes: tr.querySelector('[data-field="notes"]').value.trim(),
    annotator: el.annotator.value.trim(),
  };
  state.dirty.set(itemId, update);
  tr.classList.add("changed");
}

async function saveChanges() {
  const updates = [...state.dirty.values()];
  if (!updates.length) {
    setStatus("没有变更需要保存");
    return;
  }
  const phase0Dir = currentPhase0Dir();
  setStatus(`保存中 (${updates.length})...`);
  const result = await api(`/api/phase0/items/batch-label?phase0_dir=${encodeURIComponent(phase0Dir)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates }),
  });
  setStatus(`保存完成: changed=${result.changed}, missing=${result.missing}`);
  await loadQueue();
}

async function refreshReport() {
  const phase0Dir = currentPhase0Dir();
  setStatus("正在刷新报告...");
  const result = await api(`/api/phase0/report/refresh?phase0_dir=${encodeURIComponent(phase0Dir)}`, {
    method: "POST",
  });
  const msg = result.stdout ? result.stdout : "报告已刷新";
  setStatus(msg);
}

function setStatus(text) {
  el.statusText.textContent = text;
}

function currentPhase0Dir() {
  const value = (el.phase0Dir.value || DEFAULT_PHASE0_DIR).trim();
  const normalized = value || DEFAULT_PHASE0_DIR;
  window.localStorage.setItem("phase0_dir", normalized);
  return normalized;
}

function normalizeBool(value) {
  const text = String(value || "").trim().toLowerCase();
  if (["1", "true", "yes", "y"].includes(text)) {
    return "1";
  }
  if (["0", "false", "no", "n"].includes(text)) {
    return "0";
  }
  return "";
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`请求失败(${response.status}): ${text}`);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("'", "&#39;");
}

function wrap(fn) {
  return async () => {
    try {
      await fn();
    } catch (error) {
      console.error(error);
      alert(error.message || "操作失败");
    }
  };
}
