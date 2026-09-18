// Shared helpers for every view: API calls, formatting, icons, markdown, dialogs, snackbar.

export async function api(path, opt) {
  const r = await fetch('/api/v1' + path, opt);
  if (r.ok) return r.status === 204 ? null : r.json();
  let d; try { d = await r.json(); } catch { d = { detail: r.statusText }; }
  throw new Error(typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail));
}
const json = (method) => (path, body) => api(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
export const post = json('POST');
export const put = json('PUT');
export const del = (path) => api(path, { method: 'DELETE' });
export async function upload(path, formData) {
  const r = await fetch('/api/v1' + path, { method: 'POST', body: formData });
  let d; try { d = await r.json(); } catch { d = { detail: r.statusText }; }
  if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail));
  return d;
}

export const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
export const attr = (s) => esc(s).replace(/'/g, '&#39;');
export const num = (n) => (n ?? 0).toLocaleString();
export const dur = (s) => s == null ? '–' : (s >= 3600 ? Math.floor(s / 3600) + ':' : '') + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + ':' + String(Math.floor(s % 60)).padStart(2, '0');
export const ts = (s) => { s = Math.max(0, +s || 0); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = (s % 60).toFixed(1).padStart(4, '0'); return (h ? h + ':' : '') + String(m).padStart(2, '0') + ':' + x; };
export const bytes = (n) => n == null ? '–' : n > 1e9 ? (n / 1e9).toFixed(2) + ' GB' : n > 1e6 ? (n / 1e6).toFixed(1) + ' MB' : (n / 1e3).toFixed(0) + ' KB';
export const ago = (iso) => { if (!iso) return '–'; const d = (Date.now() - new Date(iso)) / 1000; if (d < 60) return Math.max(0, Math.round(d)) + 's ago'; if (d < 3600) return Math.round(d / 60) + 'm ago'; if (d < 86400) return Math.round(d / 3600) + 'h ago'; return Math.round(d / 86400) + 'd ago'; };
export const tm = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '–';
export const dt = (iso) => iso ? new Date(iso).toLocaleString([], { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '–';
export const words = (s) => (s || '').split(/\s+/).filter(Boolean).length;
export const r1 = (x) => Math.round(x * 10) / 10;
export const inr = (usd) => '₹' + (usd * 88).toFixed(usd * 88 < 10 ? 2 : 0);
export const costOf = (usage) => { const u = usage || {}; return (u.input_tokens || 0) * 0.30 / 1e6 + ((u.output_tokens || 0) + (u.thought_tokens || 0)) * 2.5 / 1e6; };

// ---- icons (stroke paths, 24-box) -------------------------------------------------------------
const ICONS = {
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  video: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M10 9l5 3-5 3z" fill="currentColor" stroke="none"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  spark: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 16l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/>',
  article: '<path d="M6 3h9l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M14 3v6h6M8 13h8M8 17h6"/>',
  review: '<path d="M9 12l2 2 4-4"/><path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/>',
  movie: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18M8 4v5M16 4v5M8 15l4 2.5V12.5z" />',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  dns: '<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5M12 7v5l3 2"/>',
  add: '<path d="M12 5v14M5 12h14"/>',
  upload: '<path d="M12 16V4M6 10l6-6 6 6"/><path d="M4 20h16"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  check: '<path d="M5 12l5 5L20 7"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  edit: '<path d="M4 20h4l11-11-4-4L4 16z"/><path d="M13 7l4 4"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v5h-5"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  download: '<path d="M12 4v12M6 10l6 6 6-6"/><path d="M4 20h16"/>',
  warn: '<path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5v.5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 8v.5M12 11v6"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  open: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M19 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h6"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5-8 8"/>',
  play: '<path d="M7 4l13 8-13 8z"/>',
  pause: '<path d="M7 5v14M17 5v14"/>',
  bolt: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  cut: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M8.5 8L20 19M8.5 16L20 5"/>',
  people: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4a3.5 3.5 0 0 1 0 7M21.5 20a6.5 6.5 0 0 0-5-6.3"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  wallet: '<rect x="3" y="6" width="18" height="13" rx="2.5"/><path d="M3 10h18M16 15h2"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  send: '<path d="M22 2L11 13"/><path d="M22 2L15 22l-4-9-9-4z"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5M3 17l9 5 9-5"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  brain: '<path d="M9 4a3 3 0 0 0-3 3v1a3 3 0 0 0-2 3 3 3 0 0 0 2 3v1a3 3 0 0 0 3 3h1V4z"/><path d="M15 4a3 3 0 0 1 3 3v1a3 3 0 0 1 2 3 3 3 0 0 1-2 3v1a3 3 0 0 1-3 3h-1V4z"/>',
  grid_view: '<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/>',
  hub: '<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="18" r="2.5"/><circle cx="19" cy="18" r="2.5"/><path d="M12 7.5v4M10 12.5l-3.5 3.5M14 12.5l3.5 3.5"/>',
  auto_awesome: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/>',
  edit_note: '<path d="M4 6h12M4 10h8M4 14h6"/><path d="M14 18l6-6-2-2-6 6v2z"/>',
  rate_review: '<path d="M4 4h16v12H8l-4 4z"/><path d="M8 9l6-1-1 4-4 1z"/>',
  tune: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  qr_code: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM19 14h2M14 19h2M19 19h2v2"/>',
};
// Material Symbols ligature names (Google's own icon set); the inline SVG only shows when the icon font failed to load.
const SYMBOLS = {
  dashboard: 'dashboard', video: 'video_library', search: 'search', spark: 'auto_awesome', article: 'article', review: 'rate_review', movie: 'movie',
  folder: 'folder', history: 'history', add: 'add', upload: 'upload', link: 'link', check: 'check', close: 'close', edit: 'edit', refresh: 'refresh',
  trash: 'delete', download: 'download', warn: 'warning', info: 'info', chevron: 'chevron_right', open: 'open_in_new', image: 'image', play: 'play_arrow',
  pause: 'pause', bolt: 'bolt', cut: 'content_cut', people: 'group', clock: 'schedule', wallet: 'account_balance_wallet', eye: 'visibility', send: 'send',
  layers: 'layers', settings: 'settings', brain: 'psychology', grid_view: 'grid_view', hub: 'hub', auto_awesome: 'auto_awesome', edit_note: 'edit_note', rate_review: 'rate_review', qr_code: 'qr_code_2', tune: 'tune', dns: 'dns',
};
export const icon = (name, cls = '') => `<span class="icn"><span class="msym ${cls}" aria-hidden="true">${SYMBOLS[name] || name}</span><svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ''}</svg></span>`;

// ---- markdown (article rendering) -------------------------------------------------------------
export function md(src, { figure } = {}) {
  const lines = (src || '').split('\n'); let h = '', list = null;
  const cites = new Map();   // block id -> [n], numbered in order of first appearance
  const inl = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/(^|[^*])\*(?!\*)(.+?)\*/g, '$1<i>$2</i>').replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\((\/[^)\s]+)\)/g, '<a href="$2" target="_blank">$1</a>')
    .replace(/\[id=([^\]]+)\]/g, (m, ids) => ids.split(',').map((x) => x.trim().replace(/^id=/, '')).map((x) => { if (!cites.has(x)) cites.set(x, cites.size + 1); return `<sup class="cite" data-id="${attr(x)}" title="${attr(x)}">[${cites.get(x)}]</sup>`; }).join(''));
  const close = () => { if (list) { h += `</${list}>`; list = null; } };
  for (const raw of lines) {
    const l = raw.trimEnd(); let m;
    if ((m = l.match(/^\s*\[img=([^\]]+)\]\s*$/))) { close(); h += figure ? figure(m[1].trim()) : ''; continue; }
    if ((m = l.match(/^!\[([^\]]*)\]\(([^)\s]+)\)$/))) { close(); h += `<figure><img src="${attr(m[2])}" alt="${attr(m[1])}" loading="lazy">${m[1] ? '<figcaption>' + esc(m[1]) + '</figcaption>' : ''}</figure>`; continue; }
    if (l.startsWith('<figure') || l.startsWith('</figure') || l.startsWith('<img')) { close(); h += l; continue; }
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { close(); const n = Math.min(3, m[1].length + 1); h += `<h${n}>${inl(m[2])}</h${n}>`; continue; }
    if ((m = l.match(/^>\s?(.*)/))) { close(); h += `<blockquote>${inl(m[1])}</blockquote>`; continue; }
    if ((m = l.match(/^\s*[-*]\s+(.*)/))) { if (list !== 'ul') { close(); list = 'ul'; h += '<ul>'; } h += `<li>${inl(m[1])}</li>`; continue; }
    if ((m = l.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== 'ol') { close(); list = 'ol'; h += '<ol>'; } h += `<li>${inl(m[1])}</li>`; continue; }
    if (!l.trim()) { close(); continue; }
    close(); h += `<p>${inl(l)}</p>`;
  }
  close(); return h;
}

// ---- snackbar / dialogs -----------------------------------------------------------------------
let snackT = null;
export function toast(text, err = false) {
  const el = document.getElementById('snack'); if (!el) return;
  el.textContent = text; el.className = 'snack on' + (err ? ' err' : '');
  clearTimeout(snackT); snackT = setTimeout(() => el.classList.remove('on'), err ? 4200 : 2800);
}
export function confirmDialog({ title, body, ok = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    const d = document.createElement('dialog');
    d.innerHTML = `<div class="dhd"><h3>${esc(title)}</h3>${body ? `<p>${esc(body)}</p>` : ''}</div><div class="dft"><button class="btn" data-r="0">Cancel</button><button class="btn ${danger ? 'danger-filled' : 'filled'}" data-r="1">${esc(ok)}</button></div>`;
    document.body.appendChild(d);
    d.querySelectorAll('[data-r]').forEach((b) => b.onclick = () => { d.close(); resolve(b.dataset.r === '1'); });
    d.addEventListener('close', () => { d.remove(); resolve(false); });
    d.showModal();
  });
}
export function openDialog(html, { onOpen } = {}) {
  const d = document.createElement('dialog'); d.innerHTML = html; document.body.appendChild(d);
  d.addEventListener('close', () => d.remove());
  d.querySelectorAll('[data-close]').forEach((b) => b.onclick = () => d.close());
  d.showModal(); if (onOpen) onOpen(d); return d;
}
export const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
export const emptyState = (iconName, text, sub = '') => `<div class="empty">${icon(iconName)}<div>${esc(text)}</div>${sub ? `<div class="body-s" style="margin-top:4px">${sub}</div>` : ''}</div>`;
