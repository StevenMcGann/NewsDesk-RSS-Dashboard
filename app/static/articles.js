// Main pane: header (title, search, filters, view toggle), article cards or list, and pager.
import { api } from './api.js';
import {
  debounce, el, favicon, fmt, fullDate, icon, iconButton, imagesAreBlocked, menu, popover, relTime, setVar, store, tagChip, toast,
} from './util.js';
import * as dialogs from './dialogs.js';
import { toggleSidebar } from './sidebar.js';

let ctx;
const refs = {};
const LAYOUTS = [['cards', 'Cards', 'layout-grid'], ['list', 'List', 'list'], ['feed', 'Feed', 'gallery-vertical-end']];
const GROUPS = [['none', 'None'], ['date', 'Date'], ['feed', 'Feed'], ['date_feed', 'Date + feed']];
const SIZE_NAMES = ['Smallest', 'Small', 'Default', 'Large', 'Largest'];
const SIZES = {
  cards: { '--card-width': [220, 260, 300, 360, 420], '--card-font': [13, 14, 15, 16, 17] },
  list: { '--row-padding': [6, 9, 12, 16, 20], '--row-font': [12, 13, 14, 15, 16] },
  feed: { '--feed-width': [620, 740, 860, 1000, 1140], '--feed-hero': [180, 240, 300, 360, 420],
    '--feed-padding': [12, 16, 20, 24, 28], '--feed-font': [18, 19, 20, 22, 24] },
};
const collapsed = new Set(store('collapsedGroups') || []);

function layout() {
  const mode = ctx.state.settings.view_mode;
  return LAYOUTS.some(([v]) => v === mode) ? mode : 'cards';
}

function sizeIndex(mode) {
  const value = ctx.state.settings[`${mode}_size`];
  return Number.isInteger(value) && value >= 0 && value < SIZE_NAMES.length ? value : 2;
}

function applySize() {
  const box = document.getElementById('articles');
  const mode = layout();
  for (const [name, values] of Object.entries(SIZES[mode])) {
    box.style.setProperty(name, `${values[sizeIndex(mode)]}px`);
  }
}

function sizeControls() {
  const mode = layout();
  const label = LAYOUTS.find(([v]) => v === mode)[1];
  let saving = false;
  const status = el('span', { class: 'size-value', role: 'status', 'aria-live': 'polite' });
  const update = () => {
    const size = sizeIndex(mode);
    status.textContent = SIZE_NAMES[size];
    smaller.disabled = saving || size === 0;
    larger.disabled = saving || size === SIZE_NAMES.length - 1;
    reset.disabled = saving || size === 2;
  };
  const change = async (value) => {
    if (saving) return;
    const key = `${mode}_size`;
    const previous = sizeIndex(mode);
    saving = true;
    const pending = ctx.updateSettings({ [key]: value });
    applySize();
    update();
    try {
      await pending;
    } catch {
      ctx.state.settings[key] = previous;
    } finally {
      saving = false;
      applySize();
      update();
    }
  };
  const smaller = el('button', { type: 'button', 'aria-label': `Decrease ${label.toLowerCase()} size`,
    onclick: () => change(sizeIndex(mode) - 1) }, '−');
  const larger = el('button', { type: 'button', 'aria-label': `Increase ${label.toLowerCase()} size`,
    onclick: () => change(sizeIndex(mode) + 1) }, '+');
  const reset = el('button', { type: 'button', class: 'btn small ghost', 'aria-label': `Reset ${label.toLowerCase()} size`,
    onclick: () => change(2) }, 'Reset');
  update();
  return el('div', { class: 'size-controls', role: 'group', 'aria-label': `${label} size` },
    el('div', { class: 'seg size-stepper' }, smaller, status, larger), reset);
}

export function init(c) {
  ctx = c;
  refs.showSidebar = iconButton('panel-left', 'Show sidebar', () => toggleSidebar(), 'hidden');
  refs.title = el('h1', {});
  refs.sub = el('span', { class: 'sub' });
  refs.search = el('input', { id: 'search', type: 'search', placeholder: 'Search articles  ( / )', 'aria-label': 'Search articles' });
  refs.search.addEventListener('input', debounce(() => ctx.setFilters({ q: refs.search.value.trim() }), 300));
  refs.search.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      refs.search.value = '';
      ctx.setFilters({ q: '' });
      refs.search.blur();
    }
  });
  refs.badge = el('span', { class: 'badge hidden' });
  refs.filter = iconButton('list-filter', 'Filter articles', () => openFilters());
  refs.filter.append(refs.badge);
  refs.view = iconButton('menu', 'View options: layout, grouping and sort order', () => openViewOptions());
  refs.markAll = iconButton('check-check', 'Mark all as read', () => ctx.markAllRead());
  document.getElementById('main-head').replaceChildren(
    refs.showSidebar,
    el('div', { class: 'head-title' }, refs.title, refs.sub),
    el('div', { class: 'search' }, icon('search', 15), refs.search),
    refs.filter, refs.view, refs.markAll);
}

export function renderHeader() {
  const s = ctx.state;
  const f = s.filters;
  refs.showSidebar.classList.toggle('hidden', !s.settings.sidebar_collapsed);
  refs.title.textContent = ctx.viewTitle();
  document.title = `${ctx.viewTitle()} · NewsDesk`;
  const status = { unread: 'Unread', read: 'Read', all: 'All' }[f.status];
  refs.sub.textContent = `${status} · ${fmt(s.list.total)} article${s.list.total === 1 ? '' : 's'}`;
  if (refs.search.value.trim() !== f.q) refs.search.value = f.q;
  const active = (f.status !== 'all') + Boolean(f.starred) + f.tags.length + (f.since ? 1 : 0);
  refs.badge.textContent = active;
  refs.badge.classList.toggle('hidden', !active);
}

function openViewOptions() {
  const s = ctx.state;
  const save = (patch) => ctx.updateSettings(patch, { quiet: true }).catch(() => {});
  popover(el('div', {},
    el('h4', {}, 'Layout'),
    el('div', { class: 'seg', role: 'group' }, LAYOUTS.map(([v, label, iconName]) => el('button', {
      type: 'button',
      class: layout() === v ? 'on' : '',
      'aria-pressed': String(layout() === v),
      onclick: () => { save({ view_mode: v }); render(); openViewOptions(); },
    }, icon(iconName, 14), label))),
    el('h4', {}, 'Size'),
    sizeControls(),
    el('p', { class: 'hint' }, 'Saved separately for each layout.'),
    el('h4', {}, 'Group by'),
    segmented(GROUPS, s.settings.group_by, (v) => { save({ group_by: v }); render({ keepScroll: true }); openViewOptions(); }),
    el('h4', {}, 'Sort'),
    segmented([['new', 'Newest first'], ['old', 'Oldest first']], s.sort, (v) => {
      s.sort = v;
      save({ sort: v });
      ctx.setPage(1);
      openViewOptions();
    })), refs.view, { align: 'end', className: 'filter-pop' });
}

function segmented(options, value, onPick) {
  return el('div', { class: 'seg', role: 'group' }, options.map(([v, label]) =>
    el('button', { type: 'button', class: v === value ? 'on' : '', 'aria-pressed': String(v === value), onclick: () => onPick(v) }, label)));
}

function openFilters() {
  const f = ctx.state.filters;
  const update = (patch) => { ctx.setFilters(patch); openFilters(); };
  const starred = el('input', { type: 'checkbox', checked: f.starred, onchange: () => update({ starred: starred.checked }) });
  const tags = ctx.state.data.tags;
  popover(el('div', {},
    el('h4', {}, 'Show'),
    segmented([['unread', 'Unread'], ['read', 'Read'], ['all', 'All']], f.status, (v) => update({ status: v })),
    el('h4', {}, 'Published'),
    segmented([['', 'Any time'], ['today', 'Today'], ['7', '7 days'], ['30', '30 days']], f.since, (v) => update({ since: v })),
    el('h4', {}, 'Only'),
    el('label', { class: 'check-row' }, el('span', { class: 'switch' }, starred, el('span')), 'Starred articles'),
    tags.length ? el('h4', {}, 'Tags') : null,
    tags.length ? el('div', { class: 'chips' }, tags.map((tag) => {
      const on = f.tags.includes(tag.id);
      return el('button', {
        type: 'button',
        class: `chip-toggle${on ? ' on' : ''}`,
        'aria-pressed': String(on),
        onclick: () => update({ tags: on ? f.tags.filter((t) => t !== tag.id) : [...f.tags, tag.id] }),
      }, setVar(el('span', { class: 'dot' }), '--c', tag.color), tag.name);
    })) : null,
    el('div', { class: 'foot' }, el('button', {
      type: 'button',
      class: 'btn small ghost',
      onclick: () => update({ status: 'all', since: '', starred: false, tags: [] }),
    }, 'Clear filters'))), refs.filter, { align: 'end', className: 'filter-pop' });
}

// ---- articles -----------------------------------------------------------------------

export function render({ keepScroll = false } = {}) {
  const s = ctx.state;
  const box = document.getElementById('articles');
  const mode = layout();
  box.className = `articles ${mode}`;
  applySize();
  if (!keepScroll) document.getElementById('articles-scroll').scrollTop = 0;
  box.replaceChildren(...(s.list.items.length ? grouped(s.list.items, mode) : [emptyState()]));
  renderPager();
  renderHeader();
}

// ---- grouping (per page, like the plugin) -------------------------------------------------

function dayKey(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (d.toDateString() === now.toDateString()) return 'Today';
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
  return d.toLocaleDateString(undefined, {
    weekday: 'long', month: 'short', day: 'numeric', ...(d.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
  });
}

function groupItems(items, by, parentKey = '') {
  const groups = new Map();
  for (const item of items) {
    const key = `${parentKey}${by}:${by === 'date' ? dayKey(item.published) : item.feed_id}`;
    if (!groups.has(key)) {
      groups.set(key, { key, by, first: item, label: by === 'date' ? dayLabel(item.published) : item.feed_title, items: [] });
    }
    groups.get(key).items.push(item);
  }
  return [...groups.values()];
}

function grouped(items, mode) {
  const by = ctx.state.settings.group_by;
  if (!['date', 'feed', 'date_feed'].includes(by)) return items.map((item) => build(item, mode));
  return groupItems(items, by === 'feed' ? 'feed' : 'date').flatMap((group) => {
    const nodes = [groupHeader(group, false)];
    if (collapsed.has(group.key)) return nodes;
    if (by !== 'date_feed') return [...nodes, ...group.items.map((item) => build(item, mode))];
    for (const sub of groupItems(group.items, 'feed', `${group.key}|`)) {
      nodes.push(groupHeader(sub, true));
      if (!collapsed.has(sub.key)) nodes.push(...sub.items.map((item) => build(item, mode)));
    }
    return nodes;
  });
}

function toggleGroup(key) {
  if (!collapsed.delete(key)) collapsed.add(key);
  store('collapsedGroups', [...collapsed].slice(-300));
  render({ keepScroll: true });
}

function groupHeader(group, sub) {
  const closed = collapsed.has(group.key);
  const count = group.items.length;
  const head = el('div', {
    class: `group-head${sub ? ' sub' : ''}`,
    role: 'button',
    tabindex: 0,
    'aria-expanded': String(!closed),
    title: closed ? 'Expand' : 'Collapse',
  }, el('span', { class: 'group-toggle' }, icon(closed ? 'chevron-right' : 'chevron-down', 14)),
  group.by === 'feed' ? favicon(group.first.feed_icon, group.first.feed_title) : null,
  el('span', { class: 'group-label' }, group.label),
  el('span', { class: 'group-count' }, `${count} article${count === 1 ? '' : 's'}`));
  head.addEventListener('click', () => toggleGroup(group.key));
  head.addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target === head) {
      e.preventDefault();
      e.stopPropagation();
      toggleGroup(group.key);
    }
  });
  return head;
}

function build(item, mode) {
  const node = mode === 'list' ? row(item) : mode === 'feed' ? feedItem(item) : card(item);
  node.dataset.id = item.id;
  node.tabIndex = 0;
  node.addEventListener('click', (e) => { if (!e.target.closest('button, a')) ctx.openItem(item.id); });
  node.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target === node) { e.stopPropagation(); ctx.openItem(item.id); }
  });
  node.addEventListener('contextmenu', (e) => { e.preventDefault(); itemMenu(item, { x: e.clientX, y: e.clientY }); });
  return node;
}

function classes(item, base) {
  return `${base}${item.read ? ' read' : ''}${item.id === ctx.state.selectedId ? ' selected' : ''}`;
}

function card(item) {
  return el('article', { class: classes(item, 'card'), 'aria-label': item.title },
    el('div', { class: 'card-title', title: item.title }, item.title),
    el('div', { class: 'source' }, favicon(item.feed_icon, item.feed_title), el('span', {}, item.feed_title)),
    media(item),
    tagRow(item),
    el('div', { class: 'card-foot' }, ...actions(item), el('span', { class: 'date', title: fullDate(item.published) }, relTime(item.published))));
}

function row(item) {
  return el('div', { class: classes(item, 'row') },
    el('span', { class: 'dot', title: item.read ? 'Read' : 'Unread' }),
    el('span', { class: 'row-source' }, favicon(item.feed_icon, item.feed_title), el('span', {}, item.feed_title)),
    el('span', { class: 'row-text', title: item.title }, el('span', { class: 'row-title' }, item.title),
      item.snippet ? el('span', { class: 'row-snippet' }, item.snippet) : null),
    tagRow(item),
    ...actions(item, true),
    el('span', { class: 'date', title: fullDate(item.published) }, relTime(item.published)));
}

// Feed layout: one wide column; cover image over a blurred copy of itself, then the text.
function feedItem(item) {
  const by = ctx.state.settings.group_by;
  const showSource = !ctx.state.view.startsWith('feed:') && by !== 'feed' && by !== 'date_feed';
  return el('article', { class: classes(item, 'feed-item'), 'aria-label': item.title },
    feedHero(item),
    el('div', { class: 'feed-body' },
      el('div', { class: 'feed-title' }, item.title),
      showSource ? el('div', { class: 'source' }, favicon(item.feed_icon, item.feed_title), el('span', {}, item.feed_title)) : null,
      item.snippet ? el('div', { class: 'feed-summary' }, item.snippet) : null,
      tagRow(item)),
    el('div', { class: 'feed-foot' }, ...actions(item),
      el('span', { class: 'date', title: fullDate(item.published) }, relTime(item.published))));
}

function feedHero(item) {
  if (!item.image_url || imagesAreBlocked()) return null;
  const attrs = { src: item.image_url, alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer' };
  const img = el('img', { ...attrs, class: 'feed-hero-img' });
  const box = el('div', { class: 'feed-hero' }, el('img', { ...attrs, class: 'feed-hero-blur', 'aria-hidden': 'true' }), img);
  img.addEventListener('error', () => box.remove(), { once: true });
  return box;
}

function media(item) {
  if (item.image_url && !imagesAreBlocked()) {
    const img = el('img', { src: item.image_url, alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer' });
    const box = el('div', { class: 'card-media' }, img);
    img.addEventListener('error', () => box.replaceWith(snippetBox(item)), { once: true });
    return box;
  }
  return snippetBox(item);
}

function snippetBox(item) {
  return el('div', { class: 'card-snippet' }, item.snippet || 'No preview available.');
}

function tagRow(item) {
  const tags = ctx.tagsOf(item.tags);
  return tags.length ? el('div', { class: 'tag-row' }, tags.map(tagChip)) : null;
}

function actions(item, compact = false) {
  const buttons = [];
  if (!compact) {
    buttons.push(iconButton(item.read ? 'circle-check' : 'circle', item.read ? 'Mark as unread (M)' : 'Mark as read (M)',
      () => ctx.patchItem(item.id, { read: !item.read }), item.read ? '' : 'on'));
  }
  buttons.push(iconButton('star', item.starred ? 'Unstar (S)' : 'Star (S)',
    () => ctx.patchItem(item.id, { starred: !item.starred }), `star${item.starred ? ' on' : ''}`));
  if (!compact) {
    buttons.push(iconButton('tag', 'Tags', (e) => dialogs.tagPicker(item, e.currentTarget)));
    if (item.saved) {
      buttons.push(iconButton('file-check', 'Saved as Markdown: show in folder',
        () => api.post('/api/open-folder', { item_id: item.id }).catch((e) => toast(e.message, { error: true })), 'on'));
    }
    if (item.url) buttons.push(iconButton('external-link', 'Open in browser', () => openExternal(item.url)));
  }
  return buttons;
}

export function openExternal(url) {
  if (/^https?:\/\//i.test(url || '')) window.open(url, '_blank', 'noopener,noreferrer');
}

function itemMenu(item, anchor) {
  menu([
    { icon: item.read ? 'circle' : 'circle-check', label: item.read ? 'Mark as unread' : 'Mark as read', action: () => ctx.patchItem(item.id, { read: !item.read }) },
    { icon: 'star', label: item.starred ? 'Unstar' : 'Star', action: () => ctx.patchItem(item.id, { starred: !item.starred }) },
    { icon: 'tag', label: 'Tags…', action: () => dialogs.tagPicker(item, anchor) },
    '-',
    item.url ? { icon: 'external-link', label: 'Open in browser', action: () => openExternal(item.url) } : null,
    item.url ? { icon: 'rss', label: 'Copy link', action: () => navigator.clipboard.writeText(item.url).then(() => toast('Link copied')) } : null,
    { icon: 'check-check', label: 'Mark everything older as read', action: () => markOlderRead(item) },
  ], anchor);
}

async function markOlderRead(item) {
  try {
    const { changed } = await api.post('/api/items/mark-read', { filter: { view: ctx.state.view, until: item.published } });
    toast(`Marked ${changed.toLocaleString()} older article${changed === 1 ? '' : 's'} as read`);
    await ctx.loadState();
    await ctx.loadItems({ keepScroll: true });
  } catch (e) {
    toast(e.message, { error: true });
  }
}

export function updateItem(id) {
  const item = ctx.state.list.items.find((i) => i.id === id);
  const node = document.querySelector(`#articles [data-id="${id}"]`);
  if (!item || !node) return;
  const fresh = build(item, layout());
  const focused = node.contains(document.activeElement);
  node.replaceWith(fresh);
  if (focused) fresh.focus();
  renderPager();
}

export function markSelected() {
  for (const node of document.querySelectorAll('#articles [data-id]')) {
    const on = Number(node.dataset.id) === ctx.state.selectedId;
    node.classList.toggle('selected', on);
    if (on) node.scrollIntoView({ block: 'nearest' });
  }
}

function emptyState() {
  const s = ctx.state;
  const f = s.filters;
  const box = (iconName, title, text, ...buttons) => el('div', { class: 'empty' },
    iconName === 'logo' ? el('img', { class: 'empty-logo', src: 'favicon.svg', width: 72, height: 72, alt: '' }) : icon(iconName, 40),
    el('h2', {}, title), el('p', {}, text), buttons.length ? el('div', { class: 'actions' }, buttons) : null);
  const button = (label, onClick, primary = false, iconName = null) => el('button', {
    type: 'button', class: `btn${primary ? ' primary' : ''}`, onclick: onClick,
  }, iconName ? icon(iconName) : null, label);
  if (!s.data.feeds.length) {
    return box('logo', 'Welcome to NewsDesk RSS Dashboard',
      'Follow news sites and blogs in one place instead of visiting each one. Start by adding a feed.',
      button('Add a feed', () => dialogs.addFeed(), true, 'plus'),
      s.data.app.starter_available ? button('Add the starter feeds', () => dialogs.importStarter(), false, 'newspaper') : null,
      button('Import from a file', () => dialogs.importExport(), false, 'upload'));
  }
  if (f.q) return box('search', 'No matches', `Nothing here matches "${f.q}".`, button('Clear search', () => ctx.setFilters({ q: '' })));
  const otherFilters = f.starred || f.tags.length || f.since;
  if (f.status === 'unread' && !otherFilters) {
    return box('check-check', 'All caught up', 'There are no unread articles here.',
      button('Show read articles too', () => ctx.setFilters({ status: 'all' })));
  }
  if (s.view === 'starred' && !otherFilters && f.status === 'all') {
    return box('star', 'No starred articles', 'Star an article to keep it here.');
  }
  if (s.view === 'saved' && !otherFilters && f.status === 'all') {
    return box('file-down', 'Nothing saved yet', 'Use "Save as Markdown" in the reader to keep a copy of an article.');
  }
  return box('inbox', 'Nothing here', 'No articles match the current filters.',
    button('Clear filters', () => ctx.setFilters({ status: 'all', since: '', starred: false, tags: [] })));
}

function renderPager() {
  const pager = document.getElementById('pager');
  const { total, size, page } = ctx.state.list;
  if (!total) {
    pager.replaceChildren();
    return;
  }
  const pages = Math.max(1, Math.ceil(total / size));
  const pg = (p, label = String(p), title = `Page ${p}`) => el('button', {
    type: 'button',
    class: `pg${p === page ? ' current' : ''}`,
    title,
    disabled: p < 1 || p > pages,
    'aria-current': p === page ? 'page' : null,
    onclick: () => { if (p !== page) ctx.setPage(p); },
  }, label);
  const numbers = new Set([1, pages]);
  for (let p = page - 2; p <= page + 2; p += 1) if (p >= 1 && p <= pages) numbers.add(p);
  const parts = [pg(page - 1, '‹', 'Previous page')];
  let last = 0;
  for (const p of [...numbers].sort((a, b) => a - b)) {
    if (p - last > 1) parts.push(el('span', { class: 'gap' }, '…'));
    parts.push(pg(p));
    last = p;
  }
  parts.push(pg(page + 1, '›', 'Next page'));
  const sizes = [...new Set([10, 20, 30, 50, 100, size])].sort((a, b) => a - b);
  const sizeSelect = el('select', { 'aria-label': 'Articles per page' },
    sizes.map((n) => el('option', { value: String(n), selected: n === size }, `${n} per page`)));
  sizeSelect.addEventListener('change', () => {
    ctx.updateSettings({ page_size: Number(sizeSelect.value) }).catch(() => {});
    ctx.setPage(1);
  });
  const unread = ctx.state.list.items.some((i) => !i.read);
  const first = (page - 1) * size + 1;
  pager.replaceChildren(...parts, el('span', { class: 'spacer' }),
    el('button', { type: 'button', class: 'btn small', disabled: !unread, onclick: () => ctx.markPageRead() },
      icon('check-check', 14), 'Mark page read'),
    sizeSelect,
    el('span', {}, `Results: ${fmt(first)}–${fmt(Math.min(total, page * size))} of ${fmt(total)}`));
}

// ---- new-articles banner ----------------------------------------------------------------

export function showBanner() {
  const banner = document.getElementById('banner');
  banner.replaceChildren(icon('newspaper'), el('span', {}, 'New articles have arrived.'),
    el('button', { type: 'button', class: 'btn small primary', onclick: () => ctx.loadItems() }, 'Show them'));
  banner.classList.remove('hidden');
}

export function hideBanner() {
  document.getElementById('banner').classList.add('hidden');
}
