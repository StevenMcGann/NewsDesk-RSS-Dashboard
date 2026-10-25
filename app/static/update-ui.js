// User-initiated updates. Release text is never interpreted as HTML or Markdown.
import { api } from './api.js';
import { el, iconButton, toast } from './util.js';
import { openExternal } from './articles.js';

const ACTIVE = new Set(['checking', 'downloading', 'verifying', 'staging', 'preparing', 'restarting']);
const MARKER = 'newsdesk-update-reconnect';
const CONTEXT = 'newsdesk-update-view';
const PHASES = { checking: 'Checking the selected release…', downloading: 'Downloading update…',
  verifying: 'Verifying download…', staging: 'Checking the package…', preparing: 'Preparing to restart…',
  restarting: 'Installing update and restarting…', cancelled: 'Update cancelled.',
  failed: 'Update could not finish.', complete: 'Update complete.' };
let ctx;
let dialog;
let refs;
let pollTimer;
let reconnectTimer;
let reconnecting = false;
let reconnectStarted = 0;
let restore;

export function init(c) {
  ctx = c;
  window.addEventListener('storage', (event) => {
    if (event.key === MARKER && event.newValue) saveView();
  });
}

function marker() {
  try { return JSON.parse(localStorage.getItem(MARKER) || 'null'); } catch { return null; }
}

function saveView() {
  const m = marker();
  if (!m || m.version === ctx.state.data.app.version) return;
  try {
    sessionStorage.setItem(CONTEXT, JSON.stringify({ job: m.id, view: ctx.state.view, filters: ctx.state.filters,
      sort: ctx.state.sort, page: ctx.state.page, reader: ctx.state.readerItem?.id,
      scroll: document.getElementById('articles-scroll')?.scrollTop || 0,
      readerScroll: document.querySelector('.reader-scroll')?.scrollTop || 0 }));
  } catch { /* Storage is optional; update verification does not depend on it. */ }
}

export function restoreBeforeItems() {
  try {
    restore = JSON.parse(sessionStorage.getItem(CONTEXT) || 'null');
    if (!restore) return;
    const s = ctx.state;
    if (typeof restore.view === 'string') s.view = restore.view;
    if (restore.filters && typeof restore.filters === 'object') Object.assign(s.filters, restore.filters);
    if (['new', 'old'].includes(restore.sort)) s.sort = restore.sort;
    if (Number.isInteger(restore.page) && restore.page > 0) s.page = restore.page;
  } catch { restore = null; }
}

export async function restoreAfterItems() {
  if (!restore) return;
  sessionStorage.removeItem(CONTEXT);
  document.getElementById('articles-scroll').scrollTop = Number(restore.scroll) || 0;
  if (Number.isInteger(restore.reader) && restore.reader > 0) {
    await ctx.openItem(restore.reader);
    const pane = document.querySelector('.reader-scroll');
    if (pane) pane.scrollTop = Number(restore.readerScroll) || 0;
  }
  restore = null;
}

function remember(job) {
  if (!job?.reconnect) return;
  localStorage.setItem(MARKER, JSON.stringify(job.reconnect));
  saveView();
}

export function observe(status) {
  const job = status?.job;
  remember(job);
  if (refs && dialog?.isConnected) {
    refs.progress.hidden = !job || !ACTIVE.has(job.phase);
    refs.progress.max = Math.max(1, job?.total || 1);
    refs.progress.value = job?.bytes || 0;
    refs.status.textContent = job?.error || PHASES[job?.phase] || '';
    refs.cancel.hidden = !job?.can_cancel;
    const changed = refs.candidate && status?.candidate !== refs.candidate;
    refs.start.disabled = !status?.can_update || !status?.candidate || !status?.available || changed || ACTIVE.has(job?.phase);
    refs.reason.textContent = changed ? 'The release changed. Close this dialog and review the latest notes.'
      : status?.eligibility_reason || status?.manual_reason || '';
  }
  if (job?.phase === 'restarting') reconnect();
  if (job && ['failed', 'cancelled'].includes(job.phase)) {
    const m = marker();
    if (m?.id === job.id) localStorage.removeItem(MARKER);
  }
  const result = status?.last_result;
  if (result && sessionStorage.getItem('newsdesk-update-result') !== result.id) {
    sessionStorage.setItem('newsdesk-update-result', result.id);
    toast(result.phase === 'committed' ? `Updated to NewsDesk ${result.version}.`
      : 'The update did not start. Your previous version and data were restored.', { error: result.phase !== 'committed' });
  }
}

function button(label, action, className = '') {
  return el('button', { type: 'button', class: `btn ${className}`, onclick: action }, label);
}

export async function open() {
  if (dialog?.isConnected) { dialog.focus(); return; }
  const notes = el('pre', { class: 'release-notes' }, 'Loading release notes…');
  const heading = el('p');
  const reason = el('p', { class: 'hint' });
  const statusText = el('p', { role: 'status', 'aria-live': 'polite' });
  const progress = el('progress', { hidden: true, 'aria-label': 'Update download progress', max: 1, value: 0 });
  let selectedCandidate = null;
  const start = button('Update and restart', async () => {
    start.disabled = true;
    try {
      const job = await api.post('/api/update/start', { candidate: selectedCandidate });
      ctx.state.update.job = job;
      observe(ctx.state.update);
      scheduleStatus();
    } catch (error) { statusText.textContent = error.message; start.disabled = false; }
  }, 'primary');
  start.disabled = true;
  const cancel = button('Cancel update', async () => {
    try { await api.post('/api/update/cancel'); } catch (error) { toast(error.message, { error: true }); }
  });
  cancel.hidden = true;
  const link = () => ctx.state.update?.latest?.url || ctx.state.update?.releases_url;
  dialog = el('dialog', { class: 'dlg wide', id: 'update-dialog', 'aria-label': 'Update NewsDesk' },
    el('div', { class: 'dlg-head' }, el('h2', {}, 'Update NewsDesk'), iconButton('x', 'Close', () => dialog.close())),
    el('div', { class: 'dlg-body' }, heading, notes, reason,
      el('p', { class: 'hint' }, 'Download and verification happen first. NewsDesk will then restart and reconnect your open windows.'),
      progress, statusText),
    el('div', { class: 'dlg-foot' }, button('View on GitHub', () => openExternal(link())),
      button('Download manually', () => openExternal(link())), cancel, start));
  refs = { progress, status: statusText, start, cancel, reason };
  dialog.addEventListener('close', () => { dialog.remove(); refs = null; });
  document.body.append(dialog);
  dialog.showModal();
  try {
    const u = await api.get('/api/update/status');
    const n = await api.get('/api/update/notes');
    if (u.candidate !== n.candidate || u.latest?.version !== n.latest?.version) throw new Error('The release changed. Reopen this dialog to review it.');
    selectedCandidate = u.candidate;
    if (refs) refs.candidate = selectedCandidate;
    ctx.state.update = u;
    const size = u.download_size ? ` · ${(u.download_size / 1048576).toFixed(1)} MB` : '';
    heading.textContent = u.latest ? `NewsDesk ${u.current} → ${u.latest.version}${size}` : `NewsDesk ${u.current}`;
    notes.textContent = n.notes || 'No release notes were supplied.';
    observe(u);
    scheduleStatus();
  } catch (error) { statusText.textContent = error.message; }
}

function scheduleStatus() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try {
      const u = await api.get('/api/update/status');
      ctx.state.update = u;
      observe(u);
      if (ACTIVE.has(u.job?.phase)) scheduleStatus();
    } catch { if (marker()) reconnect(); }
  }, 400);
}

export function isReconnecting() { return reconnecting; }

export function reconnect() {
  const m = marker();
  if (!m || !/^[0-9a-f]{64}$/.test(m.nonce || '') || !/^[0-9a-f]{32}$/.test(m.id || '')) return false;
  if (m.version === ctx.state.data.app.version) return false;
  if (!reconnecting) {
    reconnecting = true;
    reconnectStarted = Date.now();
    saveView();
    dialog?.close();
    const box = document.getElementById('offline');
    box.querySelector('strong').textContent = 'Updating NewsDesk';
    box.querySelector('p').textContent = 'Installing and restarting. This window will reconnect automatically.';
    box.classList.remove('hidden');
    probe(m);
  }
  return true;
}

async function probe(m) {
  try {
    // No old API token or handoff nonce is sent to a possibly reassigned port.
    const response = await fetch('/api/ping', { cache: 'no-store' });
    const ping = await response.json();
    const r = ping.reconnect;
    if (ping.app === 'newsdesk' && r?.id === m.id && r.port === Number(location.port) &&
        ['committed', 'rolled_back'].includes(r.phase) &&
        r.version === (r.phase === 'committed' ? m.version : m.previous_version)) {
      const raw = new Uint8Array(m.nonce.match(/../g).map((b) => parseInt(b, 16)));
      const key = await crypto.subtle.importKey('raw', raw, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
      const bytes = await crypto.subtle.sign('HMAC', key,
        new TextEncoder().encode(`${r.id}\n${r.version}\n${r.port}\n${r.phase}`));
      const signature = Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, '0')).join('');
      if (signature === r.proof) { location.reload(); return; }
    }
  } catch { /* The old server is expected to disappear briefly. */ }
  if (Date.now() - reconnectStarted > 180000) {
    document.querySelector('#offline p').textContent = 'The restart is taking longer than expected. Start NewsDesk from its shortcut; recovery details are kept in the update log.';
  }
  reconnectTimer = setTimeout(() => probe(m), 1000);
}
