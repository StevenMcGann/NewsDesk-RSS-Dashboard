// Small DOM helpers shared by the UI modules.
import { ICONS } from './icons.js';

export function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (key === 'value' || key === 'checked' || key === 'selected') node[key] = value;
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

export function icon(name, size = 16) {
  const span = document.createElement('span');
  span.className = 'ic';
  // ICONS is vendored path data, never feed content.
  span.innerHTML = `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor"
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;
  return span;
}

export function iconButton(name, title, onClick, cls = '') {
  return el('button', { type: 'button', class: `icon-btn ${cls}`, title, 'aria-label': title, onclick: onClick }, icon(name));
}

export function setVar(node, name, value) {
  node.style.setProperty(name, value);
  return node;
}

// ---- images ----------------------------------------------------------------

let imagesBlocked = false;
const brokenIcons = new Set();
const PALETTE = ['#7f6df2', '#e5484d', '#30a46c', '#3e63dd', '#f76b15', '#12a594', '#d6409f', '#8e4ec6', '#c58a1d'];

export function setImagesBlocked(blocked) { imagesBlocked = blocked; }
export function imagesAreBlocked() { return imagesBlocked; }

export function letterColor(text) {
  let h = 0;
  for (const ch of text || '') h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

export function favicon(url, name) {
  const letter = () => setVar(el('span', { class: 'favicon letter', 'aria-hidden': 'true' },
    (name || '?').trim().charAt(0).toUpperCase() || '?'), '--c', letterColor(name));
  if (!url || imagesBlocked || brokenIcons.has(url)) return letter();
  const img = el('img', { class: 'favicon', src: url, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer' });
  img.addEventListener('error', () => { brokenIcons.add(url); img.replaceWith(letter()); }, { once: true });
  img.addEventListener('load', () => { if (img.naturalWidth < 8) { brokenIcons.add(url); img.replaceWith(letter()); } }, { once: true });
  return img;
}

export function contrastText(hex) {
  const n = parseInt(String(hex).slice(1), 16) || 0;
  const r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  return 0.299 * r + 0.587 * g + 0.114 * b > 165 ? '#1b1b1f' : '#ffffff';
}

export function tagChip(tag) {
  const chip = el('span', { class: 'chip' }, tag.name);
  setVar(chip, '--c', tag.color);
  return setVar(chip, '--chip-text', contrastText(tag.color));
}

// ---- time and numbers --------------------------------------------------------

export function relTime(ts) {
  const date = new Date(ts * 1000);
  const now = new Date();
  const diff = (now - date) / 1000;
  if (diff < 60) return 'Just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (date.getTime() >= today) return `${Math.floor(diff / 3600)}h ago`;
  if (date.getTime() >= today - 86400000) return 'Yesterday';
  if (diff < 6 * 86400) return date.toLocaleDateString(undefined, { weekday: 'short' });
  const sameYear = date.getFullYear() === now.getFullYear();
  return date.toLocaleDateString(undefined, sameYear ? { month: 'short', day: 'numeric' }
    : { year: 'numeric', month: 'short', day: 'numeric' });
}

export function fullDate(ts) {
  return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

export function ago(ts) {
  const minutes = Math.round((Date.now() / 1000 - ts) / 60);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours} hr ago` : fullDate(ts);
}

export const fmt = (n) => Number(n || 0).toLocaleString();

export function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

export function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(`newsdesk.${key}`));
    localStorage.setItem(`newsdesk.${key}`, JSON.stringify(value));
  } catch { /* storage can be unavailable; it only holds conveniences */ }
  return undefined;
}

// ---- popovers and menus ---------------------------------------------------------

let open = null;

export function closePopover() {
  if (open) {
    const { cleanup } = open;
    open = null;
    cleanup();
  }
}

export function popoverIsOpen() { return open !== null; }

export function popover(content, anchor, { align = 'start', className = '', onClose } = {}) {
  closePopover();
  const pop = el('div', { class: `popover ${className}` }, content);
  document.body.append(pop);
  place(pop, anchor, align);
  const onDown = (e) => {
    if (!pop.contains(e.target) && !(anchor instanceof Element && anchor.contains(e.target))) closePopover();
  };
  const onKey = (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); closePopover(); }
  };
  document.addEventListener('mousedown', onDown, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', closePopover);
  open = {
    pop,
    cleanup() {
      pop.remove();
      document.removeEventListener('mousedown', onDown, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', closePopover);
      if (onClose) onClose();
    },
  };
  return pop;
}

function place(pop, anchor, align) {
  const r = anchor instanceof Element ? anchor.getBoundingClientRect()
    : { left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y };
  const width = pop.offsetWidth;
  const height = pop.offsetHeight;
  let left = align === 'end' ? r.right - width : r.left;
  left = Math.max(8, Math.min(left, innerWidth - width - 8));
  let top = r.bottom + 4;
  if (top + height > innerHeight - 8) top = Math.max(8, r.top - height - 4);
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}

export function menu(items, anchor, opts) {
  const buttons = [];
  const list = el('div', { role: 'menu' }, items.filter(Boolean).map((item) => {
    if (item === '-') return el('div', { class: 'menu-sep' });
    if (item.header) return el('div', { class: 'menu-label' }, item.header);
    const button = el('button', {
      type: 'button',
      role: 'menuitem',
      class: `menu-item${item.danger ? ' danger' : ''}`,
      onclick: () => { closePopover(); item.action(); },
    }, item.icon ? icon(item.icon) : null, el('span', {}, item.label),
    item.checked ? el('span', { class: 'check' }, icon('check', 14)) : null);
    buttons.push(button);
    return button;
  }));
  list.addEventListener('keydown', (e) => {
    const i = buttons.indexOf(document.activeElement);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const next = (i + (e.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus();
    }
  });
  const pop = popover(list, anchor, opts);
  buttons[0]?.focus();
  return pop;
}

// ---- toasts --------------------------------------------------------------------

export function toast(message, { error = false, action = null, timeout = 5000 } = {}) {
  const box = document.getElementById('toasts');
  const t = el('div', { class: `toast${error ? ' error' : ''}`, role: error ? 'alert' : 'status' },
    el('span', { class: 'msg' }, message));
  if (action) {
    t.append(el('button', { type: 'button', class: 'btn link', onclick: () => { t.remove(); action.fn(); } }, action.label));
  }
  t.append(iconButton('x', 'Dismiss', () => t.remove(), 'small'));
  box.append(t);
  while (box.children.length > 3) box.firstChild.remove();
  if (timeout) setTimeout(() => t.remove(), error ? timeout * 2 : timeout);
}
