/* stream-archive 监控界面（Vue 3，无构建步骤） */

const { createApp, ref, reactive, computed, watch, onMounted, onUnmounted } = Vue;

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

const STEP_ICONS = { done: "✓", failed: "✕", running: "▶", skipped: "–", pending: "·" };
const STATUS_LABELS = { done: "完成", failed: "失败", running: "运行中", pending: "等待", skipped: "跳过" };

const REVIEW_MAP = {
  published: ["done", "已通过"],
  reviewing: ["running", "审核中"],
  self_only: ["skipped", "仅自见"],
  rejected: ["failed", "已退回"],
  not_found: ["pending", "待审核"],
  unknown: ["pending", "未知"],
  error: ["failed", "查询失败"],
};

const REPAIR_MAP = {
  running: ["running", "处理中"],
  ready: ["pending", "待投稿"],
  uploaded: ["done", "已重投"],
  failed: ["failed", "失败"],
  skipped: ["pending", "未处理"],
};

/* ---------------- 工具函数 ---------------- */

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

function fmtPercent(value) {
  if (value === null || value === undefined || value === "") return "";
  const number = Number(value);
  if (Number.isNaN(number)) return "";
  return `${Math.round(number)}%`;
}

function stepLabel(name) {
  return STEP_LABELS[name] || name || "";
}

function videoUrl(id) {
  return `https://www.bilibili.com/video/${encodeURIComponent(id)}`;
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

const toastMessage = ref("");
let toastTimer = null;
function showToast(message) {
  toastMessage.value = String(message || "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastMessage.value = "";
  }, 5000);
}

/* ---------------- 通用组件 ---------------- */

const ProgressBar = {
  props: {
    percent: { default: null },
    status: { type: String, default: "" },
    indeterminate: { type: Boolean, default: false },
  },
  computed: {
    width() {
      const value = Number(this.percent);
      if (Number.isNaN(value)) return 0;
      return Math.max(0, Math.min(100, value));
    },
  },
  template: `
    <div class="progress" :class="[status, { indeterminate }]">
      <span :style="{ width: width + '%' }"></span>
    </div>`,
};

const StatusBadge = {
  props: { status: { type: String, default: "pending" } },
  computed: {
    label() {
      return STATUS_LABELS[this.status] || this.status || "-";
    },
    cls() {
      return STATUS_LABELS[this.status] ? this.status : "pending";
    },
  },
  template: `<span class="badge" :class="cls">{{ label }}</span>`,
};

const ReviewBadge = {
  props: { review: { type: Object, default: () => ({}) } },
  computed: {
    mapped() {
      const kind = (this.review && this.review.kind) || "";
      return REVIEW_MAP[kind] || (kind ? ["pending", kind] : ["pending", "-"]);
    },
  },
  template: `<span class="badge" :class="mapped[0]">{{ mapped[1] }}</span>`,
};

const RepairBadge = {
  props: { job: { type: Object, default: () => ({}) } },
  computed: {
    item() {
      const daily = this.job.repair || {};
      const monthly = this.job.monthly_repair || {};
      return daily.status ? daily : monthly;
    },
    mapped() {
      if (!this.item.status) return ["pending", "-"];
      const [cls, text] = REPAIR_MAP[this.item.status] || ["pending", this.item.status];
      const prefix = (this.job.repair || {}).status ? "" : "月稿 ";
      return [cls, prefix + text];
    },
  },
  template: `<span class="badge" :class="mapped[0]">{{ mapped[1] }}</span>`,
};

const ReviewPanel = {
  props: { review: { type: Object, default: () => ({}) }, label: { type: String, default: "" } },
  components: { ReviewBadge },
  template: `
    <div v-if="review && review.kind" class="card panel-card">
      <div class="title">
        <strong>{{ label }}</strong>
        <ReviewBadge :review="review" />
      </div>
      <div class="muted small" style="margin-top:4px">{{ review.message || "" }}</div>
      <div v-if="review.reject_reason" class="reason">退回原因：{{ review.reject_reason }}</div>
      <div v-for="(item, index) in review.problem_details || []" :key="index" class="problem">
        {{ index + 1 }}. {{ item.reason || "" }}
        <span v-if="item.violation_time" class="muted"> · {{ item.violation_time }}</span>
        <span v-if="item.violation_position" class="muted"> · {{ item.violation_position }}</span>
      </div>
      <div v-if="review.modify_advise" class="advice">建议：{{ review.modify_advise }}</div>
    </div>`,
};

const RepairPanel = {
  props: { repair: { type: Object, default: () => ({}) }, label: { type: String, default: "" } },
  computed: {
    mapped() {
      if (!this.repair || !this.repair.status) return null;
      return REPAIR_MAP[this.repair.status] || ["pending", this.repair.status];
    },
  },
  template: `
    <div v-if="mapped" class="card panel-card">
      <div class="title">
        <strong>{{ label }}</strong>
        <span class="badge" :class="mapped[0]">{{ mapped[1] }}</span>
        <a v-if="repair.bvid" :href="'https://www.bilibili.com/video/' + encodeURIComponent(repair.bvid)" target="_blank" rel="noopener">{{ repair.bvid }}</a>
      </div>
      <div class="muted small" style="margin-top:4px">{{ repair.message || "" }}</div>
      <div v-if="repair.fixed_file" class="file">修复文件：{{ repair.fixed_file }}</div>
    </div>`,
};

const JobTable = {
  components: { ProgressBar, StatusBadge, ReviewBadge, RepairBadge },
  props: { jobs: { type: Array, default: () => [] } },
  emits: ["open"],
  computed: {
    rows() {
      return this.jobs || [];
    },
  },
  template: `
    <div v-if="rows.length" class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>任务</th><th>主播</th><th>标题</th><th>状态</th>
            <th>当前步骤 / 进度</th><th class="nw">耗时</th><th>稿件</th><th>审核</th><th>修复</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="job in rows" :key="job.id" class="clickable" @click="$emit('open', job.id)">
            <td class="small nw">{{ job.id }}</td>
            <td class="nw">{{ job.streamer || "-" }}</td>
            <td class="ellipsis" style="max-width:230px" :title="job.stream_title">{{ job.stream_title || "(无标题)" }}</td>
            <td><StatusBadge :status="job.status" /></td>
            <td class="step-cell">
              <div class="step-line">
                <span class="name">{{ stepName(job) }}</span>
                <span class="muted">{{ $pct(stepPercent(job)) }}</span>
              </div>
              <ProgressBar
                :percent="stepPercent(job)"
                :status="job.status"
                :indeterminate="job.status === 'running' && stepPercent(job) === null"
              />
              <div v-if="stepDetail(job)" class="muted tiny ellipsis" style="margin-top:4px">{{ stepDetail(job) }}</div>
            </td>
            <td class="nw small muted">{{ $dur(job.elapsed_seconds) }}</td>
            <td class="nw">
              <a v-if="job.video_id" :href="videoUrl(job.video_id)" target="_blank" rel="noopener" @click.stop>{{ job.video_id }}</a>
              <span v-else class="muted">-</span>
            </td>
            <td><ReviewBadge :review="(job.review && job.review.kind) ? job.review : job.append_review" /></td>
            <td><RepairBadge :job="job" /></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else class="card empty">暂无任务</div>`,
  methods: {
    videoUrl,
    stepName(job) {
      if (job.status === "done") return "已完成";
      if (job.status === "failed") return stepLabel(job.current_step) || "失败";
      return stepLabel(job.current_step) || "等待开始";
    },
    stepPercent(job) {
      const progress = job.current_step_progress || {};
      if (job.status === "done") return 100;
      if (progress.percent === null || progress.percent === undefined) return null;
      return progress.percent;
    },
    stepDetail(job) {
      return (job.current_step_progress && job.current_step_progress.detail) || "";
    },
  },
};

/* ---------------- 总览 ---------------- */

const DashboardView = {
  components: { JobTable, ProgressBar },
  props: { tick: { type: Number, default: 0 } },
  emits: ["open"],
  setup(props, { emit }) {
    const data = ref(null);
    const error = ref("");

    async function load(silent = false) {
      try {
        data.value = await api("/api/overview");
        error.value = "";
      } catch (err) {
        error.value = err.message;
        if (!silent) showToast(err.message);
      }
    }

    onMounted(() => load());
    watch(() => props.tick, () => load(true));

    const active = computed(() => {
      const jobs = (data.value && data.value.recent) || [];
      return jobs.filter((job) => job.status === "running" || job.status === "pending");
    });

    const recordings = computed(() => (data.value && data.value.recordings) || []);

    function stepPercent(job) {
      const progress = job.current_step_progress || {};
      return progress.percent === undefined ? null : progress.percent;
    }

    function stepDetail(job) {
      return (job.current_step_progress && job.current_step_progress.detail) ||
        job.stream_title || "";
    }

    return {
      data, error, active, recordings, stepPercent, stepDetail,
      openJob: (id) => emit("open", id),
      stepLabel, videoUrl,
    };
  },
  template: `
    <div v-if="error" class="card error-card">加载失败：{{ error }}</div>
    <template v-else-if="data">
      <div class="page-head">
        <div>
          <h1>总览</h1>
          <div class="sub">任务、录制目录与磁盘状态 · 刷新 {{ data.now }}</div>
        </div>
        <div class="chips">
          <span class="chip">磁盘剩余 {{ data.disk ? $bytes(data.disk.free) : "-" }}</span>
          <span class="chip">watcher {{ data.watcher && data.watcher.alive ? "正常" : "异常" }}</span>
        </div>
      </div>

      <div class="grid metrics">
        <div class="metric">
          <div class="label">任务总数</div>
          <div class="value">{{ data.jobs.total }}</div>
          <div class="hint">今日 {{ data.jobs.today }}</div>
        </div>
        <div class="metric accent">
          <div class="label">运行中</div>
          <div class="value">{{ data.jobs.running }}</div>
          <div class="hint">等待 / 处理中</div>
        </div>
        <div class="metric" :class="{ danger: data.jobs.failed > 0 }">
          <div class="label">失败</div>
          <div class="value">{{ data.jobs.failed }}</div>
          <div class="hint">需要 retry</div>
        </div>
        <div class="metric">
          <div class="label">已完成</div>
          <div class="value">{{ data.jobs.done }}</div>
          <div class="hint">已转写 {{ data.jobs.transcribed }}</div>
        </div>
        <div class="metric">
          <div class="label">磁盘剩余</div>
          <div class="value">{{ data.disk ? $bytes(data.disk.free) : "-" }}</div>
          <div class="hint" v-if="data.disk">已用 {{ $bytes(data.disk.used) }} / {{ $bytes(data.disk.total) }}</div>
          <div class="progress" style="margin-top:8px;height:6px">
            <span :style="{ width: (data.disk ? data.disk.percent : 0) + '%' }"></span>
          </div>
        </div>
      </div>

      <section class="section" v-if="active.length">
        <div class="section-head">
          <h2>正在处理</h2>
          <span class="muted small">{{ active.length }} 个任务</span>
        </div>
        <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
          <div v-for="job in active" :key="job.id" class="card current" style="cursor:pointer" @click="openJob(job.id)">
            <div class="current-head">
              <div>
                <div class="eyebrow">当前步骤</div>
                <h2>{{ stepLabel(job.current_step) || "等待开始" }}</h2>
              </div>
              <div class="big-percent">{{ $pct(stepPercent(job)) || "—" }}</div>
            </div>
            <ProgressBar
              :percent="stepPercent(job)"
              :status="job.status"
              :indeterminate="job.status === 'running' && stepPercent(job) === null"
            />
            <div class="current-detail">{{ stepDetail(job) }}</div>
            <div class="chips">
              <span class="chip">{{ job.streamer || "-" }}</span>
              <span class="chip">已耗时 {{ $dur(job.elapsed_seconds) }}</span>
              <span class="chip" v-if="job.current_step_progress && job.current_step_progress.eta_seconds">预计剩余 {{ $dur(job.current_step_progress.eta_seconds) }}</span>
              <span class="chip" v-if="job.current_step_progress && job.current_step_progress.speed">速度 {{ job.current_step_progress.speed }}</span>
            </div>
          </div>
        </div>
      </section>

      <section class="section">
        <div class="section-head"><h2>录制目录</h2></div>
        <div class="table-wrap" v-if="recordings.length">
          <table>
            <thead><tr><th>目录</th><th class="nw">视频 / 文件</th><th class="nw">大小</th><th class="nw">最后写入</th><th class="nw">静默</th><th class="nw">状态</th></tr></thead>
            <tbody>
              <tr v-for="item in recordings" :key="item.path">
                <td class="small" style="word-break:break-all">{{ item.path }}</td>
                <td class="nw">{{ item.videos }} / {{ item.files }}</td>
                <td class="nw">{{ $bytes(item.size) }}</td>
                <td class="nw small muted">{{ item.last_write || "-" }}</td>
                <td class="nw small muted">{{ item.age_seconds == null ? "-" : $dur(item.age_seconds) }}</td>
                <td class="nw">
                  <span class="badge" :class="item.status === 'recording' ? 'running' : 'pending'">
                    {{ item.status === "recording" ? "录制中" : "已静默" }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="card empty">没有检测到录制目录</div>
      </section>

      <section class="section">
        <div class="section-head">
          <h2>最近任务</h2>
          <a class="small" href="#/jobs">全部任务 →</a>
        </div>
        <JobTable :jobs="data.recent" @open="openJob" />
      </section>
    </template>
    <div v-else class="loading">加载中…</div>`,
};

/* ---------------- 任务列表 ---------------- */

const JobsView = {
  components: { JobTable },
  props: { tick: { type: Number, default: 0 } },
  emits: ["open"],
  setup(props, { emit }) {
    const jobs = ref([]);
    const streamers = ref([]);
    const total = ref(0);
    const error = ref("");
    const filters = reactive({ status: "", streamer: "", q: "" });
    let searchTimer = null;

    async function load(silent = false) {
      const params = new URLSearchParams();
      if (filters.status) params.set("status", filters.status);
      if (filters.streamer) params.set("streamer", filters.streamer);
      if (filters.q) params.set("q", filters.q);
      try {
        const data = await api(`/api/jobs?${params.toString()}`);
        jobs.value = data.jobs || [];
        streamers.value = data.streamers || [];
        total.value = data.total || 0;
        error.value = "";
      } catch (err) {
        error.value = err.message;
        if (!silent) showToast(err.message);
      }
    }

    function onSearch() {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => load(), 250);
    }

    watch(() => [filters.status, filters.streamer], () => load());
    watch(() => props.tick, () => load(true));
    onMounted(() => load());

    return {
      jobs, streamers, total, error, filters,
      load, onSearch,
      openJob: (id) => emit("open", id),
    };
  },
  template: `
    <div class="page-head">
      <div>
        <h1>任务</h1>
        <div class="sub">共 {{ total }} 个任务</div>
      </div>
      <button class="btn" @click="load()">刷新</button>
    </div>

    <div class="toolbar">
      <select v-model="filters.status">
        <option value="">全部状态</option>
        <option value="running">运行中</option>
        <option value="pending">等待</option>
        <option value="failed">失败</option>
        <option value="done">完成</option>
      </select>
      <select v-model="filters.streamer">
        <option value="">全部主播</option>
        <option v-for="name in streamers" :key="name" :value="name">{{ name }}</option>
      </select>
      <input type="search" placeholder="搜索任务号 / 标题 / 主播" v-model="filters.q" @input="onSearch" style="min-width:220px">
      <span class="muted small">{{ jobs.length }} / {{ total }}</span>
    </div>

    <div v-if="error" class="card error-card">加载失败：{{ error }}</div>
    <JobTable v-else :jobs="jobs" @open="openJob" />`,
};

/* ---------------- 任务详情 ---------------- */

const JobView = {
  components: {
    ProgressBar, StatusBadge, ReviewPanel, RepairPanel,
  },
  props: { jobId: { type: String, required: true }, tick: { type: Number, default: 0 } },
  setup(props) {
    const data = ref(null);
    const error = ref("");

    async function load(silent = false) {
      try {
        data.value = await api(`/api/jobs/${encodeURIComponent(props.jobId)}`);
        error.value = "";
      } catch (err) {
        error.value = err.message;
        if (!silent) showToast(err.message);
      }
    }

    onMounted(() => load());
    watch(() => props.tick, () => load(true));
    watch(() => props.jobId, () => load());

    const current = computed(() => {
      if (!data.value) return null;
      return data.value.current_step_progress || null;
    });

    const currentPercent = computed(() => {
      if (!data.value) return null;
      if (data.value.status === "done") return 100;
      const progress = data.value.current_step_progress || {};
      return progress.percent === undefined ? null : progress.percent;
    });

    const currentLabel = computed(() => {
      if (!data.value) return "";
      if (data.value.status === "done") return "已完成";
      return stepLabel(data.value.current_step) || "等待开始";
    });

    function stepPercent(step) {
      if (step.status === "done" || step.status === "skipped") return 100;
      const progress = step.progress || {};
      return progress.percent === undefined ? null : progress.percent;
    }

    function stepDetail(step) {
      return step.detail || (step.progress && step.progress.detail) || "";
    }

    function isActive(step) {
      return data.value && data.value.current_step === step.name;
    }

    return {
      data, error, current, currentPercent, currentLabel,
      stepPercent, stepDetail, isActive,
      stepLabel, videoUrl, stepIcon: (status) => STEP_ICONS[status] || "·",
      reload: () => load(),
      openCover: (url) => window.open(url, "_blank"),
    };
  },
  template: `
    <div v-if="error" class="card error-card">加载失败：{{ error }}</div>
    <template v-else-if="data">
      <div class="page-head">
        <div>
          <div class="small"><a href="#/jobs">← 返回任务列表</a></div>
          <h1>{{ data.stream_title || "(无标题)" }}</h1>
          <div class="sub">{{ data.id }}</div>
        </div>
        <div class="chips">
          <StatusBadge :status="data.status" />
          <a v-if="data.video_id" class="chip" :href="videoUrl(data.video_id)" target="_blank" rel="noopener">稿件 {{ data.video_id }}</a>
        </div>
      </div>

      <div class="meta small muted">
        <span>主播 {{ data.streamer || "-" }}</span>
        <span>·</span>
        <span>开播 {{ data.start_time || "-" }}</span>
        <span>·</span>
        <span>总耗时 {{ $dur(data.elapsed_seconds) }}</span>
        <span>·</span>
        <span>整体进度 {{ $pct(data.progress) }}</span>
      </div>

      <div class="card current">
        <div class="current-head">
          <div>
            <div class="eyebrow">{{ data.status === "done" ? "任务完成" : "当前步骤" }}</div>
            <h2>{{ currentLabel }}</h2>
          </div>
          <div class="big-percent">{{ $pct(currentPercent) || "—" }}</div>
        </div>
        <ProgressBar
          :percent="currentPercent"
          :status="data.status"
          :indeterminate="data.status === 'running' && currentPercent === null"
        />
        <div class="current-detail">
          {{ (current && current.detail) || (data.status === "done" ? "全部步骤已完成" : "等待进度上报…") }}
        </div>
        <div class="chips" v-if="current">
          <span class="chip" v-if="current.speed">速度 {{ current.speed }}</span>
          <span class="chip" v-if="current.processed_seconds != null">已处理 {{ $dur(current.processed_seconds) }} / {{ $dur(current.total_seconds) }}</span>
          <span class="chip" v-if="current.eta_seconds">预计剩余 {{ $dur(current.eta_seconds) }}</span>
          <span class="chip">更新时间 {{ current.updated_at || "-" }}</span>
        </div>
      </div>

      <div class="detail-grid" style="margin-top:16px">
        <div>
          <section class="section" style="margin-top:0">
            <div class="section-head"><h2>步骤</h2></div>
            <div class="steps">
              <div v-for="step in data.steps" :key="step.name" class="step" :class="{ active: isActive(step) }">
                <div class="icon" :class="step.status">{{ stepIcon(step.status) }}</div>
                <div class="name">{{ stepLabel(step.name) }}</div>
                <div class="mid">
                  <div class="detail" :title="stepDetail(step)">{{ stepDetail(step) || (step.status === "running" ? "处理中…" : "") }}</div>
                  <ProgressBar
                    v-if="step.status === 'running' || (step.progress && step.progress.percent != null)"
                    :percent="stepPercent(step)"
                    :status="step.status"
                    :indeterminate="step.status === 'running' && stepPercent(step) === null"
                  />
                </div>
                <div class="time">{{ step.duration_seconds ? $dur(step.duration_seconds) : "" }}</div>
              </div>
            </div>
          </section>

          <section class="section">
            <div class="section-head"><h2>产物</h2></div>
            <div class="outputs">
              <div v-if="data.cover_url" class="output-box" style="padding:8px">
                <img class="cover" :src="data.cover_url" alt="封面" @click="openCover(data.cover_url)" style="cursor:zoom-in">
              </div>
              <div v-else class="output-box muted">暂无封面</div>
              <div v-for="item in data.files" :key="item.kind + item.name" class="output-box">
                <div class="kind">{{ item.kind === "pressed_dir" ? "压制版" : "原文件" }}</div>
                <div>{{ item.name }}</div>
                <div class="muted small">{{ $bytes(item.size) }}</div>
              </div>
            </div>
            <div v-if="data.asr_files && data.asr_files.length" style="margin-top:14px">
              <h2>语音转写</h2>
              <div class="outputs">
                <div v-for="(item, index) in data.asr_files" :key="index" class="output-box">
                  <div>{{ item.video || "" }}</div>
                  <div class="small muted">{{ item.txt ? item.txt.split("/").pop() : "无 txt" }}</div>
                  <div class="small muted">{{ item.srt ? item.srt.split("/").pop() : "无 srt" }}</div>
                </div>
              </div>
            </div>
            <details v-if="data.upload && data.upload.detail">
              <summary>投稿输出</summary>
              <pre class="log" style="margin-top:8px">{{ data.upload.detail }}</pre>
            </details>
          </section>

          <section class="section">
            <div class="section-head"><h2>任务日志</h2></div>
            <pre class="log">{{ data.log_tail || "（暂无日志）" }}</pre>
          </section>
        </div>

        <div>
          <ReviewPanel :review="data.review" label="单场投稿审核" />
          <ReviewPanel :review="data.append_review" label="月稿追加审核" />
          <ReviewPanel :review="data.repair_review" label="修复版审核" />
          <RepairPanel :repair="data.repair" label="自动修复（单场）" />
          <RepairPanel :repair="data.monthly_repair" label="月稿修复" />
          <div class="card panel-card">
            <div class="title"><strong>路径</strong></div>
            <div class="file">缓存：{{ data.paths.work_dir || "-" }}</div>
            <div class="file">源目录：{{ data.paths.source_dir || "-" }}</div>
            <div class="file">压制版：{{ data.paths.pressed_dir || "-" }}</div>
            <div class="file">原文件：{{ data.paths.raw_dir || "-" }}</div>
          </div>
        </div>
      </div>
    </template>
    <div v-else class="loading">加载中…</div>`,
};

/* ---------------- 日志 ---------------- */

const LogsView = {
  props: { tick: { type: Number, default: 0 } },
  setup(props) {
    const logs = ref([]);
    const name = ref("");
    const tail = ref(500);
    const auto = ref(true);
    const content = ref("");
    const error = ref("");
    const loading = ref(false);

    async function loadList() {
      try {
        const data = await api("/api/logs");
        logs.value = data.logs || [];
        if (!logs.value.some((item) => item.name === name.value)) {
          name.value = logs.value.length ? logs.value[0].name : "";
        }
        error.value = "";
      } catch (err) {
        error.value = err.message;
      }
    }

    async function loadContent(silent = false) {
      if (!name.value) return;
      loading.value = true;
      try {
        const data = await api(`/api/logs/${encodeURIComponent(name.value)}?tail=${tail.value}`);
        content.value = data.content || "（空）";
        error.value = "";
        if (!silent) {
          const pre = document.querySelector(".log-pane pre.log");
          if (pre) pre.scrollTop = pre.scrollHeight;
        }
      } catch (err) {
        error.value = err.message;
      } finally {
        loading.value = false;
      }
    }

    function selectLog(item) {
      name.value = item.name;
      loadContent();
    }

    function onTailChange() {
      tail.value = Math.max(50, Math.min(5000, Number(tail.value) || 500));
      loadContent();
    }

    watch(() => props.tick, () => {
      if (auto.value) loadContent(true);
    });

    onMounted(async () => {
      await loadList();
      await loadContent();
    });

    return {
      logs, name, tail, auto, content, error, loading,
      selectLog, loadContent, onTailChange,
    };
  },
  template: `
    <div class="page-head">
      <div>
        <h1>日志</h1>
        <div class="sub">服务日志与任务日志尾部</div>
      </div>
      <button class="btn" @click="loadContent()" :disabled="loading">刷新</button>
    </div>

    <div class="logs-grid">
      <aside class="card" style="padding:10px">
        <div class="log-list" v-if="logs.length">
          <button
            v-for="item in logs"
            :key="item.name"
            class="log-item"
            :class="{ active: item.name === name }"
            @click="selectLog(item)"
          >
            <span class="size">{{ $bytes(item.size) }}</span>
            {{ item.label }}
          </button>
        </div>
        <div v-else class="muted small">没有可用日志</div>
      </aside>

      <section class="log-pane">
        <div class="toolbar">
          <select v-model="tail" @change="onTailChange">
            <option :value="200">200 行</option>
            <option :value="500">500 行</option>
            <option :value="1000">1000 行</option>
            <option :value="3000">3000 行</option>
          </select>
          <label class="small muted"><input type="checkbox" v-model="auto"> 自动刷新</label>
          <span class="muted small" v-if="loading">加载中…</span>
        </div>
        <div v-if="error" class="card error-card" style="margin-bottom:10px">加载失败：{{ error }}</div>
        <pre class="log">{{ content || "（空）" }}</pre>
      </section>
    </div>`,
};

/* ---------------- 根组件 / 路由 ---------------- */

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

const App = {
  components: { DashboardView, JobsView, JobView, LogsView },
  setup() {
    const route = ref(parseRoute());
    const health = ref(null);
    const serverName = ref("");
    const refreshSeconds = ref(5);
    const tick = ref(0);
    let timer = null;

    async function updateHealth() {
      try {
        health.value = await api("/api/health");
      } catch (error) {
        health.value = null;
      }
    }

    function openJob(id) {
      location.hash = `#/jobs/${encodeURIComponent(id)}`;
    }

    function onHashChange() {
      route.value = parseRoute();
      window.scrollTo(0, 0);
    }

    onMounted(async () => {
      try {
        const config = await api("/api/config");
        serverName.value = config.server_name || "";
        refreshSeconds.value = (config.web && config.web.refresh_seconds) || 5;
      } catch (error) {
        /* 配置接口失败不影响页面 */
      }
      await updateHealth();
      timer = setInterval(async () => {
        await updateHealth();
        tick.value += 1;
      }, Math.max(2, refreshSeconds.value) * 1000);
      window.addEventListener("hashchange", onHashChange);
    });

    onUnmounted(() => {
      clearInterval(timer);
      window.removeEventListener("hashchange", onHashChange);
    });

    const watcher = computed(() => health.value && health.value.watcher);
    const healthClass = computed(() => (watcher.value && watcher.value.alive ? "ok" : "bad"));
    const healthText = computed(() => (watcher.value && watcher.value.alive ? "watcher 正常" : "watcher 未运行"));
    const healthSub = computed(() => {
      if (!watcher.value) return "检查中…";
      if (!watcher.value.alive) return "请检查容器";
      return `上次扫描 ${watcher.value.last_scan || "-"}`;
    });

    return {
      route, serverName, tick, healthClass, healthText, healthSub,
      toast: toastMessage,
      openJob,
    };
  },
  template: `
    <div class="shell">
      <aside class="sidebar">
        <div class="brand">
          <span class="logo">▣</span>
          <div>
            <b>stream-archive</b>
            <small>{{ serverName }}</small>
          </div>
        </div>
        <nav class="nav">
          <a href="#/" :class="{ active: route.name === 'dashboard' }"><span class="ico">◉</span>总览</a>
          <a href="#/jobs" :class="{ active: route.name === 'jobs' || route.name === 'job' }"><span class="ico">☰</span>任务</a>
          <a href="#/logs" :class="{ active: route.name === 'logs' }"><span class="ico">≡</span>日志</a>
        </nav>
        <div class="side-foot">
          <span class="dot" :class="healthClass"></span>
          <div>
            <div>{{ healthText }}</div>
            <small>{{ healthSub }}</small>
          </div>
        </div>
      </aside>

      <main class="main">
        <div class="mobilebar">
          <span class="dot" :class="healthClass"></span>
          <span>{{ healthText }}</span>
          <span class="muted">· {{ healthSub }}</span>
        </div>
        <div class="main-inner">
          <DashboardView v-if="route.name === 'dashboard'" :tick="tick" @open="openJob" />
          <JobsView v-else-if="route.name === 'jobs'" :tick="tick" @open="openJob" />
          <JobView v-else-if="route.name === 'job'" :job-id="route.id" :tick="tick" />
          <LogsView v-else :tick="tick" />
        </div>
      </main>

      <div v-if="toast" class="toast">{{ toast }}</div>
    </div>`,
};

const app = createApp(App);
app.config.globalProperties.$bytes = fmtBytes;
app.config.globalProperties.$dur = fmtDuration;
app.config.globalProperties.$pct = fmtPercent;
app.mount("#app");
