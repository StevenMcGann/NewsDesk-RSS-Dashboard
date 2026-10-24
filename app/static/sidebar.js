// Sidebar: toolbar, smart views, the folder/feed tree with drag and drop, and refresh status.
import { api } from './api.js';
import { ago, el, favicon, fmt, fullDate, icon, iconButton, menu, setVar, toast } from './util.js';
import * as dialogs from './dialogs.js';

let ctx;
let drag = null;

export function init(c) {
  ctx = c;
  document.getElementById('sb-toolbar').replaceChildren(
    iconButton('plus', 'Add a feed', () => dialogs.addFeed()),
    iconButton('folder-plus', 'New folder', () => dialogs.addFolder()),
    iconButton('refresh-cw', 'Check all feeds for new articles (R)', () => ctx.refresh(), 'refresh-btn'),
    iconButton('upload', 'Import or export feeds', () => dialogs.importExport()),
    el('span', { class: 'grow' }),
    iconButton('settings', 'Settings', () => dialogs.settings()),
    iconButton('panel-left', 'Hide sidebar', () => toggleSidebar()),
  );
  setupRootDrop(document.getElementById('sb-root'));
}

export function toggleSidebar() {
  const collapsed = !ctx.state.settings.sidebar_collapsed;
  ctx.state.settings.sidebar_collapsed = collapsed;
  ctx.applyLayout();
  ctx.updateSettings({ sidebar_collapsed: collapsed }, { quiet: true }).catch(() => {});
}

export function render() {
  renderViews();
  renderTree();
  renderStatus();
}

function activate(row, action) {
  row.addEventListener('click', action);
  row.addEventListener('keydown', (e) => {
    if (e.target !== row) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      e.stopPropagation();
      action();
    }
  });
}

function rowIcon(name) {
  const node = icon(name);
  node.classList.add('row-ic');
  return node;
}

function renderViews() {
  const { totals } = ctx.state.data;
  const view = (id, iconName, label, count, muted) => {
    const row = el('div', { class: `nav-row${ctx.state.view === id ? ' active' : ''}`, tabindex: 0, role: 'button' },
      rowIcon(iconName), el('span', { class: 'label' }, label),
      count ? el('span', { class: `pill${muted ? ' muted' : ''}` }, fmt(count)) : null);
    activate(row, () => ctx.setView(id));
    return row;
  };
  document.getElementById('sb-views').replaceChildren(
    view('all', 'inbox', 'All articles', totals.unread),
    view('starred', 'star', 'Starred', totals.starred, true),
    view('saved', 'file-check', 'Saved', totals.saved, true),
  );
}

function renderTree() {
  const { folders, feeds } = ctx.state.data;
  const unread = new Map();
  const folderUnread = (id) => {
    if (!unread.has(id)) {
      let sum = feeds.filter((f) => f.folder_id === id).reduce((n, f) => n + f.unread, 0);
      for (const child of folders.filter((f) => f.parent_id === id)) sum += folderUnread(child.id);
      unread.set(id, sum);
    }
    return unread.get(id);
  };
  const build = (parentId, depth) => [
    ...folders.filter((f) => f.parent_id === parentId).map((f) => folderNode(f, depth, folderUnread, build)),
    ...feeds.filter((f) => f.folder_id === parentId).map((f) => feedRow(f, depth)),
  ];
  const nodes = build(null, 0);
  document.getElementById('tree').replaceChildren(
    ...(nodes.length ? nodes : [el('div', { class: 'tree-empty' }, 'No feeds yet. Click + to add one.')]));
}

function folderNode(folder, depth, folderUnread, build) {
  const id = `folder:${folder.id}`;
  const count = folderUnread(folder.id);
  const chev = el('span', { class: 'chev', title: folder.collapsed ? 'Expand' : 'Collapse' }, icon('chevron-down', 14));
  chev.addEventListener('click', (e) => { e.stopPropagation(); toggleFolder(folder); });
  const row = el('div', {
    class: `nav-row${ctx.state.view === id ? ' active' : ''}`,
    tabindex: 0,
    role: 'treeitem',
    'aria-expanded': String(!folder.collapsed),
    draggable: 'true',
  }, chev, rowIcon(folder.collapsed ? 'folder' : 'folder-open'), el('span', { class: 'label' }, folder.name),
  count ? el('span', { class: 'pill' }, fmt(count)) : null,
  iconButton('ellipsis', 'Folder options', (e) => { e.stopPropagation(); folderMenu(folder, e.currentTarget); }, 'small more'));
  setVar(row, '--depth', depth);
  activate(row, () => ctx.setView(id));
  row.addEventListener('contextmenu', (e) => { e.preventDefault(); folderMenu(folder, { x: e.clientX, y: e.clientY }); });
  wireDrag(row, 'folder', folder.id);
  return el('div', { class: `tree-folder${folder.collapsed ? ' collapsed' : ''}` }, row,
    el('div', { class: 'tree-children', role: 'group' }, build(folder.id, depth + 1)));
}

function feedRow(feed, depth) {
  const id = `feed:${feed.id}`;
  const row = el('div', {
    class: `nav-row${ctx.state.view === id ? ' active' : ''}`,
    tabindex: 0,
    role: 'treeitem',
    draggable: 'true',
    title: feed.last_error ? `Last update failed: ${feed.last_error}` : feed.url,
  }, favicon(feed.icon_url, feed.title), el('span', { class: 'label' }, feed.title),
  feed.last_error ? el('span', { class: 'err', 'aria-label': 'Update failed' }) : null,
  feed.unread ? el('span', { class: 'pill muted' }, fmt(feed.unread)) : null,
  iconButton('ellipsis', 'Feed options', (e) => { e.stopPropagation(); feedMenu(feed, e.currentTarget); }, 'small more'));
  setVar(row, '--depth', depth);
  activate(row, () => ctx.setView(id));
  row.addEventListener('contextmenu', (e) => { e.preventDefault(); feedMenu(feed, { x: e.clientX, y: e.clientY }); });
  wireDrag(row, 'feed', feed.id);
  return row;
}

async function toggleFolder(folder) {
  folder.collapsed = !folder.collapsed;
  renderTree();
  try {
    await api.patch(`/api/folders/${folder.id}`, { collapsed: folder.collapsed });
  } catch (e) {
    toast(e.message, { error: true });
  }
}

function feedIdsIn(folderId) {
  const { folders, feeds } = ctx.state.data;
  const ids = new Set([folderId]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const f of folders) {
      if (ids.has(f.parent_id) && !ids.has(f.id)) { ids.add(f.id); grew = true; }
    }
  }
  return feeds.filter((f) => ids.has(f.folder_id)).map((f) => f.id);
}

async function markViewRead(view, name) {
  try {
    const { changed } = await api.post('/api/items/mark-read', { filter: { view } });
    toast(`Marked ${changed.toLocaleString()} article${changed === 1 ? '' : 's'} in ${name} as read`);
    await ctx.loadState();
    await ctx.loadItems({ keepScroll: true });
  } catch (e) {
    toast(e.message, { error: true });
  }
}

function feedMenu(feed, anchor) {
  menu([
    { icon: 'check-check', label: 'Mark all as read', action: () => markViewRead(`feed:${feed.id}`, feed.title) },
    { icon: 'refresh-cw', label: 'Check for new articles', action: () => ctx.refresh([feed.id]) },
    '-',
    { icon: 'pencil', label: 'Rename…', action: () => dialogs.renameFeed(feed) },
    { icon: 'rss', label: 'Change feed address…', action: () => dialogs.changeFeedUrl(feed) },
    { icon: 'folder-input', label: 'Move to folder…', action: () => dialogs.moveFeed(feed) },
    { icon: 'file-text', label: 'Always load full articles', checked: feed.full_text, action: () => toggleFullText(feed) },
    /^https?:\/\//i.test(feed.site_url || '')
      ? { icon: 'external-link', label: 'Open website', action: () => window.open(feed.site_url, '_blank', 'noopener,noreferrer') }
      : null,
    '-',
    { icon: 'trash-2', label: 'Unsubscribe…', danger: true, action: () => dialogs.deleteFeed(feed) },
  ], anchor);
}

function folderMenu(folder, anchor) {
  menu([
    { icon: 'check-check', label: 'Mark all as read', action: () => markViewRead(`folder:${folder.id}`, folder.name) },
    { icon: 'refresh-cw', label: 'Check for new articles', action: () => ctx.refresh(feedIdsIn(folder.id)) },
    '-',
    { icon: 'plus', label: 'Add a feed here…', action: () => dialogs.addFeed(folder.id) },
    { icon: 'folder-plus', label: 'New subfolder…', action: () => dialogs.addFolder(folder.id) },
    { icon: 'pencil', label: 'Rename…', action: () => dialogs.renameFolder(folder) },
    '-',
    { icon: 'trash-2', label: 'Delete folder…', danger: true, action: () => dialogs.deleteFolder(folder) },
  ], anchor);
}

async function toggleFullText(feed) {
  try {
    await api.patch(`/api/feeds/${feed.id}`, { full_text: !feed.full_text });
    feed.full_text = !feed.full_text;
    toast(feed.full_text ? `Articles from ${feed.title} will open as full articles`
      : `Articles from ${feed.title} will show the feed's own text`);
  } catch (e) {
    toast(e.message, { error: true });
  }
}

// ---- drag and drop ------------------------------------------------------------------

function isInside(folderId, ancestorId) {
  for (let id = folderId; id != null; id = ctx.folder(id)?.parent_id ?? null) {
    if (id === ancestorId) return true;
  }
  return false;
}

function dropZone(e, row, kind, id) {
  if (!drag || (drag.kind === kind && drag.id === id)) return null;
  const r = row.getBoundingClientRect();
  const y = (e.clientY - r.top) / r.height;
  if (kind === 'folder') {
    if (drag.kind === 'folder' && isInside(id, drag.id)) return null;
    if (drag.kind === 'feed') return 'into';
    return y < 0.25 ? 'before' : y > 0.75 ? 'after' : 'into';
  }
  return drag.kind === 'feed' ? (y < 0.5 ? 'before' : 'after') : null;
}

function clearMarks() {
  for (const node of document.querySelectorAll('.drop-before, .drop-after, .drop-into')) {
    node.classList.remove('drop-before', 'drop-after', 'drop-into');
  }
}

function wireDrag(row, kind, id) {
  row.addEventListener('dragstart', (e) => {
    drag = { kind, id };
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', `${kind}:${id}`);
    row.classList.add('dragging');
  });
  row.addEventListener('dragend', () => { drag = null; row.classList.remove('dragging'); clearMarks(); });
  row.addEventListener('dragover', (e) => {
    const zone = dropZone(e, row, kind, id);
    if (!zone) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    clearMarks();
    row.classList.add(`drop-${zone}`);
  });
  row.addEventListener('dragleave', () => row.classList.remove('drop-before', 'drop-after', 'drop-into'));
  row.addEventListener('drop', (e) => {
    const zone = dropZone(e, row, kind, id);
    clearMarks();
    if (!zone) return;
    e.preventDefault();
    const { folders, feeds } = ctx.state.data;
    const moving = drag;
    if (kind === 'folder' && zone === 'into') return move(moving, id, Number.MAX_SAFE_INTEGER);
    const target = kind === 'folder' ? ctx.folder(id) : ctx.feed(id);
    const parent = kind === 'folder' ? target.parent_id : target.folder_id;
    const siblings = (kind === 'folder' ? folders.filter((f) => f.parent_id === parent) : feeds.filter((f) => f.folder_id === parent))
      .filter((s) => s.id !== moving.id);
    return move(moving, parent, siblings.findIndex((s) => s.id === id) + (zone === 'after' ? 1 : 0));
  });
}

function setupRootDrop(root) {
  root.addEventListener('dragover', (e) => {
    if (!drag) return;
    e.preventDefault();
    clearMarks();
    root.classList.add('drop-into');
  });
  root.addEventListener('dragleave', () => root.classList.remove('drop-into'));
  root.addEventListener('drop', (e) => {
    e.preventDefault();
    clearMarks();
    if (drag) move(drag, null, Number.MAX_SAFE_INTEGER);
  });
}

async function move(moving, parentId, index) {
  try {
    await api.post('/api/tree/move', { kind: moving.kind, id: moving.id, parent_id: parentId, index });
    if (parentId != null && ctx.folder(parentId)?.collapsed) {
      await api.patch(`/api/folders/${parentId}`, { collapsed: false });
    }
    await ctx.loadState();
    if (ctx.state.view.startsWith('folder:')) ctx.loadItems({ keepScroll: true });
  } catch (e) {
    toast(e.message, { error: true });
  }
}

// ---- status line --------------------------------------------------------------------

export function renderStatus() {
  const r = ctx.state.refresh;
  document.querySelector('.refresh-btn')?.classList.toggle('spin', Boolean(r.running));
  const box = document.getElementById('sb-status');
  if (r.running) {
    const pct = r.total ? Math.round((r.done / r.total) * 100) : 0;
    box.replaceChildren(
      el('span', { class: 'grow' }, r.total ? `Checking feeds… ${r.done} of ${r.total}` : 'Checking feeds…'),
      setVar(el('div', { class: 'progress' }, el('i')), '--p', `${pct}%`));
    return;
  }
  const failed = ctx.state.data.feeds.filter((f) => f.last_error);
  box.replaceChildren(...[
    el('span', { class: 'grow', title: r.last_run ? fullDate(r.last_run) : '' },
      r.last_run ? `Updated ${ago(r.last_run)}` : 'Not checked yet'),
    failed.length ? el('button', { type: 'button', class: 'status-err', onclick: () => dialogs.feedErrors(failed) },
      `${failed.length} feed${failed.length === 1 ? '' : 's'} failed`) : null,
  ].filter(Boolean));
}
