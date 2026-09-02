"use strict";

const DEFAULT_THEME = {mode: "dark", lightStart: "07:00", darkStart: "19:00"};
function storedTheme() {
  try { return JSON.parse(localStorage.getItem("aiVideoStationTheme") || "{}"); }
  catch (_) { return {}; }
}
const state = {
  apiKey: sessionStorage.getItem("aiVideoStationApiKey") || sessionStorage.getItem("sixvApiKey") || "",
  watchlist: [], downloads: [], hiddenDownloads: [], naming: [], hardlinks: [], cleanupPlans: [], sites: [], pathRules: [], agents: [], logs: [],
  paths: null, downloaderSettings: null, systemSettings: null, qb: null, loading: false,
  pathsDirty: false, downloaderDirty: false, systemDirty: false, downloadsLoading: false, downloadSyncedAt: null,
  searchResults: [], namingPage: 1, hardlinksPage: 1, namingMeta: {}, hardlinksMeta: {}, cleanupMeta: {}, cleanupExecution: null, modalReturnFocus: null,
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
  if (!response.ok) {
    const detail = body.detail || body.message || `请求失败 (${response.status})`;
    const errors = body.errors || body.problem?.errors || [];
    const error = new Error(`${Array.isArray(errors) && errors.length ? `${detail}：${errors.map((item) => item.message || item.msg || item).join("；")}` : detail}${(body.request_id || body.problem?.request_id) ? `（请求 ${body.request_id || body.problem?.request_id}）` : ""}`);
    error.errors = errors; error.requestId = body.request_id || body.problem?.request_id; error.problem = body.problem || body;
    throw error;
  }
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
    submitting:"提交中", awaiting_binding:"等待绑定下载器", waiting_metadata:"等待元数据", retrying:"重试中", failed:"失败", found:"已找到", monitoring:"追更中", expired:"超期监听",
    waiting_download:"等待下载完成", waiting_selection:"全集待确认", missing_in_downloader:"下载器中缺失", done:"硬链接完成", ready:"待处理", partial:"部分入库", conflict:"入库冲突", disabled:"硬链接已关闭", site_disabled:"已停用",
  };
  return labels[value] || value || "未知";
}

function chip(value) {
  const ok = ["completed","found","monitoring","downloading","uploading","pausedUP","done"].includes(value);
  const error = ["failed", "missing_in_downloader", "partial", "conflict"].includes(value);
  return `<span class="status-chip ${error ? "error" : ok ? "ok" : "wait"}">${escapeHTML(stateLabel(value))}</span>`;
}

function cleanupPlanChip(value) {
  const label = value === "partial" ? "部分失败" : stateLabel(value);
  const kind = value === "completed" ? "ok" : value === "partial" ? "error" : "wait";
  return `<span class="status-chip ${kind}">${escapeHTML(label)}</span>`;
}

function namingStatus(item) {
  if (["waiting_download", "retrying", "failed", "partial", "conflict"].includes(item.hardlink_status)) return item.hardlink_status;
  return item.hardlink_status === "done" ? "done" : item.status;
}

function mediaTypeLabel(value) {
  return ({movie:"电影", tv:"电视剧", anime:"动漫", custom:"自定义", auto:"自动"})[value] || value || "未知";
}

function viewingModeLabel(value) {
  return ({daily:"日常观看", collection:"收藏", compact:"省空间"})[value] || "日常观看";
}

let toastTimer;
function actionSummary(message, error = false, requestId = "") {
  const element = $("#action-summary");
  element.textContent = `${error ? "操作未完成：" : "操作结果："}${message}${requestId ? `（请求 ${requestId}）` : ""}`;
  element.className = `action-summary show${error ? " error" : ""}`;
}
function toast(message, error = false, requestId = "") {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.className = "toast"; }, 3200);
  actionSummary(message, error, requestId);
}
function showError(error) { toast(error?.message || "请求失败", true, error?.requestId || ""); }

function showAuth(message = "") {
  $("#auth-modal").classList.remove("hidden");
  $("#auth-error").textContent = message;
  setTimeout(() => $("#api-key").focus(), 50);
}

function openModal(selector, trigger = document.activeElement) { state.modalReturnFocus = trigger; const modal = $(selector); modal.classList.remove("hidden"); setTimeout(() => modal.querySelector("input,select,textarea,button")?.focus(), 0); }
function closeModal(element) { const modal = element.closest(".modal"); if (!modal) return; modal.classList.add("hidden"); state.modalReturnFocus?.focus?.(); }

function setTab(name) {
  $$(".tab").forEach((button) => { const active = button.dataset.tab === name; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); button.tabIndex = active ? 0 : -1; });
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
    <td>${escapeHTML(mediaTypeLabel(item.type))}</td><td><select class="filter-input compact-select" data-watch-mode="${escapeHTML(item.id)}" aria-label="${escapeHTML(item.keyword)}的观看模式"><option value="daily" ${item.viewing_mode === "daily" || !item.viewing_mode ? "selected" : ""}>日常观看</option><option value="collection" ${item.viewing_mode === "collection" ? "selected" : ""}>收藏</option><option value="compact" ${item.viewing_mode === "compact" ? "selected" : ""}>省空间</option></select></td><td>${chip(item.status)}</td>
    <td>${item.downloaded_episodes?.length || item.downloaded_links?.length || 0}</td><td>${formatDate(item.last_check)}</td>
    <td><button class="danger-button" data-delete-watch="${escapeHTML(item.id)}" type="button">移除</button></td>
  </tr>`).join("") : '<tr><td colspan="7"><div class="empty">没有符合条件的监听任务</div></td></tr>';
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
    <td>${item.source_kind === "directory_sync"
      ? `<div class="job-actions"><button class="quiet-button" data-sync-rule="${escapeHTML(item.path_rule_id || "")}" type="button">重新同步此目录</button></div>`
      : jobActionMarkup(
          item.id,
          ["failed","retrying","waiting_download","partial","conflict"].includes(item.status),
          ["failed","partial","conflict"].includes(item.status),
          false,
        )}</td>
  </tr>`).join("") : '<tr><td colspan="8"><div class="empty">暂无符合条件的硬链接任务</div></td></tr>';
  const recent = state.hardlinks.slice(0,4);
  $("#overview-hardlinks").innerHTML = recent.length ? recent.map((item) => `<button class="activity hardlink-activity" data-go="hardlinks" type="button">${chip(item.status)}<strong title="${escapeHTML(item.name)}">${escapeHTML(item.name || "未命名")}</strong><small>${Number(item.linked || 0)} 个文件 · ${formatDate(item.completed_at)}</small></button>`).join("") : '<div class="empty">尚无硬链接记录</div>';
  renderPagination("hardlinks", state.hardlinksMeta);
}

function renderSites() {
  $("#site-count").textContent = `${state.sites.length} 个`;
  $("#sites-body").innerHTML = state.sites.length ? state.sites.map((item) => `<tr>
    <td><div class="cell-main">${escapeHTML(item.name)}<small>${escapeHTML(item.id)}</small></div></td>
    <td>${item.adapter === "sixv" ? "6v 内置" : "通用 HTML"}</td><td class="target-text" title="${escapeHTML(item.base_urls?.[0] || "")}">${escapeHTML(item.base_urls?.[0] || "—")}</td>
    <td>${chip(item.enabled ? "completed" : "site_disabled")}</td>
    <td><div class="inline-actions"><button class="quiet-button" data-toggle-site="${escapeHTML(item.id)}" data-enabled="${item.enabled}" type="button">${item.enabled ? "停用" : "启用"}</button><button class="danger-button" data-delete-site="${escapeHTML(item.id)}" type="button">删除</button></div></td>
  </tr>`).join("") : '<tr><td colspan="5"><div class="empty">尚未配置站点</div></td></tr>';
  const selector = $("#provider-preview-site");
  const selected = selector.value;
  selector.innerHTML = state.sites.length ? state.sites.map((item) => `<option value="${escapeHTML(item.id)}" ${item.id === selected ? "selected" : ""}>${escapeHTML(item.name)}${item.enabled ? "" : "（已停用）"}</option>`).join("") : '<option value="">尚未配置站点</option>';
  $("#provider-preview-submit").disabled = state.sites.length === 0;
}

function renderNaming() {
  const filter = $("#naming-filter").value;
  const values = state.naming.filter((item) => !filter || namingStatus(item) === filter);
  $("#naming-body").innerHTML = values.length ? values.map((item) => `<tr>
    <td><div class="cell-main">${escapeHTML(item.plan?.root_name || "未命名")}<small>${escapeHTML(mediaTypeLabel(item.plan?.media_type))}</small></div></td>
    <td title="${escapeHTML(item.plan?.link_name || "")}">${escapeHTML(item.plan?.link_name || "—")}</td><td>${chip(namingStatus(item))}</td>
    <td>${item.result?.file_count ?? item.result?.video_count ?? "—"}</td><td>${formatDate(item.updated_at)}</td>
    <td class="error-text" title="${escapeHTML(item.hardlink_error || item.last_error || "")}">${escapeHTML(item.hardlink_error || item.last_error || "—")}</td>
    <td>${(() => {
      const retryable = ["pending","submitting","awaiting_binding","waiting_metadata","waiting_selection","waiting_download","retrying","failed","missing_in_downloader"].includes(item.status)
        || ["waiting_download","retrying","failed","partial","conflict"].includes(item.hardlink_status);
      const deletable = canDiscardJob(item);
      const correctable = ["failed","missing_in_downloader"].includes(item.status) || item.hardlink_status === "failed";
      return jobActionMarkup(item.id, retryable, deletable, correctable);
    })()}</td>
  </tr>`).join("") : '<tr><td colspan="7"><div class="empty">暂无命名任务</div></td></tr>';
  const recent = state.naming.slice(0,4);
  $("#overview-naming").innerHTML = recent.length ? recent.map((item) => `<button class="activity hardlink-activity" data-go="naming" type="button">${chip(namingStatus(item))}<strong title="${escapeHTML(item.plan?.root_name)}">${escapeHTML(item.plan?.root_name || "未命名")}</strong><small>${formatDate(item.updated_at)}</small></button>`).join("") : '<div class="empty">尚无规范命名记录</div>';
  renderPagination("naming", state.namingMeta);
}

function renderPagination(kind, meta = {}) {
  const total = Number(meta.total ?? meta.total_count ?? meta.count ?? (kind === "naming" ? state.naming.length : state.hardlinks.length));
  const page = Number(meta.page ?? state[`${kind}Page`] ?? 1); const perPage = Number(meta.per_page ?? meta.page_size ?? 50);
  const pages = Math.max(1, Math.ceil(total / perPage)); const root = $(`#${kind}-pagination`); if (!root) return;
  root.innerHTML = `<span>共 ${total} 条 · 第 ${page}/${pages} 页</span><div><button class="quiet-button" data-page-kind="${kind}" data-page="${page - 1}" type="button" ${page <= 1 ? "disabled" : ""}>上一页</button><button class="quiet-button" data-page-kind="${kind}" data-page="${page + 1}" type="button" ${page >= pages ? "disabled" : ""}>下一页</button></div>`;
}

function renderSearchResults() {
  const root = $("#search-results");
  root.innerHTML = state.searchResults.length ? state.searchResults.map((item, index) => {
    const provider = item.provider || item.source || item.site_name || "未知来源";
    const watchlisted = Boolean(item.watchlist_exists);
    const quality = [item.resolution, item.source, item.language, item.size].filter(Boolean).join(" · ");
    return `<article class="search-result"><div><span class="count-chip">${escapeHTML(provider)}</span><h3>${escapeHTML(item.title || item.name || "未命名资源")}</h3><p>${escapeHTML(quality || item.description || item.subtitle || item.url || "")}</p></div><div class="search-actions"><select data-search-type="${index}" aria-label="媒体类型"><option value="auto">自动</option><option value="movie">电影</option><option value="tv">电视剧</option><option value="anime">动漫</option><option value="custom">自定义</option></select><select data-search-rule="${index}" aria-label="目录映射"><option value="">自动路径</option>${pathRuleOptions()}</select><button class="quiet-button" data-search-download="${index}" type="button">下载</button><button class="primary-button" data-search-subscribe="${index}" type="button" ${watchlisted ? "disabled" : ""}>${watchlisted ? "已订阅" : "订阅"}</button></div></article>`;
  }).join("") : '<div class="empty">没有找到资源。</div>';
}

function pathRuleOptions(selected = "") { return state.pathRules.filter((rule) => rule.enabled).map((rule) => `<option value="${escapeHTML(rule.id)}" ${rule.id === selected ? "selected" : ""}>${escapeHTML(`${mediaTypeLabel(rule.media_type)} · ${rule.name}`)}</option>`).join(""); }
function fillPathRuleSelect(id, selected = "") { const select = $(id); select.innerHTML = `<option value="">自动选择路径规则</option>${pathRuleOptions(selected)}`; select.value = selected || ""; }
async function actOnSearchResult(index, action, button) {
  const item = state.searchResults[index]; if (!item) return;
  const type = document.querySelector(`[data-search-type="${index}"]`)?.value || item.type || "auto";
  const pathRuleId = document.querySelector(`[data-search-rule="${index}"]`)?.value || undefined;
  button.disabled = true;
  try {
    const payload = action === "download"
      ? {
          result_id: item.id,
          download_link: item.download_link,
          title: item.title || item.media_name || "未命名资源",
          type,
          path_rule_id: pathRuleId,
        }
      : {
          keyword: $("#search-keyword").value.trim() || item.media_name || item.title || item.name,
          type,
          path_rule_id: pathRuleId,
          viewing_mode: $("#search-viewing-mode").value,
        };
    if (action === "download") await api("/api/download", {method:"POST", body:JSON.stringify(payload)});
    else await api("/api/watchlist/add", {method:"POST", body:JSON.stringify(payload)});
    if (action === "subscribe") item.watchlist_exists = true;
    renderSearchResults(); toast(action === "download" ? "资源已加入下载队列" : "已加入订阅监听"); await loadAll(true);
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
}

function jobActionMarkup(id, retryable, deletable, correctable = false) {
  return `<div class="job-actions"><button class="quiet-button" data-job-detail="${escapeHTML(id)}" type="button">详情</button>${correctable ? `<button class="quiet-button" data-correct-job="${escapeHTML(id)}" type="button">修正预览</button><button class="quiet-button" data-relocate-job="${escapeHTML(id)}" type="button">安全迁移</button>` : ""}${retryable ? `<button class="quiet-button" data-retry-job="${escapeHTML(id)}" type="button">重试</button>` : ""}${deletable ? `<button class="danger-button" data-delete-job="${escapeHTML(id)}" type="button">放弃 AVS 记录</button>` : ""}</div>`;
}

function canDiscardJob(item) {
  const checkpoint = item.rename_checkpoint || {};
  const pristine = !Number(checkpoint.files || 0) && !Number(checkpoint.folders || 0) && !checkpoint.torrent;
  const active = ["submitting","awaiting_binding","pending","waiting_metadata","waiting_selection","waiting_download","retrying"].includes(item.status);
  const hardlink = item.status === "completed" && ["pending","waiting_download","retrying","failed","partial","conflict"].includes(item.hardlink_status);
  return ["failed","missing_in_downloader"].includes(item.status) || (active && pristine) || hardlink;
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
  if (!confirm("只放弃这条 AVS 命名/硬链接记录；即使下载器中仍有任务，qB/Transmission 任务、下载文件和媒体库文件也绝不会被删除。确定继续吗？")) return;
  button.disabled = true;
  try {
    await api(`/api/naming/jobs/${encodeURIComponent(jobId)}`, {method:"DELETE"});
    $("#job-detail-modal").classList.add("hidden");
    toast("AVS 记录已放弃，下载器和文件未受影响");
    await loadAll(true);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

function renderAutomation() {
  const waiting = state.naming.filter((item) => ["pending","submitting","awaiting_binding","waiting_metadata","waiting_selection","waiting_download","retrying"].includes(item.status) || ["waiting_download","retrying"].includes(item.hardlink_status)).length;
  const failed = state.naming.filter((item) => ["failed","missing_in_downloader"].includes(item.status) || ["failed","missing_in_downloader","partial","conflict"].includes(item.hardlink_status)).length;
  const online = state.qb?.connected;
  const downloader = state.qb?.client === "transmission" ? "Transmission" : "qBittorrent";
  const hours = state.systemSettings?.watchlist_check_hours || 12;
  const cleanupMode = state.systemSettings?.cleanup_auto_scan_enabled
    ? (state.systemSettings.cleanup_auto_execute_enabled ? "自动扫描并执行" : "只自动扫描")
    : "手动扫描";
  const syncMode = state.systemSettings?.directory_sync_enabled
    ? `每 ${state.systemSettings.directory_sync_minutes || 5} 分钟`
    : "仅手动";
  $("#automation-status").innerHTML = `
    <div class="status-row ${online ? "" : "error"}"><i></i><span>${downloader} 连接</span><small>${online ? escapeHTML(state.qb.version) : "不可用"}</small></div>
    <div class="status-row ${waiting ? "warn" : ""}"><i></i><span>等待命名或入库</span><small>${waiting} 项</small></div>
    <div class="status-row ${failed ? "error" : ""}"><i></i><span>命名或入库失败</span><small>${failed} 项</small></div>
    <div class="status-row"><i></i><span>订阅定时检查</span><small>每 ${hours} 小时</small></div>
    <div class="status-row"><i></i><span>下载目录同步</span><small>${syncMode}</small></div>
    <div class="status-row ${state.systemSettings?.cleanup_auto_delete_source ? "warn" : ""}"><i></i><span>重复清理</span><small>${cleanupMode}</small></div>`;
}

const MEDIA_GROUPS = [
  ["movie", "电影", "MOVIES"], ["tv", "电视剧", "SERIES"], ["anime", "动漫", "ANIMATION"], ["custom", "自定义", "CUSTOM"],
];

function renderPathRules() {
  const html = MEDIA_GROUPS.map(([type, label, code]) => {
    const rules = state.pathRules.filter((item) => item.media_type === type);
    const body = rules.length ? rules.map((item) => `<article class="rule-item ${item.enabled ? "" : "off"}">
      <div class="rule-item-head"><h4>${escapeHTML(item.name)}</h4><div class="rule-badges">${item.default_download ? '<span class="count-chip">默认下载</span>' : ""}${chip(item.enabled ? "completed" : "site_disabled")}</div></div>
      <div class="rule-path"><span title="NAS：${escapeHTML(item.source_path)}">${escapeHTML(item.source_path)}</span><i>→</i><span title="下载器：${escapeHTML(item.downloader_path)}">${escapeHTML(item.downloader_path || item.source_path)}</span><i>→</i><span title="媒体库：${escapeHTML(item.target_path)}">${escapeHTML(item.target_path)}</span></div>
      <div class="rule-preflight" data-rule-preflight="${escapeHTML(item.id)}">预检未运行（只读，不会修改路径）</div><div class="rule-actions"><span class="count-chip">${item.rename_enabled ? "规范命名" : "保留原名"}</span><button class="quiet-button" data-check-rule="${escapeHTML(item.id)}" type="button">预检</button><button class="quiet-button" data-edit-rule="${escapeHTML(item.id)}" type="button">编辑</button><button class="danger-button" data-delete-rule="${escapeHTML(item.id)}" type="button">删除</button></div>
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
  fillPathRuleSelect("#manual-path-rule", $("#manual-path-rule").value);
  fillPathRuleSelect("#watchlist-path-rule", $("#watchlist-path-rule").value);
}

function renderSystemSettings() {
  if (state.systemSettings && !state.systemDirty) {
    $("#watchlist-hours").value = state.systemSettings.watchlist_check_hours;
    $("#directory-sync-enabled").checked = Boolean(state.systemSettings.directory_sync_enabled);
    $("#directory-sync-minutes").value = state.systemSettings.directory_sync_minutes || 5;
    $("#directory-sync-settle-seconds").value = state.systemSettings.directory_sync_settle_seconds ?? 120;
    $("#cleanup-auto-scan").checked = Boolean(state.systemSettings.cleanup_auto_scan_enabled);
    $("#cleanup-auto-execute").checked = Boolean(state.systemSettings.cleanup_auto_execute_enabled);
    $("#cleanup-auto-delete-source").checked = Boolean(state.systemSettings.cleanup_auto_delete_source);
    $("#cleanup-policy").value = state.systemSettings.cleanup_policy || "quality_first";
    $("#cleanup-scan-hours").value = state.systemSettings.cleanup_scan_hours || 24;
  }
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

function cleanupVersionName(version) {
  const path = version.source_paths?.[0] || version.target_paths?.[0] || version.id;
  return String(path).split("/").filter(Boolean).at(-1) || version.id;
}

function cleanupVersionMarkup(plan, group, version, keepId) {
  const recommended = version.id === group.recommended_keep_id;
  const selectable = plan.status === "ready";
  const completed = (plan.results || []).some((item) => item.version_id === version.id && item.status === "completed");
  const checked = version.id === keepId;
  const paths = [
    ...(version.source_paths || []).map((path) => `<span>源 · ${escapeHTML(path)}</span>`),
    ...(version.target_paths || []).map((path) => `<span>库 · ${escapeHTML(path)}</span>`),
  ].join("");
  const quality = [version.resolution, version.source, version.hdr, version.audio, version.codec].filter(Boolean);
  const removable = version.managed && version.safe && !completed;
  const safety = !version.managed ? "可以保留；非 AVS 管理，系统不会自动删除" : !version.safe ? `可以保留；其他操作需人工处理：${version.safety_reason || "无法可靠识别"}` : completed ? "这个版本已完成清理" : "不保留时将作为安全版本清理";
  return `<label class="cleanup-version ${recommended ? "recommended" : ""} ${removable ? "" : "unsafe"}">
    <input type="radio" name="cleanup-keep-${escapeHTML(plan.id)}-${escapeHTML(group.id)}" data-cleanup-keep="${escapeHTML(version.id)}" data-cleanup-group="${escapeHTML(group.id)}" data-cleanup-plan="${escapeHTML(plan.id)}" ${checked ? "checked" : ""} ${selectable ? "" : "disabled"} aria-label="保留 ${escapeHTML(cleanupVersionName(version))}">
    <span class="cleanup-version-main"><span class="cleanup-version-title"><strong>${escapeHTML(cleanupVersionName(version))}</strong><small>${formatBytes(version.size)}</small></span>
    <span class="cleanup-quality">${recommended ? '<i class="quality-pill keep">建议保留</i>' : ""}${quality.map((item) => `<i class="quality-pill">${escapeHTML(item)}</i>`).join("")}<i class="quality-pill warn">inode ${escapeHTML(version.inode)} · ${Number(version.link_count || 0)} 链接</i></span>
    <span class="cleanup-paths">${paths || "<span>未记录路径</span>"}<span>${escapeHTML(safety)}</span></span></span>
  </label>`;
}

function renderCleanupPlans() {
  const filter = $("#cleanup-status-filter").value;
  const plans = state.cleanupPlans.filter((item) => !filter || item.status === filter);
  $("#cleanup-plan-count").textContent = `${state.cleanupMeta.total ?? state.cleanupPlans.length} 个计划`;
  $("#cleanup-plans").innerHTML = plans.length ? plans.map((plan, index) => {
    const summary = plan.summary || {};
    const groups = plan.groups || [];
    const failed = (plan.results || []).filter((item) => item.status === "failed");
    const groupMarkup = groups.length ? groups.map((group) => {
      const versions = group.versions || [];
      const keepId = versions.some((item) => item.id === group.recommended_keep_id) ? group.recommended_keep_id : versions[0]?.id;
      return `<section class="cleanup-group"><div class="cleanup-group-head"><strong>${escapeHTML(group.identity || group.id)}</strong><small>${escapeHTML(mediaTypeLabel(group.media_type))} · 请选择保留 1 个版本</small></div>${versions.map((version) => cleanupVersionMarkup(plan, group, version, keepId)).join("")}</section>`;
    }).join("") : '<div class="empty">本次扫描没有发现可比较的重复版本</div>';
    const retry = plan.status === "partial" ? `<button class="quiet-button" data-cleanup-retry="${escapeHTML(plan.id)}" type="button">重试失败项</button>` : "";
    const execute = plan.status === "ready" && groups.length ? `<button class="primary-button" data-cleanup-execute="${escapeHTML(plan.id)}" type="button">核对并清理多余版本</button>` : "";
    return `<details class="cleanup-plan" ${index === 0 ? "open" : ""}><summary><span class="cleanup-plan-title">${cleanupPlanChip(plan.status)}<strong>${plan.policy === "space_first" ? "空间优先" : "质量优先"}扫描</strong></span><small>${formatDate(plan.created_at)} · ${groups.length} 组重复</small></summary><div class="cleanup-plan-body">
      <div class="cleanup-summary-strip"><div><span>重复组</span><strong>${Number(summary.groups || groups.length)}</strong></div><div><span>逻辑重复体积</span><strong>${formatBytes(summary.logical_duplicate_size)}</strong></div><div><span>删源后预计释放</span><strong>${formatBytes(summary.reclaimable_if_source_deleted || summary.estimated_reclaimable)}</strong></div></div>
      ${groupMarkup}${failed.length ? `<div class="cleanup-result-error">${failed.map((item) => `${escapeHTML(item.version_id)}：${escapeHTML(item.error || "未知错误")}`).join("<br>")}</div>` : ""}
      <div class="cleanup-plan-actions"><small>每组只需选择“保留哪个”；其他可验证的安全版本会自动列入清理，执行前仍会再次确认路径。</small><div class="head-actions">${retry}${execute}</div></div>
    </div></details>`;
  }).join("") : '<div class="empty">还没有符合条件的清理计划。先执行一次只读扫描。</div>';
}

function prepareCleanupExecution(planId, trigger) {
  const plan = state.cleanupPlans.find((item) => item.id === planId);
  if (!plan) { toast("找不到清理计划，请刷新后重试", true); return; }
  const selections = [];
  const selectedVersions = [];
  const keptVersions = [];
  for (const group of plan.groups || []) {
    const keep = document.querySelector(`[data-cleanup-plan="${CSS.escape(planId)}"][data-cleanup-group="${CSS.escape(group.id)}"][data-cleanup-keep]:checked`);
    if (!keep) { toast(`请为「${group.identity}」选择一个保留版本`, true); return; }
    const completedIds = new Set((plan.results || []).filter((item) => item.status === "completed").map((item) => item.version_id));
    const deletable = (group.versions || []).filter((item) => item.id !== keep.dataset.cleanupKeep && item.managed && item.safe && !completedIds.has(item.id));
    if (deletable.length) {
      selections.push({group_id: group.id, delete_version_ids: deletable.map((item) => item.id)});
      selectedVersions.push(...deletable);
      const kept = (group.versions || []).find((item) => item.id === keep.dataset.cleanupKeep);
      if (kept) keptVersions.push(kept);
    }
  }
  if (!selections.length) { toast("当前选择下没有可安全清理的多余版本", true); return; }
  state.cleanupExecution = {planId, selections, selectedVersions, keptVersions};
  $("#cleanup-confirm-summary").textContent = `将保留 ${keptVersions.length} 个版本，清理 ${selectedVersions.length} 个多余版本（${formatBytes(selectedVersions.reduce((sum, item) => sum + Number(item.size || 0), 0))}）。默认只删除媒体库硬链接。`;
  $("#cleanup-confirm-kept").innerHTML = `<strong>保留：</strong>${keptVersions.map((item) => escapeHTML(cleanupVersionName(item))).join("；")}`;
  $("#cleanup-confirm-paths").innerHTML = selectedVersions.map((item) => `<div class="cleanup-confirm-item"><strong>${escapeHTML(cleanupVersionName(item))}</strong><small>${[...(item.target_paths || []), ...(item.source_paths || [])].map(escapeHTML).join("<br>")}</small></div>`).join("");
  $("#cleanup-delete-source").checked = false; $("#cleanup-understand").checked = false;
  openModal("#cleanup-confirm-modal", trigger);
}

function renderAgents() {
  const active = state.agents.filter((item) => !item.revoked);
  const button = $("#agent-connect");
  button.classList.toggle("online", active.length > 0);
  button.querySelector("span").textContent = active.length ? `${active.length} 个 Agent 已授权` : "连接 Agent";
  $("#agent-count").textContent = `${state.agents.length} 个`;
  $("#agent-list").innerHTML = state.agents.length ? state.agents.map((item) => { const granted = (item.scopes || []).join("、") || "未报告"; const reported = (item.capabilities || item.reported_capabilities || []).join("、") || "尚未报告"; return `<div class="agent-item ${item.online ? "online" : ""}"><i></i><div><strong>${escapeHTML(item.name)}</strong><small>${item.revoked ? "已撤销" : item.last_seen ? `最近使用 · ${formatDate(item.last_seen)}` : `已授权 · ${formatDate(item.created_at)}`} · 授予：${escapeHTML(granted)} · 报告能力：${escapeHTML(reported)}</small></div>${item.revoked ? "" : `<div class="agent-actions"><button class="quiet-button" data-edit-agent-scopes="${escapeHTML(item.id)}" type="button">编辑权限</button><button class="danger-button" data-revoke-agent="${escapeHTML(item.id)}" type="button">撤销</button></div>`}</div>`; }).join("") : '<div class="empty">尚未授权 Agent</div>';
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
  renderMetrics(); renderDownloads(); renderWatchlist(); renderNaming(); renderHardlinks(); renderCleanupPlans(); renderSites(); renderPaths(); renderSystemSettings(); renderAgents(); renderAutomation(); renderLogs();
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
      "/api/downloader/status", "/api/downloader/tasks", "/api/watchlist", `/api/naming/jobs?page=${state.namingPage}&per_page=50`,
      `/api/hardlinks?status=all&page=${state.hardlinksPage}&per_page=50`, "/api/settings/sites", "/api/settings/paths", "/api/settings/path-rules",
      "/api/settings/downloader", "/api/settings/system", "/api/agents", "/api/logs?limit=500", "/api/cleanup/plans?per_page=20",
    ];
    const requests = await Promise.allSettled(paths.map((path) => api(path)));
    const authFailure = requests.find((item) => item.status === "rejected" && item.reason instanceof AuthError);
    if (authFailure) throw authFailure.reason;
    const value = (index, fallback) => requests[index].status === "fulfilled" ? requests[index].value : fallback;
    const downloadsResult = value(1, {tasks:[]});
    state.qb = value(0, {configured:false,connected:false}); state.downloads = downloadsResult.tasks; state.hiddenDownloads = downloadsResult.hidden_tasks || [];
    state.downloadSyncedAt = downloadsResult.synced_at || new Date().toISOString();
    state.watchlist = value(2, {items:[]}).items; const namingResult = value(3, {items:[]}); const hardlinksResult = value(4, {items:[]}); state.naming = namingResult.items || []; state.hardlinks = hardlinksResult.items || []; state.namingMeta = namingResult; state.hardlinksMeta = hardlinksResult;
    state.sites = value(5, {items:[]}).items; state.paths = value(6, {settings:state.paths}).settings; state.pathRules = value(7, {items:[]}).items;
    state.downloaderSettings = value(8, {settings:state.downloaderSettings}).settings; state.systemSettings = value(9, {settings:state.systemSettings}).settings; state.agents = value(10, {items:[]}).items; state.logs = value(11, {items:[]}).items;
    const cleanupResult = value(12, {items:[], total:0}); state.cleanupPlans = cleanupResult.items || []; state.cleanupMeta = cleanupResult;
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
  try { const params = new URLSearchParams({limit:"500"}); if ($("#log-level").value) params.set("level", $("#log-level").value); if ($("#log-query").value.trim()) params.set("query", $("#log-query").value.trim()); if ($("#log-since").value) params.set("since", new Date($("#log-since").value).toISOString()); state.logs = (await api(`/api/logs?${params}`)).items; renderLogs(); toast("日志已刷新"); }
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
  $("#path-rule-name").value = item?.name || ""; $("#path-rule-source").value = item?.source_path || ""; $("#path-rule-downloader").value = item?.downloader_path || item?.source_path || ""; $("#path-rule-target").value = item?.target_path || "";
  $("#path-rule-enabled").checked = item?.enabled ?? true; $("#path-rule-rename").checked = item?.rename_enabled ?? true; $("#path-rule-default").checked = item?.default_download ?? false;
  openModal("#path-rule-modal");
}

function planPayload() {
  return {
    media_name: $("#plan-media-name").value.trim() || undefined,
    original_title: $("#plan-original-title").value.trim() || undefined,
    year: Number($("#plan-year").value) || undefined,
    media_type: $("#plan-type").value,
    season: Number($("#plan-season").value) || undefined,
    episode: $("#plan-episode").value.trim() || undefined,
    episode_title: $("#plan-episode-title").value.trim() || undefined,
  };
}
function planFieldErrors(error) {
  const root = $("#naming-plan-errors");
  const details = Array.isArray(error.errors) ? error.errors.map((item) => `${item.field || item.loc?.join(".") || "计划"}：${item.message || item.msg || item}`).join("；") : error.message;
  root.textContent = `${details || "请求失败"}${error.requestId ? `（请求 ${error.requestId}）` : ""}`;
}
function defaultRelocationPath(mediaType) {
  const matched = state.pathRules.find((rule) => rule.enabled && rule.media_type === mediaType && rule.default_download);
  return matched?.downloader_path || "";
}
async function openNamingPlan(jobId, relocate = false, trigger = document.activeElement) {
  try {
    const item = (await api(`/api/naming/jobs/${encodeURIComponent(jobId)}`)).item;
    if (["done", "partial", "conflict"].includes(item.hardlink_status)) { toast("成功、部分或冲突入库的任务不能修改计划或迁移", true); return; }
    const plan = item.plan || {};
    const modal = $("#naming-plan-modal");
    modal.dataset.mode = relocate ? "relocate" : "correct";
    $("#naming-plan-id").value = jobId; $("#naming-plan-hash").value = item.torrent_hash || "";
    $("#plan-media-name").value = plan.media_name || ""; $("#plan-original-title").value = plan.original_title || ""; $("#plan-year").value = plan.year || ""; $("#plan-type").value = plan.media_type || "movie"; $("#plan-season").value = plan.season ?? ""; $("#plan-episode").value = plan.episode ?? ""; $("#plan-episode-title").value = plan.episode_title || "";
    $("#naming-plan-fields").querySelectorAll("input, select").forEach((field) => { field.disabled = relocate; });
    $("#naming-relocate-fields").classList.toggle("hidden", !relocate);
    $("#relocate-location").required = relocate;
    $("#relocate-location").value = relocate ? defaultRelocationPath(plan.media_type) : "";
    $("#naming-plan-title").textContent = relocate ? "安全迁移下载器任务" : "修正规范命名";
    $("#naming-plan-help").textContent = relocate ? "仅迁移当前下载器任务到明确输入的新保存路径；不会修改命名计划、下载文件以外的内容或已入库文件。" : "修正后先预览，再确认保存计划并重试。成功或部分硬链接的任务不可修改。";
    $("#preview-naming-plan").textContent = relocate ? "查看当前计划" : "预览路径";
    $("#save-naming-plan").textContent = relocate ? "确认迁移下载器任务" : "确认计划并重试";
    $("#naming-preview").textContent = relocate ? "输入新保存路径后确认迁移；可先查看当前计划。" : "尚未预览。";
    $("#naming-plan-errors").textContent = ""; openModal("#naming-plan-modal", trigger);
  } catch (error) { showError(error); }
}
async function previewNamingPlan() {
  const id = $("#naming-plan-id").value; const button = $("#preview-naming-plan"); button.disabled = true;
  try { const result = await api(`/api/naming/jobs/${encodeURIComponent(id)}/preview`, {method:"POST", body:JSON.stringify(planPayload())}); $("#naming-preview").textContent = JSON.stringify(result.preview || result.plan || result, null, 2); $("#naming-plan-errors").textContent = ""; }
  catch (error) { planFieldErrors(error); showError(error); } finally { button.disabled = false; }
}
async function checkPathRule(id, button) {
  button.disabled = true;
  try {
    const response = await api(`/api/settings/path-rules/${encodeURIComponent(id)}/check`, {method:"POST", body:"{}"});
    const box = document.querySelector(`[data-rule-preflight="${CSS.escape(id)}"]`);
    if (box) box.textContent = JSON.stringify(response.result || {}, null, 2);
  } catch (error) { showError(error); } finally { button.disabled = false; }
}

function manualSourceChanged() {
  const selected = $('input[name="download-source"]:checked').value;
  $$('[data-source-panel]').forEach((element) => element.classList.toggle("hidden", element.dataset.sourcePanel !== selected));
}

function resetManualPreview() {
  state.manualPreviewSignature = null;
  state.manualPreview = null;
  $("#manual-download-preview").textContent = "先识别资源，确认媒体类型、规范名称和保存目录后再加入下载。";
  $("#submit-manual-download").textContent = "识别并预览";
  const explicitType = $("#manual-type").value;
  $("#manual-subscribe-options").classList.toggle("hidden", !["tv","anime"].includes(explicitType));
}

function manualSharedPayload() {
  return {
    title: $("#manual-title").value.trim() || undefined,
    type: $("#manual-type").value,
    path_rule_id: $("#manual-path-rule").value || undefined,
    original_title: $("#manual-original-title").value.trim() || undefined,
    edition: $("#manual-edition").value.trim() || undefined,
    episode_title: $("#manual-episode-title").value.trim() || undefined,
    subscribe: $("#manual-subscribe").checked,
    viewing_mode: $("#manual-viewing-mode").value,
  };
}

function manualSignature(source, shared, file = null) {
  const previewFields = {...shared}; delete previewFields.subscribe; delete previewFields.viewing_mode;
  return JSON.stringify({source, previewFields, link: $("#manual-link").value.trim(), file: file ? [file.name, file.size, file.lastModified] : null});
}

function renderManualPreview(result) {
  state.manualPreview = result;
  const plan = result.naming?.plan || {};
  $("#manual-download-preview").textContent = result.ready
    ? `识别类型：${mediaTypeLabel(result.type)}\n规范目录：${plan.root_name || "保留资源原名"}\n下载目录：${result.naming?.save_path || "按目录映射自动选择"}\n原资源名：${result.source_name || "—"}`
    : `尚未确认媒体类型：${result.source_name || result.title || "未知资源"}\n请选择电影、电视剧、动漫或自定义后重新识别。`;
  $("#manual-subscribe-options").classList.toggle("hidden", !["tv","anime"].includes(result.type));
  $("#submit-manual-download").textContent = result.ready ? "确认并加入下载" : "重新识别";
}

function manualLinkDisplayName(value) {
  try {
    const parsed = new URL(String(value || "").trim());
    if (parsed.protocol === "magnet:") return (parsed.searchParams.get("dn") || "").trim();
    const basename = decodeURIComponent(parsed.pathname.split("/").filter(Boolean).at(-1) || "");
    return basename.trim();
  } catch (_) { return ""; }
}

function updateManualLinkName() {
  const name = manualLinkDisplayName($("#manual-link").value);
  $("#manual-link-detected").textContent = name
    ? `已识别资源名：${name}`
    : "未读取到资源名；如果磁力链接没有 dn 参数，请填写中文标题";
  return name;
}

function agentPrompt(agent) {
  const origin = location.origin;
  return `请连接我的 AI Video Station，并把它作为媒体自动化工具使用。\n\n服务地址：${origin}\nOpenAPI：${origin}/openapi.yaml\n专用令牌：${agent.token}\nCLI：python -m ainas.cli\n\n连接方式：\n1. 所有 /api 请求使用 Authorization: Bearer ${agent.token}\n2. 首次使用时 POST ${origin}/api/agents/connect，JSON 为 {"name":"我的 Agent","capabilities":["search","download","watchlist","naming","hardlink","cleanup","logs"]}\n3. 不需要发送心跳；仅在需要查看或操作时调用 API，任意有效请求都会更新最近使用时间\n4. 也可以设置 AVS_URL=${origin} 与 AVS_TOKEN 后使用 CLI；先运行 python -m ainas.cli status\n5. 搜索时省略 add_to_watchlist 或明确传 false；只有用户明确要求订阅时才调用 /api/watchlist/add\n6. 读取 OpenAPI 后再调用业务接口；涉及新增下载、重复清理、删除或修改设置时，先向我确认目标\n7. 不要在回复、日志或其他文件中再次显示这枚令牌。`;
}

function openAgentScopes(agentId, trigger = document.activeElement) {
  const agent = state.agents.find((item) => item.id === agentId);
  if (!agent || agent.revoked) { toast("找不到可编辑的 Agent", true); return; }
  $("#agent-scopes-id").value = agent.id;
  $("#agent-scopes-description").textContent = `更新「${agent.name}」的授权范围会立即生效，令牌保持不变。`;
  $$("input[name=agent-edit-scope]").forEach((input) => { input.checked = (agent.scopes || []).includes(input.value); });
  $("#agent-scopes-errors").textContent = "";
  openModal("#agent-scopes-modal", trigger);
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
    const correct = event.target.closest("[data-correct-job]"); if (correct) openNamingPlan(correct.dataset.correctJob, false, correct);
    const relocate = event.target.closest("[data-relocate-job]"); if (relocate) openNamingPlan(relocate.dataset.relocateJob, true, relocate);
    const editAgentScopes = event.target.closest("[data-edit-agent-scopes]"); if (editAgentScopes) openAgentScopes(editAgentScopes.dataset.editAgentScopes, editAgentScopes);
    const page = event.target.closest("[data-page-kind]"); if (page) { state[`${page.dataset.pageKind}Page`] = Number(page.dataset.page); loadAll(true); }
    const searchDownload = event.target.closest("[data-search-download]"); if (searchDownload) actOnSearchResult(Number(searchDownload.dataset.searchDownload), "download", searchDownload);
    const searchSubscribe = event.target.closest("[data-search-subscribe]"); if (searchSubscribe) actOnSearchResult(Number(searchSubscribe.dataset.searchSubscribe), "subscribe", searchSubscribe);
    const cleanupExecute = event.target.closest("[data-cleanup-execute]"); if (cleanupExecute) prepareCleanupExecution(cleanupExecute.dataset.cleanupExecute, cleanupExecute);
    const cleanupRetry = event.target.closest("[data-cleanup-retry]"); if (cleanupRetry) runAction(cleanupRetry, `/api/cleanup/plans/${encodeURIComponent(cleanupRetry.dataset.cleanupRetry)}/retry`, "失败清理项已重试");
  });
  $$(".tab").forEach((button) => button.addEventListener("click", () => setTab(button.dataset.tab)));
  $$('[data-open-download]').forEach((button) => button.addEventListener("click", () => { resetManualPreview(); openModal("#download-modal"); }));
  $("#add-watchlist").addEventListener("click", () => openModal("#watchlist-modal"));
  $("#refresh-all").addEventListener("click", () => loadAll()); $("#refresh-downloads").addEventListener("click", () => refreshDownloads()); $("#show-hidden-downloads").addEventListener("click", () => openModal("#hidden-downloads-modal")); $("#watch-filter").addEventListener("input", renderWatchlist);
  $("#naming-filter").addEventListener("change", renderNaming); $("#hardlink-filter").addEventListener("change", renderHardlinks); $("#cleanup-status-filter").addEventListener("change", renderCleanupPlans);
  $("#log-level").addEventListener("change", renderLogs); $("#log-query").addEventListener("input", renderLogs); $("#refresh-logs").addEventListener("click", loadLogs);
  $("#log-since").addEventListener("change", loadLogs);
  $("#job-detail-logs").addEventListener("click", (event) => showJobLogs(event.currentTarget.dataset.jobId));
  $("#check-watchlist").addEventListener("click", (event) => runAction(event.currentTarget, "/api/watchlist/check", "监听检查已完成"));
  $("#check-naming").addEventListener("click", (event) => runAction(event.currentTarget, "/api/naming/jobs/check", "命名任务已处理"));
  $("#directory-sync-now").addEventListener("click", (event) => runAction(event.currentTarget, "/api/directory-sync/scan", "下载目录同步已完成"));
  $("#directory-sync-settings-form").addEventListener("input", () => { state.systemDirty = true; });
  $("#directory-sync-settings-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = event.currentTarget.querySelector('button[type="submit"]'); button.disabled = true;
    try {
      const payload = {directory_sync_enabled:$("#directory-sync-enabled").checked,directory_sync_minutes:Number($("#directory-sync-minutes").value),directory_sync_settle_seconds:Number($("#directory-sync-settle-seconds").value)};
      const result = await api("/api/settings/system", {method:"PATCH",body:JSON.stringify(payload)}); state.systemSettings = result.settings; state.systemDirty = false; toast("目录同步设置已保存"); renderSystemSettings(); renderAutomation();
    } catch (error) { showError(error); } finally { button.disabled = false; }
  });
  $("#cleanup-scan-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = $("#cleanup-scan"); button.disabled = true;
    try {
      const payload = {policy:$("#cleanup-policy").value}; if ($("#cleanup-media-type").value) payload.media_type = $("#cleanup-media-type").value;
      const result = await api("/api/cleanup/scan", {method:"POST", body:JSON.stringify(payload)});
      toast(result.item?.groups?.length ? `扫描完成，发现 ${result.item.groups.length} 组重复` : "扫描完成，没有发现重复版本"); await loadAll(true);
    } catch (error) { showError(error); } finally { button.disabled = false; }
  });
  $("#cleanup-settings-form").addEventListener("input", () => { state.systemDirty = true; });
  $("#cleanup-settings-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget; const button = form.querySelector('button[type="submit"]');
    const autoScan = $("#cleanup-auto-scan").checked; const autoExecute = $("#cleanup-auto-execute").checked; const autoDelete = $("#cleanup-auto-delete-source").checked;
    if (autoExecute && !autoScan) { toast("开启自动执行前，需要先开启定时扫描", true); return; }
    if (autoDelete && !autoExecute) { toast("自动删除源数据只能在自动执行开启后使用", true); return; }
    const becameDestructive = (autoExecute && !state.systemSettings?.cleanup_auto_execute_enabled) || (autoDelete && !state.systemSettings?.cleanup_auto_delete_source);
    if (becameDestructive && !confirm("自动执行可能移除媒体库硬链接；自动删源还会删除下载器任务或源文件。确认按当前分级开关保存吗？")) return;
    button.disabled = true;
    try {
      const payload = {cleanup_auto_scan_enabled:autoScan, cleanup_auto_execute_enabled:autoExecute, cleanup_auto_delete_source:autoDelete, cleanup_policy:$("#cleanup-policy").value, cleanup_scan_hours:Number($("#cleanup-scan-hours").value)};
      const result = await api("/api/settings/system", {method:"PATCH", body:JSON.stringify(payload)}); state.systemSettings = result.settings; state.systemDirty = false; toast("重复清理自动化设置已保存"); renderSystemSettings(); renderAutomation();
    } catch (error) { showError(error); } finally { button.disabled = false; }
  });
  $("#cleanup-confirm-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const execution = state.cleanupExecution; if (!execution) { toast("清理选择已失效，请重新打开计划", true); return; }
    const button = $("#cleanup-confirm-submit"); button.disabled = true;
    try {
      const payload = {selections:execution.selections, delete_source:$("#cleanup-delete-source").checked, confirmation:"DELETE_SELECTED_DUPLICATES"};
      const result = await api(`/api/cleanup/plans/${encodeURIComponent(execution.planId)}/execute`, {method:"POST", body:JSON.stringify(payload)});
      $("#cleanup-confirm-modal").classList.add("hidden"); state.cleanupExecution = null;
      const failed = (result.results || []).filter((item) => item.status === "failed").length;
      toast(failed ? `清理完成，但有 ${failed} 项失败，可在记录中重试` : "所选重复版本已处理", failed > 0); await loadAll(true);
    } catch (error) { showError(error); } finally { button.disabled = false; }
  });
  $("#search-form").addEventListener("submit", async (event) => { event.preventDefault(); const button = $("#search-submit"); button.disabled = true; try { const result = await api("/api/search", {method:"POST",body:JSON.stringify({keyword:$("#search-keyword").value.trim(),type:$("#search-type").value,add_to_watchlist:false})}); state.searchResults = (result.items || result.results || []).map((item) => ({...item, watchlist_exists: Boolean(result.watchlist_exists)})); renderSearchResults(); $("#search-provider-note").textContent = `已从 ${new Set(state.searchResults.map((item) => item.provider || item.source || item.site_name).filter(Boolean)).size} 个来源返回 ${state.searchResults.length} 条结果。`; toast("搜索已完成"); } catch (error) { toast(error.message, true); } finally { button.disabled = false; } });
  $("#watchlist-body").addEventListener("change", async (event) => {
    const select = event.target.closest("[data-watch-mode]"); if (!select) return;
    select.disabled = true;
    try { await api(`/api/watchlist/${select.dataset.watchMode}`, {method:"PATCH",body:JSON.stringify({viewing_mode:select.value})}); toast(`观看模式已改为${viewingModeLabel(select.value)}`); await loadAll(true); }
    catch (error) { toast(error.message, true); }
    finally { select.disabled = false; }
  });
  $("#watchlist-body").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-delete-watch]"); if (!button || !confirm("确定移除这个监听任务吗？")) return;
    try { await api(`/api/watchlist/${button.dataset.deleteWatch}`, {method:"DELETE"}); toast("监听已移除"); await loadAll(true); } catch (error) { toast(error.message, true); }
  });
  $("#hardlinks-body").addEventListener("click", async (event) => {
    const sync = event.target.closest("[data-sync-rule]");
    if (sync) { await runAction(sync, "/api/directory-sync/scan", "目录已重新同步", {path_rule_id:sync.dataset.syncRule}); return; }
    const button = event.target.closest("[data-copy-target]"); if (!button || !button.dataset.copyTarget) return;
    await copyText(button.dataset.copyTarget); toast("目标目录已复制，可在 NAS 文件管理器中打开");
  });
  $("#site-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {name:$("#site-name").value.trim(),adapter:"generic",enabled:true,base_urls:[$("#site-base").value.trim()],search_url:$("#site-search-url").value.trim(),result_selector:$("#site-result-selector").value.trim(),title_selector:$("#site-title-selector").value.trim(),link_selector:$("#site-link-selector").value.trim(),download_selector:$("#site-download-selector").value.trim(),default_type:$("#site-default-type").value,tv_path_patterns:$("#site-tv-patterns").value.split(",").map((item)=>item.trim()).filter(Boolean),anime_path_patterns:$("#site-anime-patterns").value.split(",").map((item)=>item.trim()).filter(Boolean),allow_private_hosts:$("#site-allow-private").checked};
    try { await api("/api/settings/sites", {method:"POST",body:JSON.stringify(payload)}); toast("站点已保存"); event.currentTarget.reset(); await loadAll(true); } catch (error) { toast(error.message, true); }
  });
  $("#sites-body").addEventListener("click", async (event) => {
    const toggle = event.target.closest("[data-toggle-site]"); const remove = event.target.closest("[data-delete-site]");
    try { let changed = false; if (toggle) { await api(`/api/settings/sites/${toggle.dataset.toggleSite}`, {method:"PATCH",body:JSON.stringify({enabled:toggle.dataset.enabled!=="true"})}); changed = true; } if (remove && confirm("确定删除这个站点配置吗？")) { await api(`/api/settings/sites/${remove.dataset.deleteSite}`, {method:"DELETE"}); changed = true; } if (changed) { toast("站点配置已更新"); await loadAll(true); } } catch (error) { toast(error.message, true); }
  });
  $("#provider-preview-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = $("#provider-preview-submit"); button.disabled = true;
    try {
      const result = await api("/api/settings/sites/preview", {method:"POST", body:JSON.stringify({site_id: $("#provider-preview-site").value, keyword: $("#provider-preview-keyword").value.trim(), type: $("#provider-preview-type").value})});
      $("#provider-preview-result").textContent = JSON.stringify(result.providers || result, null, 2);
      toast(result.ok ? "站点预览测试通过" : "站点预览测试返回异常", !result.ok);
    } catch (error) { $("#provider-preview-result").textContent = error.message; showError(error); } finally { button.disabled = false; }
  });
  $('input[name="download-source"][value="magnet"]').closest(".source-switch").addEventListener("change", () => { manualSourceChanged(); resetManualPreview(); });
  $("#manual-link").addEventListener("input", () => { updateManualLinkName(); resetManualPreview(); });
  $("#manual-download-form").addEventListener("input", (event) => { if (!["manual-subscribe","manual-viewing-mode","manual-link"].includes(event.target.id)) resetManualPreview(); });
  $("#manual-download-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = $("#submit-manual-download"); const source = $('input[name="download-source"]:checked').value;
    const shared = manualSharedPayload(); const file = source === "torrent" ? $("#manual-torrent").files[0] : null;
    button.disabled = true;
    try {
      const link = $("#manual-link").value.trim();
      if (source === "magnet" && !link) throw new Error("请输入磁力链接或种子链接");
      if (source === "magnet" && !shared.title && !updateManualLinkName()) { $("#manual-title").focus(); throw new Error("这个链接没有资源名，请先填写标题"); }
      if (source === "torrent" && !file) throw new Error("请选择 .torrent 文件");
      const signature = manualSignature(source, shared, file);
      if (state.manualPreviewSignature !== signature || !state.manualPreview?.ready) {
        let preview;
        if (source === "magnet") preview = await api("/api/download/manual/preview", {method:"POST",body:JSON.stringify({...shared,download_link:link})});
        else { const form = new FormData(); form.append("torrent", file); Object.entries(shared).forEach(([key,value]) => { if (value !== undefined) form.append(key,value); }); preview = await api("/api/download/manual/preview", {method:"POST",body:form}); }
        state.manualPreviewSignature = preview.ready ? signature : null; renderManualPreview(preview);
        toast(preview.ready ? "识别完成，请确认后加入下载" : "请选择媒体类型后重新识别", !preview.ready);
        return;
      }
      if (source === "magnet") await api("/api/download/manual", {method:"POST",body:JSON.stringify({...shared,download_link:link})});
      else { const form = new FormData(); form.append("torrent", file); Object.entries(shared).forEach(([key,value]) => { if (value !== undefined) form.append(key,value); }); await api("/api/download/manual", {method:"POST",body:form}); }
      toast("资源已识别并加入下载队列"); $("#download-modal").classList.add("hidden"); event.currentTarget.reset(); manualSourceChanged(); updateManualLinkName(); await loadAll(true);
    } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
  });
  $("#watchlist-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const button = event.currentTarget.querySelector('button[type="submit"]'); button.disabled = true;
    try {
      await api("/api/watchlist/add", {method:"POST",body:JSON.stringify({keyword:$("#watchlist-keyword").value.trim(),type:$("#watchlist-type").value,viewing_mode:$("#watchlist-viewing-mode").value,path_rule_id:$("#watchlist-path-rule").value || undefined})});
      toast("订阅已保存"); $("#watchlist-modal").classList.add("hidden"); event.currentTarget.reset(); await loadAll(true);
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
    const edit = event.target.closest("[data-edit-rule]"); const remove = event.target.closest("[data-delete-rule]"); const check = event.target.closest("[data-check-rule]");
    if (edit) editPathRule(state.pathRules.find((item) => item.id === edit.dataset.editRule));
    if (check) checkPathRule(check.dataset.checkRule, check);
    if (remove && confirm("确定删除这条目录映射吗？")) { try { await api(`/api/settings/path-rules/${remove.dataset.deleteRule}`, {method:"DELETE"}); toast("目录映射已删除"); await loadAll(true); } catch (error) { toast(error.message, true); } }
  });
  $("#path-rule-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const id = $("#path-rule-id").value; const payload = {media_type:$("#path-rule-type").value,name:$("#path-rule-name").value.trim(),source_path:$("#path-rule-source").value.trim(),downloader_path:$("#path-rule-downloader").value.trim(),target_path:$("#path-rule-target").value.trim(),enabled:$("#path-rule-enabled").checked,rename_enabled:$("#path-rule-rename").checked,default_download:$("#path-rule-default").checked};
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
  $("#agent-connect").addEventListener("click", (event) => openModal("#agent-modal", event.currentTarget));
  $("#create-agent-config").addEventListener("click", async (event) => { const name = $("#agent-name").value.trim(); const scopes = $$("input[name=agent-scope]:checked").map((input) => input.value); if (name.length < 2 || !scopes.length) { toast("请填写有意义的 Agent 名称并至少选择一个范围", true); return; } event.currentTarget.disabled = true; try { const result = await api("/api/agents/bootstrap", {method:"POST",body:JSON.stringify({name,scopes})}); const prompt = agentPrompt(result.agent); $("#agent-config").value = prompt; $("#agent-config-wrap").classList.remove("hidden"); await copyText(prompt); toast("连接方案已复制，请发给你的 Agent"); await loadAll(true); } catch (error) { toast(error.message, true); } finally { event.currentTarget.disabled = false; } });
  $("#copy-agent-config").addEventListener("click", async () => { await copyText($("#agent-config").value); toast("连接方案已复制"); });
  $("#agent-list").addEventListener("click", async (event) => { const button = event.target.closest("[data-revoke-agent]"); if (!button || !confirm("撤销后该 Agent 将立即无法访问，确定继续吗？")) return; try { await api(`/api/agents/${button.dataset.revokeAgent}`, {method:"DELETE"}); toast("Agent 授权已撤销"); await loadAll(true); } catch (error) { showError(error); } });
  $("#agent-scopes-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget; const scopes = $$("input[name=agent-edit-scope]:checked").map((input) => input.value); const errorRoot = $("#agent-scopes-errors");
    if (!scopes.length) { errorRoot.textContent = "至少保留一个授权范围"; return; }
    const button = form.querySelector("button[type=submit]"); button.disabled = true;
    try { await api(`/api/agents/${encodeURIComponent($("#agent-scopes-id").value)}/scopes`, {method:"PATCH", body:JSON.stringify({scopes})}); $("#agent-scopes-modal").classList.add("hidden"); toast("Agent 权限已更新"); await loadAll(true); }
    catch (error) { errorRoot.textContent = `${error.message}${error.requestId ? `（请求 ${error.requestId}）` : ""}`; showError(error); } finally { button.disabled = false; }
  });
  $("#auth-form").addEventListener("submit", async (event) => { event.preventDefault(); state.apiKey = $("#api-key").value.trim(); sessionStorage.setItem("aiVideoStationApiKey", state.apiKey); await loadAll(); });
  $("#preview-naming-plan").addEventListener("click", previewNamingPlan);
  $("#naming-plan-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const modal = $("#naming-plan-modal"); const id = $("#naming-plan-id").value; const button = $("#save-naming-plan"); button.disabled = true;
    try {
      if (modal.dataset.mode === "relocate") {
        const taskHash = $("#naming-plan-hash").value; const location = $("#relocate-location").value.trim();
        if (!taskHash || taskHash.startsWith("pending:")) throw new Error("该任务尚未绑定下载器，暂时不能迁移");
        if (!location.startsWith("/")) throw new Error("请输入下载器可见的绝对保存路径");
        await api(`/api/downloader/tasks/${encodeURIComponent(taskHash)}/relocate`, {method:"POST", body:JSON.stringify({location})});
        toast("下载器任务已开始安全迁移");
      } else {
        await api(`/api/naming/jobs/${encodeURIComponent(id)}/plan`, {method:"PATCH",body:JSON.stringify(planPayload())});
        await api(`/api/naming/jobs/${encodeURIComponent(id)}/retry`, {method:"POST",body:"{}"});
        toast("计划已确认，任务已重试");
      }
      modal.classList.add("hidden"); await loadAll(true);
    } catch (error) { planFieldErrors(error); showError(error); } finally { button.disabled = false; }
  });
  document.addEventListener("keydown", (event) => { const modal = $(".modal:not(.hidden)"); if (!modal) return; if (event.key === "Escape" && modal.id !== "auth-modal") { closeModal(modal); return; } if (event.key !== "Tab") return; const items = [...modal.querySelectorAll("button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled])")]; const current = items.indexOf(document.activeElement); if (!items.length || (event.shiftKey && current > 0) || (!event.shiftKey && current >= 0 && current < items.length - 1)) return; event.preventDefault(); (event.shiftKey ? items.at(-1) : items[0]).focus(); });
  $(".tabs").addEventListener("keydown", (event) => { if (!["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) return; const tabs = $$(".tab"); const current = tabs.indexOf(document.activeElement); if (current < 0) return; event.preventDefault(); const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length; tabs[next].focus(); setTab(tabs[next].dataset.tab); });
  $("#logout").addEventListener("click", () => { sessionStorage.removeItem("aiVideoStationApiKey"); sessionStorage.removeItem("sixvApiKey"); state.apiKey = ""; $("#api-key").value = ""; showAuth(); });
}

function tickClock() { $("#clock").textContent = new Date().toLocaleTimeString("zh-CN", {hour12:false}); if (state.theme.mode === "schedule") applyTheme(); }
applyTheme(); bindEvents(); tickClock();
const initialTab = location.hash.slice(1); if ($(`[data-panel="${CSS.escape(initialTab)}"]`)) setTab(initialTab);
loadAll(); setInterval(tickClock, 60000);
setInterval(() => refreshDownloads(true), 15000);
