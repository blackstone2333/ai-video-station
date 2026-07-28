"use strict";

const state = {
  apiKey: sessionStorage.getItem("aiNasApiKey") || sessionStorage.getItem("sixvApiKey") || "",
  watchlist: [], downloads: [], naming: [], hardlinks: [], sites: [], qb: null, loading: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHTML = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);

class AuthError extends Error {}

async function api(path, options = {}) {
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  if (state.apiKey) headers["X-Api-Key"] = state.apiKey;
  const response = await fetch(path, {...options, headers});
  if (response.status === 401) throw new AuthError("API Key 无效或尚未输入");
  if (response.status === 204) return null;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求失败 (${response.status})`);
  return body;
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes) return "0 B";
  const units = ["B","KB","MB","GB","TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index > 2 ? 1 : 0)} ${units[index]}`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}).format(date);
}

function stateLabel(value) {
  const labels = {
    downloading: "下载中", stalledDL: "等待数据", metaDL: "获取元数据", queuedDL: "排队中", pausedDL: "已暂停",
    uploading: "做种中", stalledUP: "做种等待", pausedUP: "已完成", completed: "已完成", pending: "待处理",
    waiting_metadata: "等待元数据", retrying: "重试中", failed: "失败", found: "已找到", monitoring: "追更中", expired: "超期监听",
    waiting_download: "等待下载完成", done: "硬链接完成", disabled: "硬链接已关闭", site_disabled: "已停用",
  };
  return labels[value] || value || "未知";
}

function chip(value) {
  const ok = ["completed","found","monitoring","downloading","uploading","pausedUP"].includes(value);
  const error = value === "failed";
  return `<span class="status-chip ${error ? "error" : ok ? "ok" : "wait"}">${escapeHTML(stateLabel(value))}</span>`;
}

function namingStatus(item) {
  if (["waiting_download", "retrying", "failed"].includes(item.hardlink_status)) return item.hardlink_status;
  return item.hardlink_status === "done" ? "done" : item.status;
}

function mediaTypeLabel(value) {
  return ({movie:"电影", tv:"电视剧", anime:"动漫", auto:"自动"})[value] || value || "未知";
}

let toastTimer;
function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.className = "toast"; }, 3000);
}

function showAuth(message = "") {
  $("#auth-modal").classList.remove("hidden");
  $("#auth-error").textContent = message;
  setTimeout(() => $("#api-key").focus(), 50);
}

function setTab(name) {
  $$(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === name));
  $$(".panel").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
}

function renderMetrics() {
  const activeStates = new Set(["downloading","stalledDL","metaDL","queuedDL","forcedDL","checkingDL"]);
  const active = state.downloads.filter((item) => activeStates.has(item.state));
  const errors = state.watchlist.filter((item) => item.last_error).length + state.naming.filter((item) => item.status === "failed" || item.hardlink_status === "failed").length;
  $("#metric-watch").textContent = state.watchlist.length;
  $("#metric-watch-note").textContent = `${state.watchlist.filter((item) => ["tv","anime"].includes(item.type)).length} 个追更任务`;
  $("#metric-active").textContent = active.length;
  $("#metric-speed").textContent = `${formatBytes(active.reduce((sum,item) => sum + Number(item.dlspeed || 0), 0))}/s`;
  $("#metric-complete").textContent = state.hardlinks.filter((item) => item.status === "done").length;
  $("#metric-errors").textContent = errors;
}

function downloadMarkup(item) {
  const progress = Math.max(0, Math.min(100, Number(item.progress || 0) * 100));
  return `<article class="download-item">
    <div class="download-title" title="${escapeHTML(item.name)}">${escapeHTML(item.name || "未命名任务")}</div>
    <div class="download-meta">${escapeHTML(stateLabel(item.state))} · ${formatBytes(item.size)} · ${escapeHTML(item.category || "无分类")}</div>
    <div class="progress-track" style="--progress:${progress.toFixed(1)}%"><i></i></div>
    <div class="progress-label"><span>${progress.toFixed(1)}%</span><span>${formatBytes(item.dlspeed)}/s</span></div>
  </article>`;
}

function renderDownloads() {
  const values = state.downloads;
  $("#download-count").textContent = `${values.length} 条`;
  $("#downloads-list").innerHTML = values.length ? values.map(downloadMarkup).join("") : '<div class="empty">当前没有下载记录</div>';
  const active = values.filter((item) => Number(item.progress || 0) < .999).slice(0,4);
  $("#overview-downloads").innerHTML = active.length ? active.map(downloadMarkup).join("") : '<div class="empty">队列安静，当前没有进行中的下载</div>';
}

function renderWatchlist() {
  const query = $("#watch-filter").value.trim().toLowerCase();
  const values = state.watchlist.filter((item) => !query || item.keyword.toLowerCase().includes(query));
  $("#watchlist-body").innerHTML = values.length ? values.map((item) => `<tr>
    <td><div class="cell-main">${escapeHTML(item.keyword)}<small>${escapeHTML(item.id.slice(0,8))}</small></div></td>
    <td>${escapeHTML(mediaTypeLabel(item.type))}</td>
    <td>${chip(namingStatus(item))}</td>
    <td>${item.downloaded_episodes?.length || item.downloaded_links?.length || 0}</td>
    <td>${formatDate(item.last_check)}</td>
    <td><button class="danger-button" data-delete-watch="${escapeHTML(item.id)}" type="button">移除</button></td>
  </tr>`).join("") : '<tr><td colspan="6"><div class="empty">没有符合条件的监听任务</div></td></tr>';
}

function renderHardlinks() {
  const filter = $("#hardlink-filter").value;
  const values = state.hardlinks.filter((item) => filter === "all" || item.status === filter);
  $("#hardlinks-body").innerHTML = values.length ? values.map((item) => `<tr>
    <td><div class="cell-main">${escapeHTML(item.name || "未命名")}<small>${escapeHTML(item.id.slice(0,8))}</small></div></td>
    <td>${escapeHTML(mediaTypeLabel(item.type))}</td>
    <td class="target-text" title="${escapeHTML(item.target || "")}">${escapeHTML(item.target || "—")}</td>
    <td>${Number(item.linked || 0)} 链接 / ${Number(item.skipped || 0)} 跳过</td>
    <td>${chip(item.status)}</td>
    <td>${formatDate(item.completed_at)}</td>
  </tr>`).join("") : '<tr><td colspan="6"><div class="empty">暂无符合条件的硬链接任务</div></td></tr>';
}

function renderSites() {
  $("#site-count").textContent = `${state.sites.length} 个`;
  $("#sites-body").innerHTML = state.sites.length ? state.sites.map((item) => `<tr>
    <td><div class="cell-main">${escapeHTML(item.name)}<small>${escapeHTML(item.id)}</small></div></td>
    <td>${item.adapter === "sixv" ? "6v 内置" : "通用 HTML"}</td>
    <td class="target-text" title="${escapeHTML(item.base_urls?.[0] || "")}">${escapeHTML(item.base_urls?.[0] || "—")}</td>
    <td>${chip(item.enabled ? "completed" : "site_disabled")}</td>
    <td><div class="inline-actions"><button class="quiet-button" data-toggle-site="${escapeHTML(item.id)}" data-enabled="${item.enabled}" type="button">${item.enabled ? "停用" : "启用"}</button><button class="danger-button" data-delete-site="${escapeHTML(item.id)}" type="button">删除</button></div></td>
  </tr>`).join("") : '<tr><td colspan="5"><div class="empty">尚未配置站点</div></td></tr>';
}

function renderNaming() {
  const filter = $("#naming-filter").value;
  const values = state.naming.filter((item) => !filter || namingStatus(item) === filter);
  $("#naming-body").innerHTML = values.length ? values.map((item) => `<tr>
    <td><div class="cell-main">${escapeHTML(item.plan?.root_name || "未命名")}<small>${escapeHTML(item.plan?.media_type || "")}</small></div></td>
    <td title="${escapeHTML(item.plan?.link_name || "")}">${escapeHTML(item.plan?.link_name || "—")}</td>
    <td>${chip(namingStatus(item))}</td>
    <td>${item.result?.video_count ?? "—"}</td>
    <td>${formatDate(item.updated_at)}</td>
    <td class="error-text" title="${escapeHTML(item.hardlink_error || item.last_error || "")}">${escapeHTML(item.hardlink_error || item.last_error || "—")}</td>
  </tr>`).join("") : '<tr><td colspan="6"><div class="empty">暂无命名任务</div></td></tr>';
  const recent = state.naming.slice(0,4);
  $("#overview-naming").innerHTML = recent.length ? recent.map((item) => `<article class="activity">${chip(namingStatus(item))}<strong title="${escapeHTML(item.plan?.root_name)}">${escapeHTML(item.plan?.root_name || "未命名")}</strong><small>${formatDate(item.updated_at)}</small></article>`).join("") : '<div class="empty">尚无规范命名记录</div>';
}

function renderAutomation() {
  const waiting = state.naming.filter((item) => ["pending","waiting_metadata","retrying"].includes(item.status) || ["waiting_download","retrying"].includes(item.hardlink_status)).length;
  const failed = state.naming.filter((item) => item.status === "failed" || item.hardlink_status === "failed").length;
  const qbok = state.qb?.connected;
  const downloader = state.qb?.client === "transmission" ? "Transmission" : "qBittorrent";
  $("#automation-status").innerHTML = `
    <div class="status-row ${qbok ? "" : "error"}"><i></i><span>${downloader} 连接</span><small>${qbok ? escapeHTML(state.qb.version) : "不可用"}</small></div>
    <div class="status-row ${waiting ? "warn" : ""}"><i></i><span>等待命名或入库</span><small>${waiting} 项</small></div>
    <div class="status-row ${failed ? "error" : ""}"><i></i><span>命名或入库失败</span><small>${failed} 项</small></div>
    <div class="status-row"><i></i><span>订阅定时检查</span><small>每 12 小时</small></div>`;
}

function renderAll() {
  renderMetrics(); renderDownloads(); renderWatchlist(); renderNaming(); renderHardlinks(); renderSites(); renderAutomation();
  const online = state.qb?.connected;
  const downloader = state.qb?.client === "transmission" ? "Transmission" : "qBittorrent";
  $("#downloader-name").textContent = downloader;
  $("#qb-version").textContent = online ? `在线 / ${state.qb.version}` : state.qb?.configured ? "连接异常" : "尚未配置";
  $("#site-status").className = `signal ${online ? "ok" : state.qb?.configured ? "error" : ""}`;
  $("#site-status").lastChild.textContent = online ? "系统在线" : "服务已连接";
  $("#last-updated").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", {hour12:false})}`;
}

async function loadAll(silent = false) {
  if (state.loading) return;
  state.loading = true;
  $("#refresh-all").classList.add("loading");
  try {
    const requests = await Promise.allSettled([
      api("/api/downloader/status"), api("/api/downloader/tasks"), api("/api/watchlist"), api("/api/naming/jobs?per_page=100"),
      api("/api/hardlinks?status=all&per_page=100"), api("/api/settings/sites"),
    ]);
    const authFailure = requests.find((item) => item.status === "rejected" && item.reason instanceof AuthError);
    if (authFailure) throw authFailure.reason;
    state.qb = requests[0].status === "fulfilled" ? requests[0].value : {configured:false,connected:false};
    state.downloads = requests[1].status === "fulfilled" ? requests[1].value.tasks : [];
    state.watchlist = requests[2].status === "fulfilled" ? requests[2].value.items : [];
    state.naming = requests[3].status === "fulfilled" ? requests[3].value.items : [];
    state.hardlinks = requests[4].status === "fulfilled" ? requests[4].value.items : [];
    state.sites = requests[5].status === "fulfilled" ? requests[5].value.items : [];
    renderAll();
    $("#auth-modal").classList.add("hidden");
    if (!silent && requests.some((item) => item.status === "rejected")) toast("部分数据暂时不可用", true);
  } catch (error) {
    if (error instanceof AuthError) showAuth(error.message); else if (!silent) toast(error.message, true);
  } finally {
    state.loading = false;
    $("#refresh-all").classList.remove("loading");
  }
}

async function runAction(button, path, message, body = {}) {
  button.disabled = true;
  try { await api(path, {method:"POST", body:JSON.stringify(body)}); toast(message); await loadAll(true); }
  catch (error) { if (error instanceof AuthError) showAuth(error.message); else toast(error.message, true); }
  finally { button.disabled = false; }
}

function bindEvents() {
  $$(".tab").forEach((button) => button.addEventListener("click", () => setTab(button.dataset.tab)));
  $$('[data-go]').forEach((button) => button.addEventListener("click", () => setTab(button.dataset.go)));
  $("#refresh-all").addEventListener("click", () => loadAll());
  $("#watch-filter").addEventListener("input", renderWatchlist);
  $("#naming-filter").addEventListener("change", renderNaming);
  $("#hardlink-filter").addEventListener("change", renderHardlinks);
  $("#check-watchlist").addEventListener("click", (event) => runAction(event.currentTarget, "/api/watchlist/check", "监听检查已完成"));
  $("#check-naming").addEventListener("click", (event) => runAction(event.currentTarget, "/api/naming/jobs/check", "命名任务已处理"));
  $("#watchlist-body").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-delete-watch]");
    if (!button || !confirm("确定移除这个监听任务吗？")) return;
    try { await api(`/api/watchlist/${button.dataset.deleteWatch}`, {method:"DELETE"}); toast("监听已移除"); await loadAll(true); }
    catch (error) { toast(error.message, true); }
  });
  $("#site-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      name: $("#site-name").value.trim(), adapter: "generic", enabled: true,
      base_urls: [$("#site-base").value.trim()], search_url: $("#site-search-url").value.trim(),
      result_selector: $("#site-result-selector").value.trim(), title_selector: $("#site-title-selector").value.trim(),
      link_selector: $("#site-link-selector").value.trim(), download_selector: $("#site-download-selector").value.trim(),
      default_type: $("#site-default-type").value,
      tv_path_patterns: $("#site-tv-patterns").value.split(",").map((item) => item.trim()).filter(Boolean),
      anime_path_patterns: $("#site-anime-patterns").value.split(",").map((item) => item.trim()).filter(Boolean),
    };
    try { await api("/api/settings/sites", {method:"POST", body:JSON.stringify(payload)}); toast("站点已保存"); event.currentTarget.reset(); await loadAll(true); }
    catch (error) { toast(error.message, true); }
  });
  $("#sites-body").addEventListener("click", async (event) => {
    const toggle = event.target.closest("[data-toggle-site]");
    const remove = event.target.closest("[data-delete-site]");
    try {
      let changed = false;
      if (toggle) { await api(`/api/settings/sites/${toggle.dataset.toggleSite}`, {method:"PATCH", body:JSON.stringify({enabled:toggle.dataset.enabled !== "true"})}); changed = true; }
      if (remove && confirm("确定删除这个站点配置吗？")) { await api(`/api/settings/sites/${remove.dataset.deleteSite}`, {method:"DELETE"}); changed = true; }
      if (changed) { toast("站点配置已更新"); await loadAll(true); }
    } catch (error) { toast(error.message, true); }
  });
  $("#auth-form").addEventListener("submit", async (event) => {
    event.preventDefault(); state.apiKey = $("#api-key").value.trim(); sessionStorage.setItem("aiNasApiKey", state.apiKey); await loadAll();
  });
  $("#logout").addEventListener("click", () => { sessionStorage.removeItem("aiNasApiKey"); sessionStorage.removeItem("sixvApiKey"); state.apiKey = ""; $("#api-key").value = ""; showAuth(); });
}

function tickClock() { $("#clock").textContent = new Date().toLocaleTimeString("zh-CN", {hour12:false}); }
bindEvents(); tickClock(); setInterval(tickClock, 1000); loadAll();
setInterval(() => { if (!document.hidden) loadAll(true); }, 20000);
