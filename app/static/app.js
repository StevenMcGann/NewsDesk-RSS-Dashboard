// App state and coordination between the sidebar, article list, reader and dialogs.
import { api } from './api.js';
import { closePopover, popoverIsOpen, setImagesBlocked, store, toast } from './util.js';
import * as sidebar from './sidebar.js';
import * as articles from './articles.js';
import * as reader from './reader.js';
import * as dialogs from './dialogs.js';

const state = {
  data: { folders: [], feeds: [], tags: [], totals: {}, app: {} },
  settings: {},
  view: store('view') || 'all',
  filters: { status: 'unread', starred: false, tags: [], since: '', q: '' },
  sort: 'new',
  page: 1,
  list: { items: [], total: 0, page: 1, size: 20 },
  loading: false,
  selectedId: null,
  readerItem: null,
  version: null,
  refresh: { running: false, done: 0, total: 0, errors: 0, last_run: null },
  update: null,
  userRefresh: false,
  newSinceLoad: false,
  quitting: false,
};

const darkQuery = matchMedia('(prefers-color-scheme: dark)');
let loadSeq = 0;
let pollTimer = null;

const ctx = {
  state,
  feed: (id) => state.data.feeds.find((f) => f.id === id),
  folder: (id) => state.data.folders.find((f) => f.id === id),
  tag: (id) => state.data.tags.find((t) => t.id === id),
  tagsOf: (ids) => (ids || []).map((id) => ctx.tag(id)).filter(Boolean),
  viewTitle,
  setView,
  setFilters,
  setPage,
  loadState,
  loadItems,
  updateSettings,
  patchItem,
  localPatch: applyLocal,
  watchRefresh,
  openItem,
  closeReader,
  moveSelection,
  refresh,
  quit,
  markAllRead,
  markPageRead,
  applyLayout,
};

// ---- loading -------------------------------------------------------------------

async function loadState() {
  const data = await api.get('/api/state');
  state.data = data;
  state.settings = data.settings;
  state.refresh = data.refresh;
  state.update = data.update;
  if (state.version === null) state.version = data.version;
  setImagesBlocked(state.settings.block_remote_images);
  if (!viewExists(state.view)) state.view = 'all';
  sidebar.render();
  articles.renderHeader();
}

function viewExists(view) {
  if (view.startsWith('feed:')) return Boolean(ctx.feed(Number(view.slice(5))));
  if (view.startsWith('folder:')) return Boolean(ctx.folder(Number(view.slice(7))));
  return ['all', 'starred', 'saved'].includes(view);
}

function sinceEpoch(since) {
  if (!since) return '';
  if (since === 'today') {
    const now = new Date();
    return Math.floor(new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000);
  }
  return Math.floor(Date.now() / 1000 - parseInt(since, 10) * 86400);
}

export function currentFilter() {
  const f = state.filters;
  return { view: state.view, status: f.status, tags: f.tags, q: f.q, since: sinceEpoch(f.since) || null, starred: f.starred };
}

async function loadItems({ keepScroll = false } = {}) {
  const seq = ++loadSeq;
  const f = currentFilter();
  const params = new URLSearchParams({
    view: f.view, status: f.status, sort: state.sort, page: state.page, size: state.settings.page_size || 20,
  });
  if (f.tags.length) params.set('tags', f.tags.join(','));
  if (f.q) params.set('q', f.q);
  if (f.since) params.set('since', f.since);
  if (f.starred) params.set('starred', '1');
  state.loading = true;
  try {
    const res = await api.get(`/api/items?${params}`);
    if (seq !== loadSeq) return;
    const lastPage = Math.max(1, Math.ceil(res.total / res.size));
    if (res.page > lastPage) {
      state.page = lastPage;
      await loadItems({ keepScroll });
      return;
    }
    state.list = res;
    state.newSinceLoad = false;
    articles.hideBanner();
    articles.render({ keepScroll });
  } catch (e) {
    if (seq === loadSeq) toast(e.message, { error: true });
  } finally {
    if (seq === loadSeq) state.loading = false;
  }
}

// ---- view and filters ------------------------------------------------------------

function viewTitle(view = state.view) {
  if (view === 'starred') return 'Starred';
  if (view === 'saved') return 'Saved';
  if (view.startsWith('feed:')) return ctx.feed(Number(view.slice(5)))?.title || 'Feed';
  if (view.startsWith('folder:')) return ctx.folder(Number(view.slice(7)))?.name || 'Folder';
  return 'All articles';
}

function setView(view) {
  state.view = view;
  state.page = 1;
  store('view', view);
  sidebar.render();
  articles.renderHeader();
  loadItems();
}

function setFilters(patch) {
  Object.assign(state.filters, patch);
  state.page = 1;
  store('filters', { tags: state.filters.tags, since: state.filters.since, starred: state.filters.starred });
  if ('status' in patch) updateSettings({ status_filter: patch.status });
  articles.renderHeader();
  loadItems();
}

function setPage(page) {
  state.page = page;
  document.getElementById('articles-scroll').scrollTop = 0;
  loadItems();
}

async function updateSettings(patch, { quiet = false } = {}) {
  Object.assign(state.settings, patch);
  try {
    const saved = await api.patch('/api/settings', patch);
    Object.assign(state.settings, saved);
    if (saved.save_folder_resolved) state.data.app.save_folder = saved.save_folder_resolved;
  } catch (e) {
    if (!quiet) toast(e.message, { error: true });
    throw e;
  }
}

// ---- articles --------------------------------------------------------------------

function findLocal(id) {
  return [state.list.items.find((i) => i.id === id), state.readerItem?.id === id ? state.readerItem : null].filter(Boolean);
}

function applyLocal(id, patch) {
  const copies = findLocal(id);
  const before = copies[0];
  if (before && 'read' in patch && Boolean(before.read) !== Boolean(patch.read)) {
    const delta = patch.read ? -1 : 1;
    const feed = ctx.feed(before.feed_id);
    if (feed) feed.unread = Math.max(0, feed.unread + delta);
    state.data.totals.unread = Math.max(0, (state.data.totals.unread || 0) + delta);
  }
  for (const key of ['starred', 'saved']) {
    if (before && key in patch && Boolean(before[key]) !== Boolean(patch[key])) {
      state.data.totals[key] = Math.max(0, (state.data.totals[key] || 0) + (patch[key] ? 1 : -1));
    }
  }
  for (const copy of copies) Object.assign(copy, patch);
  articles.updateItem(id);
  reader.update();
  sidebar.render();
}

async function patchItem(id, patch) {
  const before = findLocal(id)[0];
  const undo = before ? Object.fromEntries(Object.keys(patch).map((k) => [k, before[k]])) : null;
  applyLocal(id, patch);
  try {
    await api.patch(`/api/items/${id}`, patch);
  } catch (e) {
    if (undo) applyLocal(id, undo);
    toast(e.message, { error: true });
  }
}

async function openItem(id) {
  state.selectedId = id;
  articles.markSelected();
  try {
    const item = await api.get(`/api/items/${id}`);
    if (state.selectedId !== id) return;
    state.readerItem = item;
    reader.show(item);
    if (state.settings.mark_read_on_open && !item.read) patchItem(id, { read: true });
  } catch (e) {
    toast(e.message, { error: true });
  }
}

function closeReader() {
  state.readerItem = null;
  reader.hide();
}

async function moveSelection(step) {
  const items = state.list.items;
  if (!items.length) return;
  const current = items.findIndex((i) => i.id === (state.readerItem?.id ?? state.selectedId));
  const next = current + step;
  if (next >= 0 && next < items.length) return openItem(items[next].id);
  const pages = Math.ceil(state.list.total / state.list.size);
  if (next >= items.length && state.page < pages) {
    state.page += 1;
    await loadItems();
    if (state.list.items.length) openItem(state.list.items[0].id);
  } else if (next < 0 && state.page > 1) {
    state.page -= 1;
    await loadItems();
    if (state.list.items.length) openItem(state.list.items[state.list.items.length - 1].id);
  }
}

async function markAllRead() {
  const ok = await dialogs.confirm({
    title: 'Mark all as read?',
    message: `Every article in "${viewTitle()}" that matches the current filters will be marked as read.`,
    confirmLabel: 'Mark all read',
  });
  if (!ok) return;
  try {
    const { changed } = await api.post('/api/items/mark-read', { filter: currentFilter() });
    toast(`Marked ${changed.toLocaleString()} article${changed === 1 ? '' : 's'} as read`);
    await loadState();
    await loadItems();
  } catch (e) {
    toast(e.message, { error: true });
  }
}

async function markPageRead() {
  const ids = state.list.items.filter((i) => !i.read).map((i) => i.id);
  if (!ids.length) return;
  try {
    await api.post('/api/items/mark-read', { ids });
    await loadState();
    await loadItems();
  } catch (e) {
    toast(e.message, { error: true });
  }
}

// ---- refreshing and liveness ---------------------------------------------------------

async function refresh(feedIds) {
  try {
    const status = await api.post('/api/refresh', feedIds ? { feed_ids: feedIds } : {});
    state.userRefresh = true;
    state.refresh = { ...status, running: true };
    sidebar.renderStatus();
    schedulePoll(600);
  } catch (e) {
    toast(e.message, { error: true });
  }
}

// Follow a refresh the server started on its own (after an import) and show the results.
function watchRefresh() {
  state.userRefresh = true;
  state.refresh = { ...state.refresh, running: true };
  sidebar.renderStatus();
  schedulePoll(600);
}

function schedulePoll(ms) {
  clearTimeout(pollTimer);
  if (state.quitting) return;
  pollTimer = setTimeout(heartbeat, ms);
}

async function heartbeat() {
  if (state.quitting) return;
  let hb;
  try {
    hb = await api.heartbeat();
  } catch (e) {
    if (state.quitting) return;
    if (e.status === 0) document.getElementById('offline').classList.remove('hidden');
    schedulePoll(15000);
    return;
  }
  if (state.quitting) return;
  document.getElementById('offline').classList.add('hidden');
  const finished = state.refresh.running && !hb.refresh.running;
  state.refresh = hb.refresh;
  sidebar.renderStatus();
  if (JSON.stringify(hb.update) !== JSON.stringify(state.update)) {
    state.update = hb.update;
    sidebar.renderUpdate();
  }
  const changed = hb.version !== state.version;
  if (changed) {
    state.version = hb.version;
    state.newSinceLoad = true;
  }
  if (changed || finished) await loadState().catch(() => {});
  if (!hb.refresh.running && (changed || finished)) {
    if (state.userRefresh || state.list.total === 0) {
      state.userRefresh = false;
      await loadItems({ keepScroll: true });
    } else if (state.newSinceLoad) {
      articles.showBanner();
    }
  }
  schedulePoll(hb.refresh.running ? 1000 : 15000);
}

async function quit() {
  await api.post('/api/quit');
  state.quitting = true;
  clearTimeout(pollTimer);
  const offline = document.getElementById('offline');
  offline.querySelector('strong').textContent = 'NewsDesk has quit';
  offline.querySelector('p').textContent = 'You can close this window. Start NewsDesk to open it again.';
  offline.classList.remove('hidden');
}

// ---- layout ------------------------------------------------------------------------

function applyTheme() {
  const theme = state.settings.theme || 'dark';
  document.documentElement.dataset.theme = theme === 'system' ? (darkQuery.matches ? 'dark' : 'light') : theme;
}

function applyLayout() {
  applyTheme();
  const root = document.documentElement;
  root.style.setProperty('--sb-w', `${state.settings.sidebar_width || 280}px`);
  const rw = state.settings.reader_width;
  root.style.setProperty('--rd-w', rw ? `${rw}px` : '42%');
  document.getElementById('app').classList.toggle('sidebar-collapsed', Boolean(state.settings.sidebar_collapsed));
}

function makeResizer(handle, onMove, onDone) {
  handle.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add('dragging');
    document.body.classList.add('resizing');
    const move = (ev) => onMove(ev.clientX);
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.classList.remove('dragging');
      document.body.classList.remove('resizing');
      onDone();
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
  });
}

function setupResizers() {
  const root = document.documentElement;
  let sbWidth = null;
  let rdWidth = null;
  makeResizer(document.getElementById('split-sidebar'), (x) => {
    sbWidth = Math.round(Math.max(180, Math.min(600, x)));
    root.style.setProperty('--sb-w', `${sbWidth}px`);
  }, () => sbWidth && updateSettings({ sidebar_width: sbWidth }, { quiet: true }).catch(() => {}));
  makeResizer(document.getElementById('split-reader'), (x) => {
    rdWidth = Math.round(Math.max(320, Math.min(innerWidth * 0.75, innerWidth - x)));
    root.style.setProperty('--rd-w', `${rdWidth}px`);
  }, () => rdWidth && updateSettings({ reader_width: rdWidth }, { quiet: true }).catch(() => {}));
}

// ---- keyboard ------------------------------------------------------------------------

function onKey(e) {
  if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
  const target = e.target;
  if (target.closest('input, textarea, select, [contenteditable]') || document.querySelector('dialog[open]')) return;
  if (popoverIsOpen()) return;
  const current = state.readerItem?.id ?? state.selectedId;
  const item = current ? (state.list.items.find((i) => i.id === current) || state.readerItem) : null;
  switch (e.key) {
    case 'j': moveSelection(1); break;
    case 'k': moveSelection(-1); break;
    case 'o':
    case 'Enter':
      if (target.closest('button, a')) return;
      if (state.selectedId) openItem(state.selectedId);
      else if (state.list.items.length) openItem(state.list.items[0].id);
      break;
    case 'm': if (item) patchItem(item.id, { read: !item.read }); break;
    case 's': if (item) patchItem(item.id, { starred: !item.starred }); break;
    case 'r': refresh(); break;
    case '/': document.getElementById('search')?.focus(); break;
    case 'Escape': if (state.readerItem) closeReader(); else closePopover(); break;
    case '?': dialogs.shortcuts(); break;
    default: return;
  }
  e.preventDefault();
}

// ---- start -------------------------------------------------------------------------

async function start() {
  for (const mod of [sidebar, articles, reader, dialogs]) mod.init(ctx);
  const saved = store('filters');
  if (saved) Object.assign(state.filters, saved);
  try {
    await loadState();
  } catch (e) {
    toast(e.message, { error: true, timeout: 0 });
    return;
  }
  state.filters.status = state.settings.status_filter;
  state.sort = state.settings.sort;
  applyLayout();
  darkQuery.addEventListener('change', applyTheme);
  setupResizers();
  articles.renderHeader();
  await loadItems();
  document.addEventListener('keydown', onKey);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) heartbeat(); });
  window.addEventListener('pagehide', (event) => { if (!event.persisted) api.bye(); });
  window.addEventListener('pageshow', (event) => { if (event.persisted) heartbeat(); });
  heartbeat();
  if (!state.data.feeds.length && !state.settings.first_run_done) dialogs.welcome();
}

start();
