"use strict";

const DEFAULT_THEME = {mode: "dark", lightStart: "07:00", darkStart: "19:00"};
function storedTheme() {
  try { return JSON.parse(localStorage.getItem("aiVideoStationTheme") || "{}"); }
  catch (_) { return {}; }
}
const state = {
  apiKey: sessionStorage.getItem("aiVideoStationApiKey") || sessionStorage.getItem("sixvApiKey") || "",
  watchlist: [], downloads: [], hiddenDownloads: [], naming: [], hardlinks: [], sites: [], pathRules: [], agents: [], logs: [],
  paths: null, downloaderSettings: null, systemSettings: null, qb: null, loading: false,
  pathsDirty: false, downloaderDirty: false, systemDirty: false, downloadsLoading: false, downloadSyncedAt: null,
  theme: {...DEFAULT_THEME, ...storedTheme()},
};
const ACTIVE_DOWNLOAD_STATES = new Set(["downloading","stalledDL","metaDL","queuedDL","forcedDL","checkingDL"]);

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHTML = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
class AuthError extends Error {}

async function api(path, options = {}) {
  const isForm = options.body instanceof FormData;
  const headers = {...(isForm ? {} : {"Content-Type": "application/json"}), ...(options.headers || {})};
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
    downloading:"下载中", stalledDL:"等待数据", metaDL:"获取元数据", queuedDL:"排队中", forcedDL:"强制下载", checkingDL:"校验中", pausedDL:"已暂停",
    uploading:"做种中", stalledUP:"做种等待", queuedUP:"做种排队", forcedUP:"强制做种", checkingUP:"校验完成资源", pausedUP:"已完成", completed:"已完成", pending:"待处理",
    missingFiles:"文件缺失", error:"下载器错误",
    waiting_metadata:"等待元数据", retrying:"重试中", failed:"失败", found:"已找到", monitoring:"追更中", expired:"超期监听",
    waiting_download:"等待下载完成", done:"硬链接完成", disabled:"硬链接已关闭", site_disabled:"已停用",
  };
  return labels[value] || value || "未知";
}

function chip(value) {
  const ok = ["completed","found","monitoring","downloading","uploading","pausedUP","done"].includes(value);
  const error = value === "failed";
  return `<span class="status-chip ${error ? "error" : ok ? "ok" : "wait"}">${escapeHTML(stateLabel(value))}</span>`;
}

function namingStatus(item) {
  if (["waiting_download", "retrying", "failed"].includes(item.hardlink_status)) return item.hardlink_status;
  return item.hardlink_status === "done" ? "done" : item.status;
}

function mediaTypeLabel(value) {
  return ({movie:"电影", tv:"电视剧", anime:"动漫", custom:"自定义", auto:"自动"})[value] || value || "未知";
}

let toastTimer;
function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.className = "toast"; }, 3200);
}

function showAuth(message = "") {
  $("#auth-modal").classList.remove("hidden");
  $("#auth-error").textContent = message;
  setTimeout(() => $("#api-key").focus(), 50);
}

function openModal(selector) { $(selector).classList.remove("hidden"); }
function closeModal(element) { element.closest(".modal")?.classList.add("hidden"); }

function setTab(name) {
  $$(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === name));
  $$(".panel").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
  history.replaceState(null, "", `#${name}`);
  window.scrollTo({top: $(".tabs").offsetTop - 90, behavior: "smooth"});
}

function themeMinutes(value) {
  const [hour, minute] = String(value || "00:00").split(":").map(Number);
  return hour * 60 + minute;
}

function resolvedTheme() {
  if (state.theme.mode === "system") return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  if (state.theme.mode !== "schedule") return state.theme.mode === "light" ? "light" : "dark";
  const now = new Date();
  const current = now.getHours() * 60 + now.getMinutes();
  const light = themeMinutes(state.theme.lightStart);
  const dark = themeMinutes(state.theme.darkStart);
  const isLight = light <= dark ? current >= light && current < dark : current >= light || current < dark;
  return isLight ? "light" : "dark";
}

function applyTheme() {
  document.documentElement.dataset.theme = resolvedTheme();
  $("#theme-mode").value = state.theme.mode;
  $("#header-theme-mode").value = state.theme.mode;
  $("#theme-light-start").value = state.theme.lightStart;
  $("#theme-dark-start").value = state.theme.darkStart;
  $("#theme-schedule").classList.toggle("hidden", state.theme.mode !== "schedule");
}

function saveTheme() {
  localStorage.setItem("aiVideoStationTheme", JSON.stringify(state.theme));
  applyTheme();
}

function renderMetrics() {
  const active = state.downloads.filter((item) => ACTIVE_DOWNLOAD_STATES.has(item.state));
  const hardlinkDone = state.hardlinks.filter((item) => item.status === "done").length;
  $("#metric-watch").textContent = state.watchlist.length;
  $("#metric-watch-note").textContent = `${state.watchlist.filter((item) => ["tv","anime"].includes(item.type)).length} 个追更任务`;
  $("#metric-active").textContent = active.length;
  $("#metric-speed").textContent = `${formatBytes(active.reduce((sum,item) => sum + Number(item.dlspeed || 0), 0))}/s`;
  $("#metric-complete").textContent = state.naming.filter((item) => item.status === "completed").length;
  $("#metric-hardlinks").textContent = state.hardlinks.length;
  $("#metric-hardlink-note").textContent = `${hardlinkDone} 成功 · ${state.hardlinks.length - hardlinkDone} 处理中`;
}

function downloadProgress(item) {
  let progress = Number(item.progress || 0);
  if (["missingFiles","error"].includes(item.state)) return Math.max(0, Math.min(100, progress * 100));
  const size = Number(item.size || 0);
  const downloaded = Number(item.downloaded || 0);
  if (size > 0) progress = Math.max(progress, downloaded / size);
  if (item.completed || Number(item.completion_on || 0) > 0 || ["uploading","stalledUP","queuedUP","forcedUP","pausedUP","checkingUP"].includes(item.state)) progress = 1;
  return Math.max(0, Math.min(100, progress * 100));
}

function downloadMarkup(item) {
  const progress = downloadProgress(item);
  const canDismiss = !ACTIVE_DOWNLOAD_STATES.has(item.state);
  const canRecover = ["missingFiles","error"].includes(item.state);
  const actions = `${canRecover ? `<button class="quiet-button" data-recover-download="${escapeHTML(item.hash || "")}" type="button">校验并恢复</button>` : ""}${canDismiss ? `<button class="danger-button" data-dismiss-download="${escapeHTML(item.hash || "")}" type="button">隐藏记录</button>` : ""}`;
  return `<article class="download-item">
    <div class="download-title" title="${escapeHTML(item.name)}">${escapeHTML(item.name || "未命名任务")}</div>
    <div class="download-meta"><span>${escapeHTML(stateLabel(item.state))} · ${formatBytes(item.size)} · ${escapeHTML(item.category || "无分类")}</span>${actions}</div>
    <progress class="progress-track" max="100" value="${progress.toFixed(1)}" aria-label="下载进度 ${progress.toFixed(1)}%"></progress>
    <div class="progress-label"><span>${progress.toFixed(1)}%</span><span>${formatBytes(item.dlspeed)}/s</span></div>
  </article>`;
}

function renderDownloads() {
  const values = state.downloads;
  $("#download-count").textContent = `${values.length} 条`;
  $("#download-synced").textContent = state.downloadSyncedAt ? `同步于 ${new Date(state.downloadSyncedAt).toLocaleTimeString("zh-CN", {hour12:false})}` : "等待读取";
  $("#show-hidden-downloads").textContent = `已隐藏 ${state.hiddenDownloads.length} 条`;
  $("#downloads-list").innerHTML = values.length ? values.map(downloadMarkup).join("") : '<div class="empty">当前没有下载记录</div>';
  const active = values.filter((item) => ACTIVE_DOWNLOAD_STATES.has(item.state)).slice(0,4);
  $("#overview-downloads").innerHTML = active.length ? active.map(downloadMarkup).join("") : '<div class="empty">队列安静，当前没有进行中的下载</div>';
  renderHiddenDownloads();
}

function renderHiddenDownloads() {
  $("#hidden-downloads-list").innerHTML = state.hiddenDownloads.length ? state.hiddenDownloads.map((item) => `<article class="hidden-download-item"><strong title="${escapeHTML(item.name)}">${escapeHTML(item.name || item.hash)}</strong><button class="quiet-button" data-restore-download="${escapeHTML(item.hash)}" type="button">恢复显示</button><small>${escapeHTML(stateLabel(item.state))} · ${formatDate(item.dismissed_at)}</small></article>`).join("") : '<div class="empty">没有隐藏的下载记录</div>';
}

async function dismissDownload(taskHash, button) {
  if (!taskHash || !confirm("只从 AVS 页面隐藏这条记录；下载器任务和文件都会保留。确定隐藏吗？")) return;
  button.disabled = true;
  try { await api(`/api/downloader/tasks/${encodeURIComponent(taskHash)}/dismiss`, {method:"POST", body:"{}"}); toast("记录已从 AVS 隐藏，下载器未受影响"); await refreshDownloads(true); }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function restoreDownload(taskHash, button) {
  button.disabled = true;
  try { await api(`/api/downloader/tasks/${encodeURIComponent(taskHash)}/dismiss`, {method:"DELETE"}); toast("记录已恢复显示"); await refreshDownloads(true); }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function recoverDownload(taskHash, button) {
  if (!taskHash || !confirm("AVS 将要求下载器重新校验这个任务，并恢复下载缺失的数据块；不会删除或重新添加任务。继续吗？")) return;
  button.disabled = true;
  try { await api(`/api/downloader/tasks/${encodeURIComponent(taskHash)}/recover`, {method:"POST", body:"{}"}); toast("重新校验与恢复已启动"); await refreshDownloads(true); }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function renderWatchlist() {
  const query = $("#watch-filter").value.trim().toLowerCase();
  const values = state.watchlist.filter((item) => !query || item.keyword.toLowerCase().includes(query));
  $("#watchlist-body").innerHTML = values.length ? values.map((item) => `<tr>
    <td><div class="cell-main">${escapeHTML(item.keyword)}<small>${escapeHTML(item.id.slice(0,8))}</small></div></td>
    <td>${escapeHTML(mediaTypeLabel(item.type))}</td><td>${chip(item.status)}</td>
    <td>${item.downloaded_episodes?.length || item.downloaded_links?.length || 0}</td><td>${formatDate(item.last_check)}</td>
    <td><button class="danger-button" data-delete-watch="${escapeHTML(item.id)}" type="button">移除</button></td>
  </tr>`).join("") : '<tr><td colspan="6"><div class="empty">没有符合条件的监听任务</div></td></tr>';
}

async function copyText(value) {
  try { await navigator.clipboard.writeText(value); }
  catch (_) {
    const textarea = document.createElement("textarea");
    textarea.value = value; textarea.style.position = "fixed"; textarea.style.opacity = "0";
    document.body.append(textarea); textarea.select(); document.execCommand("copy"); textarea.remove();
  }
}

function renderHardlinks() {
  const filter = $("#hardlink-filter").value;
  const values = state.hardlinks.filter((item) => filter === "all" || item.status === filter);
  $("#hardlinks-body").innerHTML = values.length ? values.map((item) => `<tr>
    <td><div class="cell-main">${escapeHTML(item.name || "未命名")}<small>${escapeHTML(item.id.slice(0,8))}</small></div></td>
    <td>${escapeHTML(mediaTypeLabel(item.type))}</td>
    <td><button class="target-button" data-copy-target="${escapeHTML(item.target || "")}" title="点击复制目录：${escapeHTML(item.target || "")}" type="button">${escapeHTML(item.target || "—")}</button></td>
    <td>${Number(item.linked || 0)} 链接 / ${Number(item.skipped || 0)} 跳过</td><td>${chip(item.status)}</td>
    <td class="error-text" title="${escapeHTML(item.error || "")}">${escapeHTML(item.error || "—")}</td><td>${formatDate(item.completed_at)}</td>
    <td>${jobActionMarkup(item.id, ["failed","retrying","waiting_download"].includes(item.status), item.status === "failed")}</td>
  </tr>`).join("") : '<tr><td colspan="8"><div class="empty">暂无符合条件的硬链接任务</div></td></tr>';
  const recent = state.hardlinks.slice(0,4);
  $("#overview-hardlinks").innerHTML = recent.length ? recent.map((item) => `<button class="activity hardlink-activity" data-go="hardlinks" type="button">${chip(item.status)}<strong title="${escapeHTML(item.name)}">${escapeHTML(item.name || "未命名")}</strong><small>${Number(item.linked || 0)} 个文件 · ${formatDate(item.completed_at)}</small></button>`).join("") : '<div class="empty">尚无硬链接记录</div>';
}

function renderSites() {
  $("#site-count").textContent = `${state.sites.length} 个`;
  $("#sites-body").innerHTML = state.sites.length ? state.sites.map((item) => `<tr>
    <td><div class="cell-main">${escapeHTML(item.name)}<small>${escapeHTML(item.id)}</small></div></td>
    <td>${item.adapter === "sixv" ? "6v 内置" : "通用 HTML"}</td><td class="target-text" title="${escapeHTML(item.base_urls?.[0] || "")}">${escapeHTML(item.base_urls?.[0] || "—")}</td>
    <td>${chip(item.enabled ? "completed" : "site_disabled")}</td>
    <td><div class="inline-actions"><button class="quiet-button" data-toggle-site="${escapeHTML(item.id)}" data-enabled="${item.enabled}" type="button">${item.enabled ? "停用" : "启用"}</button><button class="danger-button" data-delete-site="${escapeHTML(item.id)}" type="button">删除</button></div></td>
  </tr>`).join("") : '<tr><td colspan="5"><div class="empty">尚未配置站点</div></td></tr>';
}

function renderNaming() {
  const filter = $("#naming-filter").value;
  const values = state.naming.filter((item) => !filter || namingStatus(item) === filter);
  $("#naming-body").innerHTML = values.length ? values.map((item) => `<tr>
    <td><div class="cell-main">${escapeHTML(item.plan?.root_name || "未命名")}<small>${escapeHTML(mediaTypeLabel(item.plan?.media_type))}</small></div></td>
    <td title="${escapeHTML(item.plan?.link_name || "")}">${escapeHTML(item.plan?.link_name || "—")}</td><td>${chip(namingStatus(item))}</td>
    <td>${item.result?.file_count ?? item.result?.video_count ?? "—"}</td><td>${formatDate(item.updated_at)}</td>
    <td class="error-text" title="${escapeHTML(item.hardlink_error || item.last_error || "")}">${escapeHTML(item.hardlink_error || item.last_error || "—")}</td>
    <td>${jobActionMarkup(
      item.id,
      ["pending","waiting_metadata","waiting_download","retrying","failed"].includes(item.status) || ["waiting_download","retrying","failed"].includes(item.hardlink_status),
      item.status === "failed" || item.hardlink_status === "failed",
    )}</td>
  </tr>`).join("") : '<tr><td colspan="7"><div class="empty">暂无命名任务</div></td></tr>';
  const recent = state.naming.slice(0,4);
  $("#overview-naming").innerHTML = recent.length ? recent.map((item) => `<button class="activity hardlink-activity" data-go="naming" type="button">${chip(namingStatus(item))}<strong title="${escapeHTML(item.plan?.root_name)}">${escapeHTML(item.plan?.root_name || "未命名")}</strong><small>${formatDate(item.updated_at)}</small></button>`).join("") : '<div class="empty">尚无规范命名记录</div>';
}

function jobActionMarkup(id, retryable, deletable) {
  return `<div class="job-actions"><button class="quiet-button" data-job-detail="${escapeHTML(id)}" type="button">详情</button>${retryable ? `<button class="quiet-button" data-retry-job="${escapeHTML(id)}" type="button">重试</button>` : ""}${deletable ? `<button class="danger-button" data-delete-job="${escapeHTML(id)}" type="button">删除记录</button>` : ""}</div>`;
}

function renderJobDetail(item) {
  const error = item.hardlink_error || item.last_error || "当前没有错误";
  $("#job-detail-title").textContent = item.plan?.root_name || "任务详情";
  $("#job-detail-summary").innerHTML = [
    ["媒体类型", mediaTypeLabel(item.plan?.media_type)],
    ["命名状态", `${stateLabel(item.status)} · ${Number(item.attempts || 0)} 次失败尝试`],
    ["硬链接状态", `${stateLabel(item.hardlink_status)} · ${Number(item.hardlink_attempts || 0)} 次失败尝试`],
    ["Torrent Hash", item.torrent_hash || "—"],
    ["下载分类", item.final_category || "—"],
    ["最近检查", formatDate(item.last_check)],
  ].map(([label,value]) => `<div class="job-detail-item"><span>${escapeHTML(label)}</span><strong>${escapeHTML(value)}</strong></div>`).join("");
  $("#job-detail-error").textContent = error;
  $("#job-detail-json").textContent = JSON.stringify(item, null, 2);
  $("#job-detail-logs").dataset.jobId = item.id;
}

async function showJobDetail(jobId) {
  try {
    const result = await api(`/api/naming/jobs/${encodeURIComponent(jobId)}`);
    renderJobDetail(result.item);
    openModal("#job-detail-modal");
  } catch (error) { toast(error.message, true); }
}

async function showJobLogs(jobId) {
  $("#job-detail-modal").classList.add("hidden");
  $("#log-query").value = jobId || "";
  setTab("logs");
  try {
    state.logs = (await api(`/api/logs?query=${encodeURIComponent(jobId || "")}&limit=500`)).items;
    renderLogs();
  } catch (error) { toast(error.message, true); }
}

async function retryJob(jobId, button) {
  if (!confirm("只重试这个 AVS 命名/硬链接任务，不会删除或重新添加下载器任务。继续吗？")) return;
  button.disabled = true;
  try {
    const report = await api(`/api/naming/jobs/${encodeURIComponent(jobId)}/retry`, {method:"POST", body:"{}"});
    toast(report.running ? "任务正在由其他检查处理" : "任务重试已执行");
    await loadAll(true);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function deleteFailedJob(jobId, button) {
  if (!confirm("仅删除这条 AVS 失败记录；下载器任务、下载文件和媒体库文件都会保留。确定删除吗？")) return;
  button.disabled = true;
  try {
    await api(`/api/naming/jobs/${encodeURIComponent(jobId)}`, {method:"DELETE"});
    $("#job-detail-modal").classList.add("hidden");
    toast("失败记录已删除，下载器和文件未受影响");
    await loadAll(true);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function renderAutomation() {
  const waiting = state.naming.filter((item) => ["pending","waiting_metadata","retrying"].includes(item.status) || ["waiting_download","retrying"].includes(item.hardlink_status)).length;
  const failed = state.naming.filter((item) => item.status === "failed" || item.hardlink_status === "failed").length;
  const online = state.qb?.connected;
  const downloader = state.qb?.client === "transmission" ? "Transmission" : "qBittorrent";
  const hours = state.systemSettings?.watchlist_check_hours || 12;
  $("#automation-status").innerHTML = `
    <div class="status-row ${online ? "" : "error"}"><i></i><span>${downloader} 连接</span><small>${online ? escapeHTML(state.qb.version) : "不可用"}</small></div>
    <div class="status-row ${waiting ? "warn" : ""}"><i></i><span>等待命名或入库</span><small>${waiting} 项</small></div>
    <div class="status-row ${failed ? "error" : ""}"><i></i><span>命名或入库失败</span><small>${failed} 项</small></div>
    <div class="status-row"><i></i><span>订阅定时检查</span><small>每 ${hours} 小时</small></div>`;
}

const MEDIA_GROUPS = [
  ["movie", "电影", "MOVIES"], ["tv", "电视剧", "SERIES"], ["anime", "动漫", "ANIMATION"], ["custom", "自定义", "CUSTOM"],
];

function renderPathRules() {
  const html = MEDIA_GROUPS.map(([type, label, code]) => {
    const rules = state.pathRules.filter((item) => item.media_type === type);
    const body = rules.length ? rules.map((item) => `<article class="rule-item ${item.enabled ? "" : "off"}">
      <div class="rule-item-head"><h4>${escapeHTML(item.name)}</h4><div class="rule-badges">${item.default_download ? '<span class="count-chip">默认下载</span>' : ""}${chip(item.enabled ? "completed" : "site_disabled")}</div></div>
      <div class="rule-path"><span title="${escapeHTML(item.source_path)}">${escapeHTML(item.source_path)}</span><i>→</i><span title="${escapeHTML(item.target_path)}">${escapeHTML(item.target_path)}</span></div>
      <div class="rule-actions"><span class="count-chip">${item.rename_enabled ? "规范命名" : "保留原名"}</span><button class="quiet-button" data-edit-rule="${item.id}" type="button">编辑</button><button class="danger-button" data-delete-rule="${item.id}" type="button">删除</button></div>
    </article>`).join("") : '<div class="empty">这个分类还没有目录映射</div>';
    return `<section class="rule-group"><div class="rule-group-head"><div><p class="eyebrow">${code}</p><h3>${label}</h3></div><small>${rules.length} 个源目录</small></div><div class="rule-grid">${body}</div></section>`;
  }).join("");
  $("#path-rules-groups").innerHTML = html;
}

function renderPaths() {
  if (state.paths && !state.pathsDirty) {
    $("#path-medialib-base").value = state.paths.medialib_base_path || "";
    $("#path-medialib-mount").value = state.paths.medialib_mount_path || "";
    $("#path-hardlink-enabled").checked = Boolean(state.paths.medialib_hardlink_enabled);
  }
  renderPathRules();
}

function renderSystemSettings() {
  if (state.systemSettings && !state.systemDirty) $("#watchlist-hours").value = state.systemSettings.watchlist_check_hours;
  if (!state.downloaderSettings || state.downloaderDirty) return;
  const item = state.downloaderSettings;
  $("#downloader-type").value = item.downloader_type;
  $("#qb-host").value = item.qb_host || ""; $("#qb-port").value = item.qb_port || 8080; $("#qb-username").value = item.qb_username || "";
  $("#qb-password").value = ""; $("#qb-password").placeholder = item.qb_password_configured ? "已配置；留空保持原密码" : "尚未配置";
  $("#qb-https").checked = Boolean(item.qb_use_https); $("#qb-verify").checked = Boolean(item.qb_verify_ssl);
  $("#tr-host").value = item.transmission_host || ""; $("#tr-port").value = item.transmission_port || 9091; $("#tr-username").value = item.transmission_username || "";
  $("#tr-password").value = ""; $("#tr-password").placeholder = item.transmission_password_configured ? "已配置；留空保持原密码" : "尚未配置";
  $("#tr-rpc-path").value = item.transmission_rpc_path || "/transmission/rpc"; $("#tr-https").checked = Boolean(item.transmission_use_https); $("#tr-verify").checked = Boolean(item.transmission_verify_ssl);
  toggleDownloaderFields();
}

function renderAgents() {
  const active = state.agents.filter((item) => !item.revoked);
  const button = $("#agent-connect");
  button.classList.toggle("online", active.length > 0);
  button.querySelector("span").textContent = active.length ? `${active.length} 个 Agent 已授权` : "连接 Agent";
  $("#agent-count").textContent = `${state.agents.length} 个`;
  $("#agent-list").innerHTML = state.agents.length ? state.agents.map((item) => `<div class="agent-item ${item.online ? "online" : ""}"><i></i><div><strong>${escapeHTML(item.name)}</strong><small>${item.revoked ? "已撤销" : item.last_seen ? `最近使用 · ${formatDate(item.last_seen)}` : `已授权 · ${formatDate(item.created_at)}`}</small></div>${item.revoked ? "" : `<button class="danger-button" data-revoke-agent="${item.id}" type="button">撤销</button>`}</div>`).join("") : '<div class="empty">尚未授权 Agent</div>';
}

function renderLogs() {
  const level = $("#log-level").value;
  const query = $("#log-query").value.trim().toLowerCase();
  const values = state.logs.filter((item) => (!level || item.level === level) && (!query || JSON.stringify(item).toLowerCase().includes(query)));
  $("#log-count").textContent = `${values.length} 条`;
  $("#log-list").innerHTML = values.length ? values.map((item) => {
    const details = {...item};
    ["timestamp", "level", "logger", "message"].forEach((key) => delete details[key]);
    const detailText = Object.keys(details).length ? JSON.stringify(details, null, 2) : "";
    return `<article class="log-entry"><time>${escapeHTML(item.timestamp ? new Date(item.timestamp).toLocaleString("zh-CN", {hour12:false}) : "—")}</time><span class="log-level ${escapeHTML(item.level)}">${escapeHTML(String(item.level || "info").toUpperCase())}</span><span class="log-source">${escapeHTML(item.logger || "app")}</span><span class="log-message">${escapeHTML(item.message || "—")}</span>${detailText ? `<details class="log-detail"><summary>查看上下文</summary><pre>${escapeHTML(detailText)}</pre></details>` : ""}</article>`;
  }).join("") : '<div class="empty">没有符合条件的日志</div>';
}

function renderAll() {
  renderMetrics(); renderDownloads(); renderWatchlist(); renderNaming(); renderHardlinks(); renderSites(); renderPaths(); renderSystemSettings(); renderAgents(); renderAutomation(); renderLogs();
  const online = state.qb?.connected;
  const downloader = state.qb?.client === "transmission" ? "Transmission" : "qBittorrent";
  $("#downloader-name").textContent = downloader;
  $("#qb-version").textContent = online ? `在线 / ${state.qb.version}` : state.qb?.configured ? "连接异常" : "尚未配置";
  $("#site-status").className = `signal ${online ? "ok" : state.qb?.configured ? "error" : ""}`;
  $("#site-status span").textContent = online ? "系统在线" : "服务已连接";
  $("#last-updated").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", {hour12:false})}`;
}

async function loadAll(silent = false) {
  if (state.loading) return;
  state.loading = true; $("#refresh-all").classList.add("loading");
  try {
    const paths = [
      "/api/downloader/status", "/api/downloader/tasks", "/api/watchlist", "/api/naming/jobs?per_page=100",
      "/api/hardlinks?status=all&per_page=100", "/api/settings/sites", "/api/settings/paths", "/api/settings/path-rules",
      "/api/settings/downloader", "/api/settings/system", "/api/agents", "/api/logs?limit=500",
    ];
    const requests = await Promise.allSettled(paths.map((path) => api(path)));
    const authFailure = requests.find((item) => item.status === "rejected" && item.reason instanceof AuthError);
    if (authFailure) throw authFailure.reason;
    const value = (index, fallback) => requests[index].status === "fulfilled" ? requests[index].value : fallback;
    const downloadsResult = value(1, {tasks:[]});
    state.qb = value(0, {configured:false,connected:false}); state.downloads = downloadsResult.tasks; state.hiddenDownloads = downloadsResult.hidden_tasks || [];
    state.downloadSyncedAt = downloadsResult.synced_at || new Date().toISOString();
    state.watchlist = value(2, {items:[]}).items; state.naming = value(3, {items:[]}).items; state.hardlinks = value(4, {items:[]}).items;
    state.sites = value(5, {items:[]}).items; state.paths = value(6, {settings:state.paths}).settings; state.pathRules = value(7, {items:[]}).items;
    state.downloaderSettings = value(8, {settings:state.downloaderSettings}).settings; state.systemSettings = value(9, {settings:state.systemSettings}).settings; state.agents = value(10, {items:[]}).items; state.logs = value(11, {items:[]}).items;
    renderAll(); $("#auth-modal").classList.add("hidden");
    if (!silent && requests.some((item) => item.status === "rejected")) toast("部分数据暂时不可用", true);
  } catch (error) {
    if (error instanceof AuthError) showAuth(error.message); else if (!silent) toast(error.message, true);
  } finally { state.loading = false; $("#refresh-all").classList.remove("loading"); }
}

async function refreshDownloads(silent = false) {
  if (state.loading || state.downloadsLoading || document.hidden || !state.apiKey) return;
  state.downloadsLoading = true;
  const button = $("#refresh-downloads");
  button.disabled = true;
  try {
    const result = await api("/api/downloader/tasks");
    state.downloads = result.tasks || [];
    state.hiddenDownloads = result.hidden_tasks || [];
    state.downloadSyncedAt = result.synced_at || new Date().toISOString();
    renderDownloads(); renderMetrics();
  } catch (error) {
    if (error instanceof AuthError) showAuth(error.message); else if (!silent) toast(error.message, true);
  } finally { state.downloadsLoading = false; button.disabled = false; }
}

async function runAction(button, path, message, body = {}) {
  button.disabled = true;
  try { await api(path, {method:"POST", body:JSON.stringify(body)}); toast(message); await loadAll(true); }
  catch (error) { if (error instanceof AuthError) showAuth(error.message); else toast(error.message, true); }
  finally { button.disabled = false; }
}

async function loadLogs() {
  const button = $("#refresh-logs");
  button.disabled = true;
  try { state.logs = (await api("/api/logs?limit=500")).items; renderLogs(); toast("日志已刷新"); }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function toggleDownloaderFields() {
  const selected = $("#downloader-type").value;
  $$('[data-downloader]').forEach((element) => element.classList.toggle("hidden", element.dataset.downloader !== selected));
}

function editPathRule(item = null) {
  $("#path-rule-title").textContent = item ? "编辑目录映射" : "新增目录映射";
  $("#path-rule-id").value = item?.id || ""; $("#path-rule-type").value = item?.media_type || "movie"; $("#path-rule-type").disabled = Boolean(item);
  $("#path-rule-name").value = item?.name || ""; $("#path-rule-source").value = item?.source_path || ""; $("#path-rule-target").value = item?.target_path || "";
  $("#path-rule-enabled").checked = item?.enabled ?? true; $("#path-rule-rename").checked = item?.rename_enabled ?? true; $("#path-rule-default").checked = item?.default_download ?? false;
  openModal("#path-rule-modal");
}

function manualSourceChanged() {
  const selected = $('input[name="download-source"]:checked').value;
  $$('[data-source-panel]').forEach((element) => element.classList.toggle("hidden", element.dataset.sourcePanel !== selected));
}

function agentPrompt(agent) {
  const origin = location.origin;
  return `请连接我的 AI Video Station，并把它作为媒体自动化工具使用。\n\n服务地址：${origin}\nOpenAPI：${origin}/openapi.yaml\n专用令牌：${agent.token}\nCLI：python -m ainas.cli\n\n连接方式：\n1. 所有 /api 请求使用 Authorization: Bearer ${agent.token}\n2. 首次使用时 POST ${origin}/api/agents/connect，JSON 为 {"name":"我的 Agent","capabilities":["search","download","watchlist","naming","hardlink","logs"]}\n3. 不需要发送心跳；仅在需要查看或操作时调用 API，任意有效请求都会更新最近使用时间\n4. 也可以设置 AVS_URL=${origin} 与 AVS_TOKEN 后使用 CLI；先运行 python -m ainas.cli status\n5. 搜索时省略 add_to_watchlist 或明确传 false；只有用户明确要求订阅时才调用 /api/watchlist/add\n6. 读取 OpenAPI 后再调用业务接口；涉及新增下载、删除或修改设置时，先向我确认目标\n7. 不要在回复、日志或其他文件中再次显示这枚令牌。`;
}

function bindEvents() {
  document.addEventListener("click", (event) => {
    const go = event.target.closest("[data-go]"); if (go) setTab(go.dataset.go);
    const close = event.target.closest("[data-close-modal]"); if (close) closeModal(close);
    const detail = event.target.closest("[data-job-detail]"); if (detail) showJobDetail(detail.dataset.jobDetail);
    const retry = event.target.closest("[data-retry-job]"); if (retry) retryJob(retry.dataset.retryJob, retry);
    const removeJob = event.target.closest("[data-delete-job]"); if (removeJob) deleteFailedJob(removeJob.dataset.deleteJob, removeJob);
    const dismissDownloadButton = event.target.closest("[data-dismiss-download]"); if (dismissDownloadButton) dismissDownload(dismissDownloadButton.dataset.dismissDownload, dismissDownloadButton);
    const restoreDownloadButton = event.target.closest("[data-restore-download]"); if (restoreDownloadButton) restoreDownload(restoreDownloadButton.dataset.restoreDownload, restoreDownloadButton);
    const recoverDownloadButton = event.target.closest("[data-recover-download]"); if (recoverDownloadButton) recoverDownload(recoverDownloadButton.dataset.recoverDownload, recoverDownloadButton);
  });
  $$(".tab").forEach((button) => button.addEventListener("click", () => setTab(button.dataset.tab)));
  $$('[data-open-download]').forEach((button) => button.addEventListener("click", () => openModal("#download-modal")));
  $("#refresh-all").addEventListener("click", () => loadAll()); $("#refresh-downloads").addEventListener("click", () => refreshDownloads()); $("#show-hidden-downloads").addEventListener("click", () => openModal("#hidden-downloads-modal")); $("#watch-filter").addEventListener("input", renderWatchlist);
  $("#naming-filter").addEventListener("change", renderNaming); $("#hardlink-filter").addEventListener("change", renderHardlinks);
  $("#log-level").addEventListener("change", renderLogs); $("#log-query").addEventListener("input", renderLogs); $("#refresh-logs").addEventListener("click", loadLogs);
  $("#job-detail-logs").addEventListener("click", (event) => showJobLogs(event.currentTarget.dataset.jobId));
  $("#check-watchlist").addEventListener("click", (event) => runAction(event.currentTarget, "/api/watchlist/check", "监听检查已完成"));
  $("#check-naming").addEventListener("click", (event) => runAction(event.currentTarget, "/api/naming/jobs/check", "命名任务已处理"));
  $("#watchlist-body").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-delete-watch]"); if (!button || !confirm("确定移除这个监听任务吗？")) return;
    try { await api(`/api/watchlist/${button.dataset.deleteWatch}`, {method:"DELETE"}); toast("监听已移除"); await loadAll(true); } catch (error) { toast(error.message, true); }
  });
  $("#hardlinks-body").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy-target]"); if (!button || !button.dataset.copyTarget) return;
    await copyText(button.dataset.copyTarget); toast("目标目录已复制，可在 NAS 文件管理器中打开");
  });
  $("#site-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {name:$("#site-name").value.trim(),adapter:"generic",enabled:true,base_urls:[$("#site-base").value.trim()],search_url:$("#site-search-url").value.trim(),result_selector:$("#site-result-selector").value.trim(),title_selector:$("#site-title-selector").value.trim(),link_selector:$("#site-link-selector").value.trim(),download_selector:$("#site-download-selector").value.trim(),default_type:$("#site-default-type").value,tv_path_patterns:$("#site-tv-patterns").value.split(",").map((item)=>item.trim()).filter(Boolean),anime_path_patterns:$("#site-anime-patterns").value.split(",").map((item)=>item.trim()).filter(Boolean)};
    try { await api("/api/settings/sites", {method:"POST",body:JSON.stringify(payload)}); toast("站点已保存"); event.currentTarget.reset(); await loadAll(true); } catch (error) { toast(error.message, true); }
  });
  $("#sites-body").addEventListener("click", async (event) => {
    const toggle = event.target.closest("[data-toggle-site]"); const remove = event.target.closest("[data-delete-site]");
    try { let changed = false; if (toggle) { await api(`/api/settings/sites/${toggle.dataset.toggleSite}`, {method:"PATCH",body:JSON.stringify({enabled:toggle.dataset.enabled!=="true"})}); changed = true; } if (remove && confirm("确定删除这个站点配置吗？")) { await api(`/api/settings/sites/${remove.dataset.deleteSite}`, {method:"DELETE"}); changed = true; } if (changed) { toast("站点配置已更新"); await loadAll(true); } } catch (error) { toast(error.message, true); }
  });
  $('input[name="download-source"][value="magnet"]').closest(".source-switch").addEventListener("change", manualSourceChanged);
  $("#manual-download-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = $("#submit-manual-download"); const source = $('input[name="download-source"]:checked').value;
    const shared = {title:$("#manual-title").value.trim() || undefined,type:$("#manual-type").value,original_title:$("#manual-original-title").value.trim() || undefined,edition:$("#manual-edition").value.trim() || undefined,episode_title:$("#manual-episode-title").value.trim() || undefined};
    button.disabled = true;
    try {
      if (source === "magnet") { const link = $("#manual-link").value.trim(); if (!link) throw new Error("请输入磁力链接或种子链接"); await api("/api/download/manual", {method:"POST",body:JSON.stringify({...shared,download_link:link})}); }
      else { const file = $("#manual-torrent").files[0]; if (!file) throw new Error("请选择 .torrent 文件"); const form = new FormData(); form.append("torrent", file); Object.entries(shared).forEach(([key,value]) => { if (value !== undefined) form.append(key,value); }); await api("/api/download/manual", {method:"POST",body:form}); }
      toast("资源已识别并加入下载队列"); $("#download-modal").classList.add("hidden"); event.currentTarget.reset(); manualSourceChanged(); await loadAll(true);
    } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
  });
  $("#path-hardlink-enabled").addEventListener("change", () => { state.pathsDirty = true; });
  $("#save-hardlink-toggle").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try { const result = await api("/api/settings/paths", {method:"PATCH",body:JSON.stringify({medialib_hardlink_enabled:$("#path-hardlink-enabled").checked})}); state.paths = result.settings; state.pathsDirty = false; toast("硬链接开关已保存"); }
    catch (error) { toast(error.message, true); } finally { event.currentTarget.disabled = false; }
  });
  $("#add-path-rule").addEventListener("click", () => editPathRule());
  $("#path-rules-groups").addEventListener("click", async (event) => {
    const edit = event.target.closest("[data-edit-rule]"); const remove = event.target.closest("[data-delete-rule]");
    if (edit) editPathRule(state.pathRules.find((item) => item.id === edit.dataset.editRule));
    if (remove && confirm("确定删除这条目录映射吗？")) { try { await api(`/api/settings/path-rules/${remove.dataset.deleteRule}`, {method:"DELETE"}); toast("目录映射已删除"); await loadAll(true); } catch (error) { toast(error.message, true); } }
  });
  $("#path-rule-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const id = $("#path-rule-id").value; const payload = {media_type:$("#path-rule-type").value,name:$("#path-rule-name").value.trim(),source_path:$("#path-rule-source").value.trim(),target_path:$("#path-rule-target").value.trim(),enabled:$("#path-rule-enabled").checked,rename_enabled:$("#path-rule-rename").checked,default_download:$("#path-rule-default").checked};
    if (id) delete payload.media_type;
    try { await api(id ? `/api/settings/path-rules/${id}` : "/api/settings/path-rules", {method:id?"PATCH":"POST",body:JSON.stringify(payload)}); toast("目录映射已保存"); $("#path-rule-modal").classList.add("hidden"); await loadAll(true); } catch (error) { toast(error.message, true); }
  });
  $("#header-theme-mode").addEventListener("change", () => { state.theme = {...state.theme, mode:$("#header-theme-mode").value}; saveTheme(); toast("主题已切换"); });
  $("#theme-mode").addEventListener("change", () => $("#theme-schedule").classList.toggle("hidden", $("#theme-mode").value !== "schedule"));
  $("#theme-form").addEventListener("submit", (event) => { event.preventDefault(); state.theme = {mode:$("#theme-mode").value,lightStart:$("#theme-light-start").value,darkStart:$("#theme-dark-start").value}; saveTheme(); toast("主题设置已保存"); });
  $("#system-form").addEventListener("input", () => { state.systemDirty = true; });
  $("#system-form").addEventListener("submit", async (event) => { event.preventDefault(); const button = event.currentTarget.querySelector("button"); button.disabled = true; try { const result = await api("/api/settings/system", {method:"PATCH",body:JSON.stringify({watchlist_check_hours:Number($("#watchlist-hours").value)})}); state.systemSettings = result.settings; state.systemDirty = false; toast("订阅检查周期已更新"); renderAutomation(); } catch (error) { toast(error.message, true); } finally { button.disabled = false; } });
  $("#downloader-type").addEventListener("change", () => { state.downloaderDirty = true; toggleDownloaderFields(); });
  $("#downloader-form").addEventListener("input", () => { state.downloaderDirty = true; });
  $("#downloader-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = $("#save-downloader"); const payload = {downloader_type:$("#downloader-type").value,qb_host:$("#qb-host").value.trim() || null,qb_port:Number($("#qb-port").value),qb_username:$("#qb-username").value.trim(),qb_use_https:$("#qb-https").checked,qb_verify_ssl:$("#qb-verify").checked,transmission_host:$("#tr-host").value.trim() || null,transmission_port:Number($("#tr-port").value),transmission_username:$("#tr-username").value.trim(),transmission_use_https:$("#tr-https").checked,transmission_verify_ssl:$("#tr-verify").checked,transmission_rpc_path:$("#tr-rpc-path").value.trim()}; if ($("#qb-password").value) payload.qb_password=$("#qb-password").value; if ($("#tr-password").value) payload.transmission_password=$("#tr-password").value;
    button.disabled = true; try { const result = await api("/api/settings/downloader", {method:"PATCH",body:JSON.stringify(payload)}); state.downloaderSettings = result.settings; state.downloaderDirty = false; toast("下载器配置已保存并切换"); await loadAll(true); } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
  });
  $("#test-downloader").addEventListener("click", async (event) => { event.currentTarget.disabled = true; try { const result = await api("/api/settings/downloader/test", {method:"POST",body:"{}"}); toast(`${result.client === "transmission" ? "Transmission" : "qBittorrent"} 连接成功：${result.version}`); } catch (error) { toast(error.message, true); } finally { event.currentTarget.disabled = false; } });
  $("#agent-connect").addEventListener("click", () => openModal("#agent-modal"));
  $("#create-agent-config").addEventListener("click", async (event) => { event.currentTarget.disabled = true; try { const result = await api("/api/agents/bootstrap", {method:"POST",body:JSON.stringify({name:"My Agent"})}); const prompt = agentPrompt(result.agent); $("#agent-config").value = prompt; $("#agent-config-wrap").classList.remove("hidden"); await copyText(prompt); toast("连接方案已复制，请发给你的 Agent"); await loadAll(true); } catch (error) { toast(error.message, true); } finally { event.currentTarget.disabled = false; } });
  $("#copy-agent-config").addEventListener("click", async () => { await copyText($("#agent-config").value); toast("连接方案已复制"); });
  $("#agent-list").addEventListener("click", async (event) => { const button = event.target.closest("[data-revoke-agent]"); if (!button || !confirm("撤销后该 Agent 将立即无法访问，确定继续吗？")) return; try { await api(`/api/agents/${button.dataset.revokeAgent}`, {method:"DELETE"}); toast("Agent 授权已撤销"); await loadAll(true); } catch (error) { toast(error.message, true); } });
  $("#auth-form").addEventListener("submit", async (event) => { event.preventDefault(); state.apiKey = $("#api-key").value.trim(); sessionStorage.setItem("aiVideoStationApiKey", state.apiKey); await loadAll(); });
  $("#logout").addEventListener("click", () => { sessionStorage.removeItem("aiVideoStationApiKey"); sessionStorage.removeItem("sixvApiKey"); state.apiKey = ""; $("#api-key").value = ""; showAuth(); });
}

function tickClock() { $("#clock").textContent = new Date().toLocaleTimeString("zh-CN", {hour12:false}); if (state.theme.mode === "schedule") applyTheme(); }
applyTheme(); bindEvents(); tickClock();
const initialTab = location.hash.slice(1); if ($(`[data-panel="${CSS.escape(initialTab)}"]`)) setTab(initialTab);
loadAll(); setInterval(tickClock, 60000);
setInterval(() => refreshDownloads(true), 15000);
