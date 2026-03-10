const REFRESH_MS = 30000;

const state = {
  snapshot: null,
  rounds: [],
  inFlight: false,
  timer: null,
};

const el = {
  projectTitle: document.querySelector("#project-title"),
  northStar: document.querySelector("#north-star"),
  refreshBtn: document.querySelector("#refresh-btn"),
  lastRefresh: document.querySelector("#last-refresh"),
  activityAt: document.querySelector("#activity-at"),
  kpiGrid: document.querySelector("#kpi-grid"),
  regressionLatest: document.querySelector("#regression-latest"),
  regressionBody: document.querySelector("#regression-body"),
  splitChart: document.querySelector("#split-chart"),
  splitEmpty: document.querySelector("#split-empty"),
  volumeChart: document.querySelector("#volume-chart"),
  volumeEmpty: document.querySelector("#volume-empty"),
  planTableBody: document.querySelector("#plan-table tbody"),
  addPlanItemBtn: document.querySelector("#add-plan-item"),
  savePlanBtn: document.querySelector("#save-plan"),
  eventForm: document.querySelector("#event-form"),
  eventTitle: document.querySelector("#event-title"),
  eventDetail: document.querySelector("#event-detail"),
  eventCategory: document.querySelector("#event-category"),
  eventStatus: document.querySelector("#event-status"),
  eventTime: document.querySelector("#event-time"),
  actionResult: document.querySelector("#action-result"),
  nextActions: document.querySelector("#next-actions"),
  timelineList: document.querySelector("#timeline-list"),
  roundASelect: document.querySelector("#round-a-select"),
  roundBSelect: document.querySelector("#round-b-select"),
  diffLoadBtn: document.querySelector("#diff-load-btn"),
  diffSummary: document.querySelector("#diff-summary"),
  diffDocBody: document.querySelector("#diff-doc-body"),
  diffSectionBody: document.querySelector("#diff-section-body"),
  diffAddedBody: document.querySelector("#diff-added-body"),
  diffRemovedBody: document.querySelector("#diff-removed-body"),
  focusRoundSelect: document.querySelector("#focus-round-select"),
  focusLoadBtn: document.querySelector("#focus-load-btn"),
  errorClosureBody: document.querySelector("#error-closure-body"),
  highRiskBody: document.querySelector("#high-risk-body"),
};

init().catch((error) => {
  console.error(error);
  alert(error.message || "加载进度看板失败，请检查后端服务。");
});

function bindEvents() {
  el.refreshBtn.addEventListener("click", () => loadSnapshot({ showToast: true }));
  el.eventForm.addEventListener("submit", wrap(onCreateEvent));
  el.addPlanItemBtn.addEventListener("click", () => appendPlanRow());
  el.savePlanBtn.addEventListener("click", wrap(onSavePlan));
  el.diffLoadBtn.addEventListener("click", wrap(onLoadRoundDiff));
  el.focusLoadBtn.addEventListener("click", wrap(onLoadFocusRound));
  window.addEventListener("resize", () => {
    if (state.snapshot) {
      renderCharts(state.snapshot);
    }
  });
}

async function init() {
  bindEvents();
  await loadSnapshot();
  await loadRoundOptions();
  await onLoadRoundDiff();
  await onLoadFocusRound();
  state.timer = setInterval(() => {
    loadSnapshot().catch((error) => console.error(error));
  }, REFRESH_MS);
}

async function loadSnapshot({ showToast = false } = {}) {
  if (state.inFlight) {
    return;
  }
  state.inFlight = true;
  try {
    const snapshot = await api("/api/progress/snapshot?limit_rounds=120&limit_events=240");
    state.snapshot = snapshot;
    renderSnapshot(snapshot);
    if (showToast) {
      flashAction("进度看板已刷新。");
    }
  } finally {
    state.inFlight = false;
  }
}

async function loadRoundOptions() {
  const payload = await api("/api/iterations/rounds?limit=120");
  state.rounds = payload.rounds || [];
  renderRoundSelectors();
}

function renderSnapshot(snapshot) {
  const tracker = snapshot.tracker || {};
  el.projectTitle.textContent = tracker.project_name || "项目进度看板";
  el.northStar.textContent = tracker.north_star || "";
  el.lastRefresh.textContent = `最后刷新: ${formatTime(snapshot.generated_at)}`;
  el.activityAt.textContent = `最近活动: ${formatTime(snapshot.latest_activity_at)}`;
  renderKpis(snapshot.kpis || {});
  renderRegression(snapshot.regression || {});
  renderPlanTable(snapshot.plan || []);
  renderNextActions(snapshot.next_actions || []);
  renderTimeline(snapshot.timeline || []);
  renderCharts(snapshot);
}

function renderRoundSelectors() {
  const rounds = state.rounds || [];
  renderRoundSelect(el.roundASelect, rounds);
  renderRoundSelect(el.roundBSelect, rounds);
  renderRoundSelect(el.focusRoundSelect, rounds);

  if (rounds.length >= 2) {
    if (!el.roundBSelect.value) {
      el.roundBSelect.value = rounds[0].round_id;
    }
    if (!el.roundASelect.value) {
      el.roundASelect.value = rounds[1].round_id;
    }
  } else if (rounds.length === 1) {
    el.roundASelect.value = rounds[0].round_id;
    el.roundBSelect.value = rounds[0].round_id;
  }
  if (rounds.length && !el.focusRoundSelect.value) {
    el.focusRoundSelect.value = rounds[0].round_id;
  }
}

function renderRoundSelect(selectEl, rounds) {
  const current = selectEl.value;
  selectEl.innerHTML = "";
  rounds.forEach((row) => {
    const option = document.createElement("option");
    option.value = row.round_id;
    option.textContent = row.round_id;
    if (row.round_id === current) {
      option.selected = true;
    }
    selectEl.appendChild(option);
  });
}

function renderKpis(kpis) {
  const cards = [
    {
      label: "总迭代轮次",
      value: String(kpis.total_rounds ?? 0),
      foot: `文本轮次: ${kpis.text_only_rounds ?? 0}`,
      tone: "neutral",
    },
    {
      label: "最新轮次",
      value: kpis.latest_round_id || "-",
      foot: `解析覆盖: ${fmtPct(kpis.latest_parse_coverage_pct)}`,
      tone: "neutral",
    },
    {
      label: "最新拆解准确率",
      value: fmtPct(kpis.latest_split_accuracy_pct),
      foot: `提升: ${fmtSignedPct(kpis.split_improvement_pct)}`,
      tone: kpis.latest_split_accuracy_pct >= 90 ? "good" : "warning",
    },
    {
      label: "最佳拆解准确率",
      value: fmtPct(kpis.best_split_accuracy_pct),
      foot: `轮次: ${kpis.best_split_round_id || "-"}`,
      tone: kpis.best_split_accuracy_pct >= 90 ? "good" : "warning",
    },
    {
      label: "最新结构门禁",
      value: fmtPct(kpis.latest_quality_gate_score_pct),
      foot: "quality gate",
      tone: kpis.latest_quality_gate_score_pct >= 90 ? "good" : "warning",
    },
    {
      label: "最新回归Run",
      value: kpis.latest_regression_run_id || "-",
      foot: `通过率: ${fmtPct(kpis.regression_pass_rate_pct)}`,
      tone: "neutral",
    },
    {
      label: "最新回归结论",
      value: kpis.latest_regression_overall_pass ? "PASS" : "RISK",
      foot: `runs: ${kpis.regression_pass_runs ?? 0}/${kpis.regression_total_runs ?? 0}`,
      tone: kpis.latest_regression_overall_pass ? "good" : "risk",
    },
    {
      label: "最新回归结构分",
      value: fmtPct(kpis.latest_regression_quality_gate_score_pct),
      foot: "regression quality",
      tone: kpis.latest_regression_quality_gate_score_pct >= 95 ? "good" : "warning",
    },
    {
      label: "最新回归保真分",
      value: fmtPct(kpis.latest_regression_fidelity_score_pct),
      foot: "regression fidelity",
      tone: kpis.latest_regression_fidelity_score_pct >= 95 ? "good" : "warning",
    },
    {
      label: "计划项完成",
      value: `${kpis.plan_done ?? 0}/${kpis.plan_total ?? 0}`,
      foot: `进行中: ${kpis.plan_in_progress ?? 0}`,
      tone: "neutral",
    },
    {
      label: "计划平均进度",
      value: fmtPct(kpis.plan_overall_progress_pct),
      foot: "plan progress",
      tone: kpis.plan_overall_progress_pct >= 70 ? "good" : "warning",
    },
    {
      label: "综合进展指数",
      value: fmtPct(kpis.overall_progress_index_pct),
      foot: "split/parse/plan weighted",
      tone: kpis.overall_progress_index_pct >= 75 ? "good" : "warning",
    },
  ];
  el.kpiGrid.innerHTML = cards
    .map((card) => {
      const toneClass =
        card.tone === "good" ? "kpi-good" : card.tone === "risk" ? "kpi-risk" : card.tone === "warning" ? "kpi-warning" : "";
      return `
        <article class="kpi-card ${toneClass}">
          <p class="kpi-label">${escapeHtml(card.label)}</p>
          <p class="kpi-value">${escapeHtml(card.value)}</p>
          <p class="kpi-foot">${escapeHtml(card.foot)}</p>
        </article>
      `;
    })
    .join("");
}

function renderRegression(payload) {
  const runs = [...(payload.runs || [])].reverse();
  const latest = payload.latest || runs[0] || null;
  if (!runs.length) {
    el.regressionLatest.textContent = "暂无回归数据。";
    el.regressionBody.innerHTML = `<tr><td colspan="7">暂无可展示的回归运行记录</td></tr>`;
    return;
  }
  const latestPass = latest?.overall_pass ? "PASS" : "RISK";
  el.regressionLatest.textContent = `最新: ${latest?.run_id || "-"} | 时间: ${formatTime(
    latest?.timestamp || ""
  )} | 结构=${fmtPct(latest?.quality_gate_score_pct)} | 保真=${fmtPct(latest?.fidelity_score_pct)} | ${latestPass}`;
  el.regressionBody.innerHTML = runs
    .slice(0, 20)
    .map((row) => {
      const pass = Boolean(row.overall_pass);
      const tagClass = pass ? "tag-DONE" : "tag-RISK";
      const tagText = pass ? "PASS" : "RISK";
      return `<tr>
      <td>${escapeHtml(row.run_id || "")}</td>
      <td>${escapeHtml(formatTime(row.timestamp || ""))}</td>
      <td>${escapeHtml(fmtPct(row.quality_gate_score_pct || 0))}</td>
      <td>${Number(row.quality_high_severity_issues || 0)}</td>
      <td>${escapeHtml(fmtPct(row.fidelity_score_pct || 0))}</td>
      <td>${Number(row.fidelity_target_docs || 0)}</td>
      <td><span class="tag ${tagClass}">${tagText}</span></td>
    </tr>`;
    })
    .join("");
}

function renderPlanTable(items) {
  el.planTableBody.innerHTML = "";
  if (!items.length) {
    appendPlanRow();
    return;
  }
  items.forEach((item) => appendPlanRow(item));
}

function appendPlanRow(item = {}) {
  const row = document.createElement("tr");
  row.dataset.id = item.id || "";
  row.innerHTML = `
    <td><input data-field="title" maxlength="200" value="${escapeAttr(item.title || "")}" placeholder="任务标题" /></td>
    <td><input data-field="owner" maxlength="100" value="${escapeAttr(item.owner || "")}" placeholder="负责人" /></td>
    <td>
      <select data-field="status">
        ${planStatusOptions(item.status || "TODO")}
      </select>
    </td>
    <td><input data-field="progress_pct" type="number" min="0" max="100" value="${escapeAttr(
      String(item.progress_pct ?? 0)
    )}" /></td>
    <td><input data-field="target_date" type="date" value="${escapeAttr(item.target_date || "")}" /></td>
    <td><input data-field="notes" maxlength="1000" value="${escapeAttr(item.notes || "")}" placeholder="备注" /></td>
    <td><button type="button" class="ghost" data-remove-plan>删除</button></td>
  `;
  row.querySelector("[data-remove-plan]").addEventListener("click", () => row.remove());
  el.planTableBody.appendChild(row);
}

function planStatusOptions(current) {
  const statuses = ["TODO", "IN_PROGRESS", "BLOCKED", "DONE"];
  return statuses
    .map((status) => `<option value="${status}" ${status === current ? "selected" : ""}>${status}</option>`)
    .join("");
}

function renderNextActions(items) {
  if (!items.length) {
    el.nextActions.innerHTML = `<p class="meta">暂无待推进任务。</p>`;
    return;
  }
  el.nextActions.innerHTML = items
    .map(
      (item) => `
        <article class="next-action">
          <h3>${escapeHtml(item.title || "-")}</h3>
          <p class="meta">状态: ${escapeHtml(item.status || "TODO")} | 负责人: ${escapeHtml(item.owner || "-")}</p>
          <p class="meta">目标日期: ${escapeHtml(item.target_date || "-")}</p>
          <p class="meta">${escapeHtml(item.notes || "")}</p>
          <div class="progress-bar"><span style="width:${Number(item.progress_pct || 0)}%"></span></div>
        </article>
      `
    )
    .join("");
}

function renderTimeline(items) {
  if (!items.length) {
    el.timelineList.innerHTML = `<p class="meta">暂无时间线数据。</p>`;
    return;
  }
  el.timelineList.innerHTML = items
    .map(
      (item) => `
        <article class="timeline-item">
          <div class="timeline-head">
            <p class="timeline-title">${escapeHtml(item.title || "-")}</p>
            <span class="tag tag-${escapeAttr(item.status || "INFO")}">${escapeHtml(item.status || "INFO")}</span>
          </div>
          <p class="meta">${formatTime(item.time)} | ${escapeHtml(item.category || "manual")} | ${
            item.source === "auto" ? "auto" : "manual"
          }</p>
          <p class="timeline-detail">${escapeHtml(item.detail || "")}</p>
        </article>
      `
    )
    .join("");
}

function renderCharts(snapshot) {
  const splitTrend = snapshot?.charts?.split_trend || [];
  const volumeTrend = snapshot?.charts?.volume_trend || [];
  renderSplitChart(splitTrend);
  renderVolumeChart(volumeTrend);
}

async function onLoadRoundDiff() {
  const roundA = (el.roundASelect.value || "").trim();
  const roundB = (el.roundBSelect.value || "").trim();
  if (!roundA || !roundB) {
    return;
  }
  const payload = await api(
    `/api/iterations/diff?round_a=${encodeURIComponent(roundA)}&round_b=${encodeURIComponent(roundB)}&limit=180`
  );
  renderRoundDiff(payload);
}

function renderRoundDiff(payload) {
  const summary = payload.summary || {};
  el.diffSummary.textContent = `A=${payload.round_a} | B=${payload.round_b} | A条目 ${summary.items_a || 0}，B条目 ${
    summary.items_b || 0
  }，新增 ${summary.added_count || 0}，移除 ${summary.removed_count || 0}。`;
  renderDiffChangeTable(el.diffDocBody, payload.by_doc || [], "文档维度无变化");
  renderDiffChangeTable(el.diffSectionBody, payload.by_section || [], "章节维度无变化");
  renderDiffSampleTable(el.diffAddedBody, payload.added || [], "无新增条目样本");
  renderDiffSampleTable(el.diffRemovedBody, payload.removed || [], "无移除条目样本");
}

function renderDiffChangeTable(target, rows, emptyText) {
  if (!rows.length) {
    target.innerHTML = `<tr><td colspan="4">${emptyText}</td></tr>`;
    return;
  }
  target.innerHTML = rows
    .slice(0, 120)
    .map(
      (row) => `<tr>
      <td>${escapeHtml(row.key || "")}</td>
      <td>${Number(row.added || 0)}</td>
      <td>${Number(row.removed || 0)}</td>
      <td>${Number(row.delta || 0)}</td>
    </tr>`
    )
    .join("");
}

function renderDiffSampleTable(target, rows, emptyText) {
  if (!rows.length) {
    target.innerHTML = `<tr><td colspan="5">${emptyText}</td></tr>`;
    return;
  }
  target.innerHTML = rows
    .slice(0, 80)
    .map(
      (row) => `<tr>
      <td>${escapeHtml(row.file_name || "")}</td>
      <td>${escapeHtml(row.section_code || "")}</td>
      <td>${escapeHtml(`${row.source_page_start || ""}-${row.source_page_end || ""}`)}</td>
      <td>${escapeHtml(row.item_id || "")}</td>
      <td>${escapeHtml(row.text_preview || "")}</td>
    </tr>`
    )
    .join("");
}

async function onLoadFocusRound() {
  const roundId = (el.focusRoundSelect.value || "").trim();
  if (!roundId) {
    return;
  }
  const [closure, queue] = await Promise.all([
    api(`/api/iterations/error-closure?round_id=${encodeURIComponent(roundId)}&top_n=24`),
    api(`/api/iterations/high-risk-queue?round_id=${encodeURIComponent(roundId)}&limit=120`),
  ]);
  renderErrorClosure(closure);
  renderHighRiskQueue(queue);
}

function renderErrorClosure(payload) {
  const actions = payload.actions || [];
  if (!actions.length) {
    el.errorClosureBody.innerHTML = `<tr><td colspan="3">无可用错误标签聚合数据</td></tr>`;
    return;
  }
  el.errorClosureBody.innerHTML = actions
    .map(
      (row) => `<tr>
      <td>${escapeHtml(row.tag || "")}</td>
      <td>${Number(row.count || 0)}</td>
      <td>${escapeHtml(row.suggested_action || "")}</td>
    </tr>`
    )
    .join("");
}

function renderHighRiskQueue(payload) {
  const rows = payload.rows || [];
  if (!rows.length) {
    el.highRiskBody.innerHTML = `<tr><td colspan="7">未发现高风险样本队列</td></tr>`;
    return;
  }
  el.highRiskBody.innerHTML = rows
    .slice(0, 120)
    .map(
      (row) => `<tr>
      <td>${Number(row.rank || 0)}</td>
      <td>${escapeHtml(row.file_name || "")}</td>
      <td>${escapeHtml(row.section_code || "")}</td>
      <td>${escapeHtml(`${row.source_page_start || ""}-${row.source_page_end || ""}`)}</td>
      <td>${Number(row.risk_score || 0).toFixed(2)}</td>
      <td>${escapeHtml(row.risk_reasons || "")}</td>
      <td>${escapeHtml(row.text_preview || "")}</td>
    </tr>`
    )
    .join("");
}

function renderSplitChart(points) {
  const data = points.slice(-24);
  if (!data.length) {
    el.splitChart.innerHTML = "";
    el.splitEmpty.textContent = "暂无迭代趋势数据。";
    return;
  }
  el.splitEmpty.textContent = "";
  const width = Math.max(el.splitChart.clientWidth || 760, 760);
  const height = 280;
  const pad = { top: 16, right: 20, bottom: 40, left: 42 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const count = data.length;
  const step = count > 1 ? plotW / (count - 1) : plotW;
  const xAt = (idx) => pad.left + idx * step;
  const yAt = (value) => pad.top + (1 - clamp(value, 0, 100) / 100) * plotH;
  const splitPoints = data.map((item, idx) => `${xAt(idx)},${yAt(Number(item.split_accuracy_pct || 0))}`).join(" ");
  const parsePoints = data.map((item, idx) => `${xAt(idx)},${yAt(Number(item.parse_coverage_pct || 0))}`).join(" ");
  const yTicks = [0, 25, 50, 75, 100];
  const grid = yTicks
    .map((tick) => {
      const y = yAt(tick);
      return `<g>
        <line x1="${pad.left}" y1="${y}" x2="${width - pad.right}" y2="${y}" stroke="#dbe6f0" stroke-width="1" />
        <text x="${pad.left - 8}" y="${y + 4}" text-anchor="end" font-size="10" fill="#607286">${tick}%</text>
      </g>`;
    })
    .join("");
  const labelStep = Math.max(1, Math.ceil(count / 7));
  const xLabels = data
    .map((item, idx) => {
      if (idx % labelStep !== 0 && idx !== count - 1) {
        return "";
      }
      const x = xAt(idx);
      return `<text x="${x}" y="${height - 12}" text-anchor="middle" font-size="10" fill="#607286">${escapeHtml(
        item.round_id || ""
      )}</text>`;
    })
    .join("");
  const latest = data[data.length - 1];
  el.splitChart.setAttribute("viewBox", `0 0 ${width} ${height}`);
  el.splitChart.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" fill="#fbfdff"></rect>
    ${grid}
    <polyline fill="none" stroke="#f05b2a" stroke-width="2.6" points="${splitPoints}"></polyline>
    <polyline fill="none" stroke="#2b8af0" stroke-width="2.4" stroke-dasharray="6 4" points="${parsePoints}"></polyline>
    <text x="${pad.left + 4}" y="${pad.top + 2}" font-size="11" fill="#f05b2a">split accuracy</text>
    <text x="${pad.left + 124}" y="${pad.top + 2}" font-size="11" fill="#2b8af0">parse coverage</text>
    <text x="${width - pad.right}" y="${pad.top + 2}" text-anchor="end" font-size="11" fill="#4a6179">
      latest: ${escapeHtml(latest.round_id || "")}
    </text>
    ${xLabels}
  `;
}

function renderVolumeChart(points) {
  const data = points.slice(-20);
  if (!data.length) {
    el.volumeChart.innerHTML = "";
    el.volumeEmpty.textContent = "暂无条目量趋势数据。";
    return;
  }
  el.volumeEmpty.textContent = "";
  const width = Math.max(el.volumeChart.clientWidth || 760, 760);
  const height = 280;
  const pad = { top: 16, right: 18, bottom: 40, left: 44 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const maxValue = Math.max(
    10,
    ...data.map((item) => Math.max(Number(item.auto_predicted_items || 0), Number(item.labeled_predicted_items || 0)))
  );
  const band = plotW / data.length;
  const barW = Math.min(16, band * 0.35);
  const toY = (value) => pad.top + (1 - value / maxValue) * plotH;
  const yTicks = 5;
  const grid = new Array(yTicks + 1).fill(0).map((_, idx) => {
    const value = Math.round((maxValue / yTicks) * idx);
    const y = toY(value);
    return `<g>
      <line x1="${pad.left}" y1="${y}" x2="${width - pad.right}" y2="${y}" stroke="#dbe6f0" stroke-width="1" />
      <text x="${pad.left - 8}" y="${y + 4}" text-anchor="end" font-size="10" fill="#607286">${value}</text>
    </g>`;
  });
  const bars = data
    .map((item, idx) => {
      const auto = Number(item.auto_predicted_items || 0);
      const labeled = Number(item.labeled_predicted_items || 0);
      const baseX = pad.left + idx * band + (band - barW * 2 - 2) / 2;
      const autoY = toY(auto);
      const labeledY = toY(labeled);
      return `
        <rect x="${baseX}" y="${autoY}" width="${barW}" height="${pad.top + plotH - autoY}" fill="#f05b2a" rx="2"></rect>
        <rect x="${baseX + barW + 2}" y="${labeledY}" width="${barW}" height="${pad.top + plotH - labeledY}" fill="#2b8af0" rx="2"></rect>
      `;
    })
    .join("");
  const labelStep = Math.max(1, Math.ceil(data.length / 6));
  const xLabels = data
    .map((item, idx) => {
      if (idx % labelStep !== 0 && idx !== data.length - 1) {
        return "";
      }
      const x = pad.left + idx * band + band / 2;
      return `<text x="${x}" y="${height - 12}" text-anchor="middle" font-size="10" fill="#607286">${escapeHtml(
        item.round_id || ""
      )}</text>`;
    })
    .join("");
  el.volumeChart.setAttribute("viewBox", `0 0 ${width} ${height}`);
  el.volumeChart.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" fill="#fbfdff"></rect>
    ${grid.join("")}
    ${bars}
    <text x="${pad.left + 4}" y="${pad.top + 2}" font-size="11" fill="#f05b2a">auto predicted</text>
    <text x="${pad.left + 122}" y="${pad.top + 2}" font-size="11" fill="#2b8af0">labeled predicted</text>
    ${xLabels}
  `;
}

async function onCreateEvent(event) {
  event.preventDefault();
  const payload = {
    title: el.eventTitle.value.trim(),
    detail: el.eventDetail.value.trim(),
    category: el.eventCategory.value,
    status: el.eventStatus.value,
    event_time: el.eventTime.value ? el.eventTime.value : null,
  };
  if (!payload.title) {
    alert("事件标题不能为空。");
    return;
  }
  await api("/api/progress/events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  el.eventTitle.value = "";
  el.eventDetail.value = "";
  el.eventTime.value = "";
  flashAction("事件已写入。");
  await loadSnapshot();
}

async function onSavePlan() {
  const items = collectPlanItems();
  await api("/api/progress/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  });
  flashAction("计划已保存。");
  await loadSnapshot();
}

function collectPlanItems() {
  const rows = [...el.planTableBody.querySelectorAll("tr")];
  return rows
    .map((row) => {
      const read = (field) => row.querySelector(`[data-field="${field}"]`)?.value?.trim() || "";
      return {
        id: row.dataset.id || null,
        title: read("title"),
        owner: read("owner"),
        status: read("status") || "TODO",
        progress_pct: Number(read("progress_pct") || 0),
        target_date: read("target_date"),
        notes: read("notes"),
      };
    })
    .filter((item) => item.title);
}

function flashAction(message) {
  el.actionResult.textContent = `${formatTime(new Date().toISOString())} | ${message}`;
}

function fmtPct(value) {
  const num = Number(value || 0);
  if (Number.isNaN(num)) {
    return "-";
  }
  return `${num.toFixed(2)}%`;
}

function fmtSignedPct(value) {
  const num = Number(value || 0);
  if (Number.isNaN(num)) {
    return "-";
  }
  return `${num > 0 ? "+" : ""}${num.toFixed(2)}%`;
}

function formatTime(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("zh-CN", {
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function wrap(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (error) {
      console.error(error);
      alert(error.message || "操作失败。");
    }
  };
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = payload.detail || JSON.stringify(payload);
    } catch {
      detail = await response.text();
    }
    throw new Error(detail || "Request failed");
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("\n", " ");
}
