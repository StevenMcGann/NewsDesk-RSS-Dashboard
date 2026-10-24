// Reader pane: sanitized article view, full-article extraction and Save to Markdown.
/* global DOMPurify, Readability, TurndownService */
import { api } from './api.js';
import { el, favicon, fullDate, icon, iconButton, imagesAreBlocked, tagChip, toast } from './util.js';
import { openExternal } from './articles.js';
import * as dialogs from './dialogs.js';

let ctx;
let mode = 'feed'; // 'feed' shows the feed's own text, 'full' the extracted web page
let loadingId = null;
const refs = {};

// Feed and web-page HTML is untrusted: no scripts, frames, forms, styles or ids/classes
// that could collide with the app's own CSS.
const PURIFY = {
  FORBID_TAGS: ['style', 'form', 'input', 'button', 'textarea', 'select', 'option', 'iframe', 'frame', 'frameset',
    'object', 'embed', 'link', 'meta', 'base', 'noscript', 'template', 'dialog', 'svg', 'math'],
  // "background" (legacy table/cell images) would load even with "Hide images" on.
  FORBID_ATTR: ['style', 'class', 'id', 'autoplay', 'name', 'background'],
  ALLOW_DATA_ATTR: false,
};

export function init(c) {
  ctx = c;
  const pane = document.getElementById('reader');
  refs.bar = el('div', { class: 'reader-bar' });
  refs.loading = el('div', { class: 'loading-bar hidden' });
  refs.scroll = el('div', { class: 'reader-scroll' });
  pane.replaceChildren(refs.bar, refs.loading, refs.scroll);
}

export function sanitize(html) {
  const clean = DOMPurify.sanitize(html || '', PURIFY);
  // Post-process in an inert document so blocked images and tracking pixels never load.
  const doc = new DOMParser().parseFromString(`<!doctype html><body>${clean}`, 'text/html');
  for (const a of doc.querySelectorAll('a')) {
    if ((a.getAttribute('href') || '').startsWith('#')) a.removeAttribute('href');
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
  }
  for (const img of doc.querySelectorAll('img')) {
    const tiny = ['0', '1', '2'];
    if (tiny.includes(img.getAttribute('width')) || tiny.includes(img.getAttribute('height'))) {
      img.remove();
    } else if (imagesAreBlocked()) {
      const span = doc.createElement('span');
      span.className = 'img-blocked';
      span.textContent = img.getAttribute('alt') ? `Image: ${img.getAttribute('alt')}` : 'Image hidden';
      img.replaceWith(span);
    } else {
      img.setAttribute('loading', 'lazy');
      img.setAttribute('decoding', 'async');
      img.setAttribute('referrerpolicy', 'no-referrer');
    }
  }
  if (imagesAreBlocked()) for (const src of doc.querySelectorAll('picture source, video[poster]')) src.remove();
  for (const media of doc.querySelectorAll('video, audio')) {
    media.setAttribute('controls', '');
    media.setAttribute('preload', 'none');
  }
  const frag = document.createDocumentFragment();
  for (const node of doc.body.childNodes) frag.append(document.importNode(node, true));
  return frag;
}

export function show(item) {
  mode = item.content_full ? 'full' : 'feed';
  document.getElementById('reader').classList.remove('hidden');
  document.getElementById('split-reader').classList.remove('hidden');
  renderAll(item);
  refs.scroll.scrollTop = 0;
  if (!item.content_full && item.feed_full_text && item.url) loadFull(item);
}

export function hide() {
  document.getElementById('reader').classList.add('hidden');
  document.getElementById('split-reader').classList.add('hidden');
  refs.scroll.replaceChildren();
}

export function update() {
  const item = ctx.state.readerItem;
  if (item) {
    renderBar(item);
    const tags = refs.scroll.querySelector('.ra-meta .tag-row');
    const fresh = tagRow(item);
    if (tags) tags.replaceWith(fresh || el('span'));
    else if (fresh) refs.scroll.querySelector('.ra-meta')?.append(fresh);
  }
}

function renderAll(item, { keepScroll = false } = {}) {
  const top = refs.scroll.scrollTop;
  renderBar(item);
  refs.loading.classList.toggle('hidden', loadingId !== item.id);
  refs.scroll.replaceChildren(body(item));
  if (keepScroll) refs.scroll.scrollTop = top;
}

function renderBar(item) {
  const full = mode === 'full';
  const fullButton = iconButton(full ? 'newspaper' : 'file-text', full ? "Show the feed's version" : 'Load the full article',
    () => toggleFull(item), full ? 'on' : '');
  if (!item.url) fullButton.disabled = true;
  refs.bar.replaceChildren(...[
    iconButton('arrow-left', 'Close (Esc)', () => ctx.closeReader()),
    el('span', { class: 'grow' }),
    iconButton(item.read ? 'circle-check' : 'circle', item.read ? 'Mark as unread (M)' : 'Mark as read (M)',
      () => ctx.patchItem(item.id, { read: !item.read }), item.read ? '' : 'on'),
    iconButton('star', item.starred ? 'Unstar (S)' : 'Star (S)', () => ctx.patchItem(item.id, { starred: !item.starred }),
      `star${item.starred ? ' on' : ''}`),
    iconButton('tag', 'Tags', (e) => dialogs.tagPicker(item, e.currentTarget)),
    el('span', { class: 'sep' }),
    fullButton,
    iconButton(item.saved ? 'file-check' : 'file-down', item.saved ? 'Saved as Markdown (click to save again)' : 'Save as Markdown',
      () => saveMarkdown(item), item.saved ? 'on' : ''),
    item.url ? iconButton('external-link', 'Open in browser', () => openExternal(item.url)) : null,
  ].filter(Boolean));
}

function tagRow(item) {
  const tags = ctx.tagsOf(item.tags);
  return tags.length ? el('span', { class: 'tag-row' }, tags.map(tagChip)) : null;
}

function body(item) {
  const html = mode === 'full' ? item.content_full : (item.content || item.summary || '');
  const content = el('div', { class: 'ra-content' });
  content.append(sanitize(html));
  const text = content.textContent.trim();
  const words = text ? text.split(/\s+/).length : 0;
  const minutes = Math.max(1, Math.round(words / 230));
  const meta = [
    item.author ? el('span', {}, `By ${item.author}`) : null,
    el('span', { title: fullDate(item.published) }, fullDate(item.published)),
    words > 120 ? el('span', {}, `· ${minutes} min read`) : null,
    tagRow(item),
  ];
  const hero = mode === 'feed' && item.image_url && !imagesAreBlocked() && !content.querySelector('img')
    ? el('figure', { class: 'ra-hero' }, el('img', { src: item.image_url, alt: '', referrerpolicy: 'no-referrer' }))
    : null;
  const preview = mode === 'feed' && item.url && text.length < 700;
  return el('article', { class: 'reader-article' },
    el('div', { class: 'ra-source' }, favicon(item.feed_icon, item.feed_title), el('span', {}, item.feed_title)),
    el('h1', { class: 'ra-title' }, item.url
      ? el('a', { href: item.url, target: '_blank', rel: 'noopener noreferrer', title: 'Open in browser' }, item.title)
      : item.title),
    el('div', { class: 'ra-meta' }, meta),
    mode === 'full' ? el('div', { class: 'ra-mode' }, icon('file-text', 13), 'Full article, loaded from the website') : null,
    hero,
    content,
    preview ? el('div', { class: 'ra-note' },
      el('span', {}, text ? 'This feed only includes a preview of the article.' : 'This feed has no article text.'),
      el('button', { type: 'button', class: 'btn small primary', onclick: () => toggleFull(item) }, icon('file-text', 14), 'Load full article'),
      el('button', { type: 'button', class: 'btn small', onclick: () => openExternal(item.url) }, icon('external-link', 14), 'Open website'))
      : null);
}

// ---- full article -------------------------------------------------------------------

async function toggleFull(item) {
  if (mode === 'full') {
    mode = 'feed';
    renderAll(item);
  } else if (item.content_full) {
    mode = 'full';
    renderAll(item);
  } else {
    await loadFull(item);
  }
}

// The page's relative links must resolve against the article's address. A <base> tag
// can't do it: the app's CSP (base-uri 'none') also applies to parsed documents.
function absolutize(doc, base) {
  const fix = (value) => {
    try {
      return new URL(value, base).href;
    } catch {
      return value;
    }
  };
  for (const attr of ['href', 'src', 'poster', 'data-src']) {
    for (const node of doc.querySelectorAll(`[${attr}]`)) node.setAttribute(attr, fix(node.getAttribute(attr).trim()));
  }
  for (const node of doc.querySelectorAll('[srcset]')) {
    const parts = node.getAttribute('srcset').split(',').map((part) => {
      const [src, ...size] = part.trim().split(/\s+/);
      return [fix(src), ...size].join(' ');
    });
    node.setAttribute('srcset', parts.join(', '));
  }
}

function extract(pageHtml, url) {
  const doc = new DOMParser().parseFromString(pageHtml, 'text/html');
  absolutize(doc, url);
  const article = new Readability(doc, { charThreshold: 250 }).parse();
  if (!article || !article.content || (article.textContent || '').trim().length < 200) return null;
  return DOMPurify.sanitize(article.content, PURIFY);
}

async function loadFull(item) {
  if (loadingId === item.id) return;
  loadingId = item.id;
  renderAll(item, { keepScroll: true });
  try {
    const res = await api.get(`/api/article/${item.id}`);
    let html = res.content_full;
    if (!html) {
      html = extract(res.html, res.url || item.url);
      if (!html) throw new Error("couldn't find the article text on that page");
      api.put(`/api/article/${item.id}`, { html }).catch(() => {});
    }
    item.content_full = html;
    if (ctx.state.readerItem?.id === item.id) mode = 'full';
  } catch (e) {
    toast(`Couldn't load the full article: ${e.message}`, {
      error: true,
      action: { label: 'Open website', fn: () => openExternal(item.url) },
    });
  } finally {
    loadingId = null;
    if (ctx.state.readerItem?.id === item.id) renderAll(ctx.state.readerItem, { keepScroll: true });
  }
}

// ---- Markdown -----------------------------------------------------------------------

function toMarkdown(html) {
  const service = new TurndownService({ headingStyle: 'atx', codeBlockStyle: 'fenced', bulletListMarker: '-', emDelimiter: '*' });
  service.keep(['table']);
  service.remove(['script', 'style', 'noscript']);
  return service.turndown(DOMPurify.sanitize(html || '', PURIFY));
}

async function saveMarkdown(item) {
  const html = mode === 'full' && item.content_full ? item.content_full : (item.content || item.summary || '');
  try {
    const res = await api.post('/api/save-markdown', { item_id: item.id, markdown: toMarkdown(html) });
    ctx.localPatch(item.id, { saved: true });
    toast(`Saved to ${res.path}`, {
      action: { label: 'Show in folder', fn: () => api.post('/api/open-folder', { item_id: item.id }).catch(() => {}) },
    });
  } catch (e) {
    toast(e.message, { error: true });
  }
}
