/* stream-archive 监控界面（原生 JS，无构建步骤） */

const state = {
  config: null,
  refresh: 5,
  timer: null,
  filters: { status: "", streamer: "", q: "" },
  logs: { name: "", tail: 500, auto: true },
};

const STEP_LABELS = {
  scan: "扫描",
  remux: "封装",
  duration_check: "时长校验",
  encode: "压制",
  cover: "封面",
  upload: "投稿",
  append_monthly: "按月投稿",
  archive: "归档",
  transcribe: "语音转写",
  upload_danmaku: "弹幕上传",
  cleanup_after: "清理",
};

const STATUS_LABELS = {
  done: "完成",
  failed: "失败",
  running: "运行中",
  pending: "等待",
  skipped: "跳过",
};

const STEP_ICONS = {
  done: "✓",
  failed: "✕",
  running: "▶",
  skipped: "–",
  pending: "·",
};

function $(id) {
  return document.getElementById(id);
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fmtBytes(value) {
  let size = Number(value || 0);
  if (size < 1024) return `${size}B`;
  const units = ["KB", "MB", "GB", "TB"];
  for (const unit of units) {
    size /= 1024;
    if (size < 1024 || unit === "TB") return `${size.toFixed(1)}${unit}`;
  }
  return `${size.toFixed(1)}TB`;
}

function fmtDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds || 0)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function statusBadge(status) {
  const key = STATUS_LABELS[status] ? status : "pending";
  return `<span class="badge ${key}">${STATUS_LABELS[key]}</span>`;
}

function progressBar(percent, status) {
  const width = Math.max(0, Math.min(100, Number(percent || 0)));
  return `<div class="progress ${esc(status)}"><span style="width:${width}%"></span></div>`;
}

function videoLink(videoId) {
  if (!videoId) return '<span class="muted">-</span>';
  const safe = esc(videoId);
  return `<a href="https://www.bilibili.com/video/${encodeURIComponent(videoId)}" target="_blank" rel="noopener">${safe}</a>`;
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const data = await response.json();
      message = data.detail || message;
    } catch (error) {
      /* ignore */
    }
    throw new Error(message || `HTTP ${response.status}`);
  }
  return response.json();
}

let toastTimer = null;
function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.add("hidden"), 5000);
}

function setActiveNav(name) {
  document.querySelectorAll(".tabs a").forEach((link) => {
    link.classList.toggle("active", link.dataset.nav === name);
  });
}

function updateHeader(watcher, now) {
  const dot = $("watcher-dot");
  const text = $("watcher-text");
  if (watcher && watcher.alive) {
    dot.className = "dot ok";
    text.textContent = `watcher 正常 · 上次扫描 ${watcher.last_scan || "-"}`;
  } else {
    dot.className = "dot bad";
    text.textContent = "watcher 未运行";
  }
  $("updated-at").textContent = now ? `刷新 ${now}` : "";
}

async function updateHealth() {
  try {
    const data = await api("/api/health");
    updateHeader(data.watcher, data.now);
  } catch (error) {
    updateHeader(null, "");
  }
}

/* ------------------------------------------------------------------ */
/* 路由 */
/* ------------------------------------------------------------------ */

function parseRoute() {
  let hash = location.hash.replace(/^#/, "");
  if (!hash) hash = "/";
  try {
    hash = decodeURI(hash);
  } catch (error) {
    /* 保留原始值 */
  }
  if (hash.startsWith("/jobs/")) {
    return { name: "job", id: hash.slice("/jobs/".length) };
  }
  if (hash === "/jobs") return { name: "jobs" };
  if (hash === "/logs") return { name: "logs" };
  return { name: "dashboard" };
}

async function route() {
  const current = parseRoute();
  setActiveNav(current.name);
  startPolling(current);

  if (current.name === "dashboard") await renderDashboard();
  else if (current.name === "jobs") await renderJobs();
  else if (current.name === "job") await renderJobDetail(current.id);
  else if (current.name === "logs") await renderLogs();
}

function startPolling(current) {
  clearInterval(state.timer);
  state.timer = setInterval(async () => {
    await updateHealth();
    try {
      if (current.name === "dashboard") await renderDashboard(true);
      else if (current.name === "job") await renderJobDetail(current.id, true);
      else if (current.name === "logs" && state.logs.auto) await loadLogContent();
    } catch (error) {
      toast(error.message);
    }
  }, Math.max(2, state.refresh) * 1000);
}

/* ------------------------------------------------------------------ */
/* 总览 */
/* ------------------------------------------------------------------ */

function jobRow(job) {
  const title = job.stream_title || "(无标题)";
  return `
    <tr class="clickable" data-job="${esc(job.id)}">
      <td class="small">${esc(job.id)}</td>
      <td>${esc(job.streamer || "-")}</td>
      <td>${esc(title)}</td>
      <td>${statusBadge(job.status)}</td>
      <td>${esc(STEP_LABELS[job.current_step] || job.current_step || "-")}</td>
      <td>${progressBar(job.progress, job.status)}</td>
      <td>${fmtDuration(job.elapsed_seconds)}</td>
      <td>${videoLink(job.video_id)}</td>
    </tr>`;
}

function jobTable(jobs) {
  if (!jobs.length) {
    return '<div class="card muted">暂无任务</div>';
  }
  return `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>任务</th><th>主播</th><th>标题</th><th>状态</th>
            <th>当前步骤</th><th>进度</th><th>耗时</th><th>稿件</th>
          </tr>
        </thead>
        <tbody>${jobs.map(jobRow).join("")}</tbody>
      </table>
    </div>`;
}

async function renderDashboard(silent = false) {
  const app = $("app");
  if (!silent) app.innerHTML = '<div class="loading">加载中…</div>';

  let data;
  try {
    data = await api("/api/overview");
  } catch (error) {
    app.innerHTML = `<div class="card">加载失败：${esc(error.message)}</div>`;
    return;
  }

  updateHeader(data.watcher, data.now);

  const disk = data.disk;
  const diskHint = disk
    ? `已用 ${fmtBytes(disk.used)} / ${fmtBytes(disk.total)}`
    : "不可用";
  const diskValue = disk ? fmtBytes(disk.free) : "-";

  const recordingRows = data.recordings.length
    ? data.recordings
        .map(
          (item) => `
          <tr>
            <td class="small">${esc(item.path)}</td>
            <td>${item.videos} / ${item.files}</td>
            <td>${fmtBytes(item.size)}</td>
            <td>${esc(item.last_write || "-")}</td>
            <td>${item.age_seconds == null ? "-" : fmtDuration(item.age_seconds)}</td>
            <td>${item.status === "recording" ? '<span class="badge running">录制中</span>' : '<span class="badge pending">已静默</span>'}</td>
          </tr>`
        )
        .join("")
    : '<tr><td colspan="6" class="muted">没有检测到录制目录</td></tr>';

  app.innerHTML = `
    <h1>总览</h1>
    <div class="grid cards">
      <div class="card metric"><div class="label">任务总数</div><div class="value">${data.jobs.total}</div><div class="hint">今日 ${data.jobs.today}</div></div>
      <div class="card metric"><div class="label">运行中</div><div class="value">${data.jobs.running}</div><div class="hint">等待/处理中</div></div>
      <div class="card metric"><div class="label">失败</div><div class="value">${data.jobs.failed}</div><div class="hint">需要 retry</div></div>
      <div class="card metric"><div class="label">已完成</div><div class="value">${data.jobs.done}</div><div class="hint">已转写 ${data.jobs.transcribed}</div></div>
      <div class="card metric"><div class="label">磁盘剩余</div><div class="value">${diskValue}</div><div class="hint">${esc(diskHint)}</div></div>
    </div>

    <div class="section">
      <h2>录制目录</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>目录</th><th>视频/文件</th><th>大小</th><th>最后写入</th><th>静默</th><th>状态</th></tr></thead>
          <tbody>${recordingRows}</tbody>
        </table>
      </div>
    </div>

    <div class="section">
      <h2>最近任务</h2>
      ${jobTable(data.recent)}
    </div>
  `;
}

/* ------------------------------------------------------------------ */
/* 任务列表 */
/* ------------------------------------------------------------------ */

async function renderJobs() {
  const app = $("app");
  app.innerHTML = `
    <h1>任务</h1>
    <div class="toolbar">
      <select id="filter-status">
        <option value="">全部状态</option>
        <option value="running">运行中</option>
        <option value="pending">等待</option>
        <option value="failed">失败</option>
        <option value="done">完成</option>
      </select>
      <select id="filter-streamer"><option value="">全部主播</option></select>
      <input id="filter-q" type="search" placeholder="搜索任务号 / 标题 / 主播">
      <button id="filter-refresh">刷新</button>
      <span id="jobs-count" class="muted small"></span>
    </div>
    <div id="jobs-table"><div class="loading">加载中…</div></div>
  `;

  $("filter-status").value = state.filters.status;
  $("filter-q").value = state.filters.q;
  $("filter-status").addEventListener("change", (event) => {
    state.filters.status = event.target.value;
    loadJobs();
  });
  $("filter-streamer").addEventListener("change", (event) => {
    state.filters.streamer = event.target.value;
    loadJobs();
  });
  $("filter-q").addEventListener("input", (event) => {
    state.filters.q = event.target.value;
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(loadJobs, 250);
  });
  $("filter-refresh").addEventListener("click", loadJobs);

  await loadJobs();
}

async function loadJobs() {
  const params = new URLSearchParams();
  if (state.filters.status) params.set("status", state.filters.status);
  if (state.filters.streamer) params.set("streamer", state.filters.streamer);
  if (state.filters.q) params.set("q", state.filters.q);

  let data;
  try {
    data = await api(`/api/jobs?${params.toString()}`);
  } catch (error) {
    $("jobs-table").innerHTML = `<div class="card">加载失败：${esc(error.message)}</div>`;
    return;
  }

  const select = $("filter-streamer");
  if (select) {
    const current = state.filters.streamer;
    const options = ['<option value="">全部主播</option>']
      .concat(data.streamers.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`))
      .join("");
    select.innerHTML = options;
    select.value = current;
  }

  $("jobs-count").textContent = `${data.total} 个任务`;
  $("jobs-table").innerHTML = jobTable(data.jobs);
}

/* ------------------------------------------------------------------ */
/* 任务详情 */
/* ------------------------------------------------------------------ */

function stepRow(step) {
  return `
    <div class="step">
      <div class="icon ${esc(step.status)}">${STEP_ICONS[step.status] || "·"}</div>
      <div class="name">${esc(STEP_LABELS[step.name] || step.name)}</div>
      <div class="detail">${esc(step.detail || "")}</div>
      <div class="time">${step.duration_seconds ? fmtDuration(step.duration_seconds) : ""}</div>
    </div>`;
}

function fileList(items) {
  if (!items || !items.length) return '<div class="output-box muted">暂无产物</div>';
  return items
    .map(
      (item) => `
      <div class="output-box">
        <div>${item.kind === "pressed_dir" ? "压制版" : "原文件"}</div>
        <div class="small">${esc(item.name)}</div>
        <div class="muted small">${fmtBytes(item.size)}</div>
      </div>`
    )
    .join("");
}

async function renderJobDetail(jobId, silent = false) {
  const app = $("app");
  if (!silent) app.innerHTML = '<div class="loading">加载中…</div>';

  let data;
  try {
    data = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
  } catch (error) {
    app.innerHTML = `<div class="card">加载失败：${esc(error.message)}</div>`;
    return;
  }

  const scrollY = window.scrollY;
  const cover = data.cover_url
    ? `<img class="cover" src="${esc(data.cover_url)}" alt="封面">`
    : '<div class="output-box muted">暂无封面</div>';

  const asrFiles = (data.asr_files || [])
    .map(
      (item) => `
      <div class="output-box">
        <div>${esc(item.video || "")}</div>
        <div class="small muted">${item.txt ? esc(item.txt.split("/").pop()) : "无 txt"}</div>
        <div class="small muted">${item.srt ? esc(item.srt.split("/").pop()) : "无 srt"}</div>
      </div>`
    )
    .join("");

  app.innerHTML = `
    <div class="muted small"><a href="#/jobs">← 返回任务列表</a></div>
    <h1>${esc(data.stream_title || "(无标题)")}</h1>
    <div class="toolbar small">
      <span>任务 ${esc(data.id)}</span>
      <span>主播 ${esc(data.streamer || "-")}</span>
      <span>开播 ${esc(data.start_time || "-")}</span>
      <span>耗时 ${fmtDuration(data.elapsed_seconds)}</span>
      ${statusBadge(data.status)}
      ${data.video_id ? `稿件 ${videoLink(data.video_id)}` : ""}
    </div>
    <div class="card">
      <div class="small muted">进度 ${data.progress}%</div>
      ${progressBar(data.progress, data.status)}
    </div>

    <div class="section">
      <h2>步骤</h2>
      <div class="steps">${data.steps.map(stepRow).join("")}</div>
    </div>

    <div class="section">
      <h2>产物</h2>
      <div class="outputs">
        ${cover}
        ${fileList(data.files)}
      </div>
      ${asrFiles ? `<h2 style="margin-top:16px">语音转写</h2><div class="outputs">${asrFiles}</div>` : ""}
      ${
        data.upload && data.upload.title
          ? `<details style="margin-top:12px"><summary>投稿输出</summary><pre class="log">${esc(data.upload.detail || "")}</pre></details>`
          : ""
      }
    </div>

    <div class="section">
      <h2>任务日志</h2>
      <pre class="log" id="job-log"></pre>
    </div>
  `;

  const log = $("job-log");
  if (log) log.textContent = data.log_tail || "（暂无日志）";
  if (silent) window.scrollTo(0, scrollY);
}

/* ------------------------------------------------------------------ */
/* 日志 */
/* ------------------------------------------------------------------ */

async function renderLogs() {
  const app = $("app");
  app.innerHTML = `
    <h1>日志</h1>
    <div class="toolbar">
      <select id="log-select"></select>
      <input id="log-tail" type="number" min="50" max="5000" value="${state.logs.tail}" style="width:110px">
      <label class="small muted"><input id="log-auto" type="checkbox" ${state.logs.auto ? "checked" : ""}> 自动刷新</label>
      <button id="log-refresh">刷新</button>
    </div>
    <pre class="log" id="log-content">加载中…</pre>
  `;

  let data;
  try {
    data = await api("/api/logs");
  } catch (error) {
    $("log-content").textContent = `加载失败：${error.message}`;
    return;
  }

  const select = $("log-select");
  select.innerHTML = data.logs
    .map(
      (item) =>
        `<option value="${esc(item.name)}">${esc(item.label)}（${fmtBytes(item.size)}）</option>`
    )
    .join("");
  if (!data.logs.length) {
    $("log-content").textContent = "没有可用日志";
    return;
  }

  if (!state.logs.name || !data.logs.some((item) => item.name === state.logs.name)) {
    state.logs.name = data.logs[0].name;
  }
  select.value = state.logs.name;

  select.addEventListener("change", (event) => {
    state.logs.name = event.target.value;
    loadLogContent();
  });
  $("log-tail").addEventListener("change", (event) => {
    state.logs.tail = Math.max(50, Math.min(5000, Number(event.target.value) || 500));
    loadLogContent();
  });
  $("log-auto").addEventListener("change", (event) => {
    state.logs.auto = event.target.checked;
  });
  $("log-refresh").addEventListener("click", loadLogContent);

  await loadLogContent();
}

async function loadLogContent() {
  if (!state.logs.name) return;
  const pre = $("log-content");
  if (!pre) return;
  try {
    const data = await api(
      `/api/logs/${encodeURIComponent(state.logs.name)}?tail=${state.logs.tail}`
    );
    pre.textContent = data.content || "（空）";
  } catch (error) {
    pre.textContent = `加载失败：${error.message}`;
  }
}

/* ------------------------------------------------------------------ */
/* 初始化 */
/* ------------------------------------------------------------------ */

async function init() {
  try {
    state.config = await api("/api/config");
    state.refresh = state.config.web?.refresh_seconds || 5;
    $("server-name").textContent = state.config.server_name || "";
  } catch (error) {
    /* 配置接口失败不影响页面 */
  }

  $("app").addEventListener("click", (event) => {
    if (event.target.closest("a")) return;
    const row = event.target.closest("tr[data-job]");
    if (!row) return;
    location.hash = `#/jobs/${encodeURIComponent(row.dataset.job)}`;
  });

  window.addEventListener("hashchange", route);
  await updateHealth();
  await route();
}

init();
