/* ================= WXEdge 魔方管理器 · 前端核心 ================= */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  view: "overview",
  overview: null,
  plan: null,
  planSeq: 0,
  check: null,
  authToken: localStorage.getItem("wxedge_token") || "",
  filter: "all",
  query: "",
  history: { cpu: [], mem: [], at: 0 },
  refreshing: false,
  pollTimer: null,
  job: null,
  jobTimer: null,
  logsTimer: null,
  instancesSig: "",
};

const VIEW_META = {
  overview: { title: "概览", sub: "主机资源、容器状态与自动配置" },
  instances: { title: "实例管理", sub: "逐实例控制 CPU / 内存 / 硬盘 / 存储目录" },
  deploy: { title: "批量部署", sub: "按主机容量自动生成资源分配方案" },
  check: { title: "环境自检", sub: "Docker、镜像、存储与权限体检" },
  settings: { title: "系统设置", sub: "镜像、存储、后端模式与访问安全" },
};

/* ---------------- 主题与存储目录 ---------------- */
const THEMES = [
  { id: "amber", name: "琥珀机房", swatch: "#ffb000" },
  { id: "phosphor", name: "荧光绿屏", swatch: "#7dff9c" },
  { id: "ice", name: "冰蓝遥测", swatch: "#67e8f9" },
  { id: "crimson", name: "警戒红", swatch: "#ff8a4c" },
  { id: "paper", name: "纸感机柜", swatch: "#b45309" },
];

function currentTheme() {
  return document.documentElement.dataset.theme || "amber";
}

function closeThemeMenu() {
  const menu = $("#themeMenu");
  if (menu) menu.remove();
}

function outsideThemeMenu(event) {
  if (!event.target.closest("#themeMenu") && !event.target.closest("#themeBtn")) closeThemeMenu();
}

function openThemeMenu() {
  closeThemeMenu();
  const button = $("#themeBtn");
  if (!button) return;
  const menu = document.createElement("div");
  menu.id = "themeMenu";
  menu.className = "theme-menu";
  menu.innerHTML = THEMES.map((theme) => `<button type="button" class="theme-option ${theme.id === currentTheme() ? "is-current" : ""}" data-theme-id="${theme.id}"><span class="theme-swatch" style="background:${theme.swatch}"></span><span class="theme-name">${theme.name}</span><span class="theme-check">${theme.id === currentTheme() ? "●" : ""}</span></button>`).join("");
  document.body.appendChild(menu);
  const rect = button.getBoundingClientRect();
  menu.style.top = (rect.bottom + 8) + "px";
  menu.style.right = Math.max(8, window.innerWidth - rect.right) + "px";
  menu.addEventListener("click", (event) => {
    const option = event.target.closest("[data-theme-id]");
    if (option) setTheme(option.dataset.themeId);
  });
  setTimeout(() => document.addEventListener("click", outsideThemeMenu, { once: true }), 0);
}

function setTheme(id) {
  const known = THEMES.some((theme) => theme.id === id) ? id : "amber";
  document.documentElement.dataset.theme = known;
  try { localStorage.setItem("wxedge-theme", known); } catch (err) { /* ignore */ }
  closeThemeMenu();
  if (state.overview) render();
}

function initTheme() {
  const button = $("#themeBtn");
  if (!button) return;
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    if ($("#themeMenu")) closeThemeMenu(); else openThemeMenu();
  });
}

function cssVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function hexToRgba(value, alpha) {
  let hex = String(value || "").trim();
  if (!hex.startsWith("#")) return value;
  hex = hex.slice(1);
  if (hex.length === 3) hex = hex.split("").map((ch) => ch + ch).join("");
  if (hex.length !== 6) return value;
  const num = parseInt(hex, 16);
  return "rgba(" + ((num >> 16) & 255) + ", " + ((num >> 8) & 255) + ", " + (num & 255) + ", " + alpha + ")";
}

async function loadStoragePaths(force) {
  if (state.storagePaths && !force) return state.storagePaths;
  try {
    state.storagePaths = await api("/api/storage-paths");
  } catch (err) {
    state.storagePaths = null;
  }
  syncStorageDatalist();
  const panel = $("#storagePathsInfo");
  if (panel) panel.innerHTML = storagePathRowsHtml();
  return state.storagePaths;
}

const STORAGE_SOURCE_LABELS = { mount: "挂载点", common: "常用目录", settings: "当前设置" };

function storagePathRowsHtml() {
  const data = state.storagePaths;
  if (!data || !data.paths || !data.paths.length) {
    return `<div class="empty-line"><span class="led is-off"></span><span>检测中…</span></div>`;
  }
  return data.paths.slice(0, 8).map((item) => `
    <div class="info-row">
      <span class="k mono" title="${esc(item.path)}">${esc(item.path)} <em class="path-src">${STORAGE_SOURCE_LABELS[item.source] || "目录"}</em>${item.recommended ? ` <em class="path-badge">推荐</em>` : ""}</span>
      <span class="v mono">${item.writable ? "可写" : "只读"} · 剩余 ${fmtBytes(item.free_bytes)}</span>
    </div>`).join("");
}

let storageSuggestKeyBound = false;
let storageSuggestInput = null;
let storageSuggestActive = -1;

function closeStorageSuggest() {
  const panel = document.getElementById("storageSuggest");
  if (panel) panel.remove();
  storageSuggestInput = null;
  storageSuggestActive = -1;
}

function highlightStorageOption(delta) {
  const panel = document.getElementById("storageSuggest");
  if (!panel) return;
  const options = Array.from(panel.querySelectorAll(".dir-option"));
  if (!options.length) return;
  storageSuggestActive = Math.max(0, Math.min(options.length - 1, storageSuggestActive + delta));
  options.forEach((option, index) => option.classList.toggle("is-active", index === storageSuggestActive));
  options[storageSuggestActive].scrollIntoView({ block: "nearest" });
}

function positionStorageSuggest(input) {
  const panel = document.getElementById("storageSuggest");
  if (!panel) return;
  const rect = input.getBoundingClientRect();
  panel.style.left = Math.max(8, rect.left) + "px";
  panel.style.top = (rect.bottom + 4) + "px";
  panel.style.width = Math.max(300, rect.width) + "px";
  const maxBottom = window.innerHeight - 10;
  if (rect.bottom + panel.offsetHeight > maxBottom) {
    panel.style.top = Math.max(8, rect.top - panel.offsetHeight - 4) + "px";
  }
}

function openStorageSuggest(input) {
  closeStorageSuggest();
  const data = state.storagePaths;
  if (!data || !data.paths || !data.paths.length) return;
  const panel = document.createElement("div");
  panel.id = "storageSuggest";
  panel.className = "dir-suggest";
  panel.setAttribute("role", "listbox");
  panel.innerHTML = data.paths.slice(0, 10).map((item) => `
    <button type="button" class="dir-option ${item.recommended ? "is-rec" : ""}" role="option" data-path="${esc(item.path)}">
      <span class="dir-path mono">${esc(item.path)}</span>
      <span class="dir-meta">${STORAGE_SOURCE_LABELS[item.source] || "目录"} · ${item.writable ? "可写" : "只读"} · 剩余 ${fmtBytes(item.free_bytes)}${item.recommended ? " · 推荐" : ""}</span>
    </button>`).join("") + `<div class="dir-foot">来源：Linux 挂载表 + 常用目录 + 当前设置</div>`;
  document.body.appendChild(panel);
  storageSuggestInput = input;
  storageSuggestActive = -1;
  positionStorageSuggest(input);
  panel.addEventListener("wheel", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const maxScroll = panel.scrollHeight - panel.clientHeight;
    if (maxScroll > 0) panel.scrollTop = Math.max(0, Math.min(maxScroll, panel.scrollTop + event.deltaY));
  }, { passive: false });
  panel.addEventListener("mousedown", (event) => event.preventDefault());
  panel.addEventListener("click", (event) => {
    const option = event.target.closest("[data-path]");
    if (!option) return;
    input.value = option.dataset.path;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    closeStorageSuggest();
    input.focus();
  });
}

function bindStorageInput(input) {
  if (!input || input.dataset.dirBound) return;
  input.dataset.dirBound = "1";
  input.setAttribute("autocomplete", "off");
  input.removeAttribute("list");
  input.addEventListener("focus", () => openStorageSuggest(input));
  input.addEventListener("click", () => openStorageSuggest(input));
  input.addEventListener("keydown", (event) => {
    const panel = document.getElementById("storageSuggest");
    if (!panel) { if (event.key === "ArrowDown") openStorageSuggest(input); return; }
    if (event.key === "ArrowDown") { event.preventDefault(); highlightStorageOption(1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); highlightStorageOption(-1); }
    else if (event.key === "Enter") { const active = panel.querySelector(".dir-option.is-active"); if (active) { event.preventDefault(); active.click(); } }
    else if (event.key === "Escape") { closeStorageSuggest(); }
  });
  input.addEventListener("input", () => {
    const panel = document.getElementById("storageSuggest");
    if (panel) positionStorageSuggest(input);
  });
  if (!storageSuggestKeyBound) {
    storageSuggestKeyBound = true;
    document.addEventListener("mousedown", (event) => {
      if (!event.target.closest("#storageSuggest") && !event.target.closest("[data-dir-bound]")) closeStorageSuggest();
    });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeStorageSuggest(); });
    window.addEventListener("scroll", (event) => {
      const target = event.target;
      if (target && typeof target.closest === "function" && target.closest("#storageSuggest")) return;
      if (!storageSuggestInput) return;
      const rect = storageSuggestInput.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > window.innerHeight) { closeStorageSuggest(); return; }
      positionStorageSuggest(storageSuggestInput);
    }, true);
    window.addEventListener("resize", closeStorageSuggest);
  }
}

function syncStorageDatalist() {
  const paths = (state.storagePaths && state.storagePaths.paths) || [];
  const ids = ["#f_data_dir", "#f_runtime_dir", "#s_storage_root", "#d_storage"];
  ids.forEach((selector) => {
    const input = document.querySelector(selector);
    if (!input) return;
    bindStorageInput(input);
    const hint = input.closest(".field") ? input.closest(".field").querySelector(".hint") : null;
    if (hint) {
      if (!hint.dataset.baseHint) hint.dataset.baseHint = hint.textContent;
      const platform = (state.storagePaths && state.storagePaths.platform) || "";
      const platformLabel = platform.indexOf("linux") === 0 ? "Linux 已自动检测挂载点" : (platform === "win32" ? "已自动检测磁盘目录" : "已自动检测可用目录");
      hint.textContent = hint.dataset.baseHint + " · " + platformLabel + "（" + paths.length + " 个）";
    }
  });
  const recommended = state.storagePaths && state.storagePaths.recommended;
  const dataInput = document.querySelector("#f_data_dir");
  if (recommended && dataInput && !dataInput.value && !dataInput.placeholder) {
    dataInput.placeholder = recommended.path + "/wx-xx";
  }
}

async function refreshStoragePaths() {
  await loadStoragePaths(true);
  if (state.view === "settings") render();
  else syncStorageDatalist();
  toast("存储目录已重新检测", "ok", 1800);
}

const icon = (name, cls = "") => `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

function esc(value) {
  return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function fmtBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let value = n;
  for (let i = 0; i < units.length; i += 1) {
    value /= 1024;
    if (value < 1024 || i === units.length - 1) {
      return value.toFixed(value >= 100 ? 0 : 1) + " " + units[i];
    }
  }
  return "0 B";
}
const fmtMB = (mb) => {
  const value = Number(mb) || 0;
  if (value <= 0) return "0 MB";
  return fmtBytes(value * 1024 * 1024);
};

function fmtDuration(seconds) {
  const sec = Math.max(0, Math.floor(Number(seconds) || 0));
  if (sec < 60) return sec + " 秒";
  if (sec < 3600) return Math.floor(sec / 60) + " 分钟";
  if (sec < 86400) return Math.floor(sec / 3600) + " 小时 " + Math.floor((sec % 3600) / 60) + " 分";
  return Math.floor(sec / 86400) + " 天 " + Math.floor((sec % 86400) / 3600) + " 小时";
}

function timeAgo(ts) {
  if (!ts) return "—";
  const diff = Math.max(0, Date.now() / 1000 - Number(ts));
  if (diff < 10) return "刚刚";
  if (diff < 60) return Math.floor(diff) + " 秒前";
  if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
  const date = new Date(Number(ts) * 1000);
  return (date.getMonth() + 1) + " 月 " + date.getDate() + " 日";
}

async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: {} };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  if (state.authToken) init.headers["X-Auth-Token"] = state.authToken;
  const response = await fetch(path, init);
  let data = null;
  try { data = await response.json(); } catch (err) { data = null; }
  if (!response.ok) {
    const error = new Error((data && data.error) || ("请求失败（HTTP " + response.status + "）"));
    error.status = response.status;
    throw error;
  }
  return data;
}

function toast(message, type = "info", timeout = 3800) {
  const root = $("#toastRoot");
  const item = document.createElement("div");
  item.className = "toast " + type;
  const iconName = type === "ok" ? "check" : (type === "err" ? "alert" : (type === "warn" ? "alert" : "info"));
  item.innerHTML = icon(iconName) + "<div>" + esc(message) + "</div>";
  root.appendChild(item);
  setTimeout(() => {
    item.style.opacity = "0";
    item.style.transform = "translateX(12px)";
    item.style.transition = "all .2s ease";
    setTimeout(() => item.remove(), 220);
  }, timeout);
}

function skeletonHtml() {
  return `<div class="skeleton-grid"><div class="skeleton-card"></div><div class="skeleton-card"></div><div class="skeleton-card"></div><div class="skeleton-card"></div></div>`;
}

/* ---------------- 顶部与侧栏 ---------------- */
function applyChrome() {
  const o = state.overview;
  if (!o) return;
  const backend = o.backend || {};
  const pill = $("#backendPill");
  let pillClass = "is-err";
  let pillText = "Docker 不可用";
  if (backend.mode === "docker" && backend.available) {
    pillClass = "is-ok";
    pillText = "Docker 已连接";
  } else if (backend.mode === "sim") {
    pillClass = "is-sim";
    pillText = "模拟模式";
  }
  pill.className = "backend-pill " + pillClass;
  pill.innerHTML = `<span class="dot"></span><span>${esc(pillText)}</span>`;

  $("#versionText").textContent = "v" + (o.version || "1.0.0");
  $("#hostName").textContent = (o.host && o.host.os && o.host.os.hostname) || "—";
  $("#navCount").textContent = o.totals.count ? String(o.totals.count) : "";

  const host = o.host;
  const mem = host.mem;
  const disk = host.data_disk || { total_bytes: 0, free_bytes: 0, used_bytes: 0 };
  $("#hostChips").innerHTML =
    `<span class="host-chip">${icon("cpu")}CPU <b>${host.cpu.usage_pct}%</b></span>` +
    `<span class="host-chip">${icon("memory")}内存 <b>${mem.used_pct}%</b></span>` +
    `<span class="host-chip">${icon("disk")}可用 <b>${fmtBytes(disk.free_bytes)}</b></span>`;

  const viewMeta = $("#viewMeta");
  if (viewMeta) viewMeta.textContent = "LIVE TELEMETRY // " + (o.settings.poll_seconds || 4) + "s";
  const history = state.history;
  history.cpu.push(Number(host.cpu.usage_pct) || 0);
  history.mem.push(Number(mem.used_pct) || 0);
  if (history.cpu.length > 40) history.cpu.shift();
  if (history.mem.length > 40) history.mem.shift();
  history.at = Date.now();
}

function renderChromeOnly() {
  applyChrome();
  if (state.view === "overview") drawLoadChart();
}

/* ---------------- 导航与渲染 ---------------- */
function navigate(view) {
  if (!VIEW_META[view]) return;
  state.view = view;
  const sidebar = $("#sidebar");
  if (sidebar) sidebar.classList.remove("is-open");
  render();
}

function render() {
  const meta = VIEW_META[state.view];
  $("#pageTitle").textContent = meta.title;
  $("#pageSub").textContent = meta.sub;
  $$("#nav .nav-item").forEach((el) => el.classList.toggle("is-active", el.dataset.view === state.view));
  const view = $("#view");
  if (!state.overview) {
    view.innerHTML = skeletonHtml();
    return;
  }
  if (state.view === "overview") {
    view.innerHTML = overviewHtml();
    drawLoadChart();
  } else if (state.view === "instances") {
    view.innerHTML = instancesHtml();
  } else if (state.view === "deploy") {
    view.innerHTML = deployHtml();
    if (state.plan) renderPlanPanel(); else generatePlan();
  } else if (state.view === "check") {
    view.innerHTML = checkHtml();
  } else if (state.view === "settings") {
    view.innerHTML = settingsHtml();
  }
  syncStorageDatalist();
}

function startPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  const seconds = Math.max(2, Number((state.overview && state.overview.settings && state.overview.settings.poll_seconds) || 4));
  state.pollTimer = setInterval(refreshSilent, seconds * 1000);
}

async function refreshSilent() {
  if (document.hidden || state.refreshing) return;
  state.refreshing = true;
  try {
    const overview = await api("/api/overview");
    state.overview = overview;
    await loadStoragePaths();
    applyChrome();
    startPollingIfChanged(overview);
    const active = document.activeElement;
    const editing = active && active.closest && active.closest("#view input, #view textarea, #view select");
    if (!editing && (state.view === "overview" || state.view === "instances")) {
      if (state.view === "overview") { $("#view").innerHTML = overviewHtml(); drawLoadChart(); }
      else { const list = $("#instanceList"); if (list) list.innerHTML = renderInstanceList(); }
    } else if (state.view === "overview") {
      applyChrome();
      drawLoadChart();
    }
  } catch (err) {
    if (err.status === 401) { showAuth(); }
  } finally {
    state.refreshing = false;
  }
}

let lastPollSeconds = null;
function startPollingIfChanged(overview) {
  const seconds = Number((overview.settings && overview.settings.poll_seconds) || 4);
  if (lastPollSeconds !== seconds) {
    lastPollSeconds = seconds;
    startPolling();
  }
}

async function manualRefresh(button) {
  if (button) { button.classList.add("spin"); button.disabled = true; }
  try {
    state.overview = await api("/api/overview");
    applyChrome();
    render();
    toast("数据已刷新", "ok", 1800);
  } catch (err) {
    toast(err.message, "err");
  } finally {
    if (button) { button.classList.remove("spin"); button.disabled = false; }
  }
}

/* ---------------- 图表 ---------------- */
function drawLoadChart() {
  const canvas = $("#loadChart");
  if (!canvas) return;
  const width = canvas.clientWidth || 560;
  const height = 180;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(width * dpr));
  canvas.height = Math.floor(height * dpr);
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const pad = { top: 10, right: 8, bottom: 18, left: 28 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  ctx.strokeStyle = cssVar("--grid-line", "rgba(233, 229, 218, .07)");
  ctx.lineWidth = 1;
  ctx.font = "9px Consolas, monospace";
  ctx.fillStyle = cssVar("--dim", "rgba(152, 162, 156, .5)");
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i += 1) {
    const y = pad.top + (plotH / 4) * i;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    ctx.fillText(String(100 - i * 25), pad.left - 6, y);
  }
  const cpuColor = cssVar("--amber", "#ffb000");
  const memColor = cssVar("--sky", "#7cc7ff");
  const series = [
    { data: state.history.cpu, color: cpuColor, fill: hexToRgba(cpuColor, .14) },
    { data: state.history.mem, color: memColor, fill: hexToRgba(memColor, .10) },
  ];
  const total = 40;
  const drawSeries = (data, color, fill) => {
    if (!data.length) return;
    const points = [];
    for (let i = 0; i < total; i += 1) {
      const srcIndex = Math.max(0, data.length - total + i);
      const value = data[srcIndex] || 0;
      const x = pad.left + (plotW / (total - 1)) * i;
      const y = pad.top + plotH - (Math.min(100, value) / 100) * plotH;
      points.push([x, y]);
    }
    const gradient = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom);
    gradient.addColorStop(0, fill);
    gradient.addColorStop(1, "rgba(0,0,0,0)");
    ctx.beginPath();
    ctx.moveTo(points[0][0], height - pad.bottom);
    points.forEach((pt) => ctx.lineTo(pt[0], pt[1]));
    ctx.lineTo(points[points.length - 1][0], height - pad.bottom);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.beginPath();
    points.forEach((pt, i) => { if (i === 0) ctx.moveTo(pt[0], pt[1]); else ctx.lineTo(pt[0], pt[1]); });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = "round";
    ctx.stroke();
    const last = points[points.length - 1];
    ctx.beginPath();
    ctx.arc(last[0], last[1], 3, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
  };
  drawSeries(series[0].data, series[0].color, series[0].fill);
  drawSeries(series[1].data, series[1].color, series[1].fill);
}

/* ---------------- 通用片段 ---------------- */
function backendBadgeHtml(backend) {
  if (backend.mode === "docker" && backend.available) {
    return `<span class="badge badge-ok"><span class="dot"></span>Docker ${esc(backend.version || "")}</span>`;
  }
  if (backend.mode === "sim") return `<span class="badge badge-warn"><span class="dot"></span>模拟模式</span>`;
  return `<span class="badge badge-err"><span class="dot"></span>Docker 不可用</span>`;
}

function stateInfo(inst) {
  const rt = inst.runtime || {};
  const st = rt.state || "missing";
  if (st === "running") return { cls: "badge-ok", label: "运行中", dot: true };
  if (st === "restarting") return { cls: "badge-warn", label: "重启中", dot: true };
  if (st === "created") return { cls: "badge-info", label: "已创建", dot: true };
  if (st === "paused") return { cls: "badge-warn", label: "已暂停", dot: true };
  if (st === "exited") return { cls: "badge-off", label: "已停止", dot: false };
  if (st === "dead") return { cls: "badge-err", label: "异常", dot: true };
  if (inst.desired_state === "running") return { cls: "badge-err", label: "未创建", dot: true };
  return { cls: "badge-off", label: "未创建", dot: false };
}

function activityHtml(list) {
  if (!list || !list.length) {
    return `<div class="empty-line"><span class="led is-off"></span><span>暂无事件 · 创建节点后开始记录</span></div>`;
  }
  const levelClass = { success: "run", warn: "warn", error: "stop", info: "info" };
  return `<ol class="event-list">` + list.slice(0, 7).map((item) => {
    const level = item.level || "info";
    return `<li class="event-row"><span class="event-time mono">${timeAgo(item.ts)}</span><span class="led is-${levelClass[level] || "info"}"></span><span class="event-text">${esc(item.message)}</span></li>`;
  }).join("") + `</ol>`;
}

/* ---------------- 概览 ---------------- */
function meterHtml(label, valueText, pct, cls, sub) {
  const width = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="gauge ${cls}"><div class="gauge-head"><span class="gauge-label">${label}</span><span class="gauge-value">${valueText}</span></div><div class="gauge-track"><div class="gauge-fill" style="width:${width}%"></div></div>${sub ? `<div class="gauge-sub">${sub}</div>` : ""}</div>`;
}

function overviewHtml() {
  const o = state.overview;
  const t = o.totals;
  const host = o.host;
  const settings = o.settings;
  const backend = o.backend;
  const disk = host.data_disk || { total_bytes: 0, free_bytes: 0, used_bytes: 0 };
  const diskPct = disk.total_bytes ? Math.round((disk.used_bytes / disk.total_bytes) * 100) : 0;
  const cpuTotal = host.cpu.count;
  const memTotal = host.mem.total_bytes;
  const allocCpuPct = cpuTotal ? Math.round((t.cpus / cpuTotal) * 100) : 0;
  const allocMemPct = memTotal ? Math.round((t.memory_mb * 1024 * 1024 / memTotal) * 100) : 0;
  const alert = backend.warning || backend.error || "";
  const alertHtml = alert
    ? `<div class="alert-line"><span class="led is-warn"></span><span class="alert-tag">WARN</span><span class="alert-text">${esc(alert)}</span></div>`
    : "";
  const readouts = [
    { label: "FLEET", value: t.count, unit: "节点", foot: `运行 ${t.running} · 停止 ${t.stopped}`, pct: t.count ? Math.round((t.running / t.count) * 100) : 0 },
    { label: "ONLINE", value: t.running, unit: `/ ${t.count}`, foot: t.abnormal ? `${t.abnormal} 个待自愈` : "全部在线", pct: t.count ? Math.round((t.running / t.count) * 100) : 0 },
    { label: "CPU ALLOC", value: t.cpus, unit: "核", foot: `物理 ${cpuTotal} 核 · ${allocCpuPct}%`, pct: allocCpuPct },
    { label: "MEM ALLOC", value: fmtMB(t.memory_mb), unit: "", foot: `物理 ${fmtBytes(memTotal)} · ${allocMemPct}%`, pct: allocMemPct },
  ];
  const readoutHtml = readouts.map((item) => `
    <div class="readout">
      <div class="readout-label">${item.label}</div>
      <div class="readout-value">${esc(String(item.value))}<span class="readout-unit">${item.unit}</span></div>
      <div class="readout-track"><div class="readout-fill" style="width:${Math.max(0, Math.min(100, item.pct))}%"></div></div>
      <div class="readout-foot">${esc(item.foot)}</div>
    </div>`).join("");

  const bootPanel = !o.instances.length ? `
    <section class="panel boot-panel">
      <header class="panel-head"><div><span class="panel-kicker">00 / BOOT SEQUENCE</span><h2 class="panel-title">还没有节点</h2></div><span class="panel-meta">3 STEPS TO ONLINE</span></header>
      <div class="boot-steps">
        <div class="boot-step"><span class="boot-num">01</span><div><strong>环境自检</strong><p>确认 Docker、镜像、存储目录与权限就绪。</p><button class="text-btn" data-action="goto" data-view="check">开始体检 ${icon("chevron")}</button></div></div>
        <div class="boot-step"><span class="boot-num">02</span><div><strong>自动规划</strong><p>按主机 CPU / 内存 / 数据盘生成分配方案。</p><button class="text-btn" data-action="goto" data-view="deploy">生成方案 ${icon("chevron")}</button></div></div>
        <div class="boot-step"><span class="boot-num">03</span><div><strong>创建节点</strong><p>创建后打开激活入口，用网心云 App 扫码绑定。</p><button class="text-btn" data-action="new-instance">立即创建 ${icon("chevron")}</button></div></div>
      </div>
    </section>` : `
    <section class="panel quick-panel">
      <header class="panel-head"><div><span class="panel-kicker">02 / QUICK ACCESS</span><h2 class="panel-title">机群操作</h2></div><div class="panel-tools"><button class="text-btn" data-action="goto" data-view="instances">管理全部节点 ${icon("chevron")}</button></div></header>
      <div class="quick-strip">
        <button class="quick-cell" data-action="goto" data-view="instances"><span class="quick-code">NODES</span><b>${t.count}</b><span>查看与操作节点</span></button>
        <button class="quick-cell" data-action="goto" data-view="deploy"><span class="quick-code">DEPLOY</span><b>${t.running}<small> / ${t.count}</small></b><span>批量生成分配方案</span></button>
        <button class="quick-cell" data-action="goto" data-view="check"><span class="quick-code">HEALTH</span><b>${t.abnormal}</b><span>待自愈 / 去自检</span></button>
        <button class="quick-cell" data-action="new-instance"><span class="quick-code">NEW</span><b>+</b><span>创建单个节点</span></button>
      </div>
    </section>`;

  return `
  ${alertHtml}
  <div class="readout-bar">${readoutHtml}</div>
  <div class="deck-grid">
    <section class="panel telemetry-panel">
      <header class="panel-head"><div><span class="panel-kicker">01 / HOST TELEMETRY</span><h2 class="panel-title">主机遥测</h2></div><div class="panel-tools"><span class="live-tag"><span class="led is-run"></span>LIVE · ${settings.poll_seconds || 4}s</span></div></header>
      <div class="chart-box"><canvas id="loadChart" height="180"></canvas></div>
      <div class="chart-legend"><span><i class="swatch swatch-cpu"></i>CPU UTIL</span><span><i class="swatch swatch-mem"></i>MEM UTIL</span><span class="chart-scale">SCALE 0—100%</span></div>
      <div class="gauge-stack">
        ${meterHtml("CPU", (host.cpu.usage_pct || 0) + "%", host.cpu.usage_pct, "", "已分配 " + t.cpus + " / " + cpuTotal + " 核")}
        ${meterHtml("MEM", fmtBytes(host.mem.used_bytes) + " / " + fmtBytes(host.mem.total_bytes), host.mem.used_pct, "mem", "已分配 " + fmtMB(t.memory_mb))}
        ${meterHtml("DISK", fmtBytes(disk.used_bytes) + " / " + fmtBytes(disk.total_bytes), diskPct, "disk", "剩余 " + fmtBytes(disk.free_bytes) + " · " + esc(String(disk.mount || "")))}
      </div>
    </section>
    <section class="panel event-panel">
      <header class="panel-head"><div><span class="panel-kicker">03 / EVENT LOG</span><h2 class="panel-title">操作事件流</h2></div><div class="panel-tools"><button class="text-btn" data-action="goto" data-view="check">自检 ${icon("chevron")}</button></div></header>
      ${activityHtml(o.activity)}
    </section>
  </div>
  ${bootPanel}
  `;
}

function activationUrl(inst) {
  const hostname = window.location.hostname || "127.0.0.1";
  return "http://" + hostname + ":" + inst.listen_port;
}

/* ---------------- 实例列表 ---------------- */
function filteredInstances() {
  const list = (state.overview && state.overview.instances) || [];
  const query = state.query.trim().toLowerCase();
  return list.filter((inst) => {
    const rt = inst.runtime || {};
    if (state.filter === "running" && rt.state !== "running") return false;
    if (state.filter === "stopped" && ["running", "restarting"].includes(rt.state)) return false;
    if (state.filter === "issue" && !(inst.desired_state === "running" && rt.state !== "running")) return false;
    if (!query) return true;
    return [inst.name, inst.container_name, inst.id, inst.data_dir, String(inst.listen_port)].join(" ").toLowerCase().includes(query);
  });
}

function clampPct(value, max) {
  const num = Number(value);
  if (!Number.isFinite(num)) return 0;
  return Math.max(0, Math.min(max, Math.round(num)));
}

function miniMeter(label, display, pct, cls) {
  return `<div class="mini-gauge"><span class="mini-label">${label}</span><span class="mini-track"><i class="${cls}" style="width:${clampPct(pct, 100)}%"></i></span><span class="mini-val mono">${display}</span></div>`;
}

function instanceCardHtml(inst) {
  const rt = inst.runtime || {};
  const info = stateInfo(inst);
  const running = rt.state === "running";
  const cpuPct = Math.max(0, Math.min(100, Number(rt.cpu_pct) || 0));
  const allocCpu = Math.max(1, Number(inst.cpus) || 1);
  const cpuBar = Math.min(100, Math.round((cpuPct / (allocCpu * 100)) * 100));
  const memBytes = Number(rt.mem_usage_bytes) || 0;
  const memLimit = Math.max(1, Number(inst.memory_mb) || 1) * 1024 * 1024;
  const memPct = Math.max(0, Math.min(100, Math.round((memBytes / memLimit) * 100)));
  const diskUsed = Number(rt.disk_used_bytes) || 0;
  const diskLimit = Number(rt.disk_limit_bytes) || Math.max(1, Number(inst.disk_gb) || 1) * 1024 * 1024 * 1024;
  const diskPct = Math.max(0, Math.min(100, Math.round((diskUsed / diskLimit) * 100)));
  const diskTag = inst.disk_mode === "loop" ? "硬限制" : (inst.quota_action === "stop" ? "超限停止" : "监控");
  const diskUnmounted = inst.disk_mode === "loop" && running && rt.disk_mounted === false;
  const uptime = running && rt.uptime_seconds ? `UP ${fmtDuration(rt.uptime_seconds)}` : (inst.desired_state === "running" ? "AUTO" : "HOLD");
  const endpoint = inst.network_mode === "host"
    ? `<a class="node-port mono" href="${activationUrl(inst)}" target="_blank" rel="noreferrer" title="打开激活页">:${inst.listen_port}<span class="port-caret">↗</span></a>`
    : `<span class="node-port mono">${esc(inst.macvlan_ip || "macvlan")}</span>`;
  return `
  <div class="node-row">
    <div class="node-ident">
      <div class="node-name-row">
        <span class="led ${running ? "is-run" : "is-off"}"></span>
        <span class="node-name" title="${esc(inst.name)}">${esc(inst.name)}</span>
        ${inst.enabled ? "" : `<span class="tag-inline">NO-HEAL</span>`}
      </div>
      <div class="node-sub mono" title="${esc(inst.data_dir)}">${esc(inst.container_name)} · ${esc(inst.id)} · ${inst.network_mode === "host" ? "HOST" : "MACVLAN"}</div>
    </div>
    <div class="node-state">
      <span class="badge ${info.cls}">${info.label}</span>
      <span class="node-uptime mono">${uptime}</span>
    </div>
    <div class="node-specs">
      <span><b>${inst.cpus}</b> 核</span>
      <span><b>${fmtMB(inst.memory_mb)}</b></span>
      <span><b>${inst.disk_gb} GB</b><small>${diskTag}</small></span>
    </div>
    <div class="node-loads">
      ${miniMeter("CPU", cpuPct + "%", cpuBar, "")}
      ${miniMeter("MEM", fmtBytes(memBytes), memPct, "mem")}
      ${miniMeter("DSK", fmtBytes(diskUsed), diskPct, "disk")}
      ${diskUnmounted ? `<span class="rack-flag is-warn">存储未挂载</span>` : ""}
    </div>
    <div class="node-endpoint">${endpoint}</div>
    <div class="node-actions">
      ${running
        ? `<button class="act-btn" data-action="stop" data-id="${inst.id}" title="停止">${icon("stop")}<span>停止</span></button>`
        : `<button class="act-btn act-go" data-action="start" data-id="${inst.id}" title="启动">${icon("play")}<span>启动</span></button>`}
      <button class="act-btn" data-action="restart" data-id="${inst.id}" title="重启">${icon("rotate")}<span>重启</span></button>
      <button class="act-btn" data-action="recreate" data-id="${inst.id}" title="重建容器">${icon("layers")}</button>
      <button class="act-btn" data-action="logs" data-id="${inst.id}" title="查看日志" aria-label="查看日志">${icon("terminal")}</button>
      <button class="act-btn" data-action="edit" data-id="${inst.id}" title="编辑配置" aria-label="编辑配置">${icon("edit")}</button>
      <button class="act-btn act-danger" data-action="delete" data-id="${inst.id}" title="删除实例" aria-label="删除实例">${icon("trash")}</button>
    </div>
  </div>`;
}

function restartPolicyLabel(policy) {
  if (policy === "unless-stopped") return "unless-stopped";
  if (policy === "no") return "不自动重启";
  return "always";
}

function renderInstanceList() {
  const list = filteredInstances();
  if (!list.length) {
    const hasAny = (state.overview.instances || []).length > 0;
    return `
    <div class="empty-state">
      <span class="empty-code">${hasAny ? "NO MATCH" : "NO NODES"}</span>
      <h3>${hasAny ? "没有匹配的节点" : "节点列表为空"}</h3>
      <p>${hasAny ? "调整搜索关键词或状态筛选，或清除筛选条件。" : "从一台节点开始，或让系统按主机容量自动规划。"}</p>
      <div class="empty-actions">
        ${hasAny ? "" : `<button class="btn btn-primary" data-action="new-instance">${icon("plus")}新建节点</button>`}
        <button class="btn btn-line" data-action="${hasAny ? "refresh" : "goto"}" ${hasAny ? "" : `data-view="deploy"`}>${icon(hasAny ? "refresh" : "layers")}${hasAny ? "刷新" : "批量部署"}</button>
      </div>
    </div>`;
  }
  return list.map(instanceCardHtml).join("");
}

function instancesHtml() {
  const filters = [["all", "全部"], ["running", "运行中"], ["stopped", "已停止"], ["issue", "需关注"]];
  return `
  <div class="toolbar">
    <div class="toolbar-left">
      <div class="search-box">${icon("search")}<input id="searchInput" placeholder="搜索节点 / 容器 / 端口" value="${esc(state.query)}" autocomplete="off"></div>
      <div class="seg">${filters.map((item) => `<button class="${state.filter === item[0] ? "is-active" : ""}" data-action="filter" data-filter="${item[0]}">${item[1]}</button>`).join("")}</div>
    </div>
    <div class="toolbar-right">
      <button class="btn btn-line btn-sm" data-action="refresh">${icon("refresh")}刷新</button>
      <button class="btn btn-line btn-sm" data-action="goto" data-view="deploy">${icon("layers")}批量部署</button>
      <button class="btn btn-primary btn-sm" data-action="new-instance">${icon("plus")}新建节点</button>
    </div>
  </div>
  <div class="board-note"><span class="board-note-key">NOTE</span><span>编辑资源将重建容器；数据目录与 wxnode 保留。删除默认保留数据，可在确认框中勾选彻底删除。</span></div>
  <section class="panel node-panel">
    <div class="node-head">
      <span>节点</span><span>状态</span><span>资源配额</span><span>实时负载</span><span>端点</span><span>操作</span>
    </div>
    <div id="instanceList">${renderInstanceList()}</div>
  </section>
  `;
}
/* ================= WXEdge 魔方管理器 · 界面与交互 ================= */

state.deployOpts = null;
state.previewSeq = 0;
state.suggestPort = null;
state.logId = null;

function debounce(fn, delay) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

async function withBusy(el, fn) {
  if (!el) return fn();
  const previous = el.innerHTML;
  el.disabled = true;
  el.innerHTML = `<span class="spinner"></span>处理中`;
  try { return await fn(); }
  finally {
    if (el.isConnected) { el.disabled = false; el.innerHTML = previous; }
  }
}

function field(label, control, hint = "", full = false) {
  return `<div class="field ${full ? "field-full" : ""}"><label class="label">${label}</label>${control}${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
}

function infoRow(key, value) {
  return `<div class="info-row"><span class="k">${key}</span><span class="v">${esc(value)}</span></div>`;
}

function optionHtml(value, label, selected) {
  return `<option value="${esc(value)}" ${selected ? "selected" : ""}>${esc(label)}</option>`;
}

function loadingCard(text) {
  return `<div class="card"><div class="empty" style="padding:46px 16px"><span class="spinner" style="width:22px;height:22px"></span><p style="margin:14px 0 0">${esc(text)}</p></div></div>`;
}

function errorCard(text) {
  return `<div class="card"><div class="error-box">${icon("alert")}<div>${esc(text)}</div></div></div>`;
}

async function refreshNow() {
  try {
    state.overview = await api("/api/overview");
    applyChrome();
    render();
  } catch (err) {
    if (err.status === 401) showAuth();
    else toast(err.message, "err");
  }
}

/* ---------------- 批量部署 ---------------- */
function deployDefaults() {
  const o = state.overview;
  const s = o.settings;
  const cores = Number(o.host.cpu.count) || 4;
  return {
    count: Math.max(1, Math.min(4, Math.floor(cores / 2))),
    strategy: "balanced",
    storage_root: s.storage_root,
    image: s.image,
    port_base: s.port_base,
    port_end: "",
    cpus: "",
    memory_mb: "",
    disk_gb: "",
    name_prefix: s.name_prefix,
    disk_mode: s.disk_mode,
  };
}

function deployHtml() {
  const o = state.overview;
  const s = o.settings;
  if (!state.deployOpts) state.deployOpts = deployDefaults();
  const d = state.deployOpts;
  const strategies = [["balanced", "均衡分配（推荐）"], ["performance", "性能优先"], ["eco", "省资源优先"]];
  return `
  <div class="info-box section">${icon("zap")}<div>自动配置会按主机可用 CPU、内存和数据盘空间计算每实例配额，并自动分配独立端口与存储目录。先预览方案再一键创建；创建过程串行执行，不会覆盖已有实例。</div></div>
  <div class="grid-2" style="grid-template-columns:minmax(320px,390px) 1fr;align-items:start">
    <form class="card" id="deployForm">
      <div class="card-head"><div><div class="card-title">分配参数</div><div class="card-sub">修改后点击生成方案</div></div></div>
      <div class="form-grid one">
        ${field("实例数量", `<input class="input" id="d_count" type="number" min="1" value="${d.count}">`, "至少 1 个，实际数量受主机资源与可用端口约束")}
        ${field("分配策略", `<select class="select" id="d_strategy">${strategies.map((item) => optionHtml(item[0], item[1], d.strategy === item[0])).join("")}</select>`, "均衡：按比例均分；性能：提高单实例配额；省资源：降低配额")}
        <div class="form-section-title">${icon("cpu")}资源配额（留空则自动分配）</div>
        ${field("CPU 核心数（可选）", `<input class="input" id="d_cpus" type="number" min="0.1" step="0.1" placeholder="自动" value="${esc(d.cpus || "")}">`, "填写后所有实例使用该 CPU 配额")}
        ${field("内存 MB（可选）", `<input class="input" id="d_memory" type="number" min="128" step="128" placeholder="自动" value="${esc(d.memory_mb || "")}">`, "填写后所有实例使用该内存配额，至少 128 MB")}
        ${field("硬盘 GB（可选）", `<input class="input" id="d_disk_gb" type="number" min="1" step="1" placeholder="自动" value="${esc(d.disk_gb || "")}">`, "填写后所有实例使用该硬盘配额，至少 1 GB")}
        <div class="form-section-title">${icon("layers")}端口范围</div>
        ${field("端口起始", `<input class="input" id="d_port" type="number" min="1024" max="65535" value="${d.port_base}">`, "从该端口开始顺序分配空闲端口，对应 LISTEN_ADDR")}
        ${field("端口结束（可选）", `<input class="input" id="d_port_end" type="number" min="1024" max="65535" placeholder="默认 65535" value="${esc(d.port_end || "")}">`, "限定分配范围；留空则最多到 65535")}
        ${field("存储根目录", `<input class="input" id="d_storage" value="${esc(d.storage_root)}">`, "每个实例自动创建 wx-xx 子目录")}
        ${field("镜像地址", `<input class="input" id="d_image" value="${esc(d.image)}">`, "留空则使用系统设置中的镜像")}
        ${field("命名前缀", `<input class="input" id="d_prefix" maxlength="16" value="${esc(d.name_prefix)}">`, "例如「节点」生成 节点01、节点02")}
        ${field("磁盘模式", `<select class="select" id="d_disk_mode">${optionHtml("dir", "目录挂载（软配额）", d.disk_mode === "dir")}${optionHtml("loop", "独立磁盘镜像（ext4 硬配额）", d.disk_mode === "loop")}</select>`, "独立盘需要管理器以 root 运行")}
      </div>
      <div class="form-actions"><button class="btn btn-primary btn-block" type="submit">${icon("zap")}生成分配方案</button></div>
    </form>
    <div id="planPanel">${loadingCard("正在读取主机容量…")}</div>
  </div>`;
}

function readDeployOptions() {
  const get = (id) => { const el = document.getElementById(id); return el ? el.value : ""; };
  const count = Number(get("d_count"));
  if (!Number.isFinite(count) || count < 1) {
    toast("实例数量需为 1 或以上", "err");
    return null;
  }
  const optionalNumber = (id, label, min, integer) => {
    const raw = get(id).trim();
    if (raw === "") return undefined;
    const value = Number(raw);
    if (!Number.isFinite(value) || value < min) {
      toast(label + "不能小于 " + min, "err");
      return null;
    }
    return integer ? Math.round(value) : value;
  };
  const cpus = optionalNumber("d_cpus", "CPU 核心数", 0.1, false);
  const memoryMb = optionalNumber("d_memory", "内存", 128, true);
  const diskGb = optionalNumber("d_disk_gb", "硬盘", 1, true);
  const portBase = optionalNumber("d_port", "端口起始", 1024, true);
  const portEnd = optionalNumber("d_port_end", "端口结束", 1024, true);
  if (cpus === null || memoryMb === null || diskGb === null || portBase === null || portEnd === null) return null;
  if (portEnd !== undefined && portBase !== undefined && portEnd < portBase) {
    toast("端口结束需大于或等于端口起始", "err");
    return null;
  }
  return {
    count: Math.floor(count),
    strategy: get("d_strategy") || "balanced",
    overrides: {
      storage_root: get("d_storage").trim(),
      image: get("d_image").trim(),
      port_base: portBase,
      port_end: portEnd,
      cpus: cpus,
      memory_mb: memoryMb,
      disk_gb: diskGb,
      name_prefix: get("d_prefix").trim() || undefined,
      disk_mode: get("d_disk_mode") || undefined,
    },
  };
}

async function generatePlan() {
  const options = readDeployOptions();
  if (!options) return;
  state.deployOpts = {
    count: options.count,
    strategy: options.strategy,
    storage_root: options.overrides.storage_root,
    image: options.overrides.image,
    port_base: options.overrides.port_base,
    port_end: options.overrides.port_end,
    cpus: options.overrides.cpus,
    memory_mb: options.overrides.memory_mb,
    disk_gb: options.overrides.disk_gb,
    name_prefix: options.overrides.name_prefix,
    disk_mode: options.overrides.disk_mode,
  };
  const seq = ++state.planSeq;
  const panel = $("#planPanel");
  if (panel) panel.innerHTML = loadingCard("正在按主机容量计算资源分配…");
  try {
    const plan = await api("/api/plan", { method: "POST", body: options });
    if (seq !== state.planSeq) return;
    state.plan = plan;
    renderPlanPanel();
  } catch (err) {
    if (seq === state.planSeq && panel) panel.innerHTML = errorCard(err.message);
  }
}

function planPanelHtml() {
  const plan = state.plan;
  const items = plan.items || [];
  const warnings = (plan.warnings || []).map((text) => `<div class="warn-box">${icon("alert")}<div>${esc(text)}</div></div>`).join("");
  const rows = items.map((item) => `
    <tr>
      <td><div class="name-cell"><div class="avatar">${esc(String(item.name).slice(0, 2))}</div><div><div class="name-main">${esc(item.name)}</div><div class="name-sub">${esc(item.data_dir)}</div></div></div></td>
      <td class="cell-num">${item.cpus} 核</td>
      <td class="cell-num">${fmtMB(item.memory_mb)}</td>
      <td class="cell-num">${item.disk_gb} GB</td>
      <td><a class="port-link plan-port" href="${activationUrl(item)}" target="_blank" rel="noreferrer" title="打开 http://${esc(window.location.hostname || "127.0.0.1")}:${item.listen_port}">${item.listen_port}<span class="port-caret">↗</span></a></td>
    </tr>`).join("");
  return `
  <div class="card">
    <div class="card-head"><div><div class="card-title">分配方案预览</div><div class="card-sub">${plan.count} 个实例 · 策略：${esc({balanced:"均衡分配", performance:"性能优先", eco:"省资源"}[plan.strategy] || plan.strategy)} · 主机 ${plan.host.cpu_count} 核 / ${fmtMB(plan.host.memory_mb)} / 可用 ${plan.host.free_gb} GB</div></div><div class="card-actions"><span class="chip">${icon("shield")}创建前不会改动系统</span></div></div>
    ${warnings ? `<div class="box-list" style="margin-bottom:14px">${warnings}</div>` : `<div class="success-box" style="margin-bottom:14px">${icon("check")}<div>资源分配与主机容量匹配，可以创建。</div></div>`}
    <div class="table-wrap"><table class="table"><thead><tr><th>实例 / 存储目录</th><th>CPU</th><th>内存</th><th>硬盘</th><th>端口</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="form-actions" style="justify-content:space-between;margin-top:16px">
      <span class="hint">创建后请到实例卡片点击「激活页」扫码绑定网心云账号。</span>
      <button class="btn btn-primary" data-action="create-batch">${icon("play")}创建这 ${plan.count} 个实例</button>
    </div>
  </div>`;
}

function renderPlanPanel() {
  const panel = $("#planPanel");
  if (panel && state.plan) panel.innerHTML = planPanelHtml();
}

async function createBatch(button) {
  const options = readDeployOptions();
  if (!options) return;
  await withBusy(button, async () => {
    try {
      const result = await api("/api/batch", { method: "POST", body: options });
      state.plan = result.plan;
      const created = result.created ? result.created.length : 0;
      const errors = result.errors || [];
      await refreshNow();
      if (errors.length) {
        openBatchResultModal(result);
      } else {
        toast(`已创建 ${created} 个实例`, "ok");
      }
      state.plan = null;
      state.deployOpts = null;
      navigate("instances");
    } catch (err) {
      toast(err.message, "err");
    }
  });
}

function openBatchResultModal(result) {
  const created = result.created || [];
  const errors = result.errors || [];
  const errorHtml = errors.map((item) => `<div class="error-box">${icon("alert")}<div><b>${esc(item.name || "实例")}</b>：${esc(item.error)}</div></div>`).join("");
  openModal({
    title: "批量创建结果",
    sub: `成功 ${created.length} 个，失败 ${errors.length} 个`,
    body: `<div class="box-list">${errorHtml || `<div class="success-box">${icon("check")}<div>全部实例创建成功。</div></div>`}</div>`,
    footer: `<button class="btn btn-primary" data-action="close-modal">知道了</button>`,
  });
}

/* ---------------- 环境自检 ---------------- */
function checkItemHtml(check) {
  const iconName = check.level === "ok" ? "check" : (check.level === "warn" ? "alert" : "x");
  return `
  <div class="check-item level-${check.level}">
    <div class="check-icon">${icon(iconName)}</div>
    <div style="min-width:0">
      <div class="check-name">${esc(check.name)}<span class="badge ${check.level === "ok" ? "badge-ok" : (check.level === "warn" ? "badge-warn" : "badge-err")}">${check.level === "ok" ? "通过" : (check.level === "warn" ? "注意" : "失败")}</span></div>
      <div class="check-detail">${esc(check.detail)}</div>
      ${check.hint ? `<div class="check-hint">建议：${esc(check.hint)}</div>` : ""}
    </div>
  </div>`;
}

function jobPanelHtml() {
  if (!state.job) return "";
  const job = state.job;
  const lines = (job.lines || []).join("\n") || "等待输出…";
  const statusText = job.status === "running" ? "拉取中…" : (job.status === "done" ? "完成" : "失败");
  return `<div class="hint" style="margin-top:12px;display:flex;justify-content:space-between"><span>镜像：${esc(job.image || "")}</span><span>${statusText}</span></div><pre class="job-log" id="jobLog">${esc(lines)}</pre>${job.error ? `<div class="error-box" style="margin-top:8px">${icon("alert")}<div>${esc(job.error)}</div></div>` : ""}`;
}

function checkHtml() {
  const check = state.check;
  const items = check ? check.checks.map(checkItemHtml).join("") : "";
  return `
  <div class="grid-2" style="grid-template-columns:1.45fr 1fr;align-items:start">
    <div class="card">
      <div class="card-head"><div><div class="card-title">环境自检</div><div class="card-sub">${check ? "上次检查：" + timeAgo(check.at) : "检查 Docker、镜像、存储目录、权限与访问安全"}</div></div><div class="card-actions"><button class="btn btn-primary btn-sm" data-action="run-check">${icon("shield")}开始自检</button></div></div>
      ${check ? `<div class="check-list">${items}</div>` : `<div class="empty" style="padding:48px 16px"><div class="empty-icon">${icon("shield")}</div><h3>先给环境做一次体检</h3><p>检查 Docker 守护进程、网心云镜像、存储写入、loop 设备、root 权限与访问令牌，避免创建实例时才发现问题。</p><button class="btn btn-primary" data-action="run-check">${icon("shield")}立即自检</button></div>`}
    </div>
    <div>
      <div class="card section">
        <div class="card-head"><div><div class="card-title">镜像维护</div><div class="card-sub">${esc(state.overview.settings.image)}</div></div></div>
        <p class="hint">首次部署前建议先拉取镜像；官方镜像更新后重建实例即可生效，wxnode 与数据会保留。</p>
        <button class="btn btn-ghost btn-block" data-action="pull-image">${icon("download")}拉取 / 更新镜像</button>
        <div id="jobPanel">${jobPanelHtml()}</div>
      </div>
      <div class="card">
        <div class="card-head"><div><div class="card-title">运行须知</div></div></div>
        <ul class="hint" style="margin:0;padding-left:18px;line-height:1.9">
          <li>官方容器要求 <b>privileged</b> 与 <b>host 网络</b>；多实例通过不同 <span class="mono">LISTEN_ADDR</span> 端口区分。</li>
          <li>每个实例的 <b>wxnode</b> 位于数据目录 <span class="mono">storage/wxnode</span>，是设备唯一标识，删除实例时默认保留。</li>
          <li>官方建议单实例 ≥ 50 GB 存储空间；独立磁盘镜像模式会创建独立 ext4 卷。</li>
          <li>官方文档说明多开容器<strong>不被推荐</strong>，请自行评估账号与收益规则；本工具不修改任何设备指纹。</li>
        </ul>
      </div>
    </div>
  </div>`;
}

async function runCheck(button) {
  await withBusy(button, async () => {
    try {
      state.check = await api("/api/self-check");
      render();
      toast("自检完成", "ok");
    } catch (err) {
      toast(err.message, "err");
    }
  });
}

async function pullImage(button) {
  await withBusy(button, async () => {
    try {
      const job = await api("/api/pull-image", { method: "POST" });
      state.job = { id: job.id, status: "running", lines: [], image: state.overview.settings.image };
      const panel = $("#jobPanel");
      if (panel) panel.innerHTML = jobPanelHtml();
      pollJob(job.id);
      toast("已开始拉取镜像", "info");
    } catch (err) {
      toast(err.message, "err");
    }
  });
}

function pollJob(id) {
  if (state.jobTimer) { clearInterval(state.jobTimer); state.jobTimer = null; }
  const tick = async () => {
    try {
      const job = await api("/api/jobs/" + id);
      state.job = job;
      const panel = $("#jobPanel");
      if (panel) panel.innerHTML = jobPanelHtml();
      if (job.status !== "running") {
        if (state.jobTimer) { clearInterval(state.jobTimer); state.jobTimer = null; }
        toast(job.status === "done" ? "镜像拉取完成" : "镜像拉取失败", job.status === "done" ? "ok" : "err");
      }
    } catch (err) {
      if (state.jobTimer) { clearInterval(state.jobTimer); state.jobTimer = null; }
    }
  };
  tick();
  state.jobTimer = setInterval(tick, 900);
}

/* ---------------- 系统设置 ---------------- */
function settingsHtml() {
  const o = state.overview;
  const s = o.settings;
  const host = o.host;
  const backend = o.backend;
  const disk = host.data_disk || {};
  return `
  <div class="settings-grid">
    <form class="card" id="settingsForm">
      <div class="card-head"><div><div class="card-title">系统设置</div><div class="card-sub">保存后生效；镜像与存储等默认值影响新实例</div></div></div>
      <div class="form-grid">
        ${field("面板监听地址", `<input class="input" id="s_panel_host" value="${esc(s.panel_bind_host || "0.0.0.0")}">`, "默认 0.0.0.0；仅本机访问可填 127.0.0.1")}
        ${field("面板监听端口", `<div class="input-group"><input class="input" id="s_panel_port" type="number" min="1024" max="65535" value="${s.panel_bind_port || 8080}"><button class="btn btn-ghost btn-sm" type="button" data-action="restart-panel">保存并重启</button></div>`, "当前生效端口 " + ((state.overview.panel && state.overview.panel.bind_port) || location.port || 8080) + "；重启后地址会自动跳转")}
        ${field("镜像地址", `<input class="input" id="s_image" value="${esc(s.image)}">`, "官方推荐使用 images-cluster.xycloud.com 镜像源", true)}
        ${field("实例存储根目录", `<input class="input" id="s_storage_root" value="${esc(s.storage_root)}">`, "每个实例默认在该目录下创建独立子目录", true)}
        ${field("默认端口起始", `<input class="input" id="s_port_base" type="number" min="1024" max="65000" value="${s.port_base}">`, "新实例默认从该端口开始分配")}
        ${field("默认名称前缀", `<input class="input" id="s_name_prefix" maxlength="16" value="${esc(s.name_prefix)}">`, "批量部署命名使用")}
        ${field("默认磁盘模式", `<select class="select" id="s_disk_mode">${optionHtml("dir", "目录挂载（监控配额）", s.disk_mode === "dir")}${optionHtml("loop", "独立磁盘镜像（ext4 硬配额）", s.disk_mode === "loop")}</select>`, "")}
        ${field("默认存储超限动作", `<select class="select" id="s_quota_action">${optionHtml("none", "只记录告警", s.default_quota_action !== "stop")}${optionHtml("stop", "超限自动停止实例", s.default_quota_action === "stop")}</select>`, "新建实例默认策略")}
        ${field("默认重启策略", `<select class="select" id="s_restart">${optionHtml("always", "always（总是重启）", s.restart_policy === "always")}${optionHtml("unless-stopped", "unless-stopped", s.restart_policy === "unless-stopped")}${optionHtml("no", "no（不自动重启）", s.restart_policy === "no")}</select>`, "")}
        ${field("后端模式", `<select class="select" id="s_backend_mode">${optionHtml("auto", "自动（优先 Docker）", s.backend_mode === "auto")}${optionHtml("docker", "强制 Docker", s.backend_mode === "docker")}${optionHtml("sim", "模拟模式（仅演示）", s.backend_mode === "sim")}</select>`, "没有 Docker 时自动模式会回落到模拟后端")}
        ${field("界面刷新间隔", `<input class="input" id="s_poll" type="number" min="2" max="60" value="${s.poll_seconds}">`, "单位：秒")}
        ${field("健康检测间隔", `<input class="input" id="s_health" type="number" min="5" max="3600" value="${s.health_interval}">`, "自动修复检查周期（秒）")}
        ${field("访问令牌", `<input class="input" id="s_token" type="password" placeholder="${s.auth_enabled ? "已设置，留空保持不变" : "未设置（建议至少 6 位）"}">`, "设置后所有 API 与页面需要令牌访问；留空保持不变", true)}
        ${s.auth_enabled ? `<label class="checkbox field-full"><input type="checkbox" id="s_clear_token"> 清除已设置的访问令牌（恢复无密码访问）</label>` : ""}
        ${field("自动修复", `<label class="checkbox"><input type="checkbox" id="s_auto_heal" ${s.auto_heal ? "checked" : ""}> 容器异常或丢失时自动重建 / 拉起</label>`, "只对已启用自愈且期望状态为「运行中」的实例生效", true)}
        ${field("镜像自动更新", `<label class="checkbox"><input type="checkbox" id="s_auto_update" ${s.auto_update ? "checked" : ""}> 通过 watchtower 自动更新网心云镜像</label>`, "需要 Docker 环境；关闭后移除 watchtower 容器", true)}
      </div>
      <div class="form-actions"><button class="btn btn-ghost" type="button" data-action="goto" data-view="check">前往自检</button><button class="btn btn-primary" type="submit">${icon("save")}保存设置</button></div>
    </form>
    <div>
      <div class="card section">
        <div class="card-head"><div><div class="card-title">运行信息</div></div></div>
        <div class="info-list">
          ${infoRow("管理器版本", "v" + o.version)}
          ${infoRow("面板监听", ((state.overview.panel && state.overview.panel.bind_host) || "0.0.0.0") + ":" + ((state.overview.panel && state.overview.panel.bind_port) || 8080))}
          ${infoRow("后端模式", backend.mode === "docker" ? "Docker " + (backend.version || "") : "模拟模式")}
          ${infoRow("操作系统", host.os.pretty)}
          ${infoRow("内核 / 架构", host.os.kernel + " / " + host.os.arch)}
          ${infoRow("主机名", host.os.hostname)}
          ${infoRow("Python", host.os.python)}
          ${infoRow("数据盘", (disk.mount || "—") + " · 剩余 " + fmtBytes(disk.free_bytes || 0))}
          ${infoRow("运行时长", fmtDuration(host.uptime_seconds))}
        </div>
      </div>
      <div class="card section">
        <div class="card-head"><div><div class="card-title">可用存储目录</div><div class="card-sub">自动检测磁盘挂载 / 数据目录</div></div><div class="card-actions"><button class="btn btn-line btn-sm" data-action="refresh-storage-paths">刷新检测</button></div></div>
        <div class="path-note">来源：Linux 挂载表（/proc/mounts）+ 常用数据目录 + 当前设置路径；同一文件系统会显示相同剩余空间。</div>
        <div class="info-list" id="storagePathsInfo">${storagePathRowsHtml()}</div>
      </div>
      <div class="card">
        <div class="card-head"><div><div class="card-title">安全提示</div></div></div>
        <div class="warn-box">${icon("alert")}<div>面板默认可被局域网访问。若映射到公网，请务必设置 <b>访问令牌</b>，并通过反向代理启用 HTTPS。令牌保存在当前浏览器本地。</div></div>
      </div>
    </div>
  </div>`;
}

async function saveSettings(form) {
  const value = (id) => { const el = document.getElementById(id); return el ? el.value : ""; };
  const checked = (id) => { const el = document.getElementById(id); return !!(el && el.checked); };
  const button = form.querySelector('button[type="submit"]');
  await withBusy(button, async () => {
    try {
      const body = {
        image: value("s_image").trim(),
        storage_root: value("s_storage_root").trim(),
        port_base: Number(value("s_port_base")),
        name_prefix: value("s_name_prefix").trim(),
        disk_mode: value("s_disk_mode"),
        restart_policy: value("s_restart"),
        backend_mode: value("s_backend_mode"),
        poll_seconds: Number(value("s_poll")),
        health_interval: Number(value("s_health")),
        auto_heal: checked("s_auto_heal"),
        auto_update: checked("s_auto_update"),
        default_quota_action: value("s_quota_action"),
        panel_bind_host: value("s_panel_host").trim() || "0.0.0.0",
        panel_bind_port: Number(value("s_panel_port")),
      };
      const token = value("s_token").trim();
      if (token) body.auth_token = token;
      if (document.getElementById("s_clear_token") && checked("s_clear_token")) body.auth_token = "";
      const portChanged = Number(body.panel_bind_port) !== Number(((state.overview.panel || {}).bind_port) || 0);
      const data = await api("/api/settings", { method: "PUT", body });
      state.overview.settings = data.settings;
      applyChrome();
      startPolling();
      render();
      toast(portChanged ? "设置已保存；面板端口需重启后生效" : "设置已保存", portChanged ? "warn" : "ok");
    } catch (err) {
      toast(err.message, "err");
    }
  });
}

/* ---------------- 实例弹窗 ---------------- */
function envToText(env) {
  if (!env) return "";
  return Object.keys(env).map((key) => key + "=" + env[key]).join("\n");
}

function readInstanceForm() {
  const value = (id) => { const el = document.getElementById(id); return el ? el.value : ""; };
  const env = {};
  String(value("f_env") || "").split("\n").forEach((line) => {
    const index = line.indexOf("=");
    if (index > 0) env[line.slice(0, index).trim()] = line.slice(index + 1).trim();
  });
  return {
    name: value("f_name").trim(),
    image: value("f_image").trim(),
    notes: value("f_notes"),
    cpus: Number(value("f_cpus")) || 1,
    cpuset: value("f_cpuset").trim(),
    memory_mb: Number(value("f_memory")) || 1024,
    disk_gb: Number(value("f_disk")) || 50,
    disk_mode: value("f_disk_mode") || "dir",
    quota_action: value("f_quota_action") || "none",
    data_dir: value("f_data_dir").trim(),
    runtime_dir: value("f_runtime_dir").trim(),
    network_mode: value("f_network") || "host",
    listen_port: Number(value("f_port")) || undefined,
    restart_policy: value("f_restart") || "always",
    macvlan_parent: value("f_mav_parent").trim(),
    macvlan_subnet: value("f_mav_subnet").trim(),
    macvlan_gateway: value("f_mav_gateway").trim(),
    macvlan_ip: value("f_mav_ip").trim(),
    env,
    enabled: (() => { const el = document.getElementById("f_autoheal"); return el ? el.checked : true; })(),
  };
}

function instanceModalBody(inst) {
  const s = state.overview.settings;
  const isEdit = !!inst;
  const network = isEdit ? inst.network_mode : "host";
  return `
  <form id="instanceForm">
    <div class="form-section" style="border-top:0;padding-top:0;margin-top:0">
      <div class="form-section-title">${icon("cube")}基础信息</div>
      <div class="form-grid">
        ${field("实例名称", `<input class="input" id="f_name" maxlength="32" placeholder="例如：客厅节点" value="${esc(isEdit ? inst.name : "")}" autofocus>`)}
        ${field("镜像", `<input class="input" id="f_image" placeholder="留空使用全局镜像" value="${esc(isEdit ? inst.image : "")}">`)}
        ${field("备注", `<input class="input" id="f_notes" maxlength="200" placeholder="可选" value="${esc(isEdit ? inst.notes : "")}">`, "", true)}
      </div>
    </div>
    <div class="form-section">
      <div class="form-section-title">${icon("sliders")}计算资源</div>
      <div class="form-grid">
        ${field("CPU 核心数", `<input class="input" id="f_cpus" type="number" min="0.1" max="256" step="0.1" value="${isEdit ? inst.cpus : 1}">`, "对应 docker --cpus，可填小数如 1.5")}
        ${field("绑定 CPU 核心", `<input class="input" id="f_cpuset" placeholder="例如 0-1,3（可选）" value="${esc(isEdit ? inst.cpuset : "")}">`, "对应 --cpuset-cpus，留空不绑定")}
        ${field("内存 (MB)", `<input class="input" id="f_memory" type="number" min="128" max="1048576" step="256" value="${isEdit ? inst.memory_mb : 1024}">`, "硬限制，建议 1 GB 起步")}
        ${field("硬盘 (GB)", `<input class="input" id="f_disk" type="number" min="1" max="65536" value="${isEdit ? inst.disk_gb : 50}">`, "目录模式为容量参考；独立盘模式创建 ext4 卷")}
        ${field("磁盘模式", `<select class="select" id="f_disk_mode">${optionHtml("dir", "目录挂载（监控配额）", (isEdit ? inst.disk_mode : s.disk_mode) !== "loop")}${optionHtml("loop", "独立磁盘镜像（ext4 硬配额）", (isEdit ? inst.disk_mode : s.disk_mode) === "loop")}</select>`, "独立盘创建 ext4 镜像并挂载，容量由文件系统硬限制；目录模式按统计用量执行超限策略", true)}
        ${field("存储超限动作", `<select class="select" id="f_quota_action">${optionHtml("none", "只记录告警", (isEdit ? inst.quota_action : s.default_quota_action) !== "stop")}${optionHtml("stop", "超限自动停止实例", (isEdit ? inst.quota_action : s.default_quota_action) === "stop")}</select>`, "仅目录模式生效；独立磁盘由 ext4 内核硬限制，无需此策略", true)}
      </div>
    </div>
    <div class="form-section">
      <div class="form-section-title">${icon("disk")}存储与安装位置</div>
      <div class="form-grid">
        ${field("存储目录", `<input class="input" id="f_data_dir" placeholder="${esc(s.storage_root)}/wx-xx" value="${esc(isEdit ? inst.data_dir : "")}">`, "挂载到容器 /storage，wxnode 保存在此", true)}
        ${field("容器安装目录", `<input class="input" id="f_runtime_dir" placeholder="留空使用 存储目录/containerd" value="${esc(isEdit ? inst.runtime_dir : "")}">`, "挂载到 /var/lib/containerd，可指向较大磁盘（可选）", true)}
      </div>
    </div>
    <div class="form-section">
      <div class="form-section-title">${icon("link")}网络与端口</div>
      <div class="form-grid">
        ${field("网络模式", `<select class="select" id="f_network">${optionHtml("host", "host（官方推荐）", network !== "macvlan")}${optionHtml("macvlan", "macvlan（独立 IP）", network === "macvlan")}</select>`, "host 模式靠不同端口区分多实例")}
        ${field("激活监听端口", `<div class="input-group"><input class="input" id="f_port" type="number" min="1024" max="65535" value="${isEdit ? inst.listen_port : ""}"><button class="btn btn-ghost btn-sm" type="button" data-action="auto-port">自动</button></div>`, "对应 LISTEN_ADDR，host 模式下每实例必须不同")}
      </div>
      <div id="macvlanFields" class="${network === "macvlan" ? "" : "hidden"}">
        <div class="form-grid" style="margin-top:14px">
          ${field("父网卡", `<input class="input" id="f_mav_parent" placeholder="例如 eth0" value="${esc(isEdit ? inst.macvlan_parent : "")}">`)}
          ${field("子网", `<input class="input" id="f_mav_subnet" placeholder="例如 192.168.1.0/24" value="${esc(isEdit ? inst.macvlan_subnet : "")}">`)}
          ${field("网关", `<input class="input" id="f_mav_gateway" placeholder="例如 192.168.1.1" value="${esc(isEdit ? inst.macvlan_gateway : "")}">`)}
          ${field("容器 IP", `<input class="input" id="f_mav_ip" placeholder="例如 192.168.1.240" value="${esc(isEdit ? inst.macvlan_ip : "")}">`)}
        </div>
      </div>
    </div>
    <div class="form-section">
      <div class="form-section-title">${icon("settings")}高级</div>
      <div class="form-grid">
        ${field("重启策略", `<select class="select" id="f_restart">${optionHtml("always", "always（总是重启）", (isEdit ? inst.restart_policy : s.restart_policy) === "always")}${optionHtml("unless-stopped", "unless-stopped", (isEdit ? inst.restart_policy : s.restart_policy) === "unless-stopped")}${optionHtml("no", "no（不自动重启）", (isEdit ? inst.restart_policy : s.restart_policy) === "no")}</select>`, "")}
        ${field("环境变量", `<textarea class="textarea" id="f_env" placeholder="每行一个 KEY=VALUE，可选">${esc(isEdit ? envToText(inst.env) : "")}</textarea>`, "额外传给容器的环境变量", true)}
        ${field("自动修复", `<label class="checkbox"><input type="checkbox" id="f_autoheal" ${!isEdit || inst.enabled ? "checked" : ""}> 启用健康检查自动拉起</label>`, "", true)}
      </div>
    </div>
    <div id="previewBox"></div>
  </form>`;
}

function openInstanceModal(id) {
  const overview = state.overview;
  const inst = id ? (overview.instances || []).find((item) => item.id === id) : null;
  const isEdit = !!inst;
  openModal({
    title: isEdit ? `编辑实例 · ${inst.name}` : "新建实例",
    sub: isEdit ? "保存后会重建容器以应用 CPU / 内存 / 端口等限制，数据目录与 wxnode 保留" : "逐项指定 CPU / 内存 / 硬盘 / 安装位置；也可以先到批量部署自动规划",
    body: instanceModalBody(inst),
    size: "modal-xl",
    footer: `<button class="btn btn-ghost" type="button" data-action="close-modal">取消</button><button class="btn btn-primary" type="button" data-action="submit-instance" data-id="${isEdit ? inst.id : ""}">${icon("save")}${isEdit ? "保存并重建" : "创建实例"}</button>`,
  });
  const form = $("#instanceForm");
  const refreshPreview = debounce(updateInstancePreview, 420);
  form.addEventListener("input", refreshPreview);
  form.addEventListener("change", (event) => {
    if (event.target.id === "f_network") toggleMacvlanFields();
    refreshPreview();
  });
  toggleMacvlanFields();
  updateInstancePreview();
  syncStorageDatalist();
  loadStoragePaths().then(syncStorageDatalist);
}

function toggleMacvlanFields() {
  const select = $("#f_network");
  const box = $("#macvlanFields");
  if (!select || !box) return;
  box.classList.toggle("hidden", select.value !== "macvlan");
}

async function updateInstancePreview() {
  const box = $("#previewBox");
  const form = $("#instanceForm");
  if (!box || !form) return;
  const payload = readInstanceForm();
  payload.name = payload.name || "预览实例";
  const seq = ++state.previewSeq;
  box.innerHTML = `<div class="preview"><div class="preview-head">${icon("terminal")}正在生成 docker 命令…</div></div>`;
  try {
    const data = await api("/api/preview", { method: "POST", body: { instance: payload } });
    if (seq !== state.previewSeq || !$("#previewBox")) return;
    state.suggestPort = data.suggest_port;
    const portInput = $("#f_port");
    if (portInput && !portInput.value) portInput.value = data.suggest_port;
    const warnings = (data.warnings || []).map((text) => `<div class="warn-box">${icon("alert")}<div>${esc(text)}</div></div>`).join("");
    box.innerHTML = `
      <div class="preview">
        <div class="preview-head">${icon("terminal")}docker 命令预览
          <button class="btn btn-ghost btn-sm copy-btn" type="button" data-action="copy" data-text="${esc(data.command)}">${icon("copy")}复制</button>
        </div>
        <pre>${esc(data.command)}</pre>
      </div>
      ${warnings ? `<div class="box-list" style="margin-top:10px">${warnings}</div>` : ""}`;
  } catch (err) {
    if (seq === state.previewSeq && $("#previewBox")) {
      box.innerHTML = `<div class="error-box">${icon("alert")}<div>${esc(err.message)}</div></div>`;
    }
  }
}

async function submitInstanceFrom(form, button) {
  if (!form) return;
  const payload = readInstanceForm();
  if (!payload.name) { toast("请填写实例名称", "err"); return; }
  if (!payload.listen_port) delete payload.listen_port;
  const id = button ? button.dataset.id : "";
  await withBusy(button, async () => {
    try {
      if (id) {
        await api("/api/instances/" + id, { method: "PUT", body: payload });
        toast("实例已更新并重建容器", "ok");
      } else {
        await api("/api/instances", { method: "POST", body: payload });
        toast("实例创建成功", "ok");
      }
      closeModal();
      await refreshNow();
    } catch (err) {
      toast(err.message, "err", 5200);
    }
  });
}

function fillAutoPort() {
  const input = $("#f_port");
  if (!input) return;
  if (state.suggestPort) {
    input.value = state.suggestPort;
    updateInstancePreview();
  } else {
    updateInstancePreview();
    toast("正在获取推荐端口…", "info", 1600);
  }
}

/* ---------------- 删除 / 日志弹窗 ---------------- */
function openDeleteModal(id) {
  const inst = (state.overview.instances || []).find((item) => item.id === id);
  if (!inst) return;
  openModal({
    title: "删除实例",
    sub: `${inst.name} · ${inst.container_name}`,
    body: `
      <p style="margin-top:0">确定移除该实例的容器吗？此操作不会影响其他实例。</p>
      <label class="checkbox" style="margin-top:8px"><input type="checkbox" id="purgeData"> 同时彻底删除数据目录（含 wxnode，不可恢复）</label>
      <div class="warn-box" style="margin-top:12px">${icon("alert")}<div>默认保留数据目录：wxnode 与缓存都会留下，之后用相同目录重建实例可以继续使用原设备标识。</div></div>`,
    footer: `<button class="btn btn-ghost" type="button" data-action="close-modal">取消</button><button class="btn btn-danger" type="button" data-action="confirm-delete" data-id="${id}">${icon("trash")}确认删除</button>`,
  });
}

async function confirmDelete(id, button) {
  const purge = document.getElementById("purgeData") && document.getElementById("purgeData").checked;
  await withBusy(button, async () => {
    try {
      await api("/api/instances/" + id + "?purge=" + (purge ? "1" : "0"), { method: "DELETE" });
      closeModal();
      toast(purge ? "实例与数据已删除" : "实例已删除（数据保留）", "ok");
      await refreshNow();
    } catch (err) {
      toast(err.message, "err", 5200);
    }
  });
}

function openLogsModal(id) {
  const inst = (state.overview.instances || []).find((item) => item.id === id);
  if (!inst) return;
  state.logId = id;
  openModal({
    title: `日志 · ${inst.name}`,
    sub: `容器 ${inst.container_name} · 最近输出`,
    size: "modal-xl",
    body: `
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-bottom:9px">
        <select class="select" id="logTail" style="width:auto">
          <option value="100">100 行</option>
          <option value="200" selected>200 行</option>
          <option value="500">500 行</option>
          <option value="1000">1000 行</option>
        </select>
      </div>
      <pre class="job-log" id="logText" style="max-height:52vh;min-height:120px">加载中…</pre>`,
    footer: `<button class="btn btn-ghost" type="button" data-action="download-logs">${icon("download")}下载日志</button><button class="btn btn-primary" type="button" data-action="close-modal">关闭</button>`,
  });
  loadLogs(id, 200);
  const tailSelect = $("#logTail");
  if (tailSelect) tailSelect.addEventListener("change", () => loadLogs(id, Number(tailSelect.value) || 200));
  if (state.logsTimer) clearInterval(state.logsTimer);
  state.logsTimer = setInterval(() => loadLogs(id, Number((document.getElementById("logTail") || {}).value) || 200), 3000);
}

async function loadLogs(id, tail) {
  try {
    const data = await api(`/api/instances/${id}/logs?tail=${tail || 200}`);
    const box = $("#logText");
    if (box) {
      box.textContent = data.text || "(暂无日志输出)";
      box.scrollTop = box.scrollHeight;
    }
  } catch (err) {
    const box = $("#logText");
    if (box) box.textContent = "获取日志失败：" + err.message;
  }
}

async function downloadLogs() {
  const box = $("#logText");
  if (!box) return;
  const blob = new Blob([box.textContent || ""], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "wxedge-" + (state.logId || "instance") + "-logs.txt";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

/* ---------------- 弹窗基础 ---------------- */
let modalCounter = 0;

function openModal({ title, sub = "", body = "", footer = "", size = "" }) {
  modalCounter += 1;
  $("#modalRoot").innerHTML = `
    <div class="modal-backdrop" data-modal="${modalCounter}">
      <div class="modal ${size}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="modal-head">
          <div style="min-width:0"><h2 class="modal-title">${esc(title)}</h2>${sub ? `<div class="modal-sub">${esc(sub)}</div>` : ""}</div>
          <button class="icon-btn modal-close" type="button" data-action="close-modal" aria-label="关闭">${icon("x")}</button>
        </div>
        <div class="modal-body">${body}</div>
        ${footer ? `<div class="modal-foot">${footer}</div>` : ""}
      </div>
    </div>`;
  document.body.style.overflow = "hidden";
}

function closeModal() {
  if (state.logsTimer) { clearInterval(state.logsTimer); state.logsTimer = null; }
  $("#modalRoot").innerHTML = "";
  document.body.style.overflow = "";
}

/* ---------------- 登录 ---------------- */
function showAuth(message = "") {
  if ($("#authRoot").innerHTML) return;
  $("#authRoot").innerHTML = `
    <div class="auth-overlay">
      <div class="auth-card">
        <div class="auth-logo">${icon("shield")}</div>
        <h2>需要访问令牌</h2>
        <p>${esc(message || "该管理面板已开启令牌保护")}</p>
        <form id="authForm">
          <div class="field">
            <input class="input" id="authTokenInput" type="password" placeholder="请输入访问令牌" autocomplete="current-password" autofocus>
            <div class="auth-error" id="authError">${esc(message && message !== "该管理面板已开启令牌保护" ? message : "")}</div>
          </div>
          <button class="btn btn-primary btn-block" type="submit">解锁面板</button>
        </form>
      </div>
    </div>`;
  const input = $("#authTokenInput");
  if (input) setTimeout(() => input.focus(), 50);
}

async function verifyToken() {
  const input = $("#authTokenInput");
  const errorBox = $("#authError");
  const button = $("#authForm button");
  const token = input ? input.value.trim() : "";
  if (!token) { if (errorBox) errorBox.textContent = "请输入访问令牌"; return; }
  if (button) { button.disabled = true; button.textContent = "验证中…"; }
  try {
    const response = await fetch("/api/auth/verify", { method: "POST", headers: { "X-Auth-Token": token } });
    const data = await response.json().catch(() => ({}));
    if (data && data.ok) {
      state.authToken = token;
      localStorage.setItem("wxedge_token", token);
      $("#authRoot").innerHTML = "";
      bootstrap();
    } else if (errorBox) {
      errorBox.textContent = "令牌不正确，请重试";
    }
  } catch (err) {
    if (errorBox) errorBox.textContent = "无法连接服务器：" + err.message;
  } finally {
    if (button) { button.disabled = false; button.textContent = "解锁面板"; }
  }
}

/* ---------------- 操作分发 ---------------- */
async function handleAction(event) {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  const id = target.dataset.id;
  switch (action) {
    case "refresh": manualRefresh(target); break;
    case "new-instance": openInstanceModal(); break;
    case "goto": navigate(target.dataset.view); break;
    case "edit": openInstanceModal(id); break;
    case "logs": openLogsModal(id); break;
    case "delete": openDeleteModal(id); break;
    case "confirm-delete": confirmDelete(id, target); break;
    case "start": case "stop": case "restart": case "recreate": doInstanceAction(id, action, target); break;
    case "filter": state.filter = target.dataset.filter; render(); break;
    case "run-check": runCheck(target); break;
    case "pull-image": pullImage(target); break;
    case "create-batch": createBatch(target); break;
    case "close-modal": closeModal(); break;
    case "copy": copyText(target.dataset.text || ""); break;
    case "auto-port": fillAutoPort(); break;
    case "submit-instance": submitInstanceFrom($("#instanceForm"), target); break;
    case "download-logs": downloadLogs(); break;
    case "restart-panel": restartPanel(target); break;
    case "refresh-storage-paths": refreshStoragePaths(); break;
    default: break;
  }
}

async function restartPanel(button) {
  const hostInput = document.getElementById("s_panel_host");
  const portInput = document.getElementById("s_panel_port");
  const host = hostInput ? hostInput.value.trim() || "0.0.0.0" : "0.0.0.0";
  const port = portInput ? Number(portInput.value) : 8080;
  if (!Number.isFinite(port) || port < 1024 || port > 65535) {
    toast("面板端口需在 1024 ~ 65535 之间", "err");
    return;
  }
  await withBusy(button, async () => {
    try {
      await api("/api/settings", { method: "PUT", body: { panel_bind_host: host, panel_bind_port: port } });
      await api("/api/system/restart", { method: "POST" });
      toast("面板正在重启，即将跳转到新端口…", "info", 4200);
      setTimeout(() => {
        const currentPort = location.port || (location.protocol === "https:" ? "443" : "80");
        const target = location.protocol + "//" + location.hostname + ":" + port + location.pathname;
        if (String(port) === String(currentPort)) location.reload();
        else location.href = target;
      }, 2000);
    } catch (err) {
      toast(err.message, "err");
    }
  });
}

async function doInstanceAction(id, action, button) {
  const labels = { start: "实例已启动", stop: "实例已停止", restart: "实例已重启", recreate: "容器已重建" };
  await withBusy(button, async () => {
    try {
      await api("/api/instances/" + id + "/action", { method: "POST", body: { action } });
      toast(labels[action] || "操作完成", "ok");
      await refreshNow();
    } catch (err) {
      toast(err.message, "err", 4800);
    }
  });
}

async function copyText(text) {
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    toast("已复制到剪贴板", "ok", 1600);
  } catch (err) {
    toast("复制失败，请手动选择文本", "warn");
  }
}

/* ---------------- 事件绑定 ---------------- */
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-action]")) {
    handleAction(event);
    return;
  }
  if (event.target.classList && event.target.classList.contains("modal-backdrop")) closeModal();
});

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form || !form.id) return;
  if (form.id === "deployForm") {
    event.preventDefault();
    const button = event.submitter || form.querySelector('button[type="submit"]');
    await withBusy(button, () => generatePlan());
  } else if (form.id === "settingsForm") {
    event.preventDefault();
    await saveSettings(form);
  } else if (form.id === "instanceForm") {
    event.preventDefault();
    const submit = form.closest(".modal") ? form.closest(".modal").querySelector(".modal-foot .btn-primary") : null;
    await submitInstanceFrom(form, submit);
  } else if (form.id === "authForm") {
    event.preventDefault();
    await verifyToken();
  }
});

document.addEventListener("input", (event) => {
  if (event.target.id === "searchInput") {
    state.query = event.target.value;
    const list = $("#instanceList");
    if (list) list.innerHTML = renderInstanceList();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if ($("#modalRoot").innerHTML) closeModal();
  }
});

document.addEventListener("DOMContentLoaded", () => {
  $("#nav").addEventListener("click", (event) => {
    const button = event.target.closest(".nav-item");
    if (button) navigate(button.dataset.view);
  });
  $("#refreshBtn").addEventListener("click", () => manualRefresh($("#refreshBtn")));
  const menuButton = $("#menuBtn");
  if (menuButton) menuButton.addEventListener("click", () => { const sidebar = $("#sidebar"); if (sidebar) sidebar.classList.toggle("is-open"); });
  window.addEventListener("resize", debounce(() => {
    if (state.view === "overview") drawLoadChart();
  }, 200));
  initTheme();
  bootstrap();
});

/* ---------------- 启动 ---------------- */
async function bootstrap() {
  try {
    const overview = await api("/api/overview");
    state.overview = overview;
    applyChrome();
    startPolling();
    render();
    if (overview.backend && (overview.backend.error || overview.backend.warning)) {
      toast(overview.backend.error || overview.backend.warning, "warn", 5200);
    }
  } catch (err) {
    if (err.status === 401) {
      showAuth();
      return;
    }
    $("#view").innerHTML = `<div class="card section"><div class="empty" style="padding:50px 20px"><div class="empty-icon">${icon("alert")}</div><h3>无法连接管理器</h3><p>${esc(err.message)}</p><button class="btn btn-primary" data-action="refresh">重试连接</button></div></div>`;
    $("#pageTitle").textContent = "连接失败";
    $("#pageSub").textContent = "请检查服务是否运行，或刷新页面重试";
  }
}
