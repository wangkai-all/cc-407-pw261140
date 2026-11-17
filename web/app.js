/* 云澜市调查加权统计系统 —— 前端逻辑（无框架，直接 fetch 三个独立服务） */
"use strict";

const HOST = location.hostname || "localhost";
const INGEST = `http://${HOST}:8001`;
const WEIGHTING = `http://${HOST}:8002`;
const REPORT = `http://${HOST}:8003`;

const $ = (id) => document.getElementById(id);

async function api(base, path, options = {}) {
  const resp = await fetch(base + path, options);
  const text = await resp.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function fmt(x, digits = 3) {
  if (x === null || x === undefined) return "—";
  if (typeof x === "number") return x.toLocaleString("zh-CN", { maximumFractionDigits: digits });
  return String(x);
}

function table(headers, rows) {
  const th = headers.map((h) => `<th>${h}</th>`).join("");
  const trs = rows
    .map((r) => `<tr>${r.map((c) => `<td>${c === null || c === undefined ? "—" : c}</td>`).join("")}</tr>`)
    .join("");
  return `<table><thead><tr>${th}</tr></thead><tbody>${trs || '<tr><td colspan="99">（无数据）</td></tr>'}</tbody></table>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------------------------------------------------------------- 标签页 */
document.querySelectorAll("#tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    refreshTab(btn.dataset.tab);
  });
});

function refreshTab(tab) {
  if (tab === "overview") loadOverview();
  if (tab === "ingest") { loadBenchmarks(); loadBatches(); }
  if (tab === "runs") { loadRuns(); loadBenchmarkOptions(); }
  if (tab === "reports") { loadGroupReport(); loadUnestimable(); }
  if (tab === "compare") loadCompareOptions();
  if (tab === "download") setupDownloads();
}

/* ---------------------------------------------------------------- 健康检查 */
async function checkHealth() {
  const services = [
    ["样本接收 :8001", INGEST],
    ["权重计算 :8002", WEIGHTING],
    ["报表 :8003", REPORT],
  ];
  const parts = await Promise.all(services.map(async ([label, base]) => {
    try {
      await api(base, "/health");
      return `<span class="badge ok">● ${label}</span>`;
    } catch {
      return `<span class="badge down">○ ${label} 离线</span>`;
    }
  }));
  $("health").innerHTML = parts.join(" ");
}

/* ---------------------------------------------------------------- 概览 */
async function loadOverview() {
  try {
    const snap = await api(REPORT, "/api/snapshot");
    $("snapshot-info").innerHTML = snap.snapshot
      ? table(["快照(运行)ID", "模式", "基准版本", "发布时间"], [[
          snap.snapshot.run_id,
          snap.snapshot.mode === "full" ? "全量" : "增量",
          escapeHtml(snap.snapshot.benchmark_name),
          snap.snapshot.published_at,
        ]])
      : "<p>尚未发布任何快照。请先到「权重计算」页运行并发布。</p>";
  } catch (e) {
    $("snapshot-info").innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`;
  }
  try {
    const m = await api(REPORT, "/api/reports/master");
    if (m.error) { $("master-report").innerHTML = `<p>${escapeHtml(m.error)}</p>`; return; }
    const warn = (m.warnings || []).map((w) => `<p class="warn">⚠️ ${escapeHtml(w)}</p>`).join("");
    $("master-report").innerHTML =
      table(["指标", "值"], [
        ["快照 ID", m.snapshot_id],
        ["基准版本", escapeHtml(m.benchmark_version || "—")],
        ["加权满意度均值", fmt(m.weighted_mean_satisfaction)],
        ["加权服务使用率", m.weighted_used_service_ratio === null ? "—" : (m.weighted_used_service_ratio * 100).toFixed(1) + "%"],
        ["参与估计记录数", fmt(m.n_records, 0)],
        ["权重和（=覆盖人口）", fmt(m.sum_weight, 0)],
        ["未覆盖人口（无法估计层）", fmt(m.uncovered_population, 0)],
        ["被修剪权重记录数", fmt(m.n_trimmed, 0)],
        ["距上次全量重算的增量次数", fmt(m.incremental_since_full, 0)],
      ]) + warn + `<p class="hint">${escapeHtml(m.honesty_note)}</p>`;
  } catch (e) {
    $("master-report").innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`;
  }
}

/* ---------------------------------------------------------------- 基准与样本 */
async function loadBenchmarks() {
  try {
    const data = await api(INGEST, "/api/benchmarks");
    $("benchmark-list").innerHTML = table(
      ["ID", "版本名", "层数", "总人口", "发布时间", "备注"],
      data.benchmarks.map((b) => [b.id, escapeHtml(b.name), b.cells, fmt(b.total_population, 0), b.created_at, escapeHtml(b.note || "")])
    );
  } catch (e) {
    $("benchmark-list").innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`;
  }
}

async function loadBatches() {
  try {
    const data = await api(INGEST, "/api/batches");
    $("batch-list").innerHTML = table(
      ["批次", "来源", "记录数", "接收时间", "备注"],
      data.batches.map((b) => [b.id, escapeHtml(b.source), b.n_records, b.received_at, escapeHtml(b.note || "")])
    );
  } catch (e) {
    $("batch-list").innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`;
  }
}

function uploadFile(inputId, url, resultEl, done) {
  const file = $(inputId).files[0];
  if (!file) { $(resultEl).textContent = "请先选择 CSV 文件"; return; }
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const data = await api("", url, { method: "POST", body: reader.result, headers: { "Content-Type": "text/csv" } });
      $(resultEl).textContent = JSON.stringify(data, null, 2);
      done && done();
    } catch (e) {
      $(resultEl).textContent = "错误：" + e.message;
    }
  };
  reader.readAsText(file);
}

$("bench-upload").addEventListener("click", () => {
  const name = encodeURIComponent($("bench-name").value.trim());
  const note = encodeURIComponent($("bench-note").value.trim());
  uploadFile("bench-file", `${INGEST}/api/benchmarks?name=${name}&note=${note}`, "bench-result", loadBenchmarks);
});

$("sample-upload").addEventListener("click", () => {
  const source = encodeURIComponent($("sample-source").value.trim());
  const note = encodeURIComponent($("sample-note").value.trim());
  uploadFile("sample-file", `${INGEST}/api/samples?source=${source}&note=${note}`, "sample-result", loadBatches);
});

/* ---------------------------------------------------------------- 权重计算 */
async function loadBenchmarkOptions() {
  try {
    const data = await api(INGEST, "/api/benchmarks");
    $("run-benchmark").innerHTML = data.benchmarks
      .map((b) => `<option value="${b.id}">${b.id} — ${escapeHtml(b.name)}</option>`)
      .join("");
  } catch { /* 服务离线时保持现状 */ }
}

$("run-start").addEventListener("click", async () => {
  const payload = {
    mode: $("run-mode").value,
    benchmark_version_id: Number($("run-benchmark").value),
    config: {
      min_cell_size: Number($("cfg-min-cell").value),
      trim_lo: Number($("cfg-trim-lo").value),
      trim_hi: Number($("cfg-trim-hi").value),
      chunk_delay_ms: Number($("cfg-delay").value),
    },
  };
  try {
    const data = await api(WEIGHTING, "/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("run-start-result").textContent = `已启动运行 ${data.id}（${data.mode}）`;
    loadRuns();
  } catch (e) {
    $("run-start-result").textContent = "错误：" + e.message;
  }
});

async function runAction(id, action) {
  try {
    const data = await api(WEIGHTING, `/api/runs/${id}/${action}`, { method: "POST" });
    if (data.error) alert(data.error);
  } catch (e) {
    alert(e.message);
  }
  loadRuns();
}

async function loadRuns() {
  try {
    const data = await api(REPORT, "/api/runs");
    const rows = data.runs.map((r) => {
      const pct = r.progress_total ? Math.round((100 * r.progress_done) / r.progress_total) : 0;
      const actions = [];
      if (r.status === "running") actions.push(`<button class="small" onclick="runAction(${r.id},'interrupt')">中断</button>`);
      if (r.status === "interrupted") actions.push(`<button class="small" onclick="runAction(${r.id},'resume')">恢复</button>`);
      if (r.status === "completed" && !r.published) actions.push(`<button class="small" onclick="runAction(${r.id},'publish')">发布</button>`);
      const badges = [r.published ? '<span class="badge ok">已发布</span>' : ""].join("");
      return [
        r.id,
        r.mode === "full" ? "全量" : "增量",
        escapeHtml(r.benchmark_name),
        r.status,
        `${r.progress_done}/${r.progress_total} (${pct}%)`,
        r.parent_run_id || "—",
        badges,
        actions.join(" ") || "—",
      ];
    });
    $("runs-list").innerHTML = table(
      ["ID", "模式", "基准版本", "状态", "进度", "父运行", "发布", "操作"], rows
    );
  } catch (e) {
    $("runs-list").innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`;
  }
}
$("runs-refresh").addEventListener("click", loadRuns);

/* ---------------------------------------------------------------- 报表 */
$("report-dim").addEventListener("change", loadGroupReport);

async function loadGroupReport() {
  const dim = $("report-dim").value;
  try {
    const data = await api(REPORT, `/api/reports/groups?dim=${dim}`);
    if (!data.groups.length) {
      $("group-report").innerHTML = "<p>尚无已发布快照。</p>";
      return;
    }
    $("group-report").innerHTML =
      `<p class="hint">快照 ID：${data.snapshot_id}（与总表同源）</p>` +
      table(
        [data.dimension_label, "记录数", "权重和", "加权满意度", "加权使用率"],
        data.groups.map((g) => [
          escapeHtml(g.group), g.n_records, fmt(g.sum_weight, 0),
          fmt(g.weighted_mean_satisfaction),
          g.weighted_used_service_ratio === null ? "—" : (g.weighted_used_service_ratio * 100).toFixed(1) + "%",
        ])
      );
  } catch (e) {
    $("group-report").innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`;
  }
}

async function loadUnestimable() {
  try {
    const data = await api(REPORT, "/api/reports/unestimable");
    if (!data.strata || !data.strata.length) {
      $("unestimable-report").innerHTML = `<p>快照 ${data.snapshot_id ?? "—"}：所有层均可估计，无未覆盖人口。</p>`;
      return;
    }
    $("unestimable-report").innerHTML =
      `<p>快照 ${data.snapshot_id}：共 ${data.strata.length} 个层无法估计，未覆盖人口 ${fmt(data.uncovered_population, 0)}。</p>` +
      table(
        ["区县", "性别", "年龄段", "人口", "原因"],
        data.strata.map((s) => [escapeHtml(s.region), escapeHtml(s.sex), escapeHtml(s.age_group), fmt(s.population, 0), escapeHtml(s.reason)])
      );
  } catch (e) {
    $("unestimable-report").innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`;
  }
}

/* ---------------------------------------------------------------- 修订比较 */
async function loadCompareOptions() {
  try {
    const data = await api(REPORT, "/api/runs");
    const completed = data.runs.filter((r) => r.status === "completed");
    const opts = completed
      .map((r) => `<option value="${r.id}">运行 ${r.id}（${r.mode === "full" ? "全量" : "增量"} / ${escapeHtml(r.benchmark_name)}）</option>`)
      .join("");
    $("compare-from").innerHTML = opts;
    $("compare-to").innerHTML = opts;
    if (completed.length >= 2) {
      $("compare-from").value = completed[0].id;
      $("compare-to").value = completed[completed.length - 1].id;
    }
  } catch { /* ignore */ }
}

$("compare-run").addEventListener("click", async () => {
  const from = $("compare-from").value;
  const to = $("compare-to").value;
  if (!from || !to) { $("compare-result").innerHTML = "<p>需要两个已完成的运行。</p>"; return; }
  try {
    const c = await api(REPORT, `/api/compare?from=${from}&to=${to}`);
    const pct = (x) => (x === null || x === undefined ? "—" : (x * 100).toFixed(2) + "%");
    const signed = (x, d = 4) => (x === null || x === undefined ? "—" : (x >= 0 ? "+" : "") + fmt(x, d));
    let html = table(["指标", "修订前", "修订后", "变化"], [
      ["基准版本", escapeHtml(c.benchmark_before), escapeHtml(c.benchmark_after), ""],
      ["加权满意度均值", fmt(c.satisfaction_before), fmt(c.satisfaction_after), signed(c.satisfaction_delta)],
      ["加权服务使用率", pct(c.used_service_before), pct(c.used_service_after), signed(c.used_service_delta * 100, 2) + " pp"],
      ["未覆盖人口", fmt(c.uncovered_before, 0), fmt(c.uncovered_after, 0), signed(c.uncovered_delta, 0)],
      ["权重发生变化的记录数", "", "", fmt(c.records_weight_changed, 0)],
      ["最大单条权重变化", "", "", fmt(c.max_weight_change)],
    ]);
    html += `<h4>分区县满意度变化</h4>` + table(
      ["区县", "修订前", "修订后", "变化"],
      c.by_region.map((r) => [
        escapeHtml(r.group), fmt(r.satisfaction_before), fmt(r.satisfaction_after), signed(r.satisfaction_delta),
      ])
    );
    if (c.newly_estimable_strata.length)
      html += `<p class="ok-text">✓ 新变为可估计的层：${c.newly_estimable_strata.map(escapeHtml).join("；")}</p>`;
    if (c.newly_unestimable_strata.length)
      html += `<p class="warn">⚠️ 新变为无法估计的层：${c.newly_unestimable_strata.map(escapeHtml).join("；")}</p>`;
    html += `<p class="hint">${escapeHtml(c.honesty_note)}</p>`;
    $("compare-result").innerHTML = html;
  } catch (e) {
    $("compare-result").innerHTML = `<p class="error">${escapeHtml(e.message)}</p>`;
  }
});

/* ---------------------------------------------------------------- 下载 */
function setupDownloads() {
  $("dl-records").href = `${REPORT}/api/download/record_weights.csv`;
  $("dl-strata").href = `${REPORT}/api/download/stratum_weights.csv`;
}

/* ---------------------------------------------------------------- 启动 */
checkHealth();
loadOverview();
setupDownloads();
setInterval(checkHealth, 15000);
setInterval(() => {
  if ($("tab-runs").classList.contains("active")) loadRuns();
}, 2000);
