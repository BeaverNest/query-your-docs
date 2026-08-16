/* ============================================================
   query-your-docs — NotebookLM-style web UI (Step C)
   Vanilla JS, wired to the FastAPI backend (API.md contract).
   Design handoff t_c0dc3e06 §3 (flows), §6 (interactions), §10 (data).
   ============================================================ */
'use strict';

/* ------------------------------------------------------------ state */
const state = {
  config: null,          // {llm_configured, model}
  sources: [],           // [{id,title,pages,chunks,status,error}]
  conversations: [],     // [{id,title,created_at,updated_at,message_count}]
  activeConvId: null,
  msgs: [],              // [{role:'user'|'assistant', content, sources?, error?, retryQuestion?, typing?}]
  sending: false,
  indexing: false,       // global rebuild in flight (upload index or remove reindex)
  tab: 'sources',
  drawerOpen: false,
  configBannerDismissed: false,
  upload: { files: [], uploading: false, indexing: false, error: null },
  removeTarget: null,    // {id,title} pending confirm
  initialIndexPolling: false,
  settings: {
    open: false,
    loaded: false,
    touched: false,      // user edited a field since last load
    saved: null,         // full snapshot from GET: {model, persona, retrieval, appearance, about}
    form: {
      name: '', base_url: '', api_key: '',
      preset: 'concise', custom: '',
      top_k: 4, chunk_size: 600,
      theme: 'light', language: 'en',
    },                   // staged values (key NEVER prefilled)
    dirty: false,
    advancedOpen: false,
    testing: false,
    testStatus: null,    // {kind:'ok'|'err', text}
    saving: false,
    saveError: null,
    discardConfirm: false,
  },
};

/* ------------------------------------------------------------ helpers */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const MAX_FILE_BYTES = 50 * 1024 * 1024;
const MAX_FILES = 20;
const SUGGESTIONS = [
  'Summarize the key findings of the documents',
  'What are the main differences between the reports?',
  'List the most important numbers or trends',
];

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fmtBytes(n) {
  if (!n) return '0 B';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function fmtWhen(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  const diffDays = Math.floor((now.getTime() - d.getTime()) / 86400000);
  if (diffDays >= 0 && diffDays < 7) {
    return d.toLocaleDateString([], { weekday: 'short' });
  }
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function readyDocs() {
  return state.sources.filter((s) => s.status === 'ready');
}

function canAsk() {
  return !!(
    state.config &&
    state.config.llm_configured &&
    readyDocs().length > 0 &&
    !state.sending &&
    !state.indexing
  );
}

/* ------------------------------------------------------------ API */
async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok || !body || body.ok !== true) {
    const err = (body && body.error) || { code: 'transient', message: 'HTTP ' + res.status };
    const e = new Error(err.message || 'Request failed');
    e.code = err.code;
    e.status = res.status;
    throw e;
  }
  return body.data;
}

async function refreshSources() {
  try {
    const data = await api('/api/sources');
    state.sources = data.sources || [];
    return data;
  } catch (e) {
    console.error('sources fetch failed', e);
    state.sources = [];
    return { sources: [], indexing: false };
  }
}

async function refreshHistory() {
  try {
    const data = await api('/api/history');
    state.conversations = data.conversations || [];
  } catch (e) {
    console.error('history fetch failed', e);
    state.conversations = [];
  }
  renderHistory();
}

/* ------------------------------------------------------------ theme */
const THEMES = ['dark', 'light', 'system'];
const LANGUAGES = ['en', 'id'];
const PRESET_IDS = ['concise', 'detailed', 'beginner', 'indonesian'];
const PRESET_LABELS = {
  concise: 'Concise',
  detailed: 'Detailed',
  beginner: 'Beginner',
  indonesian: 'Bahasa Indonesia',
};

function effectiveTheme(theme) {
  if (theme === 'system') {
    return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
  }
  return theme;
}

function applyTheme(theme) {
  const resolved = effectiveTheme(theme);
  document.documentElement.setAttribute('data-theme', resolved);
  $('#themeBtn').textContent = resolved === 'dark' ? '\u2600' : '\uD83C\uDF19';
  try { localStorage.setItem('qyd-theme', theme); } catch (_) { /* private mode */ }
}

function initTheme() {
  let theme = null;
  try { theme = localStorage.getItem('qyd-theme'); } catch (_) { /* ignore */ }
  if (!theme) {
    // No override yet: follow the OS (System), matching the old matchMedia default.
    theme = 'system';
  }
  applyTheme(theme);
  // Live-follow when the preference is System (design §6.9).
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      let cur = 'system';
      try { cur = localStorage.getItem('qyd-theme') || 'system'; } catch (_) { /* ignore */ }
      if (cur === 'system') applyTheme('system');
    });
  }
}

/* ------------------------------------------------------------ rendering: sources */
function renderSources() {
  const list = $('#sourcesList');
  if (!state.sources.length) {
    list.innerHTML = '<div class="empty-list">No sources yet. Add PDF or TXT files to build your knowledge base.</div>';
    return;
  }
  list.innerHTML = state.sources.map((s) => {
    const statusTxt = s.status === 'ready' ? 'Ready' : s.status === 'pending' ? 'Pending' : 'Error';
    const meta = (s.pages ? s.pages + ' page' + (s.pages === 1 ? '' : 's') : '?') +
      ' \u00B7 ' + s.chunks + ' chunk' + (s.chunks === 1 ? '' : 's');
    const errNote = s.error ? ' \u2014 ' + escapeHtml(s.error) : '';
    const removeBtn = s.status === 'ready'
      ? '<button class="src-remove" data-remove="' + escapeHtml(s.id) + '" data-title="' + escapeHtml(s.title) + '" aria-label="Remove ' + escapeHtml(s.title) + '" title="Remove source"' + (state.indexing ? ' disabled' : '') + '>\u2715</button>'
      : '';
    return (
      '<div class="src-row" role="listitem">' +
        '<span class="src-icon" aria-hidden="true">\u{1F4C4}</span>' +
        '<span class="src-main">' +
          '<div class="src-title" title="' + escapeHtml(s.title) + '">' + escapeHtml(s.title) + '</div>' +
          '<div class="src-meta">' + meta + '</div>' +
          '<div class="src-status ' + s.status + '"><span class="dot" aria-hidden="true"></span>' + statusTxt + errNote + '</div>' +
        '</span>' +
        removeBtn +
      '</div>'
    );
  }).join('');
}

/* ------------------------------------------------------------ rendering: history */
function renderHistory() {
  const list = $('#historyList');
  if (!state.conversations.length) {
    list.innerHTML = '<div class="empty-list">No conversations yet. Ask a question to start one.</div>';
    return;
  }
  list.innerHTML = state.conversations.map((c) => {
    const active = c.id === state.activeConvId;
    return (
      '<button class="conv-row' + (active ? ' active' : '') + '" data-conv="' + escapeHtml(c.id) + '" role="listitem" aria-current="' + (active ? 'true' : 'false') + '">' +
        '<span class="src-main">' +
          '<div class="conv-title" title="' + escapeHtml(c.title) + '">' + escapeHtml(c.title) + '</div>' +
          '<div class="conv-meta"><span>' + fmtWhen(c.updated_at) + '</span><span class="conv-count">' + c.message_count + ' msg' + (c.message_count === 1 ? '' : 's') + '</span></div>' +
        '</span>' +
      '</button>'
    );
  }).join('');
}

/* ------------------------------------------------------------ rendering: chat */
function renderChat() {
  const empty = $('#emptyState');
  const list = $('#msgList');
  const hasDocs = readyDocs().length > 0;

  if (!state.msgs.length) {
    list.hidden = true;
    list.innerHTML = '';
    empty.hidden = false;
    $('#emptyUploadBtn').hidden = hasDocs;
    if (hasDocs) {
      renderSuggestions();
      $('#suggestions').hidden = false;
    } else {
      $('#suggestions').hidden = true;
      $('#suggestions').innerHTML = '';
    }
  } else {
    empty.hidden = true;
    $('#suggestions').hidden = true;
    list.hidden = false;
    list.innerHTML = state.msgs.map(renderMsgHtml).join('');
  }
}

function renderSuggestions() {
  $('#suggestions').innerHTML = SUGGESTIONS.map((s) =>
    '<button class="suggestion" type="button" data-suggestion="' + escapeHtml(s) + '">' + escapeHtml(s) + '</button>'
  ).join('');
}

function renderMsgHtml(m) {
  if (m.typing) {
    return (
      '<div class="msg msg-assistant" role="status" aria-label="Assistant is typing">' +
        '<span class="msg-label">Assistant</span>' +
        '<div class="msg-body typing" aria-hidden="true"><i></i><i></i><i></i></div>' +
      '</div>'
    );
  }
  if (m.role === 'user') {
    return (
      '<div class="msg msg-user">' +
        '<span class="msg-label">You</span>' +
        '<div class="msg-body">' + escapeHtml(m.content) + '</div>' +
      '</div>'
    );
  }
  // assistant
  if (m.error) {
    return (
      '<div class="msg msg-assistant">' +
        '<span class="msg-label">Assistant</span>' +
        '<div class="msg-body" style="color:var(--error)">Failed: ' + escapeHtml(m.error) + '</div>' +
        (m.retryQuestion
          ? '<div><button class="btn btn-sm" type="button" data-retry="' + escapeHtml(m.retryQuestion) + '">Retry</button></div>'
          : '') +
      '</div>'
    );
  }
  const bodyHtml = renderAnswerHtml(m.content);
  const sourcesHtml = renderSourcesRow(m.content, m.sources);
  return (
    '<div class="msg msg-assistant">' +
      '<span class="msg-label">Assistant</span>' +
      '<div class="msg-body">' + bodyHtml + '</div>' +
      sourcesHtml +
    '</div>'
  );
}

/* Citations: every [n] / [n,m] token -> superscript chip; raw [n] never visible. */
function citeTokenRegex() {
  return /\[(\d+(?:\s*,\s*\d+)*)\]/g;
}

function citedNumbers(text) {
  const out = [];
  const re = citeTokenRegex();
  let m;
  while ((m = re.exec(String(text || '')))) {
    m[1].replace(/\s+/g, '').split(',').forEach((n) => {
      const k = parseInt(n, 10);
      if (k > 0 && out.indexOf(k) === -1) out.push(k);
    });
  }
  return out;
}

function inlineMd(text) {
  let t = escapeHtml(text);
  // citations first (our own tags are safe because escaping already happened)
  t = t.replace(citeTokenRegex(), (m, ns) => {
    const clean = ns.replace(/\s+/g, '');
    return '<sup class="cite-chip" title="Citation ' + clean + '">' + clean + '</sup>';
  });
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
  return t;
}

function renderAnswerHtml(text) {
  const src = String(text || '');
  const blocks = src.split(/\n{2,}/);
  const html = blocks.map((block) => {
    const lines = block.split('\n').filter((l) => l.trim() !== '');
    if (!lines.length) return '';
    const isList = lines.every((l) => /^\s*([-*]|\d+[.)])\s+/.test(l));
    if (isList) {
      const ordered = /^\s*\d+[.)]/.test(lines[0]);
      const items = lines.map((l) => {
        const li = l.replace(/^\s*([-*]|\d+[.)])\s+/, '');
        return '<li>' + inlineMd(li) + '</li>';
      }).join('');
      return ordered ? '<ol>' + items + '</ol>' : '<ul>' + items + '</ul>';
    }
    const joined = lines.join('<br>');
    return '<p>' + inlineMd(joined) + '</p>';
  }).join('');
  return html || '<p></p>';
}

function renderSourcesRow(answerText, sources) {
  const nums = citedNumbers(answerText);
  const byN = {};
  (sources || []).forEach((s) => { byN[s.n] = s; });
  const chips = nums
    .map((n) => byN[n])
    .filter(Boolean)
    .map((s) =>
      '<span class="source-chip" title="' + escapeHtml(s.title) + '">' +
        '<span class="n">' + s.n + '</span>' +
        '<span class="t">' + escapeHtml(s.title) + '</span>' +
        '<span class="p">p. ' + escapeHtml(s.page) + '</span>' +
      '</span>'
    ).join('');
  if (chips) {
    return '<div class="sources-row">' + chips + '</div>';
  }
  if (sources && sources.length) {
    return '<div class="no-sources-note">No sources matched \u2014 answer may be out of scope.</div>';
  }
  return '';
}

/* ------------------------------------------------------------ composer */
function autoGrow() {
  const el = $('#composer');
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 132) + 'px';
}

function renderComposer() {
  const composer = $('#composer');
  const send = $('#sendBtn');
  const hint = $('#composerHint');
  const q = composer.value.trim();
  const enabled = canAsk();
  composer.disabled = !enabled;
  send.disabled = !enabled || !q;

  if (!state.config) {
    hint.textContent = '';
  } else if (!state.config.llm_configured) {
    hint.textContent = 'LLM not configured \u2014 add OPENAI_API_KEY to .env and restart';
  } else if (readyDocs().length === 0) {
    hint.textContent = 'Add documents to start asking';
  } else if (state.indexing) {
    hint.textContent = 'Indexing knowledge base\u2026';
  } else {
    hint.textContent = '';
  }
}

function scrollChatToBottom() {
  const el = $('#chatScroll');
  el.scrollTop = el.scrollHeight;
}

/* ------------------------------------------------------------ send / ask */
async function sendQuestion(question) {
  state.sending = true;
  renderComposer();

  const userMsg = { role: 'user', content: question, sources: null };
  state.msgs.push(userMsg);
  state.msgs.push({ role: 'assistant', typing: true });
  renderChat();
  scrollChatToBottom();

  try {
    const data = await api('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_id: state.activeConvId || undefined,
        question: question,
      }),
    });
    state.activeConvId = data.conversation_id;
    state.msgs.pop(); // typing
    state.msgs.push({ role: 'assistant', content: data.answer, sources: data.sources || [] });
    await refreshHistory();
    renderChat();
    scrollChatToBottom();
  } catch (e) {
    state.msgs.pop(); // typing
    state.msgs.push({ role: 'assistant', error: e.message, retryQuestion: question });
    if (e.code === 'llm-not-configured') {
      state.config = Object.assign({}, state.config, { llm_configured: false });
      renderBanners();
    }
    renderChat();
    scrollChatToBottom();
  } finally {
    state.sending = false;
    renderComposer();
  }
}

/* ------------------------------------------------------------ upload modal */
function openUploadModal() {
  const modal = $('#uploadModal');
  modal.hidden = false;
  state.upload.error = null;
  $('#uploadError').hidden = true;
  renderFileQueue();
  updateIndexButton();
  $('#dropZone').focus();
}

function closeUploadModal() {
  if (state.upload.uploading || state.upload.indexing) return; // never lose progress
  $('#uploadModal').hidden = true;
}

function addFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const q = state.upload.files;
  const names = new Set(q.map((f) => f.name.toLowerCase()));

  files.forEach((file) => {
    const name = file.name || '';
    const size = file.size || 0;
    const ext = name.toLowerCase().split('.').pop();
    let status = 'queued';
    let error = null;

    if (!['pdf', 'txt'].includes(ext)) {
      status = 'rejected';
      error = 'unsupported type (.pdf/.txt only)';
    } else if (size > MAX_FILE_BYTES) {
      status = 'rejected';
      error = 'over 50 MB size cap';
    } else if (names.has(name.toLowerCase())) {
      status = 'rejected';
      error = 'duplicate filename';
    } else if (q.length >= MAX_FILES) {
      status = 'rejected';
      error = 'batch limit is ' + MAX_FILES + ' files';
    }
    if (status === 'queued') names.add(name.toLowerCase());
    q.push({ name, size, status, error, file });
  });

  renderFileQueue();
  updateIndexButton();
  uploadPending();
}

function uploadPending() {
  const pending = state.upload.files.filter((f) => f.status === 'queued');
  if (!pending.length || state.upload.uploading) return;
  state.upload.uploading = true;
  pending.forEach((f) => { f.status = 'uploading'; });
  renderFileQueue();

  const fd = new FormData();
  pending.forEach((f) => { if (f.file) fd.append('files', f.file, f.name); });

  api('/api/upload', { method: 'POST', body: fd })
    .then((data) => {
      const byName = {};
      (data.results || []).forEach((r) => { byName[r.name] = r; });
      pending.forEach((f) => {
        const r = byName[f.name];
        if (r && r.status === 'rejected') {
          f.status = 'rejected';
          f.error = r.error || 'rejected';
        } else if (r && r.status === 'ready') {
          f.status = 'ready';
        } else {
          f.status = 'rejected';
          f.error = 'no response entry';
        }
      });
    })
    .catch((e) => {
      pending.forEach((f) => { f.status = 'rejected'; f.error = e.message || 'upload failed'; });
      state.upload.error = 'Upload failed: ' + (e.message || 'network error');
    })
    .finally(() => {
      state.upload.uploading = false;
      renderFileQueue();
      updateIndexButton();
    });
}

function renderFileQueue() {
  const q = $('#fileQueue');
  if (!state.upload.files.length) {
    q.innerHTML = '';
    return;
  }
  q.innerHTML = state.upload.files.map((f) => {
    let statusTxt = 'queued';
    if (f.status === 'uploading') statusTxt = 'uploading\u2026';
    else if (f.status === 'ready') statusTxt = '\u2713 ready';
    else if (f.status === 'rejected') statusTxt = '\u2715 ' + (f.error || 'rejected');
    const cls = f.status;
    return (
      '<div class="file-row" role="listitem">' +
        '<span class="f-icon" aria-hidden="true">\u{1F4C4}</span>' +
        '<span class="f-main"><span class="f-name" title="' + escapeHtml(f.name) + '">' + escapeHtml(f.name) + '</span>' +
        '<span class="f-size">' + fmtBytes(f.size) + '</span></span>' +
        '<span class="f-status ' + cls + '">' + statusTxt + '</span>' +
      '</div>'
    );
  }).join('');
  $('#uploadError').hidden = !state.upload.error;
  if (state.upload.error) $('#uploadError').textContent = state.upload.error;
}

function updateIndexButton() {
  const btn = $('#indexBtn');
  const ready = state.upload.files.filter((f) => f.status === 'ready').length;
  const busy = state.upload.uploading || state.upload.indexing;
  btn.disabled = ready === 0 || busy;
  btn.textContent = ready === 0 ? 'Index documents' : 'Index ' + ready + ' document' + (ready === 1 ? '' : 's');
  $('#indexProgress').hidden = !state.upload.indexing;
  if (state.upload.indexing) btn.textContent = 'Indexing\u2026';
}

async function runIndex() {
  const ready = state.upload.files.filter((f) => f.status === 'ready').length;
  if (!ready || state.upload.indexing) return;
  state.upload.indexing = true;
  state.indexing = true;
  state.upload.error = null;
  renderFileQueue();
  updateIndexButton();
  renderBanners();
  renderComposer();

  try {
    await api('/api/index', { method: 'POST' });
    await Promise.all([refreshSources(), refreshHistory()]);
    state.upload.indexing = false;
    closeUploadModal();
    state.upload.files = [];
    showToast(ready + ' document' + (ready === 1 ? '' : 's') + ' ready');
  } catch (e) {
    state.upload.error = 'Indexing failed: ' + (e.message || 'unknown error');
    renderFileQueue();
    showToast(e.message || 'Indexing failed', true);
  } finally {
    state.upload.indexing = false;
    state.indexing = false;
    renderAll();
  }
}

/* ------------------------------------------------------------ remove source */
async function removeSource(id, title) {
  state.indexing = true;
  state.removeTarget = null;
  $('#confirmModal').hidden = true;
  renderBanners();
  renderComposer();
  renderSources();
  try {
    await api('/api/sources/' + encodeURIComponent(id), { method: 'DELETE' });
    await Promise.all([refreshSources(), refreshHistory()]);
    showToast('Removed ' + (title || id));
  } catch (e) {
    showToast(e.message || 'Remove failed', true);
  } finally {
    state.indexing = false;
    renderAll();
  }
}

/* ------------------------------------------------------------ history nav */
async function loadConversation(id) {
  try {
    const data = await api('/api/history/' + encodeURIComponent(id));
    state.activeConvId = data.conversation.id;
    state.msgs = (data.conversation.messages || []).map((m) => ({
      role: m.role,
      content: m.content,
      sources: m.sources || (m.role === 'assistant' ? [] : null),
      error: null,
    }));
  } catch (e) {
    showToast(e.message || 'Could not load conversation', true);
    return;
  }
  renderChat();
  scrollChatToBottom();
  renderHistory();
}

function newChat() {
  state.activeConvId = null;
  state.msgs = [];
  renderChat();
  renderHistory();
  $('#composer').focus();
}

/* ------------------------------------------------------------ banners / toast */
function renderBanners() {
  const cfg = $('#configBanner');
  const showCfg = state.config && !state.config.llm_configured && !state.configBannerDismissed;
  cfg.hidden = !showCfg;
  if (showCfg) {
    $('#configBannerMsg').textContent =
      'LLM not configured. Add OPENAI_API_KEY to your .env and restart the server \u2014 upload and indexing still work.';
  }
  $('#indexBanner').hidden = !state.indexing;
}

let toastTimer = null;
function showToast(msg, isError) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('error', !!isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

/* ------------------------------------------------------------ settings drawer */
function settingsFieldErrors() {
  const f = state.settings.form;
  const errs = {};
  if (!f.name.trim()) errs.name = 'Model name is required';
  else if (f.name.length > 120) errs.name = 'Max 120 characters';
  if (f.base_url.trim()) {
    try {
      const u = new URL(f.base_url.trim());
      if (u.protocol !== 'http:' && u.protocol !== 'https:') throw new Error('scheme');
    } catch (_) {
      errs.base_url = 'Must be a valid http(s) URL';
    }
  }
  const tk = Number(f.top_k);
  if (!Number.isInteger(tk) || tk < 1 || tk > 10) errs.top_k = 'Top-k must be an integer 1\u201310';
  const cs = Number(f.chunk_size);
  if (!Number.isInteger(cs) || cs < 100 || cs > 2000) errs.chunk_size = 'Chunk size must be 100\u20132000';
  return errs;
}

function settingsRenderErrors() {
  const errs = settingsFieldErrors();
  const pairs = [
    ['#settingsModelName', '#settingsModelNameErr', errs.name],
    ['#settingsBaseUrl', '#settingsBaseUrlErr', errs.base_url],
    ['#settingsTopK', '#settingsTopKErr', errs.top_k],
    ['#settingsChunkSize', '#settingsChunkSizeErr', errs.chunk_size],
  ];
  pairs.forEach(([sel, errSel, msg]) => {
    const el = $(sel);
    const errEl = $(errSel);
    el.classList.toggle('invalid', !!msg);
    errEl.hidden = !msg;
    errEl.textContent = msg || '';
  });
  return errs;
}

function settingsMarkDirty() {
  const s = state.settings;
  if (!s.saved) {
    s.dirty = false;
  } else {
    const sv = s.saved;
    const f = s.form;
    // Appearance (theme/language) is instant-only per design §6.1 — NOT staged,
    // so it never makes the form dirty. Only model/persona/retrieval do.
    s.dirty = !!(
      f.name !== sv.model.name ||
      f.base_url !== sv.model.base_url ||
      f.api_key !== '' ||
      f.preset !== sv.persona.preset ||
      f.custom !== sv.persona.custom ||
      f.top_k !== sv.retrieval.top_k ||
      f.chunk_size !== sv.retrieval.chunk_size
    );
  }
  $('#settingsDirtyChip').hidden = !s.dirty;
  settingsRenderFooter();
}

function settingsRenderFooter() {
  const s = state.settings;
  const valid = Object.keys(settingsFieldErrors()).length === 0;
  const saveBtn = $('#settingsSaveBtn');
  const discardBtn = $('#settingsDiscardBtn');
  saveBtn.disabled = !(s.dirty && valid && !s.saving);
  saveBtn.textContent = s.saving ? 'Saving\u2026' : 'Save';
  discardBtn.disabled = !(s.dirty && !s.saving);
  $('#settingsDiscardConfirm').hidden = !s.discardConfirm;
  const errEl = $('#settingsFootErr');
  errEl.hidden = !s.saveError;
  if (s.saveError) errEl.textContent = s.saveError;
}

function settingsRenderPresets() {
  const s = state.settings;
  const f = s.form;
  $$('input[name="personaPreset"]').forEach((r) => {
    r.checked = r.value === f.preset;
  });
  $('#settingsCustom').value = f.custom;
  const count = (f.custom || '').length;
  $('#settingsCustomCount').textContent = count + ' / 2000';
  const chip = $('#settingsCustomChip');
  const hasCustom = !!f.custom.trim();
  chip.hidden = !hasCustom;
  if (hasCustom) chip.textContent = 'Custom + ' + (PRESET_LABELS[f.preset] || f.preset);
}

function settingsRenderAppearance() {
  const f = state.settings.form;
  $$('input[name="appearanceTheme"]').forEach((r) => { r.checked = r.value === f.theme; });
  $$('input[name="appearanceLanguage"]').forEach((r) => { r.checked = r.value === f.language; });
}

function settingsRenderRetrieval() {
  const s = state.settings;
  const f = s.form;
  $('#settingsAdvancedToggle').setAttribute('aria-expanded', s.advancedOpen ? 'true' : 'false');
  $('#settingsAdvancedBody').hidden = !s.advancedOpen;
  $('#settingsTopK').value = f.top_k;
  $('#settingsChunkSize').value = f.chunk_size;
}

function settingsRenderAbout(about) {
  const line = $('#settingsAboutLine');
  const status = $('#settingsAboutStatus');
  if (!about) {
    line.textContent = 'Not loaded';
    status.textContent = '';
    return;
  }
  const docs = about.docs || 0;
  const chunks = about.chunks || 0;
  const convs = about.conversations || 0;
  line.textContent =
    'query-your-docs v' + about.version +
    ' \u00B7 ' + docs + ' doc' + (docs === 1 ? '' : 's') +
    ' \u00B7 ' + chunks + ' chunk' + (chunks === 1 ? '' : 's') +
    ' \u00B7 ' + convs + ' conversation' + (convs === 1 ? '' : 's');
  status.textContent = about.server_ok ? '\u2713 Server OK' : '\u2715 Server unreachable';
  status.className = 'about-status ' + (about.server_ok ? 'ok' : 'err');
}

function settingsRenderKeyMeta() {
  const s = state.settings;
  const hasKey = !!(s.saved && s.saved.model.api_key && s.saved.model.api_key.has_key);
  const typed = !!s.form.api_key;
  // Saved chip only when server has a key and the field is empty (typed value never shown on load)
  $('#settingsKeySaved').hidden = !(hasKey && !typed);
  $('#settingsApiKey').placeholder = hasKey && !typed ? '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022' : 'No API key set';
}

function settingsRender() {
  const s = state.settings;
  const f = s.form;
  $('#settingsModelName').value = f.name;
  $('#settingsBaseUrl').value = f.base_url;
  $('#settingsApiKey').value = f.api_key;
  $('#settingsKeyReveal').setAttribute('aria-label', $('#settingsApiKey').type === 'password' ? 'Show API key' : 'Hide API key');
  settingsRenderKeyMeta();
  // llm-not-configured hint mirrors the config banner
  const hint = $('#settingsKeyHint');
  const cfg = state.config;
  const hasKey = !!(s.saved && s.saved.model.api_key && s.saved.model.api_key.has_key);
  if (cfg && !cfg.llm_configured && !hasKey) {
    hint.textContent = 'No API key set \u2014 chat is disabled';
    hint.hidden = false;
  } else {
    hint.textContent = '';
    hint.hidden = true;
  }
  settingsRenderPresets();
  settingsRenderRetrieval();
  settingsRenderAppearance();
  settingsRenderAbout(s.saved ? s.saved.about : null);
  settingsRenderErrors();
  settingsMarkDirty();
  settingsRenderTestButton();
  settingsRenderFooter();
}

function settingsRenderTestButton() {
  const s = state.settings;
  const btn = $('#settingsTestBtn');
  btn.disabled = s.testing || !s.form.name.trim();
  btn.textContent = s.testing ? 'Testing\u2026' : 'Test connection';
  const st = $('#settingsTestStatus');
  st.className = 'test-status';
  st.textContent = '';
  if (s.testing) {
    st.classList.add('testing');
    st.textContent = 'Testing\u2026';
  } else if (s.testStatus) {
    st.classList.add(s.testStatus.kind);
    st.textContent = s.testStatus.text;
  }
}

async function settingsOpen() {
  const s = state.settings;
  if (s.open) return;
  s.open = true;
  s.touched = false;
  s.testing = false;
  s.testStatus = null;
  s.saving = false;
  s.saveError = null;
  s.advancedOpen = false;
  s.discardConfirm = false;
  s.form = { name: '', base_url: '', api_key: '', preset: 'concise', custom: '', top_k: 4, chunk_size: 600, theme: 'light', language: 'en' };
  s.saved = null;
  const bd = $('#settingsDrawer');
  bd.hidden = false;
  requestAnimationFrame(() => bd.classList.add('open'));
  $('#settingsBtn').classList.add('active');
  // load saved values from GET (key is never returned)
  try {
    const data = await api('/api/settings');
    const m = (data && data.model) || {};
    const p = (data && data.persona) || {};
    const r = (data && data.retrieval) || {};
    const a = (data && data.appearance) || {};
    s.saved = {
      model: { name: m.name || '', base_url: m.base_url || '', api_key: { has_key: !!(m.api_key && m.api_key.has_key) } },
      persona: { preset: p.preset || 'concise', custom: p.custom || '' },
      retrieval: { top_k: r.top_k, chunk_size: r.chunk_size },
      appearance: { theme: a.theme || 'light', language: a.language || 'en' },
      about: (data && data.about) || null,
    };
    if (!s.touched) {
      s.form.name = s.saved.model.name;
      s.form.base_url = s.saved.model.base_url;
      s.form.preset = s.saved.persona.preset;
      s.form.custom = s.saved.persona.custom;
      s.form.top_k = s.saved.retrieval.top_k;
      s.form.chunk_size = s.saved.retrieval.chunk_size;
      s.form.theme = s.saved.appearance.theme;
      s.form.language = s.saved.appearance.language;
      // Appearance is instant and lives in localStorage (design §6.9 "one key").
      // The live preference is authoritative over the stored backend value, so sync
      // BOTH form and baseline — otherwise opening the drawer looks falsely dirty
      // after using the topbar quick toggle.
      let liveTheme = null, liveLang = null;
      try {
        liveTheme = localStorage.getItem('qyd-theme');
        liveLang = localStorage.getItem('qyd-language');
      } catch (_) { /* ignore */ }
      if (liveTheme && THEMES.indexOf(liveTheme) !== -1) {
        s.saved.appearance.theme = liveTheme;
        s.form.theme = liveTheme;
      }
      if (liveLang && LANGUAGES.indexOf(liveLang) !== -1) {
        s.saved.appearance.language = liveLang;
        s.form.language = liveLang;
      }
    }
    s.loaded = true;
    settingsRender();
  } catch (e) {
    // leave empty form; surface error inline
    $('#settingsTestStatus').className = 'test-status err';
    $('#settingsTestStatus').textContent = 'Could not load settings: ' + (e.message || 'network error');
  }
  settingsRender();
  $('#settingsModelName').focus();
}

function settingsClose() {
  const s = state.settings;
  if (!s.open) return;
  if (s.discardConfirm) {
    // Second Esc/backdrop/x while the confirm is up: cancel the confirm, keep editing.
    s.discardConfirm = false;
    settingsRenderFooter();
    return;
  }
  if (s.dirty) {
    // Design §6.6: closing while dirty asks first (tiny inline confirm in footer).
    s.discardConfirm = true;
    settingsRenderFooter();
    $('#settingsConfirmDiscard').focus();
    return;
  }
  s.open = false;
  const bd = $('#settingsDrawer');
  bd.classList.remove('open');
  setTimeout(() => { if (!s.open) bd.hidden = true; }, 220);
  $('#settingsBtn').classList.remove('active');
  $('#settingsBtn').focus();
}

function settingsToggleAdvanced() {
  const s = state.settings;
  s.advancedOpen = !s.advancedOpen;
  settingsRenderRetrieval();
  settingsRenderErrors();
}

function settingsToggleKeyReveal() {
  const inp = $('#settingsApiKey');
  const show = inp.type === 'password';
  inp.type = show ? 'text' : 'password';
  $('#settingsKeyReveal').setAttribute('aria-label', show ? 'Hide API key' : 'Show API key');
}

async function settingsTestConnection() {
  const s = state.settings;
  if (s.testing || !s.form.name.trim()) return;
  const errs = settingsRenderErrors();
  if (errs.name || errs.base_url) return;
  s.testing = true;
  s.testStatus = null;
  settingsRenderTestButton();
  try {
    const body = { name: s.form.name.trim() };
    if (s.form.base_url.trim()) body.base_url = s.form.base_url.trim();
    if (s.form.api_key) body.api_key = s.form.api_key;
    const data = await api('/api/settings/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const ms = (data && data.latency_ms != null) ? ' \u00B7 ' + Math.round(data.latency_ms) + 'ms' : '';
    s.testStatus = { kind: 'ok', text: '\u2713 Connected' + ms };
  } catch (e) {
    if (e.code === 'not_found' || e.status === 404) {
      s.testStatus = { kind: 'err', text: 'Test endpoint not available yet' };
    } else if (e.code === 'connection-failed') {
      s.testStatus = { kind: 'err', text: 'Connection failed: ' + (e.message || 'unknown') };
    } else {
      s.testStatus = { kind: 'err', text: 'Connection failed: ' + (e.message || 'unknown') };
    }
  } finally {
    s.testing = false;
    settingsRenderTestButton();
  }
}

function settingsSetPreset(value) {
  const s = state.settings;
  s.form.preset = value;
  s.touched = true;
  settingsRenderPresets();
  settingsMarkDirty();
}

function settingsSetCustom(value) {
  const s = state.settings;
  s.form.custom = value;
  s.touched = true;
  settingsRenderPresets();
  settingsMarkDirty();
}

function settingsSetTheme(value) {
  const s = state.settings;
  s.form.theme = value;
  s.touched = true;
  applyTheme(value); // instant, no save needed
  settingsRenderAppearance();
  settingsMarkDirty();
}

function settingsSetLanguage(value) {
  const s = state.settings;
  s.form.language = value;
  s.touched = true;
  document.documentElement.setAttribute('lang', value); // instant
  try { localStorage.setItem('qyd-language', value); } catch (_) { /* private mode */ }
  settingsRenderAppearance();
  settingsMarkDirty();
}

async function settingsSave() {
  const s = state.settings;
  if (s.saving || !s.dirty) return;
  const errs = settingsRenderErrors();
  if (errs.name || errs.base_url || errs.top_k || errs.chunk_size) {
    s.saveError = 'Fix the highlighted fields before saving.';
    settingsRenderFooter();
    return;
  }
  s.saving = true;
  s.saveError = null;
  settingsRenderFooter();
  const f = s.form;
  const body = {
    model: { name: f.name.trim(), base_url: f.base_url.trim() },
    persona: { preset: f.preset, custom: f.custom },
    retrieval: { top_k: f.top_k, chunk_size: f.chunk_size },
    appearance: { theme: f.theme, language: f.language },
  };
  if (f.api_key) body.model.api_key = f.api_key; // empty keeps existing key
  try {
    const data = await api('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const m = (data && data.model) || {};
    const p = (data && data.persona) || {};
    const r = (data && data.retrieval) || {};
    const a = (data && data.appearance) || {};
    s.saved = {
      model: { name: m.name || '', base_url: m.base_url || '', api_key: { has_key: !!(m.api_key && m.api_key.has_key) } },
      persona: { preset: p.preset || 'concise', custom: p.custom || '' },
      retrieval: { top_k: r.top_k, chunk_size: r.chunk_size },
      appearance: { theme: a.theme || 'light', language: a.language || 'en' },
      about: s.saved ? s.saved.about : null,
    };
    s.form.api_key = ''; // typed key is consumed by save
    s.dirty = false;
    s.testStatus = null;
    settingsRender();
    showToast('Settings saved');
  } catch (e) {
    s.saveError = e.message || 'Save failed';
    settingsRenderFooter();
  } finally {
    s.saving = false;
    settingsRenderFooter();
  }
}

function settingsDiscard() {
  const s = state.settings;
  if (!s.saved) return;
  s.form = {
    name: s.saved.model.name,
    base_url: s.saved.model.base_url,
    api_key: '',
    preset: s.saved.persona.preset,
    custom: s.saved.persona.custom,
    top_k: s.saved.retrieval.top_k,
    chunk_size: s.saved.retrieval.chunk_size,
    theme: s.saved.appearance.theme,
    language: s.saved.appearance.language,
  };
  s.testStatus = null;
  s.saveError = null;
  s.discardConfirm = false;
  settingsRender();
  // instant appearance reverts with the staged values
  applyTheme(s.form.theme);
  document.documentElement.setAttribute('lang', s.form.language);
  try { localStorage.setItem('qyd-language', s.form.language); } catch (_) { /* private mode */ }
  showToast('Changes discarded');
}

/* ------------------------------------------------------------ render all */
function renderAll() {
  renderSources();
  renderHistory();
  renderChat();
  renderComposer();
  renderBanners();
  updateIndexButton();
}

/* ------------------------------------------------------------ init */
async function init() {
  initTheme();

  // bind static controls
  $('#themeBtn').addEventListener('click', () => {
    // Quick toggle cycles dark/light (design §6.9); resolves System to its effective value first.
    const cur = effectiveTheme(document.documentElement.getAttribute('data-theme'));
    applyTheme(cur === 'dark' ? 'light' : 'dark');
    if (state.settings.open) {
      // Sync the drawer's theme radio with the quick-toggle choice (instant, dirty).
      let t = null;
      try { t = localStorage.getItem('qyd-theme'); } catch (_) { /* ignore */ }
      state.settings.form.theme = t && THEMES.indexOf(t) !== -1 ? t : 'light';
      settingsRenderAppearance();
      settingsMarkDirty();
    }
  });
  $('#addSourcesBtn').addEventListener('click', openUploadModal);
  $('#sourcesAddBtn').addEventListener('click', openUploadModal);
  $('#emptyUploadBtn').addEventListener('click', openUploadModal);
  $('#menuBtn').addEventListener('click', () => {
    state.drawerOpen = true;
    $('#sidebar').classList.add('open');
    $('#scrim').hidden = false;
  });
  $('#scrim').addEventListener('click', closeDrawer);
  $('#closeModalBtn').addEventListener('click', closeUploadModal);
  $('#browseBtn').addEventListener('click', (e) => { e.stopPropagation(); $('#fileInput').click(); });
  $('#dropZone').addEventListener('click', () => $('#fileInput').click());
  $('#fileInput').addEventListener('change', (e) => { addFiles(e.target.files); e.target.value = ''; });
  $('#indexBtn').addEventListener('click', runIndex);

  // settings drawer
  $('#settingsBtn').addEventListener('click', settingsOpen);
  $('#settingsCloseBtn').addEventListener('click', settingsClose);
  $('#settingsDrawer').addEventListener('click', (e) => {
    if (e.target === $('#settingsDrawer')) settingsClose(); // backdrop click
  });
  $('#settingsKeyReveal').addEventListener('click', settingsToggleKeyReveal);
  $('#settingsTestBtn').addEventListener('click', settingsTestConnection);
  $('#settingsAdvancedToggle').addEventListener('click', settingsToggleAdvanced);
  $('#settingsSaveBtn').addEventListener('click', settingsSave);
  $('#settingsDiscardBtn').addEventListener('click', settingsDiscard);
  $('#settingsConfirmCancel').addEventListener('click', () => {
    state.settings.discardConfirm = false;
    settingsRenderFooter();
  });
  $('#settingsConfirmDiscard').addEventListener('click', () => {
    settingsDiscard();
    settingsClose();
  });
  ['settingsModelName', 'settingsBaseUrl', 'settingsApiKey', 'settingsTopK', 'settingsChunkSize', 'settingsCustom'].forEach((id) => {
    $('#' + id).addEventListener('input', () => {
      const s = state.settings;
      s.touched = true;
      s.form.name = $('#settingsModelName').value;
      s.form.base_url = $('#settingsBaseUrl').value;
      s.form.api_key = $('#settingsApiKey').value;
      s.form.top_k = Number($('#settingsTopK').value);
      s.form.chunk_size = Number($('#settingsChunkSize').value);
      s.form.custom = $('#settingsCustom').value;
      settingsRenderPresets();
      settingsMarkDirty();
      settingsRenderKeyMeta();
      settingsRenderTestButton();
      settingsRenderErrors();
      settingsRenderFooter();
    });
  });
  ['settingsModelName', 'settingsBaseUrl', 'settingsTopK', 'settingsChunkSize'].forEach((id) => {
    $('#' + id).addEventListener('blur', () => settingsRenderErrors());
  });
  $$('input[name="personaPreset"]').forEach((r) => {
    r.addEventListener('change', () => settingsSetPreset(r.value));
  });
  $$('input[name="appearanceTheme"]').forEach((r) => {
    r.addEventListener('change', () => settingsSetTheme(r.value));
  });
  $$('input[name="appearanceLanguage"]').forEach((r) => {
    r.addEventListener('change', () => settingsSetLanguage(r.value));
  });

  // drag & drop
  const dz = $('#dropZone');
  ['dragenter', 'dragover'].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('dragover'); })
  );
  ['dragleave', 'drop'].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('dragover'); })
  );
  dz.addEventListener('drop', (e) => addFiles(e.dataTransfer.files));

  // tabs
  $$('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      state.tab = tab.dataset.tab;
      $$('.tab').forEach((t) => {
        const on = t === tab;
        t.classList.toggle('active', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      $('#sourcesView').hidden = state.tab !== 'sources';
      $('#historyView').hidden = state.tab !== 'history';
    });
  });

  // new chat
  $('#newChatBtn').addEventListener('click', newChat);

  // config banner dismiss
  $('#configBannerDismiss').addEventListener('click', () => {
    state.configBannerDismissed = true;
    renderBanners();
  });

  // history list (delegation)
  $('#historyList').addEventListener('click', (e) => {
    const row = e.target.closest('[data-conv]');
    if (row) loadConversation(row.dataset.conv);
  });

  // sources list (delegation)
  $('#sourcesList').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-remove]');
    if (btn) {
      state.removeTarget = { id: btn.dataset.remove, title: btn.dataset.title };
      $('#confirmText').textContent =
        'Remove "' + state.removeTarget.title + '" from the knowledge base and reindex the remaining documents? Chat history stays.';
      $('#confirmModal').hidden = false;
      $('#confirmOkBtn').focus();
    }
  });

  // confirm modal
  $('#confirmOkBtn').addEventListener('click', () => {
    if (state.removeTarget) removeSource(state.removeTarget.id, state.removeTarget.title);
  });
  $('#confirmCancelBtn').addEventListener('click', () => {
    state.removeTarget = null;
    $('#confirmModal').hidden = true;
  });
  $('#confirmCancelX').addEventListener('click', () => {
    state.removeTarget = null;
    $('#confirmModal').hidden = true;
  });

  // suggestions + retry (delegation on chat pane)
  $('#suggestions').addEventListener('click', (e) => {
    const b = e.target.closest('[data-suggestion]');
    if (b) {
      $('#composer').value = b.dataset.suggestion;
      autoGrow();
      renderComposer();
      $('#composer').focus();
    }
  });
  $('#msgList').addEventListener('click', (e) => {
    const b = e.target.closest('[data-retry]');
    if (b) sendQuestion(b.dataset.retry);
  });

  // composer
  const composer = $('#composer');
  composer.addEventListener('input', () => { autoGrow(); renderComposer(); });
  composer.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (canAsk() && composer.value.trim()) sendQuestion(composer.value.trim());
    }
  });
  $('#sendBtn').addEventListener('click', () => {
    if (composer.value.trim() && canAsk()) sendQuestion(composer.value.trim());
  });

  // global Esc
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (state.settings.open) {
      settingsClose();
    } else if (!$('#confirmModal').hidden) {
      state.removeTarget = null;
      $('#confirmModal').hidden = true;
    } else if (!$('#uploadModal').hidden) {
      closeUploadModal();
    } else if (state.drawerOpen) {
      closeDrawer();
    }
  });

  function closeDrawer() {
    state.drawerOpen = false;
    $('#sidebar').classList.remove('open');
    $('#scrim').hidden = true;
  }

  // resize: close drawer on wide screens
  window.addEventListener('resize', () => {
    if (window.innerWidth >= 1024) closeDrawer();
  });

  // load data
  const [sourcesData] = await Promise.all([refreshSources(), refreshHistory()]);
  try {
    state.config = await api('/api/config');
  } catch (e) {
    state.config = { llm_configured: false, model: null };
  }

  if (sourcesData && sourcesData.indexing) {
    state.indexing = true;
    state.initialIndexPolling = true;
    pollIndexStatus();
  }

  renderAll();
}

async function pollIndexStatus() {
  try {
    const data = await api('/api/index/status');
    if (data.status !== 'indexing') {
      state.indexing = false;
      state.initialIndexPolling = false;
      await Promise.all([refreshSources(), refreshHistory()]);
      renderAll();
      return;
    }
  } catch (_) { /* server may be busy */ }
  if (state.initialIndexPolling) {
    setTimeout(pollIndexStatus, 2000);
  }
}

// smoke-test surface
window.QYD = { state, renderAll, api, inlineMd, citedNumbers, renderAnswerHtml, settingsOpen, settingsClose, settingsTestConnection, settingsToggleKeyReveal, settingsRender, settingsSave, settingsDiscard, settingsToggleAdvanced, settingsSetPreset, settingsSetCustom, settingsSetTheme, settingsSetLanguage, applyTheme, effectiveTheme };

init();
