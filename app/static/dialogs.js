// Dialogs and popovers: adding and editing feeds/folders, import/export, settings, tags, help.
import { api } from './api.js';
import { ago, closePopover, el, icon, iconButton, popover, setImagesBlocked, setVar, toast } from './util.js';
import { openExternal } from './articles.js';
import { renderUpdate } from './sidebar.js';

let ctx;
const TAG_COLORS = ['#e5484d', '#3e63dd', '#e2a336', '#30a46c', '#8e4ec6', '#f76b15', '#12a594', '#d6409f', '#8b8d98'];

export function init(c) {
  ctx = c;
}

// ---- building blocks -----------------------------------------------------------------

function dialog({ title, body, foot = [], wide = false, onClose }) {
  const dlg = el('dialog', { class: `dlg${wide ? ' wide' : ''}`, 'aria-label': title });
  dlg.append(
    el('div', { class: 'dlg-head' }, el('h2', {}, title), iconButton('x', 'Close', () => dlg.close())),
    el('div', { class: 'dlg-body' }, body),
  );
  if (foot.length) dlg.append(el('div', { class: 'dlg-foot' }, foot));
  dlg.addEventListener('close', () => { dlg.remove(); if (onClose) onClose(); });
  closePopover();
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

function button(label, onClick, cls = '') {
  return el('button', { type: 'button', class: `btn ${cls}`, onclick: onClick }, label);
}

function field(label, control, hint) {
  const id = `f${Math.random().toString(36).slice(2, 8)}`;
  control.id = id;
  return el('div', { class: 'field' }, el('label', { for: id }, label), control, hint ? el('div', { class: 'hint' }, hint) : null);
}

function onEnter(input, fn) {
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); fn(); } });
}

async function busy(btn, label, work) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = label;
  try {
    return await work();
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

export function confirm({ title, message, confirmLabel = 'OK', danger = false }) {
  return new Promise((resolve) => {
    let ok = false;
    const yes = button(confirmLabel, () => { ok = true; dlg.close(); }, danger ? 'danger' : 'primary');
    const dlg = dialog({ title, body: el('p', {}, message), foot: [button('Cancel', () => dlg.close()), yes], onClose: () => resolve(ok) });
    yes.focus();
  });
}

// Ask for one value; `save` runs before closing so server errors show inside the dialog.
function prompt({ title, label, value = '', placeholder = '', hint, confirmLabel = 'Save', save }) {
  return new Promise((resolve) => {
    let result = null;
    const input = el('input', { type: 'text', value, placeholder });
    const error = el('div', { class: 'error-text' });
    const ok = button(confirmLabel, () => submit(), 'primary');
    const submit = async () => {
      const v = input.value.trim();
      if (!v) { error.textContent = 'Please enter a value.'; return; }
      try {
        await busy(ok, 'Saving…', () => save(v));
        result = v;
        dlg.close();
      } catch (e) {
        error.textContent = e.message;
      }
    };
    onEnter(input, submit);
    const dlg = dialog({ title, body: [field(label, input, hint), error], foot: [button('Cancel', () => dlg.close()), ok], onClose: () => resolve(result) });
    input.focus();
    input.select();
  });
}

function folderOptions(selected, excludeId = null) {
  const out = [el('option', { value: '', selected: selected == null }, '(No folder)')];
  const walk = (parent, depth) => {
    for (const f of ctx.state.data.folders.filter((x) => x.parent_id === parent)) {
      if (f.id === excludeId) continue;
      out.push(el('option', { value: String(f.id), selected: f.id === selected }, `${'   '.repeat(depth)}${f.name}`));
      walk(f.id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

async function afterTreeChange() {
  await ctx.loadState();
  await ctx.loadItems({ keepScroll: true });
}

// ---- feeds and folders -----------------------------------------------------------------

export function addFeed(folderId = null) {
  const url = el('input', { type: 'url', placeholder: 'https://example.com', spellcheck: 'false' });
  const folder = el('select', {}, folderOptions(folderId));
  const name = el('input', { type: 'text', placeholder: "Use the feed's own name" });
  const error = el('div', { class: 'error-text' });
  const add = button('Add feed', () => submit(), 'primary');
  const submit = async () => {
    error.textContent = '';
    if (!url.value.trim()) { error.textContent = 'Paste the address of a website or a feed.'; return; }
    try {
      const feed = await busy(add, 'Looking for the feed…', () => api.post('/api/feeds', {
        url: url.value.trim(),
        folder_id: folder.value ? Number(folder.value) : null,
        title: name.value.trim() || null,
      }));
      dlg.close();
      ctx.updateSettings({ first_run_done: true }, { quiet: true }).catch(() => {});
      await ctx.loadState();
      ctx.setView(`feed:${feed.id}`);
      toast(`Added ${feed.title} with ${feed.articles} article${feed.articles === 1 ? '' : 's'}`);
    } catch (e) {
      error.textContent = e.message;
    }
  };
  onEnter(url, submit);
  onEnter(name, submit);
  const dlg = dialog({
    title: 'Add a feed',
    body: [
      field('Website or feed address', url, 'Paste a website address and the app finds its feed, or paste the feed link itself.'),
      field('Folder', folder),
      field('Name (optional)', name),
      error,
    ],
    foot: [button('Cancel', () => dlg.close()), add],
  });
  url.focus();
}

export async function addFolder(parentId = null) {
  const name = await prompt({
    title: parentId ? 'New subfolder' : 'New folder',
    label: 'Folder name',
    confirmLabel: 'Create',
    save: (v) => api.post('/api/folders', { name: v, parent_id: parentId }),
  });
  if (name) await ctx.loadState();
}

export async function renameFeed(feed) {
  const name = await prompt({
    title: 'Rename feed',
    label: 'Name',
    value: feed.title,
    save: (v) => api.patch(`/api/feeds/${feed.id}`, { title: v }),
  });
  if (name) await afterTreeChange();
}

export async function changeFeedUrl(feed) {
  const url = await prompt({
    title: 'Change feed address',
    label: 'Feed address',
    value: feed.url,
    hint: 'Use this when a site moves its feed. Your articles stay.',
    save: (v) => api.patch(`/api/feeds/${feed.id}`, { url: v }),
  });
  if (url) {
    await ctx.loadState();
    ctx.refresh([feed.id]);
  }
}

export function moveFeed(feed) {
  const folder = el('select', {}, folderOptions(feed.folder_id));
  const error = el('div', { class: 'error-text' });
  const ok = button('Move', async () => {
    try {
      await busy(ok, 'Moving…', () => api.patch(`/api/feeds/${feed.id}`, { folder_id: folder.value ? Number(folder.value) : null }));
      dlg.close();
      await afterTreeChange();
    } catch (e) {
      error.textContent = e.message;
    }
  }, 'primary');
  const dlg = dialog({ title: `Move "${feed.title}"`, body: [field('Folder', folder), error], foot: [button('Cancel', () => dlg.close()), ok] });
  folder.focus();
}

export async function deleteFeed(feed) {
  const ok = await confirm({
    title: 'Unsubscribe?',
    message: `Stop following "${feed.title}"? Its articles are removed from the app, including starred ones. Markdown files you saved stay on disk.`,
    confirmLabel: 'Unsubscribe',
    danger: true,
  });
  if (!ok) return;
  try {
    await api.del(`/api/feeds/${feed.id}`);
    if (ctx.state.view === `feed:${feed.id}`) ctx.state.view = 'all';
    await afterTreeChange();
    toast(`Unsubscribed from ${feed.title}`);
  } catch (e) {
    toast(e.message, { error: true });
  }
}

export async function renameFolder(folder) {
  const name = await prompt({
    title: 'Rename folder',
    label: 'Folder name',
    value: folder.name,
    save: (v) => api.patch(`/api/folders/${folder.id}`, { name: v }),
  });
  if (name) await ctx.loadState();
}

export async function deleteFolder(folder) {
  const ok = await confirm({
    title: 'Delete folder?',
    message: `Delete "${folder.name}"? The feeds and folders inside it move up one level; nothing is unsubscribed.`,
    confirmLabel: 'Delete folder',
    danger: true,
  });
  if (!ok) return;
  try {
    await api.del(`/api/folders/${folder.id}`);
    if (ctx.state.view === `folder:${folder.id}`) ctx.state.view = 'all';
    await afterTreeChange();
  } catch (e) {
    toast(e.message, { error: true });
  }
}

export function feedErrors(feeds) {
  const dlg = dialog({
    title: "Feeds that couldn't be updated",
    wide: true,
    body: el('div', { class: 'error-list' }, feeds.map((f) => el('div', { class: 'item' },
      el('strong', {}, f.title),
      el('div', {}, f.last_error),
      el('div', {}, f.url),
      el('div', { class: 'inline' },
        button('Change address…', () => { dlg.close(); changeFeedUrl(f); }, 'small'),
        button('Unsubscribe…', () => { dlg.close(); deleteFeed(f); }, 'small ghost'))))),
    foot: [
      el('span', { class: 'grow hint' }, 'If every feed fails, try Settings → Test connection.'),
      button('Close', () => dlg.close()),
      button('Try again', () => { dlg.close(); ctx.refresh(feeds.map((f) => f.id)); }, 'primary'),
    ],
  });
}

// ---- import / export ---------------------------------------------------------------------

function reportImport(result) {
  const parts = [`Added ${result.feeds} feed${result.feeds === 1 ? '' : 's'}`];
  if (result.folders) parts.push(`in ${result.folders} new folder${result.folders === 1 ? '' : 's'}`);
  let msg = parts.join(' ');
  if (result.skipped) msg += ` (${result.skipped} you already follow)`;
  toast(result.feeds ? `${msg}. Downloading their articles now…` : msg);
  ctx.state.settings.first_run_done = true;
  ctx.loadState().then(() => ctx.watchRefresh());
}

export async function importStarter() {
  try {
    reportImport(await api.post('/api/opml/import', { starter: true }));
  } catch (e) {
    toast(e.message, { error: true });
  }
}

async function exportOpml() {
  try {
    const res = await api.get('/api/opml/export', { raw: true });
    const name = (res.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/)?.[1] || 'feeds.opml';
    const href = URL.createObjectURL(await res.blob());
    const a = el('a', { href, download: name, class: 'hidden' });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 10000);
    toast(`Saved ${name} to your Downloads folder`);
  } catch (e) {
    toast(e.message, { error: true });
  }
}

export function importExport() {
  const error = el('div', { class: 'error-text' });
  const file = el('input', { type: 'file', accept: '.opml,.xml,text/xml,text/x-opml', class: 'hidden' });
  const choose = button('Choose file…', () => file.click(), 'primary');
  file.addEventListener('change', async () => {
    const picked = file.files[0];
    if (!picked) return;
    error.textContent = '';
    try {
      const result = await busy(choose, 'Importing…', async () => api.post('/api/opml/import', await picked.arrayBuffer(), { contentType: 'text/x-opml' }));
      dlg.close();
      reportImport(result);
    } catch (e) {
      error.textContent = e.message;
    }
    file.value = '';
  });
  const choice = (title, desc, action) => el('div', { class: 'import-choice' },
    el('div', {}, el('strong', {}, title), el('div', { class: 'desc' }, desc)), action);
  const dlg = dialog({
    title: 'Import or export feeds',
    body: [
      choice('Import from a file', 'Bring in feeds from another reader (Feedly, Inoreader, Outlook, Obsidian) using its OPML export.', choose),
      ctx.state.data.app.starter_available
        ? choice('Starter feeds', 'Add the feed list that comes with this app.', button('Add starter feeds', () => { dlg.close(); importStarter(); }))
        : null,
      choice('Export my feeds', 'Download an OPML file with all your feeds and folders, to back up or share.', button('Download OPML', exportOpml)),
      file,
      error,
    ],
  });
}

export function welcome() {
  const done = () => ctx.updateSettings({ first_run_done: true }, { quiet: true }).catch(() => {});
  const go = (fn) => () => { dlg.close(); fn(); };
  const dlg = dialog({
    title: 'Welcome to NewsDesk RSS Dashboard',
    body: [
      el('img', { class: 'welcome-logo', src: 'favicon.svg', width: 56, height: 56, alt: '' }),
      el('p', {}, 'This app collects new articles from websites you follow and shows them in one place. It runs only on this computer.'),
      el('p', {}, 'How would you like to start?'),
    ],
    foot: [
      button('Import a file', go(importExport)),
      button('Add a feed', go(() => addFeed())),
      ctx.state.data.app.starter_available ? button('Add the starter feeds', go(importStarter), 'primary') : null,
    ].filter(Boolean),
    onClose: done,
  });
}

// ---- tags ------------------------------------------------------------------------------

export function tagPicker(item, anchor) {
  let pop;
  const render = () => {
    const chosen = new Set(item.tags);
    const input = el('input', { type: 'text', placeholder: 'New tag', 'aria-label': 'New tag name' });
    const create = async () => {
      const name = input.value.trim();
      if (!name) return;
      try {
        const { id } = await api.post('/api/tags', { name });
        await ctx.loadState();
        await ctx.patchItem(item.id, { tags: [...item.tags, id] });
        pop.replaceChildren(render());
        pop.querySelector('input')?.focus();
      } catch (e) {
        toast(e.message, { error: true });
      }
    };
    onEnter(input, create);
    return el('div', {},
      el('div', { class: 'menu-label' }, 'Tags'),
      ctx.state.data.tags.map((tag) => el('button', {
        type: 'button',
        class: 'menu-item',
        'aria-pressed': String(chosen.has(tag.id)),
        onclick: async () => {
          const next = chosen.has(tag.id) ? item.tags.filter((t) => t !== tag.id) : [...item.tags, tag.id];
          await ctx.patchItem(item.id, { tags: next });
          pop.replaceChildren(render());
        },
      }, setVar(el('span', { class: 'dot' }), '--c', tag.color), el('span', {}, tag.name),
      chosen.has(tag.id) ? el('span', { class: 'check' }, icon('check', 14)) : null)),
      el('div', { class: 'menu-sep' }),
      el('div', { class: 'inline' }, input, button('Add', create, 'small')));
  };
  pop = popover(render(), anchor, { align: 'end', className: 'tag-pop' });
}

function tagEditor(onChange) {
  const box = el('div', {});
  const render = () => {
    const rows = ctx.state.data.tags.map((tag) => {
      const name = el('input', { type: 'text', value: tag.name, 'aria-label': 'Tag name' });
      name.addEventListener('change', async () => {
        try {
          await api.patch(`/api/tags/${tag.id}`, { name: name.value.trim() });
          await ctx.loadState();
          onChange();
        } catch (e) {
          name.value = tag.name;
          toast(e.message, { error: true });
        }
      });
      const dot = setVar(el('button', { type: 'button', class: 'color-dot', title: 'Change color' }), '--c', tag.color);
      dot.addEventListener('click', async () => {
        const color = TAG_COLORS[(TAG_COLORS.indexOf(tag.color) + 1) % TAG_COLORS.length];
        await api.patch(`/api/tags/${tag.id}`, { color });
        await ctx.loadState();
        onChange();
        render();
      });
      const remove = iconButton('trash-2', `Delete tag ${tag.name}`, async () => {
        if (!await confirm({ title: 'Delete tag?', message: `Remove the tag "${tag.name}" from every article?`, confirmLabel: 'Delete', danger: true })) return;
        await api.del(`/api/tags/${tag.id}`);
        await ctx.loadState();
        onChange();
        render();
      }, 'small');
      return el('div', { class: 'tag-edit' }, dot, name, remove);
    });
    const fresh = el('input', { type: 'text', placeholder: 'New tag name' });
    const add = async () => {
      if (!fresh.value.trim()) return;
      try {
        await api.post('/api/tags', { name: fresh.value.trim() });
        await ctx.loadState();
        onChange();
        render();
      } catch (e) {
        toast(e.message, { error: true });
      }
    };
    onEnter(fresh, add);
    box.replaceChildren(...rows, el('div', { class: 'tag-edit' }, fresh, button('Add tag', add, 'small')));
  };
  render();
  return box;
}

// ---- settings ----------------------------------------------------------------------------

export function settings() {
  const s = ctx.state.settings;
  let listDirty = false;
  const save = async (patch) => {
    try {
      await ctx.updateSettings(patch);
    } catch {
      return false;
    }
    return true;
  };
  const row = (title, desc, control) => el('div', { class: 'setting-row' },
    el('div', { class: 'what' }, el('div', {}, title), desc ? el('div', { class: 'desc' }, desc) : null), control);
  const stack = (title, desc, ...controls) => el('div', { class: 'setting-row stack' },
    el('div', { class: 'what' }, el('div', {}, title), desc ? el('div', { class: 'desc' }, desc) : null), ...controls);
  const toggle = (key, after) => {
    const input = el('input', { type: 'checkbox', checked: Boolean(s[key]) });
    input.addEventListener('change', async () => { if (await save({ [key]: input.checked }) && after) after(input.checked); });
    return el('span', { class: 'switch' }, input, el('span'));
  };
  const select = (key, options, after) => {
    const sel = el('select', {}, options.map(([v, label]) => el('option', { value: String(v), selected: String(s[key]) === String(v) }, label)));
    sel.addEventListener('change', async () => {
      const v = typeof s[key] === 'number' ? Number(sel.value) : sel.value;
      if (await save({ [key]: v }) && after) after(v);
    });
    return sel;
  };

  const folder = el('input', { type: 'text', value: s.save_folder, placeholder: ctx.state.data.app.default_save_folder, spellcheck: 'false' });
  folder.addEventListener('change', () => save({ save_folder: folder.value.trim() }));
  const template = el('textarea', { spellcheck: 'false', 'aria-label': 'Markdown template' });
  template.value = s.md_template;
  template.addEventListener('change', () => save({ md_template: template.value }));

  const report = el('div', { class: 'diag hidden' });
  const test = button('Test connection', async () => {
    try {
      const r = await busy(test, 'Testing…', () => api.post('/api/diagnose'));
      const names = { python: 'Python', windows: 'Windows (.NET)', winhttp: 'Windows (WinHTTP)' };
      report.replaceChildren(
        ...r.results.map((x) => el('div', {}, el('span', { class: x.ok ? 'okay' : 'bad' }, x.ok ? '✓ ' : '✗ '),
          `${names[x.method]}: ${x.ok ? `worked in ${x.ms} ms` : x.error}`)),
        el('div', { class: 'hint' }, r.recommended
          ? `Recommended setting: ${{ auto: 'Automatic', windows: 'Windows (.NET)', python: 'Python only', winhttp: 'Windows (WinHTTP)' }[r.recommended]}.`
          : 'No download method reached the internet. Check that you are online and that Edge can open the test feed.'));
      report.classList.remove('hidden');
    } catch (e) {
      toast(e.message, { error: true });
    }
  }, 'small');

  const shortcutsBtn = button('Show', () => shortcuts(), 'small');
  const updateStatus = el('div', { class: 'desc' }, updateText(ctx.state.update));
  const getIt = button('Update', () => { dlg.close(); ctx.openUpdate(); }, 'small primary');
  getIt.classList.toggle('hidden', !ctx.state.update?.available);
  const checkNow = button('Check now', async () => {
    try {
      const status = await busy(checkNow, 'Checking…', () => api.post('/api/update/check'));
      ctx.state.update = status;
      updateStatus.textContent = updateText(status);
      getIt.classList.toggle('hidden', !status.available);
      renderUpdate();
    } catch (e) {
      toast(e.message, { error: true });
    }
  }, 'small');
  const app = ctx.state.data.app;
  const quit = button('Quit NewsDesk', async () => {
    dlg.close();
    if (!await confirm({ title: 'Quit NewsDesk?',
      message: 'Stop NewsDesk and background checks for all open windows? Your feeds and reading history are kept.',
      confirmLabel: 'Quit NewsDesk' })) return;
    try { await ctx.quit(); } catch (e) { toast(e.message, { error: true }); }
  }, 'small');
  const dlg = dialog({
    title: 'Settings',
    wide: true,
    onClose: () => { if (listDirty) ctx.loadItems({ keepScroll: true }); },
    body: [
      el('section', { class: 'settings-section' }, el('h3', {}, 'Reading'),
        row('Mark articles as read when opened', null, toggle('mark_read_on_open')),
        row('Hide images from websites', 'Text only: faster, and sites cannot tell when you read.', toggle('block_remote_images', (v) => {
          setImagesBlocked(v);
          listDirty = true;
        }))),
      el('section', { class: 'settings-section' }, el('h3', {}, 'Appearance'),
        row('Theme', null, select('theme', [['dark', 'Dark'], ['light', 'Light'], ['system', 'Match Windows']], () => ctx.applyLayout()))),
      el('section', { class: 'settings-section' }, el('h3', {}, 'Updates'),
        row('Check for new articles', 'Only while the app is open.', select('refresh_minutes',
          [[15, 'Every 15 minutes'], [30, 'Every 30 minutes'], [60, 'Every hour'], [120, 'Every 2 hours'], [240, 'Every 4 hours'], [720, 'Every 12 hours']])),
        row('Keep read articles for', 'Starred, saved and tagged articles are always kept.', select('retention_days',
          [[7, '1 week'], [30, '1 month'], [60, '2 months'], [90, '3 months'], [365, '1 year'], [3650, 'Forever']]))),
      el('section', { class: 'settings-section' }, el('h3', {}, 'Save as Markdown'),
        stack('Save folder', `Leave empty to use ${app.default_save_folder}.`,
          el('div', { class: 'inline' }, folder, button('Open', () => api.post('/api/open-folder', { which: 'saved' }).catch((e) => toast(e.message, { error: true })), 'small'))),
        stack('Note template', 'Placeholders: {{title}} {{url}} {{source}} {{author}} {{published}} {{saved}} {{tags}} {{content}}. The file also gets YAML frontmatter.',
          template,
          el('div', {}, button('Reset to default', () => { template.value = app.default_template; save({ md_template: template.value }); }, 'small')))),
      el('section', { class: 'settings-section' }, el('h3', {}, 'Connection'),
        row('How to download feeds', 'Automatic tries the available download methods and remembers which works for each site.',
          select('fetch_mode', [['auto', 'Automatic'], ['python', 'Python only'], ['windows', 'Windows (.NET)'], ['winhttp', 'Windows (WinHTTP)']])),
        stack('Check the connection', 'Downloads a test feed with each method and reports which works.', el('div', {}, test), report)),
      el('section', { class: 'settings-section' }, el('h3', {}, 'Tags'), tagEditor(() => { listDirty = true; })),
      el('section', { class: 'settings-section' }, el('h3', {}, 'Help'),
        row('Quit NewsDesk', 'Stop the app and background checks for all windows.', quit),
        row('Keyboard shortcuts', null, shortcutsBtn),
        row('Your data', app.data_dir, button('Open folder', () => api.post('/api/open-folder', { which: 'data' }).catch((e) => toast(e.message, { error: true })), 'small')),
        row('Check for new versions', 'Daily checks only notify you. Downloads and installation start when you choose Update.',
          toggle('check_updates', () => renderUpdate())),
        el('div', { class: 'setting-row' },
          el('div', { class: 'what' }, el('div', {}, `Version ${app.version}`), updateStatus),
          el('div', { class: 'inline' }, getIt, checkNow)),
        row('Credits', 'Layout and workflow modeled on RSS Dashboard for Obsidian by amatya-aditya.',
          button('View project', () => window.open('https://github.com/amatya-aditya/obsidian-rss-dashboard', '_blank', 'noopener,noreferrer'), 'small'))),
    ],
  });
}

function updateText(u) {
  if (!u) return 'Update checks are not available.';
  if (u.error) return `Couldn't check for updates: ${u.error}`;
  if (u.available) return `Version ${u.latest.version} is available.`;
  if (!u.checked_at) return 'Not checked yet.';
  if (!u.latest) return 'No releases have been published yet.';
  return `You have the latest version. Checked ${ago(u.checked_at)}.`;
}

export function shortcuts() {
  const keys = [
    ['j / k', 'Next / previous article'],
    ['o or Enter', 'Open the selected article'],
    ['m', 'Mark read or unread'],
    ['s', 'Star or unstar'],
    ['r', 'Check all feeds for new articles'],
    ['/', 'Search'],
    ['Esc', 'Close the reader'],
    ['?', 'Show this list'],
  ];
  const dlg = dialog({
    title: 'Keyboard shortcuts',
    body: el('div', { class: 'keys' }, keys.map(([k, what]) => [el('span', {}, k.split(' ').map((p) => (['/', 'or'].includes(p) && k !== '/' ? ` ${p} ` : el('kbd', {}, p)))), el('span', {}, what)])),
    foot: [button('Close', () => dlg.close(), 'primary')],
  });
}
